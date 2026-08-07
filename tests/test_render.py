"""Behavioral tests for log and status rendering."""

import json
from pathlib import Path

import pytest

from acpc import render, sessions, transcript
from acpc.output import format_duration


@pytest.fixture(autouse=True)
def isolated_state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "state"))


def make_session(
    *, session_id_hint: str = "prompt", model: str | None = "mock-sonnet-5"
) -> sessions.SessionMeta:
    resolved: dict[str, object] = {}
    if model is not None:
        resolved["model"] = {"value": model, "source": "adapter default"}
    return sessions.create_session(
        entry="mock",
        base_adapter="mock",
        prompt=session_id_hint,
        clock=lambda: 100.0,
        resolution={"resolved": resolved},
    )


def add_transcript_event(meta: sessions.SessionMeta, timestamp: float) -> None:
    transcript.Transcript(sessions.transcript_path(meta.session_id)).append(
        "msg", text="activity", ts=timestamp
    )


def status_line(text: str, meta: sessions.SessionMeta) -> str:
    return next(line for line in text.splitlines() if line.startswith(meta.session_id))


def event(index: int, event_type: str, **fields: object) -> dict[str, object]:
    return {"i": index, "ts": 1_700_000_000, "type": event_type, **fields}


def test_condensed_events_include_tool_details_and_omit_routine_message_length() -> None:
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
    assert '"short message"' in result.text
    assert "(13 chars)" not in result.text
    assert "permission denied: write outside cwd" in result.text
    assert result.next_cursor == 3


def _message_snippet(line: str, label: str = "msg   ") -> str:
    return json.loads(line.split(label, 1)[1])


def test_message_snippet_snaps_to_the_last_word_boundary() -> None:
    text = ("word " * 60).strip()

    line = render.format_event(event(1, "msg", text=text))

    assert _message_snippet(line) == ("word " * 39).strip() + "..."


def test_message_snippet_hard_cuts_a_single_oversized_token() -> None:
    text = "x" * 300

    line = render.format_event(event(1, "msg", text=text))

    assert _message_snippet(line) == "x" * 197 + "..."


def test_message_snippet_does_not_split_a_multibyte_character() -> None:
    text = "🙂" * 300

    line = render.format_event(event(1, "msg", text=text))
    snippet = _message_snippet(line)

    assert snippet == "🙂" * 197 + "..."
    snippet.encode("utf-8").decode("utf-8")


def test_message_length_is_only_reported_for_oversized_chunks() -> None:
    short = render.format_event(event(1, "msg", text="x" * 1023))
    large = render.format_event(event(2, "msg", text="x" * 1024))

    assert "chars" not in short
    assert "(1k chars)" in large


def test_continuation_marker_marks_adjacent_same_type_events() -> None:
    events = [
        event(1, "msg", text="first"),
        event(2, "msg", text="second"),
        event(3, "thought", text="thinking"),
        event(4, "thought", text="still thinking"),
    ]

    lines = render.render_events(events).text.splitlines()

    assert "msg ↪" not in lines[0]
    assert "msg ↪" in lines[1]
    assert "thought ↪" not in lines[2]
    assert "thought ↪" in lines[3]


@pytest.mark.parametrize("separator", ["tool", "thought", "state", "usage"])
def test_continuation_marker_stops_at_an_intervening_event(separator: str) -> None:
    events = [
        event(1, "msg", text="first"),
        event(2, separator, text="intervening"),
        event(3, "msg", text="second"),
    ]

    msg_lines = [line for line in render.render_events(events).text.splitlines() if "msg" in line]

    assert len(msg_lines) == 2
    assert "msg ↪" not in msg_lines[1]


def test_first_event_in_a_page_is_not_marked_as_a_continuation() -> None:
    events = [event(10, "msg", text="page starts here"), event(11, "msg", text="continues")]

    lines = render.render_events(events, cursor=9).text.splitlines()

    assert "msg ↪" not in lines[0]
    assert "msg ↪" in lines[1]


