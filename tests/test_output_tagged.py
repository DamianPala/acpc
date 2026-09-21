"""Behavioral tests for the tagged `text` presentation (SPEC.md *Text presentation*).

Envelopes are built by hand rather than through a live session: `render_tagged`
takes the JSON envelope's own dict and renders a second view of it, so a fast,
hand-built envelope exercises exactly the same code a real one would.
"""

import json
from pathlib import Path

import pytest

from acpc import output, sessions


def envelope(**overrides: object) -> dict[str, object]:
    """A baseline foreground envelope, shaped like `result_envelope`'s output."""
    base: dict[str, object] = {
        "status": "succeeded",
        "session_id": "q7x2",
        "turn": 1,
        "created_at": "2026-01-01T00:00:00Z",
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": "2026-01-01T00:00:01Z",
        "stop_reason": "end_turn",
        "context": {"used": 42, "size": 200_000, "peak": 42},
        "paths": {
            "dir": "/state/sessions/q7x2",
            "prompt": "/state/sessions/q7x2/prompt.md",
            "transcript": "/state/sessions/q7x2/transcript.ndjson",
            "answer": "/state/sessions/q7x2/answer.md",
        },
        "answer": "hello",
        "truncated": False,
        "denied": [],
        "permissions_clamp": None,
        "capabilities": {"steer_mode": "cancel-then-start"},
        "next": ["acpc", "continue", "q7x2"],
        "partial": False,
    }
    base.update(overrides)
    return base


def background_envelope(**overrides: object) -> dict[str, object]:
    base = envelope(**overrides)
    base.pop("answer", None)
    base.pop("stop_reason", None)
    base.pop("context", None)
    base["next"] = ["acpc", "wait", base["session_id"]]
    return base


ANSWER_PATH = "/state/sessions/q7x2/answer.md"


def test_layout_matches_the_spec_worked_example_exactly() -> None:
    """SPEC.md *Text presentation* worked example: `capabilities` carries
    `continue_without_message` and `context` sits between `capabilities` and
    `stop_reason` in `<metadata>`."""
    result = output.render_tagged(
        envelope(
            capabilities={"steer_mode": "in-place", "continue_without_message": True},
            context={"used": 1834, "size": 200_000, "peak": 1834},
        ),
        max_output=0,
        answer_path=ANSWER_PATH,
    )

    assert result.text == (
        '<result session_id="q7x2" status="succeeded" partial="false">\n'
        "<metadata>\n"
        '{"turn":1,"capabilities":{"steer_mode":"in-place","continue_without_message":true},'
        '"context":{"used":1834,"size":200000,"peak":1834},'
        '"stop_reason":"end_turn","next":["acpc","continue","q7x2"]}\n'
        "</metadata>\n"
        "<answer>\n"
        "hello\n"
        "</answer>\n"
        "</result>\n"
    )


def test_unobserved_context_is_omitted_while_other_lead_fields_stay() -> None:
    """SPEC.md V6c (draft.11): a `context: null` result never puts `"context":null`
    in `<metadata>` — the lead-field rule already keeps out any `None` value."""
    result = output.render_tagged(envelope(context=None), max_output=0, answer_path=ANSWER_PATH)

    metadata = json.loads(result.text.splitlines()[2])
    assert "context" not in metadata
    assert set(metadata) == {"turn", "capabilities", "stop_reason", "next"}


def test_optional_fields_land_between_the_lead_fields_and_next() -> None:
    result = output.render_tagged(
        envelope(
            denied=[{"category": "edit", "count": 1, "minimum_policy": "edit", "remedy": "x"}],
            permissions_clamp={"requested": "all", "ceiling": "edit", "effective": "edit"},
            resume="warm",
            limit={"reason": "usage", "resume_at": "2026-01-01T01:00:00Z"},
        ),
        max_output=0,
        answer_path=ANSWER_PATH,
    )

    metadata = json.loads(result.text.splitlines()[2])
    assert list(metadata) == [
        "turn",
        "capabilities",
        "context",
        "stop_reason",
        "denied",
        "permissions_clamp",
        "resume",
        "limit",
        "next",
    ]


def test_empty_optional_fields_are_omitted_not_written_as_empty() -> None:
    result = output.render_tagged(
        envelope(denied=[], permissions_clamp=None, resume=None, limit=None),
        max_output=0,
        answer_path=ANSWER_PATH,
    )

    metadata = json.loads(result.text.splitlines()[2])
    assert set(metadata) == {"turn", "capabilities", "context", "stop_reason", "next"}


