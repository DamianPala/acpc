"""The stdout/stderr contract shared by answer-printing commands.

The runner owns the lifecycle of a turn.  This module owns the last, small
boundary between that lifecycle and a caller's shell: complete answers are
written to disk, stdout is shaped according to the flags, and acpc's summary
is kept on stderr.
"""

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from acpc import paths, permissions, sessions, vocab

DEFAULT_MAX_OUTPUT = 128 * 1024

# Session states whose answer file holds the complete answer for the call.
# A refusal or an adapter error is a finished turn and a full report about it
# (O5a), so it is complete data; every other state means the turn ended — or
# was still running — before the answer did.
_COMPLETE_ANSWER_STATES = frozenset({"succeeded", "failed"})


@dataclass(frozen=True, slots=True)
class OutputResult:
    """The bytes-ready text and truncation state for one stdout view."""

    text: str
    truncated: bool
    size_bytes: int
    output_file: str | None = None


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
    return OutputResult(text, True, len(text.encode("utf-8")), str(answer_path))


def _paths_for(meta: sessions.SessionMeta, *, turn: int | None) -> dict[str, str]:
    """The `paths` object, pinned to a parked turn's own files when given one.

    SPEC.md *State on disk*: a document reporting on a turn the session has
    since rotated past names that turn's parked `prompt.<n>.md` and
    `answer.<n>.md`, never the current session's files.
    """
    result = sessions.session_paths(meta.session_id)
    if turn is not None:
        result["answer"] = str(sessions.turn_path(meta.session_id, "answer", turn))
        result["prompt"] = str(sessions.turn_path(meta.session_id, "prompt", turn))
    return result


def result_envelope(
    meta: sessions.SessionMeta,
    answer: str,
    *,
    truncated: bool = False,
    background: bool = False,
    changed: bool | None = None,
    partial: bool = False,
    include_partial: bool = True,
    extra: Mapping[str, Any] | None = None,
    turn: int | None = None,
) -> dict[str, Any]:
    """Build the pinned JSON shape for an answer-printing command.

    ``extra`` carries the fields only one command publishes, `steer`'s `turn`
    and `correction_result`, so the shared shape stays shared and the
    command-specific part stays one dict at the call site that knows it.

    ``turn`` names the parked turn a report describes when it is not the
    session's current one; `paths.answer`, `paths.prompt`, and, once
    truncated, `output_file` then all name that turn's own files instead of
    the session's current ones.
    """
    paths_for_turn = _paths_for(meta, turn=turn)
    if background:
        envelope = {
            "session_id": meta.session_id,
            "turn": meta.turns,
            "status": vocab.normalize_session_state(meta.state),
            "created_at": _timestamp_or_none(meta.created_at),
            "started_at": _timestamp_or_none(meta.started_at),
            "finished_at": _timestamp_or_none(meta.finished_at),
            "paths": paths_for_turn,
            "truncated": False,
            "partial": False,
            "denied": _denial_payload(meta),
            "permissions_clamp": _permissions_clamp(meta),
            "capabilities": _capabilities(meta),
            "next": ["acpc", "wait", meta.session_id],
        }
        if not include_partial:
            envelope.pop("partial")
        if resume := _resume_status(meta):
            envelope["resume"] = resume
        if meta.limit is not None:
            envelope["limit"] = dict(meta.limit)
        if changed is not None:
            envelope["changed"] = changed
        if extra:
            envelope.update(extra)
        return envelope

    envelope: dict[str, Any] = {
        "status": vocab.normalize_session_state(meta.state),
        "session_id": meta.session_id,
        "turn": meta.turns,
        "created_at": _timestamp_or_none(meta.created_at),
        "started_at": _timestamp_or_none(meta.started_at),
        "finished_at": _timestamp_or_none(meta.finished_at),
        "stop_reason": meta.stop_reason,
        "tokens": meta.tokens,
        "paths": paths_for_turn,
        "cost": meta.cost,
        "answer": answer,
        "truncated": truncated,
        "denied": _denial_payload(meta),
        "permissions_clamp": _permissions_clamp(meta),
        "capabilities": _capabilities(meta),
        "next": ["acpc", "continue", meta.session_id],
    }
    if include_partial:
        envelope["partial"] = partial
    if resume := _resume_status(meta):
        envelope["resume"] = resume
    if meta.limit is not None:
        envelope["limit"] = dict(meta.limit)
    if truncated:
        envelope["output_file"] = paths_for_turn["answer"]
    if changed is not None:
        envelope["changed"] = changed
    if extra:
        envelope.update(extra)
    return envelope


def _timestamp_or_none(value: float | None) -> str | None:
    return sessions.format_timestamp(value) if value is not None else None


