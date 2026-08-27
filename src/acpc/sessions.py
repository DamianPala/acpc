"""Session store: ids, `meta.json` lifecycle, states, locks, rotation, cleanup.

SPEC.md *State on disk* and *Session states*. Every session is a directory
under `<ACPC_HOME>/sessions/<id>/` holding `meta.json`, `prompt.md`,
`transcript.ndjson` and `answer.md`; earlier turns keep their `.<n>` copies.

Two invariants drive the shape of this module:

- **No torn reads.** `meta.json` is replaced atomically (frozen `paths`), and
  every mutation runs under a per-session file lock, so `run`, `continue` and
  `stop` on one session never interleave.
- **State is verified, not trusted.** A stored `running` means nothing on its
  own: `load` re-checks the host process through the frozen `proc` identity
  token and persists `orphaned` when it is gone, so every reader agrees
  without re-probing. The 30 s startup grace covers only the window before a
  host process has been recorded — once `pid` is in `meta.json`, liveness
  decides immediately.

Time enters through an injectable `clock`, so tests exercise grace windows and
retention ages without sleeping.
"""

import contextlib
import errno
import json
import os
import random
import re
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from acpc import paths, proc, transcript, vocab

Clock = Callable[[], float]

# SPEC.md *Command surface*: 4 characters from a 32-glyph alphabet — lowercase
# letters and digits minus the ambiguous `0`/`o` and `1`/`l`.
SESSION_ID_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"
SESSION_ID_LENGTH = 4
SESSION_ID_PATTERN = re.compile(f"^[{SESSION_ID_ALPHABET}]{{{SESSION_ID_LENGTH}}}$")

# SPEC.md *Session states*: below this age a session with no recorded host
# process still counts as `starting` — the process may not have got that far.
STARTUP_GRACE_SECONDS = 30.0

# `last` is a selector, never a session name (SPEC.md `run --name`).
RESERVED_NAME = "last"

META_NAME = "meta.json"
PROMPT_NAME = "prompt.md"
ANSWER_NAME = "answer.md"
TRANSCRIPT_NAME = "transcript.ndjson"
LOCK_NAME = "lock"

_PROMPT_SNIPPET_LIMIT = 200
_ID_ALLOCATION_ATTEMPTS = 64

# SPEC.md *Session states*. Finished states are terminal for the current turn;
# a new turn re-opens the session through `rotate_turn`.
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "starting": frozenset({"running", "done", "failed", "cancelled", "timeout", "orphaned"}),
    "running": frozenset({"done", "failed", "cancelled", "timeout", "orphaned"}),
}


class SessionError(Exception):
    """Base class for session-store failures; carries one actionable line."""


class SessionNotFound(SessionError):
    """No session matches the given id or name."""


class CorruptSessionError(SessionError):
    """`meta.json` exists but cannot be trusted; the message names the file."""


class SessionStateError(SessionError):
    """The session's state does not accept this operation."""


class SessionNameError(SessionError):
    """A `--name` alias or selector cannot be used as asked."""


@dataclass(slots=True, kw_only=True)
class SessionMeta:
    """Typed view of `meta.json`.

    Field order matches the on-disk key order. Unknown keys read from disk are
    preserved in `extra` and written back untouched, so a newer writer's fields
    survive a round-trip through an older reader.
    """

    session_id: str
    name: str | None = None
    entry: str
    base_adapter: str
    state: str = "starting"
    pid: int | None = None
    process_start_time: str | None = None
    created_at: float | None = None
    started_at: float | None = None
    finished_at: float | None = None
    turns: int = 1
    exit_code: int | None = None
    stop_reason: str | None = None
    failure: str | None = None
    tokens: int = 0
    cost: float | None = None
    denied: dict[str, int] = field(default_factory=dict)
    denial_details: dict[str, dict[str, Any]] = field(default_factory=dict)
    prompt_snippet: str = ""
    resolution: dict[str, Any] = field(default_factory=dict)
    adapter_session_id: str | None = None
    target: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {name: getattr(self, name) for name in _META_FIELDS}
        data.update(self.extra)
        return data

    @property
    def is_active(self) -> bool:
        return self.state in vocab.ACTIVE_STATES

    @property
    def is_finished(self) -> bool:
        return self.state in vocab.FINISHED_STATES

    @property
    def resolved_model(self) -> str | None:
        """The model this session actually ran on, as resolved at dispatch.

        Every session resolves one, adapter defaults included, but the dig is
        defensive: `meta.json` can come from an older writer or a torn write,
        and a status view must never be the thing that raises.
        """
        resolved = self.resolution.get("resolved")
        if not isinstance(resolved, dict):
            return None
        model = resolved.get("model")
        if not isinstance(model, dict):
            return None
        value = model.get("value")
        return value if isinstance(value, str) and value else None


