"""Behavioral tests for stdout shaping, JSON envelopes, and summaries."""

import json
from pathlib import Path

import pytest

from acpc import output, sessions


@pytest.fixture(autouse=True)
def isolated_state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "state"))


def make_session(tmp_path: Path) -> sessions.SessionMeta:
    return sessions.create_session(
        entry="mock",
        base_adapter="mock",
        prompt="a prompt",
        clock=lambda: 100.0,
    )


def test_answer_truncation_keeps_head_marker_and_utf8_boundary(tmp_path: Path) -> None:
    meta = make_session(tmp_path)
    answer = "head-" + ("a" * 1990) + "🙂" + "-tail"

    result = output.render_result(meta, answer, max_output=2000)

    result.text.encode("utf-8").decode("utf-8")
    assert result.truncated is True
    assert result.text.startswith("head-")
    assert f"full answer: {sessions.answer_path(meta.session_id)}" in result.text
    assert "-tail" not in result.text


def test_human_path_escapes_control_bytes_and_keeps_line_structure() -> None:
    """SPEC.md *Text presentation*: the human answer escapes control bytes the
    same way the tagged document does, keeping `\\t`, `\\n` and `\\r` literal."""
    meta = make_session(Path("."))
    answer = "safe\x1b[31mred\x1b[0m\x9binjected\ttab\nline\r\n"

    result = output.render_result(meta, answer, max_output=0)

    assert "\x1b" not in result.text
    assert "\x9b" not in result.text
    assert "^[[31mred^[[0m" in result.text
    assert "\\u009b" in result.text
    assert "\ttab" in result.text
    assert "\nline" in result.text
    assert "\r\n" in result.text


def test_human_path_truncation_counts_the_escaped_bytes() -> None:
    """`--max-output` on the human path bounds what is actually displayed:
    escaping ESC to `^[` doubles its byte length, so a cap sized to the raw
    answer's bytes must still truncate the escaped, longer text."""
    meta = make_session(Path("."))
    answer = "\x1b[31m" * 500
    raw_size = len(answer.encode("utf-8"))

    result = output.render_result(meta, answer, max_output=raw_size)

    assert result.truncated is True
    assert "\x1b" not in result.text
    assert len(result.text.encode("utf-8")) <= raw_size


def test_answer_cap_zero_disables_truncation() -> None:
    meta = make_session(Path("."))
    answer = "🙂" * 1000

    result = output.render_result(meta, answer, max_output=0)

    assert result.text == answer
    assert result.truncated is False


def test_json_envelope_has_pinned_fields_and_truncates_answer_only() -> None:
    meta = make_session(Path("."))
    answer = "🙂" * 2000

    result = output.render_result(meta, answer, json_mode=True, max_output=2000)
    payload = json.loads(result.text)

    assert set(payload) == {
        "status",
        "session_id",
        "turn",
        "created_at",
        "started_at",
        "finished_at",
        "stop_reason",
        "context",
        "paths",
        "answer",
        "truncated",
        "partial",
        "denied",
        "permissions_clamp",
        "capabilities",
        "next",
        "output_file",
    }
    assert payload["truncated"] is True
    # The fixture session never left `starting`, so no answer landed yet.
    assert payload["partial"] is True
    assert payload["denied"] == []
    assert payload["permissions_clamp"] is None
    assert "full answer:" in payload["answer"]
    assert result.text.encode("utf-8").decode("utf-8")


def test_background_and_output_file_shapes_are_separate(tmp_path: Path) -> None:
    meta = make_session(tmp_path)
    answer = "complete answer\n"
    output_file = tmp_path / "answer.md"

    background = json.loads(output.render_result(meta, json_mode=True, background=True).text)
    written = output.render_result(meta, answer)
    written_json = json.loads(output.render_result(meta, answer, json_mode=True).text)

    assert set(background) == {
        "session_id",
        "turn",
        "status",
        "created_at",
        "started_at",
        "finished_at",
        "paths",
        "denied",
        "permissions_clamp",
        "truncated",
        "partial",
        "capabilities",
        "next",
    }
    assert background["partial"] is False
    assert background["denied"] == []
    assert background["permissions_clamp"] is None
    assert background["next"] == ["acpc", "wait", meta.session_id]
    assert written_json["answer"] == answer
    assert written_json["next"] == ["acpc", "continue", meta.session_id]
    assert "output_file" not in written_json
    assert written.text == answer

    size = output.write_output_file(output_file, answer)
    assert output_file.read_text(encoding="utf-8") == answer
    assert size == len(answer.encode("utf-8"))


