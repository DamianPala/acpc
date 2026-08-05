"""Behavioral tests for log and status rendering."""

import json
from pathlib import Path

import pytest

from acpc import render, sessions


@pytest.fixture(autouse=True)
def isolated_state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "state"))


def make_session(*, session_id_hint: str = "prompt") -> sessions.SessionMeta:
    return sessions.create_session(
        entry="mock",
        base_adapter="mock",
        prompt=session_id_hint,
        clock=lambda: 100.0,
    )


def event(index: int, event_type: str, **fields: object) -> dict[str, object]:
    return {"i": index, "ts": 1_700_000_000, "type": event_type, **fields}


def test_condensed_events_include_tool_details_and_message_length() -> None:
    events = [
        event(
            1,
            "tool",
            name="Bash",
            args_summary='"pytest -x"',
            status="exit 1",
            duration_ms=2300,
        ),
        event(2, "msg", text="short message"),
        event(3, "error", message="permission denied: write outside cwd"),
    ]

    result = render.render_events(events)

    assert "tool" in result.text
    assert '"pytest -x"' in result.text
    assert "exit 1" in result.text
    assert "2.3s" in result.text
    assert '"short message" (13 chars)' in result.text
    assert "permission denied: write outside cwd" in result.text
    assert result.next_cursor == 3


def test_prose_keeps_messages_full_and_errors_but_filters_tools() -> None:
    long_message = "message " * 80
    events = [
        event(1, "tool", name="Bash", args_summary="make test", status="completed", duration_ms=1),
        event(2, "msg", text=long_message),
        event(3, "error", message="agent error"),
    ]

    result = render.render_events(events, prose=True)

    assert long_message in result.text
    assert "Bash" not in result.text
    assert "[" in result.text and "error agent error" in result.text
    assert result.next_cursor == 3


def test_failed_view_can_expand_the_last_agent_message() -> None:
    last_message = ("The failure details are important. " * 20).strip()
    events = [event(1, "msg", text="first"), event(2, "msg", text=last_message)]

    result = render.render_events(events, full_last_message=True)

    assert last_message in result.text
    assert result.next_cursor == 2


def test_log_budget_stops_before_next_event_and_keeps_cursor_on_printed_event() -> None:
    events = [
        event(1, "msg", text="first"),
        event(2, "msg", text="x" * 500),
        event(3, "msg", text="third"),
    ]
    result = render.render_events(
        events,
        max_output=200,
        transcript_path="/tmp/transcript.ndjson",
    )

    assert result.truncated is True
    assert result.next_cursor == 1
    assert len(result.text.encode("utf-8")) <= 200
    assert "first" in result.text
    assert "third" not in result.text
    assert "full transcript: /tmp/transcript.ndjson" in result.text


def test_single_over_budget_event_advances_cursor_and_is_utf8_safe() -> None:
    events = [event(9, "msg", text="🙂" * 500)]

    result = render.render_events(events, max_output=80)

    result.text.encode("utf-8").decode("utf-8")
    assert result.truncated is True
    assert result.next_cursor == 9


def test_json_log_truncation_is_a_valid_typed_event() -> None:
    events = [event(1, "msg", text="one"), event(2, "msg", text="two" * 500)]

    result = render.render_events(
        events,
        json_mode=True,
        max_output=100,
        transcript_path="transcript.ndjson",
    )
    lines = [json.loads(line) for line in result.text.splitlines()]

    assert result.truncated is True
    assert lines[-1] == {"type": "truncated", "path": "transcript.ndjson"}
    assert result.next_cursor == 1


def test_json_single_over_budget_event_advances_the_cursor() -> None:
    result = render.render_events(
        [event(9, "msg", text="x" * 500)],
        json_mode=True,
        max_output=100,
        transcript_path="transcript.ndjson",
    )

    assert json.loads(result.text) == {"type": "truncated", "path": "transcript.ndjson"}
    assert result.next_cursor == 9


def test_log_footer_groups_state_qualifier_and_matches_spec() -> None:
    meta = make_session()
    running = sessions.transition(
        meta.session_id,
        "running",
        clock=lambda: 100.0,
        pid=123,
        process_start_time="token",
    )
    running_footer = render.format_log_footer(running, cursor=45, event_count=45, runtime=192.0)
    done = sessions.transition(
        meta.session_id,
        "done",
        clock=lambda: 160.0,
        exit_code=0,
        stop_reason="end_turn",
        tokens=41_000,
        cost=0.42,
    )
    done_footer = render.format_log_footer(done, cursor=45, event_count=45, runtime=192.0)

    assert running_footer == "-- running 3m12s | 45 events | cursor: 45"
    assert done_footer == (
        f"-- done exit 0 | 3m12s | 41k tok | answer: "
        f"{sessions.answer_path(done.session_id)} | cursor: 45"
    )


def test_status_list_limits_finished_sessions_and_json_preserves_fields() -> None:
    finished = [make_session(session_id_hint=f"finished {index}") for index in range(6)]
    finished = [
        sessions.transition(meta.session_id, "done", clock=lambda: 110.0, exit_code=0)
        for meta in finished
    ]
    active = make_session(session_id_hint="active prompt")
    all_sessions = [active, *finished]

    text = render.render_status_list(all_sessions, clock=lambda: 120.0)
    payload = render.status_list_json(all_sessions, clock=lambda: 120.0)

    assert "active prompt" in text
    assert text.count("finished") == 5
    assert "--all for all 7" in text
    assert len(payload["sessions"]) == 6
    assert payload["sessions"][0]["prompt_snippet"] == "active prompt"


def test_status_detail_json_contains_pinned_vitals() -> None:
    meta = make_session()

    payload = render.status_detail_json(meta, clock=lambda: 120.0)

    assert payload["session_id"] == meta.session_id
    assert payload["state"] == "starting"
    assert payload["pid"] is None
    assert payload["turns"] == 1


def test_status_detail_abbreviates_home_and_uses_answer_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta = make_session()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    text = render.render_status_detail(meta, clock=lambda: 120.0)

    expected_dir = Path("~") / "state" / "sessions" / meta.session_id
    assert f"dir      {expected_dir} · answer: answer.md" in text
    assert f"answer: {sessions.answer_path(meta.session_id)}" not in text