_META_FIELDS: tuple[str, ...] = tuple(f.name for f in fields(SessionMeta) if f.name != "extra")
DELIVERY_RECORD_INCOMPLETE = "delivery_record_incomplete"


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------


def session_dir(session_id: str) -> Path:
    return paths.session_dir(session_id)


def meta_path(session_id: str) -> Path:
    return session_dir(session_id) / META_NAME


def prompt_path(session_id: str) -> Path:
    return session_dir(session_id) / PROMPT_NAME


def answer_path(session_id: str) -> Path:
    return session_dir(session_id) / ANSWER_NAME


def transcript_path(session_id: str) -> Path:
    return session_dir(session_id) / TRANSCRIPT_NAME


def session_paths(session_id: str) -> dict[str, str]:
    """The four advertised paths, as the `--json` envelope's `paths` object."""
    return {
        "dir": str(session_dir(session_id)),
        "prompt": str(prompt_path(session_id)),
        "transcript": str(transcript_path(session_id)),
        "answer": str(answer_path(session_id)),
    }


def turn_path(session_id: str, stem: str, turn: int) -> Path:
    """Path of an earlier turn's artifact, e.g. `prompt.2.md`."""
    return session_dir(session_id) / f"{stem}.{turn}.md"


# --------------------------------------------------------------------------
# Per-session lock
# --------------------------------------------------------------------------


class _LockDepth(threading.local):
    """Re-entrancy bookkeeping: nested `session_lock` calls must not deadlock."""

    def __init__(self) -> None:
        self.held: set[str] = set()


_lock_depth = _LockDepth()


def _acquire_file_lock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_EX)


def _release_file_lock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


def _try_acquire_file_lock(fd: int) -> bool:
    """Acquire a lock without waiting, returning false when it is occupied."""
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"0")
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            raise
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _session_busy(session_id: str) -> SessionStateError:
    """Use continue's existing actionable message for a preparation collision."""
    return SessionStateError(
        f"session {session_id} is running — wait for the current turn to finish"
    )