def test_no_selected_fields_omits_the_metadata_section() -> None:
    bare = envelope(
        turn=None,
        capabilities=None,
        stop_reason=None,
        context=None,
        next=None,
    )
    result = output.render_tagged(bare, max_output=0, answer_path=ANSWER_PATH)

    assert "<metadata>" not in result.text
    assert result.text == (
        '<result session_id="q7x2" status="succeeded" partial="false">\n'
        "<answer>\nhello\n</answer>\n</result>\n"
    )


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("no trailing newline", "<answer>\nno trailing newline\n</answer>"),
        ("ends with a newline\n", "<answer>\nends with a newline\n\n</answer>"),
        ("", "<answer>\n\n</answer>"),
    ],
)
def test_answer_section_newline_boundary(answer: str, expected: str) -> None:
    result = output.render_tagged(envelope(answer=answer), max_output=0, answer_path=ANSWER_PATH)

    assert expected in result.text


def test_five_line_answer_with_an_embedded_closing_tag_gets_counted_tags() -> None:
    """SPEC.md *Text presentation*: the worked example of the counted form."""
    answer = "Close each section explicitly:\n\n```text\n</answer>\n```"

    result = output.render_tagged(envelope(answer=answer), max_output=0, answer_path=ANSWER_PATH)

    assert f"<answer-5>\n{answer}\n</answer-5>\n" in result.text
    assert "<answer>" not in result.text.replace("<answer-5>", "")


@pytest.mark.parametrize(
    "answer",
    ["<answers> and <Answer> and <answer/> and a < b, none of the six exact strings"],
)
def test_near_miss_strings_do_not_trigger_counted_tags(answer: str) -> None:
    result = output.render_tagged(envelope(answer=answer), max_output=0, answer_path=ANSWER_PATH)

    assert f"<answer>\n{answer}\n</answer>" in result.text


def test_carriage_return_stays_in_the_line_and_only_lf_is_counted() -> None:
    answer = "<answer>\r\nsecond"

    result = output.render_tagged(envelope(answer=answer), max_output=0, answer_path=ANSWER_PATH)

    assert f"<answer-2>\n{answer}\n</answer-2>" in result.text
    assert "\r\n" in result.text


def test_a_content_line_matching_the_real_closing_tag_stays_content() -> None:
    """A line inside the counted extent that reads like `</answer-N>` is still
    answer text: the reader trusts N, not a string search, to find the end."""
    answer = "<answer>\n</answer-3>\nthird"

    result = output.render_tagged(envelope(answer=answer), max_output=0, answer_path=ANSWER_PATH)

    assert result.text.count("</answer-3>") == 2
    assert result.text.endswith(f"{answer}\n</answer-3>\n</result>\n")


def test_terminal_control_bytes_are_escaped_and_nothing_else_is() -> None:
    answer = 'before\x1b[31mred\x1b[0mafter <kept> & "kept"'

    result = output.render_tagged(envelope(answer=answer), max_output=0, answer_path=ANSWER_PATH)

    assert "\x1b" not in result.text
    assert "^[[31mred^[[0m" in result.text
    assert '<kept> & "kept"' in result.text


def test_attribute_values_escape_the_six_characters() -> None:
    raw_id = 'a&b<c>d"e\tf\rg\nh'
    result = output.render_tagged(
        envelope(session_id=raw_id, next=None), max_output=0, answer_path=ANSWER_PATH
    )

    opening = result.text.splitlines()[0]
    assert 'session_id="a&amp;b&lt;c&gt;d&quot;e&#9;f&#13;g&#10;h"' in opening


def test_background_receipt_has_no_answer_section_and_carries_paths() -> None:
    result = output.render_tagged(background_envelope(), max_output=0, answer_path=ANSWER_PATH)

    assert "<answer" not in result.text
    metadata = json.loads(result.text.splitlines()[2])
    assert metadata["paths"] == envelope()["paths"]
    assert metadata["next"] == ["acpc", "wait", "q7x2"]


def test_max_output_truncates_the_whole_document_and_names_the_full_file() -> None:
    long_answer = "x" * 5000
    budget = 500
    result = output.render_tagged(
        envelope(answer=long_answer), max_output=budget, answer_path=ANSWER_PATH
    )

    assert result.truncated is True
    assert len(result.text.encode("utf-8")) <= budget
    assert result.output_file == ANSWER_PATH
    metadata = json.loads(result.text.splitlines()[2])
    assert metadata["truncated"] is True
    assert metadata["output_file"] == ANSWER_PATH
    assert f"full answer: {ANSWER_PATH}" in result.text
    assert list(metadata)[-1] == "next"


def test_max_output_never_shortens_a_receipt() -> None:
    result = output.render_tagged(background_envelope(), max_output=1, answer_path=ANSWER_PATH)

    assert result.truncated is False
    assert "<result" in result.text


def test_json_mode_returns_the_original_answer_with_control_bytes_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "state"))
    meta = sessions.create_session(entry="mock", base_adapter="mock", prompt="p", clock=lambda: 1.0)
    answer = "before\x1b[31mred\x1b[0mafter"

    tagged = output.render_result(meta, answer, tagged=True, max_output=0)
    as_json = output.render_result(meta, answer, json_mode=True, max_output=0)

    assert "\x1b" not in tagged.text
    assert json.loads(as_json.text)["answer"] == answer