def test_json_envelope_reports_denials_and_the_inherited_clamp(tmp_path: Path) -> None:
    meta = make_session(tmp_path)
    meta.denied = {"edit": 2, "switch_mode:yolo": 1}
    meta.denial_details = {
        "edit": {
            "category": "edit",
            "minimum_policy": "edit",
            "remedy": "pass --permissions edit",
        },
        "switch_mode:yolo": {
            "category": "switch_mode",
            "target": "yolo",
            "minimum_policy": "all",
            "remedy": "pass --permissions all",
        },
    }
    meta.resolution = {
        "resolved": {
            "permissions": {
                "value": "edit",
                "clamp": {"requested": "all", "ceiling": "edit", "effective": "edit"},
            }
        }
    }

    payload = json.loads(output.render_result(meta, "answer", json_mode=True).text)

    assert payload["denied"] == [
        {
            "category": "edit",
            "count": 2,
            "minimum_policy": "edit",
            "remedy": "pass --permissions edit",
        },
        {
            "category": "switch_mode",
            "count": 1,
            "minimum_policy": "all",
            "remedy": "pass --permissions all",
            "target": "yolo",
        },
    ]
    assert payload["permissions_clamp"] == {
        "requested": "all",
        "ceiling": "edit",
        "effective": "edit",
    }


def test_flat_legacy_denials_render_as_self_describing_records() -> None:
    meta = make_session(Path("."))
    meta.denied = {"edit": 1}

    payload = json.loads(output.render_result(meta, "answer", json_mode=True).text)

    assert payload["denied"] == [
        {
            "category": "edit",
            "count": 1,
            "minimum_policy": "edit",
            "remedy": "pass --permissions edit",
        }
    ]


def test_summary_is_one_prefixed_stderr_line() -> None:
    meta = make_session(Path("."))
    meta = sessions.transition(
        meta.session_id,
        "succeeded",
        clock=lambda: 112.0,
        exit_code=0,
        stop_reason="end_turn",
        context={"used": 41_000, "size": 200_000, "peak": 41_000},
    )
    line = output.format_summary(meta, runtime=12.0)

    assert line.startswith("-- ")
    assert "\n" not in line
    assert "exit 0" in line
    assert "ctx 41k/200k, peak 41k" in line
    assert f"dir {sessions.session_dir(meta.session_id)}" in line
    assert f"Next: acpc continue {meta.session_id}" in line
    assert "steer_mode cancel-then-start" in line and "| partial |" not in line


def test_format_context_shows_a_dot_for_unobserved_usage() -> None:
    """SPEC.md V6c (draft.11): unobserved usage is `·`, never zeros."""
    assert output.format_context(None) == "ctx ·"
    assert output.format_context({"used": 0, "size": None, "peak": 0}) == "ctx 0, peak 0"
    assert (
        output.format_context({"used": 1500, "size": 200_000, "peak": 1500})
        == "ctx 1.5k/200k, peak 1.5k"
    )


def test_summary_shows_a_dot_for_unobserved_tokens() -> None:
    meta = make_session(Path("."))
    meta = sessions.transition(
        meta.session_id,
        "succeeded",
        clock=lambda: 112.0,
        exit_code=0,
        stop_reason="end_turn",
    )
    line = output.format_summary(meta, runtime=12.0)

    assert "ctx ·" in line
    assert "0 tok" not in line


def test_summary_names_partial_limit_correction_and_truncation() -> None:
    meta = make_session(Path("."))
    meta = sessions.transition(
        meta.session_id, "canceled", limit={"reason": "rate_limit", "resume_at": "soon"}
    )
    correction = {
        "steer_mode": "in-place",
        "target_turn": 1,
        "target_status": "s",
        "message_state": "m",
    }
    line = output.format_summary(
        meta, correction_result=correction, truncated_output_file="/x/answer.md"
    )

    assert "| partial |" in line and "limit: rate_limit, resumes soon" in line
    assert "correction: in-place" in line and "truncated → /x/answer.md" in line


def test_summary_places_continue_command_before_route_note() -> None:
    meta = make_session(Path("."))
    meta = sessions.transition(
        meta.session_id,
        "succeeded",
        clock=lambda: 112.0,
        exit_code=0,
        stop_reason="end_turn",
    )

    segments = output.format_summary(
        meta, runtime=12.0, route_note="queued for a daemon slot"
    ).split(" | ")

    assert segments[-2:] == [
        f"Next: acpc continue {meta.session_id}",
        "queued for a daemon slot",
    ]
