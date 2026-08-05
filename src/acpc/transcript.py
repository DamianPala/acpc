"""The versioned, append-only transcript format used by acpc sessions.

The transcript is deliberately a small file format rather than a second state
database. A header identifies the schema, and each following line is one event
with a process-wide cursor for that session. Appends are protected by a lock
shared by all :class:`Transcript` instances in this process.
"""

import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, NamedTuple

from acpc.paths import ensure_private_dir

SCHEMA = "acpc.transcript/1"
HEADER = {"schema": SCHEMA}
HEADER_LINE = (json.dumps(HEADER, separators=(", ", ": ")) + "\n").encode("utf-8")

EVENT_TYPES = frozenset({"msg", "thought", "tool", "permission", "error", "state", "usage"})

_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "msg": ("text",),
    "thought": ("text",),
    "tool": ("name", "args_summary", "status", "duration_ms"),
    "permission": ("kind", "decision"),
    "error": ("message",),
    "state": ("from", "to"),
    "usage": ("tokens", "cost"),
}


class _PathState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.last_index = 0
        self.pending_truncate: int | None = None
        self.needs_separator = False


_states_guard = threading.Lock()
_states: dict[Path, _PathState] = {}


class TranscriptError(ValueError):
    """Raised when a transcript is not a valid ``acpc.transcript/1`` file."""


class TranscriptPage(NamedTuple):
    """Events selected by a read and the cursor a caller should retain."""

    events: list[dict[str, Any]]
    next_cursor: int


class _ParsedTranscript(NamedTuple):
    events: list[dict[str, Any]]
    last_index: int
    last_complete_offset: int


def _state_for(path: Path) -> _PathState:
    canonical = path.resolve()
    with _states_guard:
        state = _states.get(canonical)
        if state is None:
            state = _PathState()
            _states[canonical] = state
        return state


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("transcript write made no progress")
        view = view[written:]


def _line_payload(raw: bytes) -> bytes:
    """Remove a physical line ending without changing JSON whitespace."""
    if raw.endswith(b"\r\n"):
        return raw[:-2]
    if raw.endswith((b"\n", b"\r")):
        return raw[:-1]
    return raw


def _format_error(path: Path, detail: str) -> TranscriptError:
    return TranscriptError(f"invalid transcript '{path}': {detail}")


def _decode_json(path: Path, raw: bytes, *, what: str) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _format_error(path, f"{what} is not UTF-8") from error
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise _format_error(path, f"{what} is not valid JSON") from error


def _validate_header(path: Path, value: Any) -> None:
    if not isinstance(value, dict) or value.get("schema") != SCHEMA or "i" in value:
        raise _format_error(path, f"first line must be the {SCHEMA!r} header")


def _validate_read_event(path: Path, value: Any, line_number: int) -> dict[str, Any]:
    """Validate only the fields needed to index and return an event."""
    if not isinstance(value, dict):
        raise _format_error(path, f"line {line_number} is not a JSON object")
    index = value.get("i")
    if isinstance(index, bool) or not isinstance(index, int):
        raise _format_error(path, f"line {line_number} has no integer index")
    return value


def _parse(
    path: Path,
    data: bytes,
    *,
    collect_events: bool,
) -> _ParsedTranscript:
    if not data:
        raise _format_error(path, "file is empty")

    lines = data.splitlines(keepends=True)
    if not lines:
        raise _format_error(path, "file has no lines")

    header = _decode_json(path, _line_payload(lines[0]), what="header")
    _validate_header(path, header)

    events: list[dict[str, Any]] = []
    last_index = 0
    offset = len(lines[0])
    for line_number, raw in enumerate(lines[1:], start=2):
        is_last = line_number == len(lines)
        try:
            value = _decode_json(path, _line_payload(raw), what=f"line {line_number}")
            event = _validate_read_event(path, value, line_number)
        except TranscriptError:
            # A process can die after writing only part of its final line. A
            # line with an ending is not a crash fragment: it is damaged
            # state, and must be reported rather than silently hidden.
            has_line_ending = raw.endswith((b"\n", b"\r"))
            if is_last and not has_line_ending:
                return _ParsedTranscript(events, last_index, offset)
            raise
        if collect_events:
            events.append(event)
        last_index = event["i"]
        offset += len(raw)
    return _ParsedTranscript(events, last_index, offset)


