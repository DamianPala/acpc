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
        "state",
        "session_id",
        "stop_reason",
        "paths",
        "cost",
        "answer",
        "truncated",
        "denied",
        "permissions_clamp",
    }
    assert payload["truncated"] is True
    assert payload["denied"] == []
    assert payload["permissions_clamp"] is None
    assert "full answer:" in payload["answer"]
    assert result.text.encode("utf-8").decode("utf-8")


def test_background_and_output_file_shapes_are_separate(tmp_path: Path) -> None:
    meta = make_session(tmp_path)
    answer = "complete answer\n"
    output_file = tmp_path / "answer.md"

    background = json.loads(output.render_result(meta, json_mode=True, background=True).text)
    written = output.render_result(meta, answer, output_file=output_file)
    written_json = json.loads(
        output.render_result(meta, answer, json_mode=True, output_file=output_file).text
    )

    assert set(background) == {
        "session_id",
        "state",
        "paths",
        "denied",
        "permissions_clamp",
    }
    assert background["denied"] == []
    assert background["permissions_clamp"] is None
    assert "answer" not in written_json
    assert written_json["output_file"] == str(output_file)
    assert "answer.md" in written.text

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
        "done",
        clock=lambda: 112.0,
        exit_code=0,
        stop_reason="end_turn",
        tokens=41_000,
        cost=0.42,
    )
    line = output.format_summary(meta, runtime=12.0)

    assert line.startswith("-- ")
    assert "\n" not in line
    assert "exit 0" in line
    assert "41k tok" in line
    assert f"dir {sessions.session_dir(meta.session_id)}" in line
    assert f"continue: acpc continue {meta.session_id}" in line


def test_summary_places_continue_command_before_route_note() -> None:
    meta = make_session(Path("."))
    meta = sessions.transition(
        meta.session_id,
        "done",
        clock=lambda: 112.0,
        exit_code=0,
        stop_reason="end_turn",
    )

    segments = output.format_summary(
        meta, runtime=12.0, route_note="queued for a daemon slot"
    ).split(" | ")

    assert segments[-2:] == [
        f"continue: acpc continue {meta.session_id}",
        "queued for a daemon slot",
    ]