@contextlib.contextmanager
def session_lock(session_id: str) -> Iterator[None]:
    """Serialize turns and state writes on one session, across processes.

    Re-entrant within a thread: a nested acquisition of the same session is a
    no-op, so the mutation helpers can be composed freely.
    """
    if session_id in _lock_depth.held:
        yield
        return
    directory = paths.ensure_private_dir(session_dir(session_id))
    fd = os.open(directory / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        _acquire_file_lock(fd)
        _lock_depth.held.add(session_id)
        try:
            yield
        finally:
            _lock_depth.held.discard(session_id)
            _release_file_lock(fd)
    finally:
        os.close(fd)


@contextlib.asynccontextmanager
async def session_reservation(session_id: str) -> AsyncIterator[None]:
    """Reserve a finished session across asynchronous resume preparation.

    The lock is deliberately non-blocking and ephemeral. A failed verification
    therefore releases only an OS lock and leaves the session files untouched;
    a competing continuation gets the same busy error as an active turn.
    """
    if session_id in _lock_depth.held:
        raise _session_busy(session_id)
    directory = paths.ensure_private_dir(session_dir(session_id))
    fd = os.open(directory / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    try:
        if not _try_acquire_file_lock(fd):
            raise _session_busy(session_id)
        acquired = True
        _lock_depth.held.add(session_id)
        meta = read_meta(session_id)
        if meta.is_active:
            raise SessionStateError(
                f"session {session_id} is {meta.state} — wait for the current turn to finish"
            )
        yield
    finally:
        if acquired:
            _lock_depth.held.discard(session_id)
            _release_file_lock(fd)
        os.close(fd)


# --------------------------------------------------------------------------
# Reading and writing meta.json
# --------------------------------------------------------------------------


def _coerce_int(value: Any, key: str, path: Path) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise CorruptSessionError(f"{path}: {key} is not an integer")
    return value


def _coerce_float(value: Any, key: str, path: Path) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CorruptSessionError(f"{path}: {key} is not a number")
    return float(value)


def _coerce_str(value: Any, key: str, path: Path) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CorruptSessionError(f"{path}: {key} is not a string")
    return value


def _coerce_denied(value: Any, key: str, path: Path) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CorruptSessionError(f"{path}: {key} is not an object")
    denied: dict[str, int] = {}
    for category, count in value.items():
        if not isinstance(category, str) or not category:
            raise CorruptSessionError(f"{path}: {key} has an invalid category")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise CorruptSessionError(f"{path}: {key}.{category} is not a non-negative integer")
        denied[category] = count
    return denied


def _coerce_denial_details(value: Any, key: str, path: Path) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CorruptSessionError(f"{path}: {key} is not an object")
    details: dict[str, dict[str, Any]] = {}
    for denial_key, detail in value.items():
        if not isinstance(denial_key, str) or not denial_key:
            raise CorruptSessionError(f"{path}: {key} has an invalid denial key")
        if not isinstance(detail, dict):
            raise CorruptSessionError(f"{path}: {key}.{denial_key} is not an object")
        details[denial_key] = dict(detail)
    return details


def meta_from_dict(data: Mapping[str, Any], *, path: Path) -> SessionMeta:
    """Build a `SessionMeta` from parsed JSON, rejecting damaged state.

    `path` only shapes the error message: SPEC.md's output contract wants one
    actionable line naming the file, never a traceback.
    """
    known = {key: value for key, value in data.items() if key in _META_FIELDS}
    extra = {key: value for key, value in data.items() if key not in _META_FIELDS}

    session_id = _coerce_str(known.get("session_id"), "session_id", path)
    entry = _coerce_str(known.get("entry"), "entry", path)
    if not session_id or not entry:
        raise CorruptSessionError(f"{path}: missing session_id or entry")

    state = _coerce_str(known.get("state"), "state", path) or "starting"
    if state not in vocab.SESSION_STATES:
        raise CorruptSessionError(f"{path}: unknown session state {state!r}")

    resolution = known.get("resolution") or {}
    if not isinstance(resolution, dict):
        raise CorruptSessionError(f"{path}: resolution is not an object")

    return SessionMeta(
        session_id=session_id,
        name=_coerce_str(known.get("name"), "name", path),
        entry=entry,
        base_adapter=_coerce_str(known.get("base_adapter"), "base_adapter", path) or entry,
        state=state,
        pid=_coerce_int(known.get("pid"), "pid", path),
        process_start_time=_coerce_str(known.get("process_start_time"), "process_start_time", path),
        created_at=_coerce_float(known.get("created_at"), "created_at", path),
        started_at=_coerce_float(known.get("started_at"), "started_at", path),
        finished_at=_coerce_float(known.get("finished_at"), "finished_at", path),
        turns=_coerce_int(known.get("turns"), "turns", path) or 1,
        exit_code=_coerce_int(known.get("exit_code"), "exit_code", path),
        stop_reason=_coerce_str(known.get("stop_reason"), "stop_reason", path),
        failure=_coerce_str(known.get("failure"), "failure", path),
        tokens=_coerce_int(known.get("tokens"), "tokens", path) or 0,
        cost=_coerce_float(known.get("cost"), "cost", path),
        denied=_coerce_denied(known.get("denied"), "denied", path),
        denial_details=_coerce_denial_details(known.get("denial_details"), "denial_details", path),
        prompt_snippet=_coerce_str(known.get("prompt_snippet"), "prompt_snippet", path) or "",
        resolution=dict(resolution),
        adapter_session_id=_coerce_str(known.get("adapter_session_id"), "adapter_session_id", path),
        target=_coerce_str(known.get("target"), "target", path),
        extra=extra,
    )


def read_meta(session_id: str) -> SessionMeta:
    """Read `meta.json` verbatim, without verifying liveness.

    Use `load` for anything that reports or gates on state.
    """
    path = meta_path(session_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SessionNotFound(f"unknown session {session_id!r}") from None
    except OSError as error:
        raise CorruptSessionError(f"{path}: cannot be read ({error.strerror})") from None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise CorruptSessionError(f"{path}: not valid JSON") from None
    if not isinstance(data, dict):
        raise CorruptSessionError(f"{path}: not a JSON object")
    return meta_from_dict(data, path=path)


def write_meta(meta: SessionMeta) -> None:
    """Publish `meta.json` atomically. Callers hold the session lock."""
    paths.ensure_private_dir(session_dir(meta.session_id))
    paths.atomic_write(meta_path(meta.session_id), meta.to_dict())


def load(session_id: str, *, clock: Clock | None = None) -> SessionMeta:
    """Read a session and verify the process behind an active state.

    A dead host process is persisted as `orphaned` (atomic, under the session
    lock) together with the placeholder `answer.md`, so later readers agree
    without re-probing.
    """
    return _verify_liveness(read_meta(session_id), clock=_resolve_clock(clock))


def _resolve_clock(clock: Clock | None) -> Clock:
    """Every time-dependent entry point takes a `clock`; this is its default."""
    return time.time if clock is None else clock


def _verify_liveness(meta: SessionMeta, *, clock: Clock) -> SessionMeta:
    if not meta.is_active:
        return meta
    if meta.pid is None:
        reference = meta.started_at if meta.started_at is not None else meta.created_at
        if reference is None or clock() - reference < STARTUP_GRACE_SECONDS:
            return meta
        reason = "no host process was ever recorded for it"
    elif proc.process_liveness(meta.pid, meta.process_start_time) == "dead":
        reason = f"the process hosting it (pid {meta.pid}) is gone"
    else:
        return meta
    return _persist_orphaned(meta.session_id, reason, clock=clock)


def _persist_orphaned(session_id: str, reason: str, *, clock: Clock) -> SessionMeta:
    with session_lock(session_id):
        current = read_meta(session_id)
        if not current.is_active:
            return current
        current.state = "orphaned"
        current.stop_reason = "orphaned"
        current.exit_code = vocab.EXIT_AGENT_ERROR
        if current.finished_at is None:
            current.finished_at = clock()
        write_meta(current)
        _write_orphan_placeholder(current, reason)
    return current


def _write_orphan_placeholder(meta: SessionMeta, reason: str) -> None:
    """Guarantee the advertised `answer.md` exists and explains itself.

    SPEC.md *State on disk*: for `orphaned` the dead process wrote nothing, so
    detection leaves a one-line placeholder naming what died.
    """
    path = answer_path(meta.session_id)
    if path.exists():
        return
    paths.atomic_write(
        path,
        f"Session {meta.session_id} was orphaned: {reason}; this turn produced no answer.\n",
    )


# --------------------------------------------------------------------------
# Creating and mutating sessions
# --------------------------------------------------------------------------


def prompt_snippet(prompt: str, *, limit: int = _PROMPT_SNIPPET_LIMIT) -> str:
    """One-line prompt digest for `status` rows; views clip it further."""
    collapsed = " ".join(prompt.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def allocate_session_id(*, rng: random.Random | None = None) -> str:
    """Reserve a free session id by creating its directory.

    Creating the directory *is* the claim: `mkdir` without `exist_ok` fails on
    a taken id, which is how collisions are detected and re-rolled.
    """
    generator = rng if rng is not None else random.SystemRandom()
    for _ in range(_ID_ALLOCATION_ATTEMPTS):
        candidate = "".join(generator.choice(SESSION_ID_ALPHABET) for _ in range(SESSION_ID_LENGTH))
        directory = paths.session_dir(candidate)
        try:
            directory.mkdir(parents=True)
        except FileExistsError:
            continue
        paths.ensure_private_dir(directory)
        return candidate
    raise SessionError(
        f"could not allocate a free session id after {_ID_ALLOCATION_ATTEMPTS} attempts — "
        f"run 'acpc prune' to clear finished sessions"
    )


def create_session(
    *,
    entry: str,
    base_adapter: str,
    prompt: str,
    resolution: Mapping[str, Any] | None = None,
    target: str | None = None,
    name: str | None = None,
    clock: Clock | None = None,
    rng: random.Random | None = None,
) -> SessionMeta:
    """Allocate a session dir, write `prompt.md`, publish `meta.json`."""
    resolved_clock = _resolve_clock(clock)
    session_id = allocate_session_id(rng=rng)
    meta = SessionMeta(
        session_id=session_id,
        name=name,
        entry=entry,
        base_adapter=base_adapter,
        state="starting",
        created_at=resolved_clock(),
        prompt_snippet=prompt_snippet(prompt),
        resolution=dict(resolution or {}),
        target=target,
    )
    with session_lock(session_id):
        paths.atomic_write(prompt_path(session_id), prompt)
        write_meta(meta)
    return meta


def write_prompt(session_id: str, prompt: str) -> SessionMeta:
    """Write the current turn's `prompt.md` and refresh the stored snippet."""
    with session_lock(session_id):
        meta = read_meta(session_id)
        paths.atomic_write(prompt_path(session_id), prompt)
        meta.prompt_snippet = prompt_snippet(prompt)
        write_meta(meta)
    return meta


def mark_prompt_delivered(session_id: str, prompt: str) -> SessionMeta:
    """Persist that the current turn's prompt crossed the ACP boundary."""
    with session_lock(session_id):
        meta = read_meta(session_id)
        records = meta.extra.get("delivered_prompts")
        delivered = list(records) if isinstance(records, list) else []
        marker = {"turn": meta.turns, "prompt": prompt}
        if not any(
            isinstance(record, Mapping) and record.get("turn") == meta.turns for record in delivered
        ):
            delivered.append(marker)
        meta.extra["delivered_prompts"] = delivered
        write_meta(meta)
    return meta


def write_answer(session_id: str, answer: str) -> None:
    """Write the current turn's `answer.md` atomically."""
    with session_lock(session_id):
        paths.atomic_write(answer_path(session_id), answer)


def update_meta(session_id: str, **changes: Any) -> SessionMeta:
    """Apply field updates under the session lock.

    `state` is deliberately not accepted here — state moves through
    `transition` so the state machine stays enforced in one place.
    """
    if "state" in changes:
        raise ValueError("state changes go through transition()")
    if "session_id" in changes:
        raise ValueError("session_id is immutable")
    unknown = set(changes) - set(_META_FIELDS)
    if unknown:
        raise ValueError(f"unknown meta fields: {', '.join(sorted(unknown))}")
    with session_lock(session_id):
        meta = read_meta(session_id)
        for key, value in changes.items():
            setattr(meta, key, value)
        write_meta(meta)
    return meta


def transition(
    session_id: str,
    to_state: str,
    *,
    clock: Clock | None = None,
    **changes: Any,
) -> SessionMeta:
    """Move a session to `to_state`, rejecting moves the vocabulary forbids.

    Timestamps follow the state: entering `running` stamps `started_at`,
    entering any finished state stamps `finished_at`.
    """
    resolved_clock = _resolve_clock(clock)
    if to_state not in vocab.SESSION_STATES:
        raise ValueError(f"unknown session state {to_state!r}")
    unknown = set(changes) - set(_META_FIELDS)
    if unknown:
        raise ValueError(f"unknown meta fields: {', '.join(sorted(unknown))}")
    with session_lock(session_id):
        meta = read_meta(session_id)
        allowed = _ALLOWED_TRANSITIONS.get(meta.state, frozenset())
        if to_state not in allowed:
            raise SessionStateError(
                f"session {session_id} is {meta.state}, which cannot become {to_state}"
            )
        meta.state = to_state
        for key, value in changes.items():
            setattr(meta, key, value)
        now = resolved_clock()
        if to_state == "running" and meta.started_at is None:
            meta.started_at = now
        if to_state in vocab.FINISHED_STATES and meta.finished_at is None:
            meta.finished_at = now
        write_meta(meta)
    return meta


def finalize_turn(
    session_id: str,
    to_state: str,
    *,
    answer: str,
    expected_turn: int | None = None,
    clock: Clock | None = None,
    error_event: Mapping[str, Any] | None = None,
    delivery_record_incomplete: bool = False,
    **changes: Any,
) -> SessionMeta | None:
    """Publish a turn answer and terminal state as one token-checked claim.

    ``continue`` can rotate a finished turn immediately after a stop. Keeping
    the answer write and terminal transition under this lock prevents a stale
    owner from writing its answer into the replacement turn.
    """
    resolved_clock = _resolve_clock(clock)
    if to_state not in vocab.SESSION_STATES:
        raise ValueError(f"unknown session state {to_state!r}")
    unknown = set(changes) - set(_META_FIELDS)
    if unknown:
        raise ValueError(f"unknown meta fields: {', '.join(sorted(unknown))}")
    with session_lock(session_id):
        meta = read_meta(session_id)
        if expected_turn is not None and meta.turns != expected_turn:
            return None
        if meta.is_finished and to_state == "failed":
            return None
        allowed = _ALLOWED_TRANSITIONS.get(meta.state, frozenset())
        if to_state not in allowed:
            raise SessionStateError(
                f"session {session_id} is {meta.state}, which cannot become {to_state}"
            )
        paths.atomic_write(answer_path(session_id), answer)
        events = transcript.Transcript(transcript_path(session_id))
        if error_event is not None:
            events.append("error", **dict(error_event))
        events.append("state", **{"from": meta.state, "to": to_state})
        meta.state = to_state
        if delivery_record_incomplete:
            meta.extra[DELIVERY_RECORD_INCOMPLETE] = True
        for key, value in changes.items():
            if key in {"tokens", "cost"} and value is None:
                continue
            setattr(meta, key, value)
        now = resolved_clock()
        if to_state == "running" and meta.started_at is None:
            meta.started_at = now
        if to_state in vocab.FINISHED_STATES and meta.finished_at is None:
            meta.finished_at = now
        write_meta(meta)
        return meta


def mark_running(
    session_id: str,
    *,
    pid: int,
    process_start_time: str | None = None,
    clock: Clock | None = None,
) -> SessionMeta:
    """Record the process hosting this session's turns and open the turn.

    The recorded pid is the daemon on the daemon path and the acpc client on
    the direct path. Recording it ends the startup grace: from here on,
    liveness alone decides whether the session is still alive.
    """
    token = process_start_time
    if token is None:
        token = proc.process_start_time(pid)
    return transition(
        session_id,
        "running",
        clock=clock,
        pid=pid,
        process_start_time=token,
    )


def rotate_turn(
    session_id: str,
    *,
    clock: Clock | None = None,
    permissions_from_meta: Callable[[SessionMeta], str | None] | None = None,
    resolution_from_meta: Callable[[SessionMeta], Mapping[str, Any]] | None = None,
    target_from_meta: Callable[[SessionMeta], str] | None = None,
    prompt: str | None = None,
    resume_status: str | None = None,
    pid: int | None = None,
) -> SessionMeta:
    """Open the next turn: park the finished turn's artifacts, reset per-turn state.

    SPEC.md *State on disk*: rotation happens at the *start* of the next turn
    and renames each file exactly once, ever — turn numbers are fixed, so
    there is no logrotate-style cascade. A mid-turn session therefore has no
    `answer.md` until the turn produces one.
    """
    resolved_clock = _resolve_clock(clock)
    with session_lock(session_id):
        meta = read_meta(session_id)
        if meta.is_active:
            raise SessionStateError(
                f"session {session_id} is {meta.state} — wait for the current turn to finish"
            )
        # Any value derived from session state must be a callback. The callback
        # receives this locked re-read, so a caller cannot accidentally compute
        # a snapshot value before the lock and write it after the lock.
        if resolution_from_meta is not None and permissions_from_meta is not None:
            raise SessionStateError(
                f"session {session_id} received both resolution and permissions updates"
            )
        if resolution_from_meta is not None:
            meta.resolution = dict(resolution_from_meta(meta))
        elif permissions_from_meta is not None:
            permissions = permissions_from_meta(meta)
            if permissions is None:
                raise SessionStateError(f"session {session_id} has no permission update")
            resolved = meta.resolution.get("resolved")
            if not isinstance(resolved, dict):
                raise SessionStateError(f"session {session_id} has no stored permission resolution")
            resolved["permissions"] = {"value": permissions, "source": "call flag"}
            meta.resolution.pop("permissions_source", None)
        if target_from_meta is not None:
            meta.target = target_from_meta(meta)
        turn = meta.turns
        for stem, current in (
            ("prompt", prompt_path(session_id)),
            ("answer", answer_path(session_id)),
        ):
            parked = turn_path(session_id, stem, turn)
            if current.exists() and not parked.exists():
                os.replace(current, parked)
        meta.turns = turn + 1
        meta.state = "starting"
        meta.pid = None
        meta.process_start_time = None
        meta.started_at = resolved_clock()
        meta.finished_at = None
        meta.exit_code = None
        meta.stop_reason = None
        meta.failure = None
        meta.denied = {}
        meta.denial_details = {}
        meta.extra.pop("failure", None)
        meta.extra.pop("resume", None)
        if prompt is not None:
            paths.atomic_write(prompt_path(session_id), prompt)
            meta.prompt_snippet = prompt_snippet(prompt)
        if resume_status is not None:
            meta.extra["resume"] = resume_status
        if pid is not None:
            meta.state = "running"
            meta.pid = pid
            meta.process_start_time = proc.process_start_time(pid)
        write_meta(meta)
    return meta


def runtime_seconds(meta: SessionMeta, *, clock: Clock | None = None) -> float:
    """Wall-clock runtime for `status` rows and `log` footers."""
    resolved_clock = _resolve_clock(clock)
    start = meta.started_at if meta.started_at is not None else meta.created_at
    if start is None:
        return 0.0
    end = meta.finished_at if meta.finished_at is not None else resolved_clock()
    return max(0.0, end - start)


# --------------------------------------------------------------------------
# Listing, selectors, names
# --------------------------------------------------------------------------


def list_sessions(*, clock: Clock | None = None, verify: bool = True) -> list[SessionMeta]:
    """Every readable session, newest first.

    Session dirs whose `meta.json` is missing or damaged are skipped: the list
    view is a pulse, and a single broken session must not hide the rest. A
    targeted read (`read_meta`/`load`) still reports the damage.
    """
    resolved_clock = _resolve_clock(clock)
    root = paths.sessions_dir()
    try:
        entries = sorted(root.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return []
    found: list[SessionMeta] = []
    for directory in entries:
        if not directory.is_dir():
            continue
        try:
            meta = read_meta(directory.name)
        except SessionError:
            continue
        if verify:
            meta = _verify_liveness(meta, clock=resolved_clock)
        found.append(meta)
    found.sort(key=lambda meta: (meta.created_at or 0.0, meta.session_id), reverse=True)
    return found


def resolve_selector(
    selector: str,
    *,
    allow_last: bool = False,
    clock: Clock | None = None,
) -> str:
    """Map a session id, a `--name` alias or `last` to a session id.

    `last` is TTY-only (SPEC.md *TTY vs non-TTY*): a stale "last" misleads an
    agent caller, so a non-TTY caller gets a reasoned rejection instead.
    """
    resolved_clock = _resolve_clock(clock)
    if selector == RESERVED_NAME:
        if not allow_last:
            raise SessionNameError(
                "`last` works on a TTY only — pass a session id, or name sessions with --name"
            )
        candidates = list_sessions(clock=resolved_clock, verify=False)
        if not candidates:
            raise SessionNotFound("no sessions yet — `last` has nothing to resolve to")
        return candidates[0].session_id
    if meta_path(selector).exists():
        return selector
    named = [
        meta for meta in list_sessions(clock=resolved_clock, verify=False) if meta.name == selector
    ]
    if named:
        return named[0].session_id
    raise SessionNotFound(f"unknown session {selector!r}")


def claim_name(name: str, *, clock: Clock | None = None) -> str | None:
    """Check a `--name` for a session about to be created.

    Returns a warning line when the name rebinds off a finished session, and
    `None` when it was free. A holder that is still active is a hard error:
    rebinding would leave the running session unreachable by name.
    """
    resolved_clock = _resolve_clock(clock)
    if name == RESERVED_NAME:
        raise SessionNameError(
            f"`{RESERVED_NAME}` is reserved as a selector — pick another name for --name"
        )
    if not name.strip():
        raise SessionNameError("--name cannot be empty")
    holders = [
        meta for meta in list_sessions(clock=resolved_clock, verify=False) if meta.name == name
    ]
    if not holders:
        return None
    holder = _verify_liveness(holders[0], clock=resolved_clock)
    if holder.is_active:
        raise SessionNameError(
            f"name {name!r} belongs to session {holder.session_id}, still {holder.state} — "
            f"stop it first or pick another name"
        )
    return f"name {name!r} was bound to session {holder.session_id}; rebinding it to the new one"


# --------------------------------------------------------------------------
# Deletion
# --------------------------------------------------------------------------


def _remove_tree(directory: Path) -> None:
    for child in sorted(directory.rglob("*"), reverse=True):
        if child.is_dir() and not child.is_symlink():
            child.rmdir()
        else:
            child.unlink()
    directory.rmdir()


def delete_session(session_id: str, *, clock: Clock | None = None) -> None:
    """Delete one session's on-disk state; active sessions are refused.

    SPEC.md `rm`: errors on `starting`/`running` — `stop` it first. Liveness is
    verified first, so a session whose process died is deletable.
    """
    resolved_clock = _resolve_clock(clock)
    meta = load(session_id, clock=resolved_clock)
    if meta.is_active:
        raise SessionStateError(f"session {session_id} is {meta.state} — stop it before rm")
    _remove_tree(session_dir(session_id))


def prune_sessions(
    *,
    older_than: float,
    dry_run: bool = False,
    clock: Clock | None = None,
) -> list[SessionMeta]:
    """Delete finished sessions older than `older_than` seconds.

    Age is measured from `finished_at` (SPEC.md `prune`), falling back to
    `created_at` for a session that never recorded one. Active sessions are
    never touched, and liveness is verified first so orphans do get collected.
    """
    resolved_clock = _resolve_clock(clock)
    now = resolved_clock()
    removed: list[SessionMeta] = []
    for meta in list_sessions(clock=resolved_clock):
        if meta.is_active:
            continue
        reference = meta.finished_at if meta.finished_at is not None else meta.created_at
        if reference is None or now - reference < older_than:
            continue
        removed.append(meta)
        if not dry_run:
            with contextlib.suppress(OSError):
                _remove_tree(session_dir(meta.session_id))
    return removed