def _ensure_header(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ensure_private_dir(path.parent)

    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return

    try:
        _write_all(fd, HEADER_LINE)
    finally:
        os.close(fd)


class Transcript:
    """Read and append events in one session's ``transcript.ndjson`` file."""

    def __init__(self, path: Path | str, *, clock: Callable[[], float] | None = None) -> None:
        self.path = Path(path).expanduser()
        self._clock = clock
        self._state = _state_for(self.path)
        with self._state.lock:
            _ensure_header(self.path)
            data = self.path.read_bytes()
            parsed = _parse(self.path, data, collect_events=False)
            if parsed.last_complete_offset != len(data):
                with self.path.open("r+b") as file:
                    file.truncate(parsed.last_complete_offset)
                data = data[: parsed.last_complete_offset]
            self._state.last_index = parsed.last_index
            self._state.pending_truncate = None
            self._state.needs_separator = bool(data) and not data.endswith((b"\n", b"\r"))

    def append(
        self,
        event: Mapping[str, Any] | str,
        /,
        **fields: Any,
    ) -> dict[str, Any]:
        """Validate and append one event, assigning its global index."""
        if isinstance(event, str):
            if "type" in fields:
                raise TypeError("event type was supplied twice")
            candidate: dict[str, Any] = {"type": event, **fields}
        else:
            if fields:
                raise TypeError("keyword fields require an event type string")
            candidate = dict(event)

        candidate.pop("i", None)
        event_type = candidate.get("type")
        if not isinstance(event_type, str) or event_type not in EVENT_TYPES:
            raise TranscriptError("event type must be one of: " + ", ".join(sorted(EVENT_TYPES)))
        missing = [field for field in _REQUIRED_FIELDS[event_type] if field not in candidate]
        if missing:
            raise TranscriptError(f"event {event_type!r} is missing {', '.join(missing)}")
        candidate.setdefault("ts", time.time() if self._clock is None else self._clock())

        with self._state.lock:
            if self._state.pending_truncate is not None:
                with self.path.open("r+b") as file:
                    file.truncate(self._state.pending_truncate)
                self._state.pending_truncate = None
                self._state.needs_separator = False

            index = self._state.last_index + 1
            materialized = {**candidate, "i": index}
            try:
                encoded = (
                    json.dumps(materialized, ensure_ascii=False, separators=(",", ": ")) + "\n"
                ).encode("utf-8")
            except (TypeError, ValueError) as error:
                raise TranscriptError(f"event {event_type!r} is not JSON serializable") from error

            payload = b"\n" + encoded if self._state.needs_separator else encoded
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
            fd = os.open(self.path, flags, 0o600)
            try:
                _write_all(fd, payload)
            finally:
                os.close(fd)
            self._state.last_index = index
            self._state.needs_separator = False
            return materialized

    def read(self, *, since: int = 0, tail: int | None = None) -> TranscriptPage:
        """Return events after ``since``, optionally limited to the last ``tail``."""
        _validate_selection(since, tail)
        with self._state.lock:
            data = self.path.read_bytes()
            parsed = _parse(self.path, data, collect_events=True)
            self._state.last_index = parsed.last_index
            self._state.pending_truncate = (
                parsed.last_complete_offset if parsed.last_complete_offset != len(data) else None
            )
            complete_data = data[: parsed.last_complete_offset]
            self._state.needs_separator = bool(complete_data) and not complete_data.endswith(
                (b"\n", b"\r")
            )

        selected = [event for event in parsed.events if event["i"] > since]
        if tail is not None:
            selected = selected[-tail:] if tail else []
        next_cursor = selected[-1]["i"] if selected else since
        return TranscriptPage(selected, next_cursor)


def _validate_selection(since: int, tail: int | None) -> None:
    if isinstance(since, bool) or not isinstance(since, int) or since < 0:
        raise ValueError("since must be a non-negative integer")
    if tail is not None and (isinstance(tail, bool) or not isinstance(tail, int) or tail < 0):
        raise ValueError("tail must be a non-negative integer")
