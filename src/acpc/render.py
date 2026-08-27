"""On-demand rendering for transcript and session status views."""

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from acpc import sessions, transcript
from acpc.output import format_duration, format_tokens

Clock = Callable[[], float]
DEFAULT_LOG_MAX_OUTPUT = 128 * 1024


@dataclass(frozen=True, slots=True)
class RenderedEvents:
    """A rendered log page and the cursor it safely covers."""

    text: str
    next_cursor: int
    truncated: bool
    printed_events: int
    first_event: int | None = None
    last_event: int | None = None


def format_table(
    rows: Sequence[Sequence[str]],
    *,
    header: Sequence[str] | None = None,
    prefix: str = "",
    continuation_prefix: str | None = None,
    separator: str = "  ",
) -> list[str]:
    """Format rows with widths measured from the header and all rendered rows.

    The final cell is deliberately free-running: it is where descriptions,
    prompts, and paths belong, so padding it would only add trailing spaces.
    ``continuation_prefix`` supports views whose first line carries a block
    label while subsequent lines are indented beneath it.
    """
    rendered_rows = [tuple(row) for row in rows]
    if header is not None:
        # Uppercased here, not at call sites, so no view can ship a
        # lowercase header; block labels in `prefix` stay as written.
        measured_rows = [tuple(label.upper() for label in header), *rendered_rows]
    else:
        measured_rows = rendered_rows
    if not measured_rows:
        return []

    column_count = len(measured_rows[0])
    if any(len(row) != column_count for row in measured_rows):
        raise ValueError("table rows must have the same number of columns")
    widths = [max(len(row[index]) for row in measured_rows) for index in range(column_count)]

    lines: list[str] = []
    all_rows = measured_rows
    for index, row in enumerate(all_rows):
        cells = [
            value if cell_index == column_count - 1 else f"{value:<{widths[cell_index]}}"
            for cell_index, value in enumerate(row)
        ]
        if continuation_prefix is None or index == 0:
            line_prefix = prefix
        else:
            line_prefix = continuation_prefix
        lines.append(line_prefix + separator.join(cells).rstrip())
    return lines


def _validate_max_output(max_output: int) -> None:
    if isinstance(max_output, bool) or not isinstance(max_output, int) or max_output < 0:
        raise ValueError("max_output must be a non-negative integer")


def _event_timestamp(event: Mapping[str, Any]) -> str:
    value = event.get("ts")
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return "??:??:??"
    try:
        return datetime.fromtimestamp(float(value), tz=UTC).astimezone().strftime("%H:%M:%S")
    except (TypeError, ValueError, OverflowError, OSError):
        return "??:??:??"


def _single_line(value: object) -> str:
    return " ".join(str(value).split())


def snippet(text: str, *, limit: int = 200) -> str:
    """Collapse whitespace, then cut at the last word boundary within `limit`.

    A single token longer than the limit is cut hard — snapping to a
    boundary that does not exist would return nothing.
    """
    normalized = _single_line(text)
    if len(normalized) <= limit:
        return normalized
    content_limit = limit - len("...")
    head = normalized[:content_limit]
    boundary = head.rfind(" ")
    if boundary >= 0:
        head = head[:boundary]
    return head.rstrip() + "..."


def _message_snippet(text: str, *, full: bool) -> str:
    return _single_line(text) if full else snippet(text)


def _event_index(event: Mapping[str, Any], fallback: int) -> int:
    value = event.get("i")
    return value if isinstance(value, int) and not isinstance(value, bool) else fallback


