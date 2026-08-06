"""Behavioral tests for log and status rendering."""

import json
from pathlib import Path

import pytest

from acpc import render, sessions, transcript
from acpc.output import format_duration


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


def add_transcript_event(meta: sessions.SessionMeta, timestamp: float) -> None:
    transcript.Transcript(sessions.transcript_path(meta.session_id)).append(
        "msg", text="activity", ts=timestamp
    )


def status_line(text: str, meta: sessions.SessionMeta) -> str:
    return next(line for line in text.splitlines() if line.startswith(meta.session_id))


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


def test_status_list_renders_idle_age_for_active_and_dot_for_finished() -> None:
    active = make_session(session_id_hint="active prompt")
    add_transcript_event(active, 115.0)
    finished = sessions.transition(
        make_session(session_id_hint="finished prompt").session_id,
        "done",
        clock=lambda: 110.0,
        exit_code=0,
    )
    add_transcript_event(finished, 105.0)

    text = render.render_status_list([active, finished], clock=lambda: 120.0, all_sessions=True)

    active_row = status_line(text, active)
    finished_row = status_line(text, finished)
    assert "idle 0m05s" in active_row
    assert "·" in finished_row
    assert "idle " not in finished_row


def test_status_detail_labels_idle_age_only_for_active_sessions() -> None:
    active = make_session()
    add_transcript_event(active, 115.0)
    finished = sessions.transition(
        make_session().session_id,
        "done",
        clock=lambda: 110.0,
        exit_code=0,
    )
    add_transcript_event(finished, 105.0)

    active_state = render.render_status_detail(active, clock=lambda: 120.0).splitlines()[0]
    finished_state = render.render_status_detail(finished, clock=lambda: 120.0).splitlines()[0]

    assert "· idle 0m05s" in active_state
    assert "idle " not in finished_state


def test_status_ages_distinguish_fresh_and_stale_activity_and_grow_with_time() -> None:
    fresh = make_session(session_id_hint="fresh")
    stale = make_session(session_id_hint="stale")
    add_transcript_event(fresh, 119.0)
    add_transcript_event(stale, 105.0)

    text = render.render_status_list([fresh, stale], clock=lambda: 120.0, all_sessions=True)
    initial = render.status_list_json([fresh, stale], all_sessions=True, clock=lambda: 120.0)
    later = render.status_list_json([fresh, stale], all_sessions=True, clock=lambda: 130.0)
    initial_rows = {row["session_id"]: row for row in initial["sessions"]}
    later_rows = {row["session_id"]: row for row in later["sessions"]}

    assert "idle " in status_line(text, fresh)
    assert "idle " in status_line(text, stale)
    assert (
        initial_rows[stale.session_id]["idle_seconds"]
        > initial_rows[fresh.session_id]["idle_seconds"]
    )
    assert (
        later_rows[stale.session_id]["idle_seconds"]
        > initial_rows[stale.session_id]["idle_seconds"]
    )


def test_status_json_idle_seconds_match_text_and_finished_sessions_are_null() -> None:
    active = make_session()
    add_transcript_event(active, 115.0)
    finished = sessions.transition(
        make_session().session_id,
        "done",
        clock=lambda: 110.0,
        exit_code=0,
    )
    add_transcript_event(finished, 105.0)

    list_payload = render.status_list_json(
        [active, finished], all_sessions=True, clock=lambda: 120.0
    )
    list_rows = {row["session_id"]: row for row in list_payload["sessions"]}
    active_idle = list_rows[active.session_id]["idle_seconds"]
    finished_idle = list_rows[finished.session_id]["idle_seconds"]
    text = render.render_status_list([active, finished], all_sessions=True, clock=lambda: 120.0)

    assert isinstance(active_idle, float)
    assert finished_idle is None
    assert f"idle {format_duration(active_idle)}" in status_line(text, active)

    detail_payload = render.status_detail_json(active, clock=lambda: 120.0)
    finished_detail = render.status_detail_json(finished, clock=lambda: 120.0)
    assert detail_payload["idle_seconds"] == active_idle
    assert isinstance(detail_payload["idle_seconds"], float)
    assert finished_detail["idle_seconds"] is None


def test_status_without_a_transcript_shows_dot_and_null() -> None:
    active = make_session()

    text = render.render_status_list([active], all_sessions=True, clock=lambda: 120.0)
    payload = render.status_list_json([active], all_sessions=True, clock=lambda: 120.0)

    assert "·" in status_line(text, active)
    assert "idle " not in status_line(text, active)
    assert payload["sessions"][0]["idle_seconds"] is None


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