def _capabilities(meta: sessions.SessionMeta) -> dict[str, str]:
    return {"steer_mode": meta.steer_mode or vocab.STEER_CANCEL_THEN_START}


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"


def _json_answer(
    meta: sessions.SessionMeta,
    answer: str,
    *,
    max_output: int,
    answer_path: Path | str,
    changed: bool | None,
    partial: bool,
    include_partial: bool,
    extra: Mapping[str, Any] | None,
    turn: int | None = None,
) -> OutputResult:
    complete = _json_text(
        result_envelope(
            meta,
            answer,
            changed=changed,
            partial=partial,
            include_partial=include_partial,
            extra=extra,
            turn=turn,
        )
    )
    if max_output == 0 or len(complete.encode("utf-8")) <= max_output:
        return OutputResult(complete, False, len(complete.encode("utf-8")))

    marker = _marker(answer_path, kind="answer")

    def candidate(prefix: str) -> str:
        return _json_text(
            result_envelope(
                meta,
                prefix + marker,
                truncated=True,
                changed=changed,
                partial=partial,
                include_partial=include_partial,
                extra=extra,
                turn=turn,
            )
        )

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
    return OutputResult(best, True, len(best.encode("utf-8")), str(answer_path))


def write_output_file(path: Path | str, answer: str) -> int:
    """Atomically write the exact stdout representation to a destination."""
    target = Path(path).expanduser()
    paths.atomic_write(target, answer)
    return len(answer.encode("utf-8"))


# V6b "Escaping": attribute values escape five characters plus tab, CR and LF;
# metadata is ordinary JSON and needs none of this.
_ATTR_ESCAPES = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "\t": "&#9;",
    "\r": "&#13;",
    "\n": "&#10;",
}

# V6b "Answer boundary": these exact, case-sensitive strings in the displayed
# answer force the counted `<answer-N>` form; a tag with attributes, or a
# near-miss such as `<answers>`, does not count.
_ANSWER_TAG_TRIGGERS = (
    "<result>",
    "</result>",
    "<metadata>",
    "</metadata>",
    "<answer>",
    "</answer>",
)

# O3d control-byte escaping keeps `\t`, `\n` and `\r` literal: they are the
# answer's own line and column structure, not terminal attacks, and the
# answer-boundary line count above depends on the LF characters staying LF.
_PRESERVED_ANSWER_WHITESPACE = frozenset({0x09, 0x0A, 0x0D})

_METADATA_LEAD_FIELDS = ("turn", "capabilities", "stop_reason", "tokens", "cost")
_METADATA_OPTIONAL_FIELDS = ("correction_result", "denied", "permissions_clamp", "resume", "limit")


def _escape_attr(value: str) -> str:
    return "".join(_ATTR_ESCAPES.get(character, character) for character in value)


def _escape_answer_controls(text: str) -> str:
    """Escape terminal control bytes in a multi-line answer (O3d).

    `render.safe_text` does the same job for the condensed, single-line views
    `log` prints, but it collapses its input to one line first — exactly the
    line structure the answer boundary (V6b) depends on — and importing it
    here would be circular: `render.py` already imports from this module.
    This keeps the same escape table (`^[` for ESC, `\\uXXXX` for the other
    non-printable bytes) but leaves `\\t`, `\\n` and `\\r` alone.
    """
    escaped: list[str] = []
    for character in text:
        codepoint = ord(character)
        if codepoint in _PRESERVED_ANSWER_WHITESPACE:
            escaped.append(character)
        elif codepoint == 0x1B:
            escaped.append("^[")
        elif codepoint < 0x20 or 0x7F <= codepoint <= 0x9F:
            escaped.append(f"\\u{codepoint:04x}")
        else:
            escaped.append(character)
    return "".join(escaped)


def _answer_tags(displayed: str) -> tuple[str, str]:
    if any(trigger in displayed for trigger in _ANSWER_TAG_TRIGGERS):
        count = 1 + displayed.count("\n")
        return f"<answer-{count}>", f"</answer-{count}>"
    return "<answer>", "</answer>"


def _tagged_metadata(envelope: Mapping[str, Any], *, background: bool) -> dict[str, Any]:
    """Select and order the JSON fields V6c keeps in `<metadata>`, minus `next`.

    `next` is added last by `_with_next`, once truncation has had its say, so
    it always ends up as the final key regardless of which optional fields
    land before it.
    """
    metadata: dict[str, Any] = {}
    for field in _METADATA_LEAD_FIELDS:
        value = envelope.get(field)
        if value is not None:
            metadata[field] = value
    for field in _METADATA_OPTIONAL_FIELDS:
        value = envelope.get(field)
        if value:
            metadata[field] = value
    if background:
        metadata["paths"] = envelope["paths"]
    return metadata


def _with_next(metadata: dict[str, Any], envelope: Mapping[str, Any]) -> dict[str, Any]:
    next_value = envelope.get("next")
    if next_value:
        metadata["next"] = next_value
    return metadata