def format_event(
    event: Mapping[str, Any], *, full_message: bool = False, continued: bool = False
) -> str:
    """Format one transcript event as a condensed, single-line view."""
    event_type = str(event.get("type", "event"))
    timestamp = _event_timestamp(event)
    labels = {
        "msg": "msg   ",
        "thought": "thought ",
        "tool": "tool  ",
        "permission": "permission ",
        "error": "error ",
        "state": "state ",
        "usage": "usage ",
    }
    label = labels.get(event_type, f"{event_type} ")
    if continued and event_type in {"msg", "thought"}:
        label = f"{event_type} ↪ "

    if event_type in {"msg", "thought"}:
        text = str(event.get("text", ""))
        snippet = _message_snippet(text, full=full_message)
        char_count = ""
        if len(text) >= 1024:
            compact_count = format_tokens(len(text)).removesuffix(" tok")
            char_count = f" ({compact_count} chars)"
        return f"[{timestamp}] {label}{json.dumps(snippet, ensure_ascii=False)}{char_count}"
    if event_type == "tool":
        name = _single_line(event.get("name", "tool"))
        args = _single_line(event.get("args_summary", ""))
        status = _single_line(event.get("status", "unknown"))
        duration = event.get("duration_ms", 0)
        try:
            duration_text = f"{float(duration) / 1000:.1f}s"
        except (TypeError, ValueError):
            duration_text = "?s"
        arguments = f" {args}" if args else ""
        return f"[{timestamp}] {label}{name}{arguments} → {status} ({duration_text})"
    if event_type == "permission":
        count = event.get("count")
        repeated = ""
        if isinstance(count, int) and count > 1:
            repeated = f" (×{count}, cursor: {_event_index(event, 0)})"
        return (
            f"[{timestamp}] {label}{event.get('kind', 'unknown')} "
            f"→ {event.get('decision', 'unknown')}{repeated}"
        )
    if event_type == "error":
        return f"[{timestamp}] {label}{_single_line(event.get('message', ''))}"
    if event_type == "state":
        return f"[{timestamp}] {label}{event.get('from', '?')} → {event.get('to', '?')}"
    if event_type == "usage":
        cost = event.get("cost")
        cost_text = ""
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            cost_text = f" · cost ${cost:.2f}"
        return f"[{timestamp}] {label}{event.get('tokens', 0)} tok{cost_text}"
    return f"[{timestamp}] {label}{_single_line(event)}"


def _is_auto_filesystem_permission(event: Mapping[str, Any]) -> bool:
    return (
        event.get("type") == "permission"
        and event.get("auto") is True
        and event.get("decision") == "allow"
        and isinstance(event.get("kind"), str)
        and event["kind"].startswith("fs/")
    )


def _is_groupable_filesystem_permission(event: Mapping[str, Any]) -> bool:
    return _is_auto_filesystem_permission(event) and (
        "count" not in event or isinstance(event.get("count"), int)
    )


