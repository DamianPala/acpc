"""The stdout/stderr contract shared by answer-printing commands.

The runner owns the lifecycle of a turn.  This module owns the last, small
boundary between that lifecycle and a caller's shell: complete answers are
written to disk, stdout is shaped according to the flags, and acpc's summary
is kept on stderr.
"""

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from acpc import paths, permissions, sessions

DEFAULT_MAX_OUTPUT = 128 * 1024


@dataclass(frozen=True, slots=True)
class OutputResult:
    """The bytes-ready text and truncation state for one stdout view."""

    text: str
    truncated: bool
    size_bytes: int


def _validate_max_output(max_output: int) -> None:
    if isinstance(max_output, bool) or not isinstance(max_output, int) or max_output < 0:
        raise ValueError("max_output must be a non-negative integer")


def _marker(path: Path | str, *, kind: str) -> str:
    return f"\n[output truncated; full {kind}: {path}]\n"


def _utf8_head(text: str, byte_limit: int) -> str:
    if byte_limit <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= byte_limit:
        return text
    return encoded[:byte_limit].decode("utf-8", errors="ignore")


def _with_marker(text: str, max_output: int, marker: str) -> str:
    marker_bytes = len(marker.encode("utf-8"))
    if marker_bytes >= max_output:
        # A path-bearing marker is more useful than an empty response when a
        # caller asks for a budget smaller than the marker itself.  The
        # contract cannot fit both pieces into that budget simultaneously.
        return marker
    return _utf8_head(text, max_output - marker_bytes) + marker


def truncate_answer(
    answer: str,
    *,
    max_output: int = DEFAULT_MAX_OUTPUT,
    answer_path: Path | str,
) -> OutputResult:
    """Keep an answer's head and append a path-bearing UTF-8-safe marker."""
    _validate_max_output(max_output)
    encoded_size = len(answer.encode("utf-8"))
    if max_output == 0 or encoded_size <= max_output:
        return OutputResult(answer, False, encoded_size)

    text = _with_marker(answer, max_output, _marker(answer_path, kind="answer"))
    return OutputResult(text, True, len(text.encode("utf-8")))


def result_envelope(
    meta: sessions.SessionMeta,
    answer: str,
    *,
    truncated: bool = False,
    background: bool = False,
    output_file: Path | str | None = None,
) -> dict[str, Any]:
    """Build the pinned JSON shape for an answer-printing command."""
    if background:
        return {
            "session_id": meta.session_id,
            "state": meta.state,
            "paths": sessions.session_paths(meta.session_id),
            "denied": _denial_payload(meta),
            "permissions_clamp": _permissions_clamp(meta),
        }

    envelope: dict[str, Any] = {
        "state": meta.state,
        "session_id": meta.session_id,
        "stop_reason": meta.stop_reason,
        "paths": sessions.session_paths(meta.session_id),
        "cost": meta.cost,
        "answer": answer,
        "truncated": truncated,
        "denied": _denial_payload(meta),
        "permissions_clamp": _permissions_clamp(meta),
    }
    if output_file is not None:
        envelope["output_file"] = str(output_file)
        envelope.pop("answer")
    return envelope


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"


def _json_answer(
    meta: sessions.SessionMeta,
    answer: str,
    *,
    max_output: int,
    answer_path: Path | str,
) -> OutputResult:
    complete = _json_text(result_envelope(meta, answer))
    if max_output == 0 or len(complete.encode("utf-8")) <= max_output:
        return OutputResult(complete, False, len(complete.encode("utf-8")))

    marker = _marker(answer_path, kind="answer")

    def candidate(prefix: str) -> str:
        return _json_text(result_envelope(meta, prefix + marker, truncated=True))

    # JSON escaping adds a fixed envelope overhead and escapes the marker's
    # line breaks.  Binary-search the largest code-point prefix that keeps the
    # complete JSON document valid and within the requested byte budget.
    low = 0
    high = len(answer)
    best = candidate("")
    if len(best.encode("utf-8")) <= max_output:
        while low <= high:
            middle = (low + high) // 2
            trial = candidate(answer[:middle])
            if len(trial.encode("utf-8")) <= max_output:
                best = trial
                low = middle + 1
            else:
                high = middle - 1
    return OutputResult(best, True, len(best.encode("utf-8")))


def write_output_file(path: Path | str, answer: str) -> int:
    """Atomically write a complete ``-o`` answer and return its byte size."""
    target = Path(path).expanduser()
    paths.atomic_write(target, answer)
    return len(answer.encode("utf-8"))


def render_result(
    meta: sessions.SessionMeta,
    answer: str = "",
    *,
    json_mode: bool = False,
    background: bool = False,
    output_file: Path | str | None = None,
    max_output: int = DEFAULT_MAX_OUTPUT,
) -> OutputResult:
    """Render one answer command's stdout payload without writing it."""
    _validate_max_output(max_output)
    answer_path = sessions.answer_path(meta.session_id)

    if background:
        if json_mode:
            text = _json_text(result_envelope(meta, "", background=True))
        else:
            text = f"{meta.session_id}\n{sessions.session_dir(meta.session_id)}\n"
        return OutputResult(text, False, len(text.encode("utf-8")))

    if output_file is not None:
        if json_mode:
            text = _json_text(result_envelope(meta, "", output_file=output_file))
        else:
            size = len(answer.encode("utf-8"))
            text = f"answer: {output_file} ({size} bytes) · session: {meta.session_id}\n"
        return OutputResult(text, False, len(text.encode("utf-8")))

    if json_mode:
        return _json_answer(
            meta,
            answer,
            max_output=max_output,
            answer_path=answer_path,
        )
    return truncate_answer(answer, max_output=max_output, answer_path=answer_path)