def test_continuation_marker_requires_consecutive_event_indices() -> None:
    events = [event(10, "msg", text="first"), event(12, "msg", text="gap")]

    lines = render.render_events(events).text.splitlines()

    assert "msg ↪" not in lines[1]


def test_footer_reports_the_range_actually_printed_for_each_window() -> None:
    meta = sessions.transition(
        make_session().session_id,
        "running",
        clock=lambda: 100.0,
        pid=123,
        process_start_time="token",
    )
    events = [event(index, "msg", text=f"event-{index}") for index in range(1, 26)]

    windows = [
        (events[-20:], 5, 0, "6–25 of 25"),
        (events[-2:], 23, 0, "24–25 of 25"),
        (events[10:], 10, 0, "11–25 of 25"),
        (
            [
                event(1, "msg", text="first"),
                event(2, "msg", text="x" * 500),
                event(3, "msg", text="last"),
            ],
            0,
            200,
            "1–1 of 3",
        ),
        (events, 0, 0, "1–25 of 25"),
    ]

    for selected, cursor, max_output, expected in windows:
        rendered = render.render_events(selected, cursor=cursor, max_output=max_output)
        footer = render.format_log_footer(
            meta,
            cursor=rendered.next_cursor,
            event_count=25 if expected != "1–1 of 3" else 3,
            page_start=rendered.first_event,
            page_end=rendered.last_event,
            runtime=192.0,
        )
        assert f"events {expected}" in footer


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
    running_footer = render.format_log_footer(
        running,
        cursor=45,
        event_count=45,
        page_start=26,
        page_end=45,
        runtime=192.0,
    )
    done = sessions.transition(
        meta.session_id,
        "done",
        clock=lambda: 160.0,
        exit_code=0,
        stop_reason="end_turn",
        tokens=41_000,
        cost=0.42,
    )
    done_footer = render.format_log_footer(
        done,
        cursor=45,
        event_count=45,
        page_start=26,
        page_end=45,
        runtime=192.0,
    )

    assert running_footer == "-- running 3m12s | events 26–45 of 45 | cursor: 45"
    assert done_footer == (
        f"-- done exit 0 | 3m12s | 41k tok | answer: {sessions.answer_path(done.session_id)} "
        "| events 26–45 of 45 | cursor: 45"
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


def test_status_list_has_one_lowercase_header_and_aligns_long_columns() -> None:
    short = sessions.create_session(
        entry="tiny",
        base_adapter="mock",
        prompt="short prompt",
        name="short-name",
        clock=lambda: 100.0,
        resolution={"resolved": {"model": {"value": "tiny-model", "source": "entry"}}},
    )
    long = sessions.create_session(
        entry="entry-name-longer-than-the-old-column",
        base_adapter="mock",
        prompt="long prompt",
        name="name-longer-than-the-old-column",
        clock=lambda: 100.0,
        resolution={
            "resolved": {
                "model": {
                    "value": "vendor/model-with-a-deliberately-long-identifier",
                    "source": "entry",
                }
            }
        },
    )

    text = render.render_status_list([short, long], all_sessions=True, clock=lambda: 120.0)
    lines = text.splitlines()
    header = lines[0]
    headings = ("ID", "ENTRY", "MODEL", "STATE", "RUNTIME", "IDLE", "NAME", "PROMPT")

    assert header.split() == list(headings)
    assert lines.count(header) == 1
    header_offsets = [header.index(heading) for heading in headings]
    expected_rows = {
        short.session_id: (
            short.session_id,
            short.entry,
            "tiny-model",
            "starting",
            "0m20s",
            "·",
            "short-name",
            '"short prompt"',
        ),
        long.session_id: (
            long.session_id,
            long.entry,
            "vendor/model-with-a-deliberately-long-identifier",
            "starting",
            "0m20s",
            "·",
            "name-longer-than-the-old-column",
            '"long prompt"',
        ),
    }
    for session_id, cells in expected_rows.items():
        row = status_line(text, sessions.read_meta(session_id))
        assert [row.index(cell, offset) for cell, offset in zip(cells, header_offsets)] == (
            header_offsets
        )


def test_status_list_computes_widths_down_for_short_values() -> None:
    meta = sessions.create_session(
        entry="tiny",
        base_adapter="mock",
        prompt="p",
        name="short-name",
        clock=lambda: 100.0,
        resolution={"resolved": {"model": {"value": "tiny-model", "source": "entry"}}},
    )

    text = render.render_status_list([meta], all_sessions=True, clock=lambda: 120.0)
    lines = text.splitlines()
    header = lines[0]
    row = status_line(text, meta)

    assert row.index("tiny") == header.index("ENTRY")
    assert row.index("tiny-model") == header.index("MODEL")
    assert row.index("short-name") == header.index("NAME")


def test_status_list_places_the_resolved_model_between_entry_and_state() -> None:
    """The column's position is the contract: entry, then who actually ran."""
    meta = make_session(model="gpt-5.6-luna")

    row = status_line(render.render_status_list([meta], clock=lambda: 120.0), meta)
    columns = row.split()

    assert columns[:4] == [meta.session_id, "mock", "gpt-5.6-luna", meta.state]


def test_status_views_fall_back_to_a_dot_when_no_model_was_resolved() -> None:
    """A meta from an older writer must not take the status views down."""
    meta = make_session(model=None)

    row = status_line(render.render_status_list([meta], clock=lambda: 120.0), meta)
    detail = render.render_status_detail(meta, clock=lambda: 120.0)

    assert row.split()[:4] == [meta.session_id, "mock", "·", meta.state]
    assert "model: ·" in detail
    assert render.status_list_json([meta], clock=lambda: 120.0)["sessions"][0]["model"] is None
    assert render.status_detail_json(meta, clock=lambda: 120.0)["model"] is None


def test_status_json_carries_the_resolved_model_in_both_shapes() -> None:
    meta = make_session(model="gpt-5.6-terra")

    list_payload = render.status_list_json([meta], clock=lambda: 120.0)
    detail_payload = render.status_detail_json(meta, clock=lambda: 120.0)
    detail_text = render.render_status_detail(meta, clock=lambda: 120.0)

    assert list_payload["sessions"][0]["model"] == "gpt-5.6-terra"
    assert detail_payload["model"] == "gpt-5.6-terra"
    assert "model: gpt-5.6-terra" in detail_text


def test_two_entries_on_one_model_are_distinguishable_only_by_the_model_column() -> None:
    """The reason the column exists: `extends` hides who does the work."""
    builder = sessions.create_session(
        entry="builder",
        base_adapter="codex",
        prompt="implement",
        clock=lambda: 100.0,
        resolution={"resolved": {"model": {"value": "gpt-5.6-luna", "source": "entry"}}},
    )
    reviewer = sessions.create_session(
        entry="reviewer",
        base_adapter="codex",
        prompt="review",
        clock=lambda: 100.0,
        resolution={"resolved": {"model": {"value": "gpt-5.6-terra", "source": "entry"}}},
    )

    text = render.render_status_list([builder, reviewer], clock=lambda: 120.0, all_sessions=True)

    assert "gpt-5.6-luna" in status_line(text, builder)
    assert "gpt-5.6-terra" in status_line(text, reviewer)


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


def test_status_detail_text_remains_the_existing_labeled_view() -> None:
    meta = make_session()

    text = render.render_status_detail(meta, clock=lambda: 120.0)

    assert text == (
        "state    starting · exit · · 0m20s · 0 tok\n"
        "agent    mock (mock) · model: mock-sonnet-5 · name: ·\n"
        f"dir      {sessions.session_dir(meta.session_id)} · answer: answer.md\n"
    )