def _tagged_document(
    envelope: Mapping[str, Any], metadata: Mapping[str, Any], answer: str | None
) -> str:
    attrs = [
        f'session_id="{_escape_attr(str(envelope["session_id"]))}"',
        f'status="{_escape_attr(str(envelope["status"]))}"',
    ]
    if "partial" in envelope:
        attrs.append(f'partial="{"true" if envelope["partial"] else "false"}"')
    lines = [f"<result {' '.join(attrs)}>"]
    if metadata:
        lines += ["<metadata>", _json_text(metadata).rstrip("\n"), "</metadata>"]
    if answer is not None:
        open_tag, close_tag = _answer_tags(answer)
        lines += [open_tag, answer, close_tag]
    lines.append("</result>")
    return "\n".join(lines) + "\n"


def _truncated_tagged_document(
    envelope: Mapping[str, Any],
    base_metadata: Mapping[str, Any],
    displayed: str,
    max_output: int,
    answer_path: Path | str,
) -> OutputResult:
    marker = _marker(answer_path, kind="answer")
    truncated_metadata = dict(base_metadata)
    truncated_metadata["truncated"] = True
    truncated_metadata["output_file"] = str(answer_path)
    truncated_metadata = _with_next(truncated_metadata, envelope)

    def candidate(prefix: str) -> str:
        return _tagged_document(envelope, truncated_metadata, prefix + marker)

    # Same binary search as `_json_answer`: the wrapper adds a fixed overhead
    # around the answer, so the largest code-point prefix within budget is
    # found by search rather than by a byte-count subtraction.
    low, high = 0, len(displayed)
    best = candidate("")
    if len(best.encode("utf-8")) <= max_output:
        while low <= high:
            middle = (low + high) // 2
            trial = candidate(displayed[:middle])
            if len(trial.encode("utf-8")) <= max_output:
                best = trial
                low = middle + 1
            else:
                high = middle - 1
    return OutputResult(best, True, len(best.encode("utf-8")), str(answer_path))


def render_tagged(
    envelope: Mapping[str, Any],
    *,
    max_output: int,
    answer_path: Path | str,
) -> OutputResult:
    """Build the tagged text document V6b describes, from the JSON envelope's own facts.

    This is a second view of `envelope` (`result_envelope`'s return value), never a
    fresh source of truth. A background envelope carries no ``answer`` key, so its
    document has no answer section (V6a: "a receipt with no answer omits the answer
    section"); a receipt has nothing to shorten, so `max_output` never truncates one.
    """
    _validate_max_output(max_output)
    background = "answer" not in envelope
    base_metadata = _tagged_metadata(envelope, background=background)
    if background:
        text = _tagged_document(envelope, _with_next(base_metadata, envelope), None)
        return OutputResult(text, False, len(text.encode("utf-8")))

    displayed = _escape_answer_controls(str(envelope["answer"]))
    metadata = _with_next(dict(base_metadata), envelope)
    full_text = _tagged_document(envelope, metadata, displayed)
    full_size = len(full_text.encode("utf-8"))
    if max_output == 0 or full_size <= max_output:
        return OutputResult(full_text, False, full_size)
    return _truncated_tagged_document(envelope, base_metadata, displayed, max_output, answer_path)


def _render_background(
    meta: sessions.SessionMeta,
    *,
    json_mode: bool,
    tagged: bool,
    max_output: int,
    changed: bool | None,
    include_partial: bool,
    extra: Mapping[str, Any] | None,
    turn: int | None,
    answer_path: Path | str,
) -> OutputResult:
    envelope = result_envelope(
        meta,
        "",
        background=True,
        changed=changed,
        include_partial=include_partial,
        extra=extra,
        turn=turn,
    )
    if json_mode:
        text = _json_text(envelope)
        return OutputResult(text, False, len(text.encode("utf-8")))
    if tagged:
        return render_tagged(envelope, max_output=max_output, answer_path=answer_path)
    text = f"{meta.session_id}\n{sessions.session_dir(meta.session_id)}\n"
    return OutputResult(text, False, len(text.encode("utf-8")))