def emit_result(
    meta: sessions.SessionMeta,
    answer: str = "",
    *,
    stream: TextIO | None = None,
    json_mode: bool = False,
    background: bool = False,
    output_file: Path | str | None = None,
    max_output: int = DEFAULT_MAX_OUTPUT,
) -> OutputResult:
    """Render and write one stdout payload; return its truncation metadata."""
    result = render_result(
        meta,
        answer,
        json_mode=json_mode,
        background=background,
        output_file=output_file,
        max_output=max_output,
    )
    (sys.stdout if stream is None else stream).write(result.text)
    return result


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    return f"{minutes}m{secs:02d}s"


def format_tokens(tokens: int) -> str:
    if tokens >= 1000:
        value = tokens / 1000
        rendered = f"{value:.1f}".rstrip("0").rstrip(".")
        return f"{rendered}k tok"
    return f"{tokens} tok"


def _resolved_permissions(meta: sessions.SessionMeta) -> dict[str, Any]:
    resolved = meta.resolution.get("resolved", {})
    permission = resolved.get("permissions", {}) if isinstance(resolved, dict) else {}
    return permission if isinstance(permission, dict) else {}


def _permissions_clamp(meta: sessions.SessionMeta) -> dict[str, str] | None:
    clamp = _resolved_permissions(meta).get("clamp")
    if not isinstance(clamp, dict):
        return None
    fields = ("requested", "ceiling", "effective")
    if any(not isinstance(clamp.get(field), str) for field in fields):
        return None
    return {field: clamp[field] for field in fields}


def _denial_record(meta: sessions.SessionMeta, key: str, count: int) -> dict[str, Any]:
    details = meta.denial_details.get(key, {})
    if not isinstance(details, dict):
        details = {}
    category = details.get("category")
    target: str | None = None
    if key.startswith("switch_mode:"):
        category = "switch_mode"
        target = key.removeprefix("switch_mode:")
    if not isinstance(category, str) or not category:
        category = key
    if category == "switch_mode" and isinstance(details.get("target"), str):
        target = details["target"]

    minimum = details.get("minimum_policy")
    if minimum is None:
        minimum = permissions.minimum_policy(category)
    remedy = details.get("remedy")
    if not isinstance(remedy, str) or not remedy:
        remedy = f"pass --permissions {minimum}" if isinstance(minimum, str) else "no policy helps"

    record: dict[str, Any] = {
        "category": category,
        "count": count,
        "minimum_policy": minimum,
        "remedy": remedy,
    }
    if target is not None:
        record["target"] = target
    return record


def _denial_payload(meta: sessions.SessionMeta) -> list[dict[str, Any]]:
    return [_denial_record(meta, key, count) for key, count in meta.denied.items() if count]


def _denied_summary(meta: sessions.SessionMeta) -> str | None:
    records = _denial_payload(meta)
    if not records:
        return None
    rendered: list[str] = []
    for record in records:
        category = record["category"]
        label = category
        if category == "switch_mode":
            label = f"switch_mode {record.get('target', '<unknown>')}"
        rendered.append(f"{record['count']} {label} ({record['remedy']})")
    return "denied: " + " · ".join(rendered)


def _clamp_summary(meta: sessions.SessionMeta) -> str | None:
    clamp = _permissions_clamp(meta)
    if clamp is None:
        return None
    return (
        f"permissions clamped from {clamp['requested']} by inherited ceiling "
        f"{clamp['ceiling']} (effective {clamp['effective']})"
    )


def _session_segments(meta: sessions.SessionMeta) -> tuple[str, str]:
    return (
        f"session {meta.session_id}",
        f"dir {sessions.session_dir(meta.session_id)}",
    )


def format_session_line(meta: sessions.SessionMeta) -> str:
    """Format the session id and directory line printed at blocking dispatch."""
    return "-- " + " | ".join(_session_segments(meta))


def format_summary(
    meta: sessions.SessionMeta,
    *,
    runtime: float | None = None,
    route_note: str | None = None,
) -> str:
    """Format the single ``--`` summary line for a completed run."""
    duration = sessions.runtime_seconds(meta) if runtime is None else runtime
    parts = [meta.state, format_duration(duration), format_tokens(meta.tokens)]
    if meta.cost is not None:
        parts.append(f"cost ${meta.cost:.2f}")
    if meta.exit_code is not None:
        parts.append(f"exit {meta.exit_code}")
    if clamp := _clamp_summary(meta):
        parts.append(clamp)
    if denied := _denied_summary(meta):
        parts.append(denied)
    parts.extend(_session_segments(meta))
    if route_note:
        parts.append(route_note)
    return "-- " + " | ".join(parts)