def condense_events(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Collapse adjacent automatic filesystem allows before applying a tail."""
    condensed: list[dict[str, Any]] = []
    for event in events:
        current = dict(event)
        if condensed and _is_auto_filesystem_permission(current):
            previous = condensed[-1]
            if _is_groupable_filesystem_permission(previous) and previous.get(
                "kind"
            ) == current.get("kind"):
                previous.setdefault("_group_start", _event_index(previous, 0))
                previous["count"] = int(previous.get("count", 1)) + 1
                previous["i"] = _event_index(current, _event_index(previous, 0))
                continue
        condensed.append(current)

    for event in condensed:
        if "count" in event:
            event.setdefault("_group_start", _event_index(event, 0))
    return condensed


def _prose_event(event: Mapping[str, Any]) -> str:
    event_type = event.get("type")
    if event_type == "msg":
        return str(event.get("text", ""))
    if event_type == "error":
        return format_event(event)
    return ""


def _utf8_head(text: str, byte_limit: int) -> str:
    if byte_limit <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= byte_limit:
        return text
    return encoded[:byte_limit].decode("utf-8", errors="ignore")


def _truncation_marker(path: Path | str) -> str:
    return f"[output truncated; full transcript: {path}]\n"


def _text_with_marker(text: str, max_output: int, marker: str) -> str:
    marker_bytes = len(marker.encode("utf-8"))
    if marker_bytes >= max_output:
        return marker
    return _utf8_head(text, max_output - marker_bytes) + marker


def _json_line(event: Mapping[str, Any]) -> str:
    return json.dumps(dict(event), ensure_ascii=False, separators=(",", ":")) + "\n"


def _continues_message(events: Sequence[Mapping[str, Any]], index: int) -> bool:
    if index == 0:
        return False
    current = events[index]
    previous = events[index - 1]
    current_type = current.get("type")
    if current_type not in {"msg", "thought"} or previous.get("type") != current_type:
        return False
    current_index = current.get("i")
    previous_index = previous.get("i")
    return (
        isinstance(current_index, int)
        and not isinstance(current_index, bool)
        and isinstance(previous_index, int)
        and not isinstance(previous_index, bool)
        and current_index == previous_index + 1
    )


def render_events(
    events: Sequence[Mapping[str, Any]],
    *,
    prose: bool = False,
    json_mode: bool = False,
    max_output: int = DEFAULT_LOG_MAX_OUTPUT,
    transcript_path: Path | str = "transcript.ndjson",
    cursor: int = 0,
    full_last_message: bool = False,
) -> RenderedEvents:
    """Render selected events while preserving the cursor contract.

    Selection happens in :mod:`acpc.transcript`; this operation only renders
    the supplied page.  A filtered prose event still advances the cursor, but
    a budget overflow never advances past an event that was not covered by
    stdout, except for the single over-budget-event case pinned by the spec.
    """
    _validate_max_output(max_output)
    if prose and json_mode:
        raise ValueError("--prose and --json are mutually exclusive views")

    last_message_index = -1
    if full_last_message:
        for index, event in enumerate(events):
            if event.get("type") == "msg":
                last_message_index = index

    if json_mode:
        return _render_json_events(
            events,
            max_output=max_output,
            transcript_path=transcript_path,
            cursor=cursor,
        )

    events = condense_events(events) if not prose else [dict(event) for event in events]

    entries: list[tuple[int, str]] = []
    entry_cursor = cursor
    prose_content = ""
    for index, event in enumerate(events):
        event_cursor = _event_index(event, entry_cursor)
        rendered = (
            _prose_event(event)
            if prose
            else format_event(
                event,
                full_message=index == last_message_index,
                continued=_continues_message(events, index),
            )
        )
        if (
            prose
            and event.get("type") == "error"
            and prose_content
            and not prose_content.endswith("\n")
        ):
            rendered = "\n" + rendered
        unit = rendered if prose else rendered + "\n" if rendered else ""
        entries.append((event_cursor, unit))
        entry_cursor = event_cursor

        if prose:
            prose_content += unit

    output = ""
    next_cursor = cursor
    printed_events = 0
    first_event: int | None = None
    last_event: int | None = None
    marker = _truncation_marker(transcript_path)
    for index, (event_cursor, unit) in enumerate(entries):
        if not unit:
            next_cursor = event_cursor
            continue

        has_later_output = any(later_unit for _, later_unit in entries[index + 1 :])
        fits = max_output == 0 or len((output + unit).encode("utf-8")) <= max_output
        marker_fits = max_output == 0 or len((output + unit + marker).encode("utf-8")) <= max_output
        if fits and (not has_later_output or marker_fits):
            output += unit
            next_cursor = event_cursor
            printed_events += 1
            if first_event is None:
                first_event = event_cursor
            last_event = event_cursor
            continue

        if not output:
            output = _text_with_marker(unit, max_output, marker)
            next_cursor = event_cursor
            printed_events += 1
            first_event = last_event = event_cursor
        else:
            output += marker
        return RenderedEvents(output, next_cursor, True, printed_events, first_event, last_event)

    return RenderedEvents(output, next_cursor, False, printed_events, first_event, last_event)


def _render_json_events(
    events: Sequence[Mapping[str, Any]],
    *,
    max_output: int,
    transcript_path: Path | str,
    cursor: int,
) -> RenderedEvents:
    output = ""
    next_cursor = cursor
    printed_events = 0
    first_event: int | None = None
    last_event: int | None = None
    for index, event in enumerate(events):
        event_cursor = _event_index(event, next_cursor)
        line = _json_line(event)
        has_later_events = index < len(events) - 1
        fits = max_output == 0 or len((output + line).encode("utf-8")) <= max_output
        marker = _json_line({"type": "truncated", "path": str(transcript_path)})
        marker_fits = max_output == 0 or len((output + line + marker).encode("utf-8")) <= max_output
        if fits and (not has_later_events or marker_fits):
            output += line
            next_cursor = event_cursor
            printed_events += 1
            if first_event is None:
                first_event = event_cursor
            last_event = event_cursor
            continue

        # A partial JSON object would make the stream unusable.  The marker is
        # itself a typed NDJSON event, and therefore remains parseable even if
        # the event that crossed the budget was larger than the budget alone.
        had_output = bool(output)
        output += marker
        if not had_output:
            next_cursor = event_cursor
        return RenderedEvents(output, next_cursor, True, printed_events, first_event, last_event)

    return RenderedEvents(output, next_cursor, False, printed_events, first_event, last_event)


def format_log_footer(
    meta: sessions.SessionMeta,
    *,
    cursor: int,
    event_count: int = 0,
    page_start: int | None = None,
    page_end: int | None = None,
    runtime: float | None = None,
    clock: Clock | None = None,
) -> str:
    """Format the stderr footer for a log page."""
    if runtime is None:
        runtime = sessions.runtime_seconds(meta, clock=clock)
    if page_start is None or page_end is None:
        page_start = page_end = 0
    coverage = f"events {page_start}–{page_end} of {event_count}"
    if meta.is_finished:
        qualifier = f" exit {meta.exit_code}" if meta.exit_code is not None else ""
        parts = [
            f"{meta.state}{qualifier}",
            format_duration(runtime),
            format_tokens(meta.tokens),
            f"answer: {sessions.answer_path(meta.session_id)}",
        ]
    else:
        parts = [
            f"{meta.state} {format_duration(runtime)}",
        ]
    parts.append(coverage)
    parts.append(f"cursor: {cursor}")
    return "-- " + " | ".join(parts)


def _status_active(meta: sessions.SessionMeta) -> bool:
    """Include the daemon's ephemeral preparation view in active status."""
    return meta.is_active or meta.state == "preparing"


def _status_selection(
    sessions_in: Sequence[sessions.SessionMeta], *, all_sessions: bool
) -> list[sessions.SessionMeta]:
    if all_sessions:
        return list(sessions_in)
    active = [meta for meta in sessions_in if _status_active(meta)]
    # SPEC `status`: the 5 most recent *finished*. The incoming order is by
    # start time, under which a long run that finished last is cut while a
    # shorter one started after it survives — so this bucket re-sorts by
    # finish time. `prune` measures age from the same field.
    finished = sorted(
        (meta for meta in sessions_in if meta.is_finished),
        key=lambda meta: (
            meta.finished_at if meta.finished_at is not None else (meta.created_at or 0.0),
            meta.session_id,
        ),
        reverse=True,
    )[:5]
    return active + finished


def _status_row(meta: sessions.SessionMeta, *, clock: Clock | None) -> tuple[str, ...]:
    runtime_seconds, now = _status_timing(meta, clock=clock)
    runtime = format_duration(runtime_seconds)
    idle_seconds = _idle_seconds(meta, now=now)
    idle = f"idle {format_duration(idle_seconds)}" if idle_seconds is not None else "·"
    name = meta.name or "·"
    model = meta.resolved_model or "·"
    snippet = json.dumps(meta.prompt_snippet, ensure_ascii=False)
    return (
        meta.session_id,
        meta.entry,
        model,
        meta.state,
        runtime,
        idle,
        name,
        snippet,
    )


def _status_timing(meta: sessions.SessionMeta, *, clock: Clock | None) -> tuple[float, float]:
    """Sample the clock once, so runtime and idle age describe one instant."""
    now = time.time() if clock is None else clock()
    runtime_seconds = sessions.runtime_seconds(meta, clock=lambda: now)
    return runtime_seconds, now


def _idle_seconds(meta: sessions.SessionMeta, *, now: float) -> float | None:
    if not _status_active(meta):
        return None
    last_event = transcript.last_event_time(sessions.transcript_path(meta.session_id))
    if last_event is None:
        return None
    return max(0.0, now - last_event)


def daemon_idle_seconds(
    sessions_in: Sequence[sessions.SessionMeta], target: str, *, now: float
) -> float | None:
    """Return the target's idle age, or ``None`` when it is not defined."""
    target_sessions = [meta for meta in sessions_in if meta.target == target]
    if any(_status_active(meta) for meta in target_sessions):
        return None
    last_finished = max(
        (meta.finished_at for meta in target_sessions if meta.finished_at is not None),
        default=None,
    )
    if last_finished is None:
        return None
    return max(0.0, now - last_finished)


def render_status_list(
    sessions_in: Sequence[sessions.SessionMeta],
    *,
    all_sessions: bool = False,
    clock: Clock | None = None,
) -> str:
    """Render the status list and its in-view summary footer."""
    selected = _status_selection(sessions_in, all_sessions=all_sessions)
    lines = format_table(
        [_status_row(meta, clock=clock) for meta in selected],
        header=("id", "entry", "model", "state", "runtime", "idle", "name", "prompt"),
        separator="  ",
    )
    running_count = sum(_status_active(meta) for meta in sessions_in)
    finished_count = len([meta for meta in sessions_in if meta.is_finished])
    if all_sessions:
        footer = f"-- {running_count} running · {len(sessions_in)} sessions"
    else:
        footer = (
            f"-- {running_count} running · {min(5, finished_count)} recent · "
            f"--all for all {len(sessions_in)}"
        )
    lines.append(footer)
    return "\n".join(lines) + "\n"


def _display_path(path: Path | str) -> str:
    value = Path(path)
    try:
        relative = value.relative_to(Path.home())
    except ValueError:
        return str(value)
    return str(Path("~") / relative) if relative.parts else "~"


def render_status_detail(
    meta: sessions.SessionMeta,
    *,
    clock: Clock | None = None,
) -> str:
    """Render one session's status detail view."""
    runtime_seconds, now = _status_timing(meta, clock=clock)
    runtime = format_duration(runtime_seconds)
    idle_seconds = _idle_seconds(meta, now=now)
    idle = f" · idle {format_duration(idle_seconds)}" if idle_seconds is not None else ""
    exit_text = f"exit {meta.exit_code}" if meta.exit_code is not None else "exit ·"
    tokens = format_tokens(meta.tokens)
    name = meta.name or "·"
    directory = _display_path(sessions.session_dir(meta.session_id))
    model = meta.resolved_model or "·"
    lines = [
        f"state    {meta.state}{idle} · {exit_text} · {runtime} · {tokens}",
        f"agent    {meta.entry} ({meta.base_adapter}) · model: {model} · name: {name}",
        f"dir      {directory} · answer: {Path(sessions.answer_path(meta.session_id)).name}",
    ]
    if meta.failure is not None:
        lines.append(f"failure  {meta.failure} · continue: acpc continue {meta.session_id}")
    return "\n".join(lines) + "\n"


def status_list_json(
    sessions_in: Sequence[sessions.SessionMeta],
    *,
    all_sessions: bool = False,
    clock: Clock | None = None,
) -> dict[str, Any]:
    """Build the JSON shape for ``status`` without an id."""
    selected = _status_selection(sessions_in, all_sessions=all_sessions)
    rows = []
    for meta in selected:
        runtime_seconds, now = _status_timing(meta, clock=clock)
        rows.append(
            {
                "session_id": meta.session_id,
                "entry": meta.entry,
                "model": meta.resolved_model,
                "state": meta.state,
                "name": meta.name,
                "prompt_snippet": meta.prompt_snippet,
                "runtime": runtime_seconds,
                "idle_seconds": _idle_seconds(meta, now=now),
            }
        )
    return {"sessions": rows}


def status_detail_json(
    meta: sessions.SessionMeta,
    *,
    clock: Clock | None = None,
) -> dict[str, Any]:
    """Build the JSON shape for ``status <id>``."""
    runtime_seconds, now = _status_timing(meta, clock=clock)
    return {
        "session_id": meta.session_id,
        "state": meta.state,
        "pid": meta.pid,
        "turns": meta.turns,
        "entry": meta.entry,
        "base_adapter": meta.base_adapter,
        "model": meta.resolved_model,
        "name": meta.name,
        "runtime": runtime_seconds,
        "idle_seconds": _idle_seconds(meta, now=now),
        "tokens": meta.tokens,
        "cost": meta.cost,
        "exit_code": meta.exit_code,
        "stop_reason": meta.stop_reason,
        "failure": meta.failure,
        "paths": sessions.session_paths(meta.session_id),
    }