def render_result(
    meta: sessions.SessionMeta,
    answer: str = "",
    *,
    json_mode: bool = False,
    tagged: bool = False,
    background: bool = False,
    max_output: int = DEFAULT_MAX_OUTPUT,
    changed: bool | None = None,
    partial: bool | None = None,
    include_partial: bool = True,
    extra: Mapping[str, Any] | None = None,
    turn: int | None = None,
) -> OutputResult:
    """Render one answer command's stdout payload without writing it.

    `partial` defaults to what the session's state says about the answer: a
    turn that finished carries complete data, a turn still running or cut
    short does not.  A caller that knows better passes it explicitly.

    `tagged` selects the non-TTY `text` presentation (V6b); it is ignored
    when `json_mode` is set, and combined with `background` it renders the
    receipt without an answer section rather than the two-line id and
    directory.

    `turn` is the turn this call actually observed. Leave it `None` when
    that is the session's current turn; `wait` reporting on a turn the
    session has since rotated past passes that turn's number instead, so
    the truncation marker, `paths.answer`, `paths.prompt`, and `output_file`
    all name that turn's own parked files (`answer.<n>.md`, `prompt.<n>.md`)
    rather than the session's current ones (SPEC.md *State on disk*).
    """
    _validate_max_output(max_output)
    answer_path = (
        sessions.answer_path(meta.session_id)
        if turn is None
        else sessions.turn_path(meta.session_id, "answer", turn)
    )
    if partial is None:
        partial = vocab.normalize_session_state(meta.state) not in _COMPLETE_ANSWER_STATES

    if background:
        return _render_background(
            meta,
            json_mode=json_mode,
            tagged=tagged,
            max_output=max_output,
            changed=changed,
            include_partial=include_partial,
            extra=extra,
            turn=turn,
            answer_path=answer_path,
        )

    if json_mode:
        return _json_answer(
            meta,
            answer,
            max_output=max_output,
            answer_path=answer_path,
            changed=changed,
            partial=partial,
            include_partial=include_partial,
            extra=extra,
            turn=turn,
        )
    if tagged:
        envelope = result_envelope(
            meta,
            answer,
            changed=changed,
            partial=partial,
            include_partial=include_partial,
            extra=extra,
            turn=turn,
        )
        return render_tagged(envelope, max_output=max_output, answer_path=answer_path)
    return truncate_answer(answer, max_output=max_output, answer_path=answer_path)


def emit_result(
    meta: sessions.SessionMeta,
    answer: str = "",
    *,
    stream: TextIO | None = None,
    json_mode: bool = False,
    background: bool = False,
    max_output: int = DEFAULT_MAX_OUTPUT,
    changed: bool | None = None,
    partial: bool | None = None,
) -> OutputResult:
    """Render and write one stdout payload; return its truncation metadata."""
    result = render_result(
        meta,
        answer,
        json_mode=json_mode,
        background=background,
        max_output=max_output,
        changed=changed,
        partial=partial,
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


def _resume_status(meta: sessions.SessionMeta) -> str | None:
    """Return the cold-resume confidence persisted for the current turn."""
    value = meta.extra.get("resume")
    return value if isinstance(value, str) else None


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


def _limit_summary(meta: sessions.SessionMeta) -> str | None:
    if meta.limit is None:
        return None
    resume_at = meta.limit.get("resume_at") or "?"
    return f"limit: {meta.limit.get('reason')}, resumes {resume_at}"


def _correction_summary(correction: Mapping[str, Any]) -> str:
    return (
        f"correction: {correction.get('steer_mode')} → turn {correction.get('target_turn')} "
        f"{correction.get('target_status')} ({correction.get('message_state')})"
    )


def format_summary(
    meta: sessions.SessionMeta,
    *,
    runtime: float | None = None,
    route_note: str | None = None,
    correction_result: Mapping[str, Any] | None = None,
    truncated_output_file: str | None = None,
) -> str:
    """Format the single ``--`` summary line for a completed run.

    V6c: the text presentation keeps the steer mode, a partial answer, a
    usage-limit wait and a `Next:` command alongside the answer, whichever
    format carries the answer itself — this line is not one of those formats.
    """
    duration = sessions.runtime_seconds(meta) if runtime is None else runtime
    parts = [meta.state, format_duration(duration), format_tokens(meta.tokens)]
    if meta.cost is not None:
        parts.append(f"cost ${meta.cost:.2f}")
    if meta.exit_code is not None:
        parts.append(f"exit {meta.exit_code}")
    parts.append(f"steer_mode {meta.steer_mode or vocab.STEER_CANCEL_THEN_START}")
    if vocab.normalize_session_state(meta.state) not in _COMPLETE_ANSWER_STATES:
        parts.append("partial")
    if clamp := _clamp_summary(meta):
        parts.append(clamp)
    if denied := _denied_summary(meta):
        parts.append(denied)
    if resume := _resume_status(meta):
        parts.append(f"resume: {resume}")
    if limit := _limit_summary(meta):
        parts.append(limit)
    if correction_result is not None:
        parts.append(_correction_summary(correction_result))
    if truncated_output_file is not None:
        parts.append(f"truncated → {truncated_output_file}")
    parts.extend(_session_segments(meta))
    parts.append(f"Next: acpc continue {meta.session_id}")
    if route_note:
        parts.append(route_note)
    return "-- " + " | ".join(parts)
