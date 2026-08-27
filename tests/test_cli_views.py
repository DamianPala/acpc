"""Behavioral tests for the ``status`` and ``log`` views."""

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from acpc import proc, sessions, transcript, vocab
from acpc.cli import main

MOCK_AGENT_SCRIPT = str(Path(__file__).with_name("mock_agent.py"))

MOCK_ENTRY = f"""
name = "Mock Agent"
command = "{sys.executable} {MOCK_AGENT_SCRIPT}"
install_command = "true"
home = "~/.mock"
home_env = "MOCK_HOME"

[modes]
default = {{ grants = "read", delegates = true }}
plan = {{ grants = "read", delegates = true }}
yolo = {{ grants = "all", delegates = false }}

[presets]
fast = {{ model = "mock-haiku-4-5", effort = "high" }}
standard = {{ model = "mock-sonnet-5", effort = "high" }}
max = {{ model = "mock-opus-5", effort = "xhigh" }}
"""


@pytest.fixture(autouse=True)
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Give every view test a private state root."""
    root = tmp_path / "state"
    agents = root / "agents"
    agents.mkdir(parents=True)
    (agents / "mock.toml").write_text(MOCK_ENTRY, encoding="utf-8")
    monkeypatch.setenv("ACPC_HOME", str(root))
    return root


@pytest.fixture
def cli() -> CliRunner:
    """Use non-TTY streams so the CLI's automation rules are exercised."""
    return CliRunner()


def invoke(cli: CliRunner, *args: str):
    """Invoke the real CLI boundary with separate stdout and stderr capture."""
    return cli.invoke(main, list(args), catch_exceptions=False)


def run_mock(
    cli: CliRunner,
    prompt: str = "a viewable answer",
    *,
    expected_exit: int = vocab.EXIT_OK,
) -> str:
    """Run the real mock adapter and return its session id."""
    result = invoke(cli, "run", "mock", prompt, "--quiet", "--json")
    assert result.exit_code == expected_exit
    return json.loads(result.stdout)["session_id"]


def finished_session(prompt: str = "finished session", *, clock_value: float = 100.0):
    """Create a finished metadata-only session for view selection checks."""
    meta = sessions.create_session(
        entry="mock",
        base_adapter="mock",
        prompt=prompt,
        clock=lambda: clock_value,
    )
    return sessions.transition(
        meta.session_id,
        "done",
        exit_code=0,
        clock=lambda: clock_value + 1,
    )


def session_with_messages(count: int):
    """Create a finished session with numbered transcript messages."""
    return session_with_texts(*(f"event-{index}" for index in range(count)))


def session_with_texts(*messages: str):
    """Create a finished session with the supplied transcript messages."""
    meta = finished_session()
    transcript_file = transcript.Transcript(sessions.transcript_path(meta.session_id))
    for message in messages:
        transcript_file.append("msg", text=message)
    return meta


def running_session(prompt: str = "running session"):
    """Create a session whose host is this test process."""
    meta = sessions.create_session(
        entry="mock",
        base_adapter="mock",
        prompt=prompt,
        clock=lambda: 100.0,
    )
    return sessions.mark_running(
        meta.session_id,
        pid=os.getpid(),
        process_start_time=proc.process_start_time(),
        clock=lambda: 101.0,
    )


def test_status_hides_finished_sessions_beyond_the_recent_five(cli: CliRunner) -> None:
    """The default status list contains only the five newest finished sessions."""
    for index in range(6):
        finished_session(f"old prompt {index}", clock_value=100 + index)

    result = invoke(cli, "status")

    assert "old prompt 0" not in result.stdout and "old prompt 5" in result.stdout


def test_status_all_includes_older_finished_sessions(cli: CliRunner) -> None:
    """Status --all includes finished sessions outside the recent window."""
    for index in range(6):
        finished_session(f"old prompt {index}", clock_value=100 + index)

    result = invoke(cli, "status", "--all")

    assert "old prompt 0" in result.stdout


def test_status_resolves_a_named_session(cli: CliRunner) -> None:
    """A status alias resolves to the aliased session directory."""
    meta = finished_session()
    sessions.update_meta(meta.session_id, name="named-session")

    result = invoke(cli, "status", "named-session")

    assert str(sessions.session_dir(meta.session_id)) in result.stdout


def test_status_json_with_an_id_reports_detail_fields(cli: CliRunner) -> None:
    """Status JSON with an id returns the detail envelope."""
    meta = finished_session()

    result = invoke(cli, "status", meta.session_id, "--json")

    payload = json.loads(result.stdout)
    assert payload["state"] == "done"
    assert payload["idle_seconds"] is None


def test_status_reports_the_model_a_real_dispatch_resolved(cli: CliRunner) -> None:
    """End to end: the column reads the resolution the dispatch actually stored."""
    session_id = run_mock(cli, "echo:which model")

    text = invoke(cli, "status", session_id).stdout
    detail = json.loads(invoke(cli, "status", session_id, "--json").stdout)
    row = json.loads(invoke(cli, "status", "--json").stdout)["sessions"][0]

    assert detail["model"] == "mock-sonnet-5"
    assert row["model"] == "mock-sonnet-5"
    assert "model: mock-sonnet-5" in text


def test_status_detail_surfaces_current_failure_and_json(cli: CliRunner) -> None:
    session_id = run_mock(
        cli,
        "crash-late:partial answer before the adapter failed",
        expected_exit=vocab.EXIT_AGENT_ERROR,
    )

    meta = sessions.read_meta(session_id)
    assert meta.failure is not None
    stored = json.loads(sessions.meta_path(session_id).read_text(encoding="utf-8"))
    assert stored["failure"] == meta.failure

    text_result = invoke(cli, "status", session_id)
    json_result = invoke(cli, "status", session_id, "--json")

    assert f"failure  {meta.failure} · continue: acpc continue {session_id}" in text_result.stdout
    assert json.loads(json_result.stdout)["failure"] == meta.failure


def test_status_detail_omits_failure_for_successful_turn(cli: CliRunner) -> None:
    session_id = run_mock(cli)

    text_result = invoke(cli, "status", session_id)
    json_result = invoke(cli, "status", session_id, "--json")

    assert "failure  " not in text_result.stdout
    assert json.loads(json_result.stdout)["failure"] is None


def test_status_detail_reads_meta_without_the_failure_field(cli: CliRunner) -> None:
    meta = finished_session()
    stored = json.loads(sessions.meta_path(meta.session_id).read_text(encoding="utf-8"))
    stored.pop("failure")
    sessions.meta_path(meta.session_id).write_text(json.dumps(stored), encoding="utf-8")

    text_result = invoke(cli, "status", meta.session_id)
    json_result = invoke(cli, "status", meta.session_id, "--json")

    assert "failure  " not in text_result.stdout
    assert json.loads(json_result.stdout)["failure"] is None


def test_continue_clears_the_previous_failure_from_status(cli: CliRunner) -> None:
    session_id = run_mock(
        cli,
        "crash-late:partial answer before the adapter failed",
        expected_exit=vocab.EXIT_AGENT_ERROR,
    )

    result = invoke(cli, "continue", session_id, "turn two succeeds", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert sessions.turn_path(session_id, "answer", 1).is_file()
    assert sessions.read_meta(session_id).failure is None
    assert "failure  " not in invoke(cli, "status", session_id).stdout
    assert json.loads(invoke(cli, "status", session_id, "--json").stdout)["failure"] is None


def test_status_json_without_an_id_returns_a_session_list(cli: CliRunner) -> None:
    """Status JSON without an id returns the list envelope."""
    meta = finished_session()

    result = invoke(cli, "status", "--json")

    row = json.loads(result.stdout)["sessions"][0]
    assert row["session_id"] == meta.session_id
    assert row["idle_seconds"] is None
    assert result.stdout == json.dumps(json.loads(result.stdout), ensure_ascii=False) + "\n"


def test_status_without_a_transcript_is_clean_for_an_active_session(cli: CliRunner) -> None:
    meta = running_session("no transcript yet")

    text_result = invoke(cli, "status", meta.session_id)
    json_result = invoke(cli, "status", meta.session_id, "--json")

    assert text_result.exit_code == 0
    assert "idle " not in text_result.stdout
    assert json.loads(json_result.stdout)["idle_seconds"] is None


def test_status_rejects_all_with_a_session_id(cli: CliRunner) -> None:
    """Status refuses --all when a particular session was selected."""
    meta = finished_session()

    result = invoke(cli, "status", meta.session_id, "--all")

    assert result.exit_code == vocab.EXIT_USAGE


def test_status_reports_a_dead_running_session_as_orphaned(cli: CliRunner) -> None:
    """Status verifies liveness and creates no transcript for an orphan."""
    meta = sessions.create_session(entry="mock", base_adapter="mock", prompt="live check")
    sessions.mark_running(
        meta.session_id,
        pid=999_999_999,
        process_start_time="stale-process-token",
    )

    result = invoke(cli, "status", meta.session_id, "--json")

    assert json.loads(result.stdout)["state"] == "orphaned"
    assert not sessions.transcript_path(meta.session_id).exists()


def test_log_keeps_events_on_stdout_and_footer_on_stderr(cli: CliRunner) -> None:
    """A condensed log keeps content and acpc metadata on separate streams."""
    session_id = run_mock(cli)

    result = invoke(cli, "log", session_id)

    assert "cursor:" in result.stderr and "cursor:" not in result.stdout


def test_failed_log_and_wait_surface_the_recorded_cause(cli: CliRunner) -> None:
    run_result = invoke(cli, "run", "mock", "auth:missing credentials", "--quiet", "--json")
    assert run_result.exit_code == vocab.EXIT_AGENT_ERROR
    session_id = json.loads(run_result.stdout)["session_id"]

    log_result = invoke(cli, "log", session_id, "--quiet")
    wait_result = invoke(cli, "wait", session_id, "--quiet")

    assert log_result.exit_code == vocab.EXIT_OK
    assert "authentication was refused" in log_result.stdout
    assert wait_result.exit_code == vocab.EXIT_AGENT_ERROR
    assert "run 'mock login'" in wait_result.stdout

    # Without --quiet the same cause rides the stderr summary, so a poller that
    # discards the answer still learns why the session failed.
    loud_result = invoke(cli, "wait", session_id)
    assert "| failure: " in loud_result.stderr
    assert "run 'mock login'" in loud_result.stderr


def test_log_defaults_to_the_last_twenty_events(cli: CliRunner) -> None:
    """A log without --since selects the last twenty events."""
    meta = session_with_messages(21)

    result = invoke(cli, "log", meta.session_id)

    assert "event-0" not in result.stdout and "event-20" in result.stdout


def test_log_groups_reads_before_taking_the_default_tail(cli: CliRunner) -> None:
    meta = finished_session()
    transcript_file = transcript.Transcript(sessions.transcript_path(meta.session_id))
    transcript_file.append("msg", text="agent prose must remain visible")
    for _ in range(25):
        transcript_file.append(
            "permission",
            kind="fs/read_text_file",
            decision="allow",
            auto=True,
        )

    result = invoke(cli, "log", meta.session_id)

    assert "agent prose must remain visible" in result.stdout
    assert "fs/read_text_file" in result.stdout
    assert "×25" in result.stdout


def test_log_since_a_group_cursor_resumes_after_the_collapsed_reads(cli: CliRunner) -> None:
    meta = finished_session()
    transcript_file = transcript.Transcript(sessions.transcript_path(meta.session_id))
    transcript_file.append("msg", text="agent prose before reads")
    for _ in range(25):
        transcript_file.append(
            "permission",
            kind="fs/read_text_file",
            decision="allow",
            auto=True,
        )
    transcript_file.append("msg", text="agent prose after reads")

    first = invoke(cli, "log", meta.session_id)
    group_line = next(line for line in first.stdout.splitlines() if "×25" in line)
    group_cursor = int(group_line.split("cursor: ", 1)[1].split(")", 1)[0])

    resumed = invoke(cli, "log", meta.session_id, "--since", str(group_cursor))

    assert "agent prose after reads" in resumed.stdout
    assert "fs/read_text_file" not in resumed.stdout


def test_wait_accepts_a_suffixed_timeout_on_a_finished_session(cli: CliRunner) -> None:
    session_id = run_mock(cli)

    result = invoke(cli, "wait", session_id, "--timeout", "1m", "--quiet")

    assert result.exit_code == vocab.EXIT_OK


def test_log_accepts_a_suffixed_timeout_on_a_finished_session(cli: CliRunner) -> None:
    session_id = run_mock(cli)

    result = invoke(cli, "log", session_id, "--follow", "--timeout", "1m", "--quiet")

    assert result.exit_code == vocab.EXIT_OK


def test_zero_timeout_remains_allowed_for_wait_and_log(cli: CliRunner) -> None:
    running = running_session("zero wait timeout")
    wait_result = invoke(cli, "wait", running.session_id, "--timeout", "0", "--quiet")

    finished = run_mock(cli)
    log_result = invoke(cli, "log", finished, "--wait-new", "--timeout", "0", "--quiet")

    assert wait_result.exit_code == vocab.EXIT_TIMEOUT
    assert log_result.exit_code == vocab.EXIT_TIMEOUT


@pytest.mark.parametrize("verb_args", [("wait", "missing"), ("log", "missing", "--wait-new")])
def test_negative_timeout_is_a_usage_error_for_waiting_views(
    cli: CliRunner, verb_args: tuple[str, ...]
) -> None:
    result = invoke(cli, *verb_args, "--timeout", "-1")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "--timeout" in result.stderr


def test_log_tail_limits_the_selected_events(cli: CliRunner) -> None:
    """Log --tail limits the number of rendered event lines."""
    session_id = run_mock(cli)

    result = invoke(cli, "log", session_id, "--tail", "1")

    assert len(result.stdout.splitlines()) <= 1


def test_log_json_emits_indexed_ndjson(cli: CliRunner) -> None:
    """Log --json emits valid event lines carrying integer indices."""
    session_id = run_mock(cli)

    result = invoke(cli, "log", session_id, "--json")
    events = [json.loads(line) for line in result.stdout.splitlines()]

    assert events and all(isinstance(event["i"], int) for event in events)


def test_log_prose_renders_markdown_without_tool_lines(cli: CliRunner) -> None:
    """Log --prose shows agent markdown and filters tool events."""
    session_id = run_mock(cli)

    result = invoke(cli, "log", session_id, "--prose")

    assert "## Answer" in result.stdout and "tool" not in result.stdout


def test_the_log_footer_starts_on_a_fresh_line_after_unterminated_prose(cli: CliRunner) -> None:
    """Prose is verbatim message text, which need not end with a newline; the
    footer's compensating newline goes to stderr, keeping stdout clean."""
    session_id = run_mock(cli, "echo:prose without a newline")

    result = invoke(cli, "log", session_id, "--prose")

    assert result.stdout == "prose without a newline"
    assert result.stderr.startswith("\n-- ")


def test_log_quiet_suppresses_the_stderr_footer(cli: CliRunner) -> None:
    """Log --quiet suppresses its stderr footer."""
    session_id = run_mock(cli)

    result = invoke(cli, "log", session_id, "--quiet")

    assert result.stderr == ""


def test_log_max_output_truncates_between_events_and_names_the_transcript(cli: CliRunner) -> None:
    """Log --max-output stops at an event boundary and names transcript.ndjson."""
    meta = session_with_texts("event-0", "x" * 1000, "event-2")

    result = invoke(cli, "log", meta.session_id, "--since", "0", "--max-output", "300")

    assert (
        "event-0" in result.stdout
        and "event-2" not in result.stdout
        and "transcript.ndjson" in result.stdout
    )


def test_log_applies_tail_after_since(cli: CliRunner) -> None:
    """Log combines --since and --tail by tailing the post-cursor events."""
    meta = session_with_messages(5)

    result = invoke(cli, "log", meta.session_id, "--since", "1", "--tail", "2")

    assert (
        "event-1" not in result.stdout
        and "event-2" not in result.stdout
        and "event-4" in result.stdout
    )


def test_log_since_past_the_end_notes_highest_cursor(cli: CliRunner) -> None:
    """An explicit cursor past a finished transcript is noted, not rejected."""
    meta = session_with_messages(3)

    result = invoke(cli, "log", meta.session_id, "--since", "999")

    assert result.exit_code == vocab.EXIT_OK
    assert result.stdout == ""
    assert result.stderr.startswith(
        "-- --since 999 is past the transcript's end (highest cursor: 3)"
    )


def test_log_since_equal_to_the_end_is_silent(cli: CliRunner) -> None:
    """A caught-up cursor does not produce a past-the-end note."""
    meta = session_with_messages(3)

    result = invoke(cli, "log", meta.session_id, "--since", "3")

    assert result.exit_code == vocab.EXIT_OK
    assert result.stdout == ""
    assert "past the transcript's end" not in result.stderr


def test_log_since_below_the_end_stays_silent_and_renders_events(cli: CliRunner) -> None:
    """A cursor inside the transcript keeps the ordinary event view."""
    meta = session_with_messages(3)

    result = invoke(cli, "log", meta.session_id, "--since", "1")

    assert result.exit_code == vocab.EXIT_OK
    assert "event-2" in result.stdout
    assert "past the transcript's end" not in result.stderr


def test_log_since_on_an_empty_transcript_uses_zero_as_the_end(cli: CliRunner) -> None:
    """An empty transcript treats zero as caught up and one as too far."""
    meta = finished_session()
    transcript.Transcript(sessions.transcript_path(meta.session_id))

    caught_up = invoke(cli, "log", meta.session_id, "--since", "0")
    past_end = invoke(cli, "log", meta.session_id, "--since", "1")

    assert caught_up.exit_code == vocab.EXIT_OK
    assert "past the transcript's end" not in caught_up.stderr
    assert past_end.exit_code == vocab.EXIT_OK
    assert past_end.stdout == ""
    assert past_end.stderr.startswith(
        "-- --since 1 is past the transcript's end (highest cursor: 0)"
    )


def test_log_since_past_the_end_is_suppressed_by_quiet(cli: CliRunner) -> None:
    """Quiet suppresses the past-the-end note along with the footer."""
    meta = session_with_messages(3)

    result = invoke(cli, "log", meta.session_id, "--since", "999", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert result.stdout == ""
    assert result.stderr == ""


def test_finished_log_footer_carries_exit_code_and_answer_path(cli: CliRunner) -> None:
    """A finished log footer identifies the exit code and answer artifact."""
    session_id = run_mock(cli)

    result = invoke(cli, "log", session_id)

    assert (
        "exit 0" in result.stderr and f"answer: {sessions.answer_path(session_id)}" in result.stderr
    )


def test_running_log_footer_carries_the_event_coverage(cli: CliRunner) -> None:
    """A running log footer reports its transcript event count."""
    meta = running_session()
    transcript.Transcript(sessions.transcript_path(meta.session_id)).append(
        "msg", text="one running event"
    )

    result = invoke(cli, "log", meta.session_id)

    assert "-- running" in result.stderr and "events 1–1 of 1" in result.stderr


def test_log_rejects_prose_and_json_together(cli: CliRunner) -> None:
    """Log refuses the mutually exclusive prose and JSON views."""
    session_id = run_mock(cli)

    result = invoke(cli, "log", session_id, "--prose", "--json")

    assert result.exit_code == vocab.EXIT_USAGE


def test_log_rejects_a_negative_since_cursor(cli: CliRunner) -> None:
    """Log rejects a negative --since cursor as a usage error."""
    result = invoke(cli, "log", "does-not-exist", "--since", "-1")

    assert result.exit_code == vocab.EXIT_USAGE


def test_log_rejects_a_negative_tail_count(cli: CliRunner) -> None:
    """Log rejects a negative --tail count as a usage error."""
    result = invoke(cli, "log", "does-not-exist", "--tail", "-1")

    assert result.exit_code == vocab.EXIT_USAGE


def test_log_rejects_an_unknown_session(cli: CliRunner) -> None:
    """Log reports an unknown session as a usage error."""
    result = invoke(cli, "log", "does-not-exist")

    assert result.exit_code == vocab.EXIT_USAGE


def test_log_wait_new_timeout_prints_the_footer(cli: CliRunner) -> None:
    """SPEC --wait-new: the timeout exit still prints the footer — a bare 124
    with zero bytes is indistinguishable from a hang."""
    session_id = run_mock(cli)

    result = invoke(
        cli,
        "log",
        session_id,
        "--since",
        "999999",
        "--wait-new",
        "--timeout",
        "0",
    )

    assert result.exit_code == vocab.EXIT_TIMEOUT
    assert result.stdout == ""
    assert "-- done" in result.stderr
    assert "cursor:" in result.stderr


def test_wait_new_footer_reports_the_state_reached_during_the_wait(cli: CliRunner) -> None:
    """The footer is the caller's termination signal, so it must reflect the
    state after the wait, not the one the command started with."""
    meta = sessions.create_session(entry="mock", base_adapter="mock", prompt="finishing mid-wait")
    sessions.mark_running(meta.session_id, pid=os.getpid())
    transcript_file = transcript.Transcript(sessions.transcript_path(meta.session_id))

    def finish_session() -> None:
        time.sleep(0.05)
        sessions.transition(meta.session_id, "done", exit_code=0, stop_reason="end_turn")
        transcript_file.append("msg", text="the last word")

    writer = threading.Thread(target=finish_session)
    writer.start()
    try:
        result = invoke(cli, "log", meta.session_id, "--since", "0", "--wait-new", "--timeout", "2")
    finally:
        writer.join(timeout=2)

    assert "the last word" in result.stdout
    assert result.stderr.lstrip().startswith("-- done")


def test_log_rejects_timeout_without_wait_new(cli: CliRunner) -> None:
    """Log requires --wait-new when --timeout is supplied."""
    result = invoke(cli, "log", "does-not-exist", "--timeout", "0")

    assert result.exit_code == vocab.EXIT_USAGE


def test_wait_new_on_a_finished_session_returns_at_once(cli: CliRunner) -> None:
    """SPEC --wait-new, the `logs -f` convention: following a stopped stream
    ends — the full-timeout block would read as a hang."""
    session_id = run_mock(cli)

    started = time.monotonic()
    result = invoke(cli, "log", session_id, "--since", "999999", "--wait-new", "--timeout", "30")

    assert time.monotonic() - started < 5
    assert result.exit_code == vocab.EXIT_TIMEOUT
    assert "-- done" in result.stderr


def test_wait_new_notes_only_an_explicit_past_end_cursor(cli: CliRunner) -> None:
    """Wait-new keeps its 124 result while distinguishing an explicit overshoot."""
    meta = session_with_messages(3)

    explicit = invoke(
        cli,
        "log",
        meta.session_id,
        "--since",
        "999",
        "--wait-new",
        "--timeout",
        "30",
    )
    implicit = invoke(cli, "log", meta.session_id, "--wait-new", "--timeout", "30")

    assert explicit.exit_code == vocab.EXIT_TIMEOUT
    assert explicit.stdout == ""
    assert "-- --since 999 is past the transcript's end (highest cursor: 3)" in explicit.stderr
    assert implicit.exit_code == vocab.EXIT_TIMEOUT
    assert "past the transcript's end" not in implicit.stderr


def test_wait_new_timeout_on_a_running_session_says_it_still_runs(cli: CliRunner) -> None:
    """The 124 exit never touches the session; the stderr note says so and
    names the cancel verb."""
    meta = running_session("still going")

    result = invoke(cli, "log", meta.session_id, "--wait-new", "--timeout", "0.2")

    assert result.exit_code == vocab.EXIT_TIMEOUT
    assert "still running (gave up waiting after 0.2s)" in result.stderr
    assert f"acpc stop {meta.session_id} to cancel" in result.stderr


def test_wait_timeout_on_a_running_session_says_it_still_runs(cli: CliRunner) -> None:
    """The wait verb's timeout gets the same still-running note."""
    meta = running_session("still going")

    result = invoke(cli, "wait", meta.session_id, "--timeout", "0.1")

    assert result.exit_code == vocab.EXIT_TIMEOUT
    assert "still running (gave up waiting after 0.1s)" in result.stderr
    assert f"acpc stop {meta.session_id} to cancel" in result.stderr


def test_log_wait_new_returns_after_the_transcript_grows(cli: CliRunner) -> None:
    """A waiting log call wakes when a new event is appended."""
    meta = running_session("wait for activity")
    transcript_file = transcript.Transcript(sessions.transcript_path(meta.session_id))

    def append_event() -> None:
        time.sleep(0.01)
        transcript_file.append("msg", text="new event arrived")

    writer = threading.Thread(target=append_event)
    writer.start()
    try:
        result = invoke(
            cli,
            "log",
            meta.session_id,
            "--since",
            "0",
            "--wait-new",
            "--timeout",
            "1",
        )
    finally:
        writer.join(timeout=1)

    assert "new event arrived" in result.stdout


def test_failed_log_expands_the_last_agent_message(cli: CliRunner) -> None:
    """Failure views include the full final agent message for post-mortem use."""
    session_id = run_mock(cli, "please fail this", expected_exit=vocab.EXIT_AGENT_ERROR)

    result = invoke(cli, "log", session_id, "--since", "0")

    assert "outside what the mock adapter will attempt" in result.stdout


# ---------------------------------------------------------------------------
# `log --follow` (SPEC `log`): one bounded call in place of a polling loop.
# ---------------------------------------------------------------------------


def test_follow_replays_the_last_ten_events_by_default(cli: CliRunner) -> None:
    """SPEC --follow: the start point is a bounded replay, for orientation."""
    meta = session_with_messages(25)

    result = invoke(cli, "log", meta.session_id, "--follow")

    assert result.exit_code == vocab.EXIT_OK
    assert len(result.stdout.splitlines()) == 10
    assert '"event-15"' in result.stdout
    assert '"event-14"' not in result.stdout


def test_follow_tail_zero_replays_nothing(cli: CliRunner) -> None:
    """--tail 0 is the new-events-only start point."""
    meta = session_with_messages(5)

    result = invoke(cli, "log", meta.session_id, "--follow", "--tail", "0")

    assert result.exit_code == vocab.EXIT_OK
    assert result.stdout == ""


def test_follow_notes_only_an_explicit_past_end_cursor(cli: CliRunner) -> None:
    """Follow keeps its success result and only notes an explicit overshoot."""
    meta = session_with_messages(3)

    explicit = invoke(cli, "log", meta.session_id, "--follow", "--since", "999")
    implicit = invoke(cli, "log", meta.session_id, "--follow", "--tail", "0")

    assert explicit.exit_code == vocab.EXIT_OK
    assert explicit.stdout == ""
    assert "-- --since 999 is past the transcript's end (highest cursor: 3)" in explicit.stderr
    assert implicit.exit_code == vocab.EXIT_OK
    assert implicit.stdout == ""
    assert "past the transcript's end" not in implicit.stderr


def test_follow_since_resumes_without_a_replay(cli: CliRunner) -> None:
    """An explicit --since resumes exactly: the replay default never applies."""
    meta = session_with_messages(25)

    result = invoke(cli, "log", meta.session_id, "--follow", "--since", "20")

    assert result.exit_code == vocab.EXIT_OK
    assert len(result.stdout.splitlines()) == 5
    assert '"event-20"' in result.stdout
    assert '"event-19"' not in result.stdout


def test_follow_on_a_finished_session_returns_at_once(cli: CliRunner) -> None:
    """Following a stopped stream ends, and that ending is success — unlike
    --wait-new, whose question is 'has anything new happened'."""
    meta = session_with_messages(3)

    started = time.monotonic()
    result = invoke(cli, "log", meta.session_id, "--follow", "--timeout", "30")

    assert time.monotonic() - started < 5
    assert result.exit_code == vocab.EXIT_OK
    assert result.stderr.lstrip().startswith("-- done")


def test_follow_ends_when_the_session_finishes(cli: CliRunner) -> None:
    """The stream collects what arrives during the wait and ends on the
    session's end, with the state it reached."""
    meta = running_session("finishing under follow")
    transcript_file = transcript.Transcript(sessions.transcript_path(meta.session_id))

    def finish() -> None:
        time.sleep(0.05)
        transcript_file.append("msg", text="mid-follow event")
        time.sleep(0.05)
        transcript_file.append("msg", text="the last word")
        sessions.transition(meta.session_id, "done", exit_code=0, stop_reason="end_turn")

    writer = threading.Thread(target=finish)
    writer.start()
    try:
        result = invoke(cli, "log", meta.session_id, "--follow", "--timeout", "5")
    finally:
        writer.join(timeout=5)

    assert result.exit_code == vocab.EXIT_OK
    assert "mid-follow event" in result.stdout
    assert "the last word" in result.stdout
    assert result.stderr.lstrip().startswith("-- done")


def test_follow_timeout_exits_124_and_leaves_the_session_alone(cli: CliRunner) -> None:
    """The 124 exit never touches the session; the note says so, and the
    footer's cursor is what the caller resumes from."""
    meta = running_session("still going")

    result = invoke(cli, "log", meta.session_id, "--follow", "--timeout", "0.2")

    assert result.exit_code == vocab.EXIT_TIMEOUT
    assert "still running (gave up waiting after 0.2s)" in result.stderr
    assert f"acpc stop {meta.session_id} to cancel" in result.stderr
    assert "cursor:" in result.stderr
    assert sessions.load(meta.session_id).state == "running"


def test_follow_budget_exhaustion_exits_four_and_says_how_to_resume(cli: CliRunner) -> None:
    """SPEC exit codes: a cut stream is not a completed follow, so it gets its
    own code and a way back into the stream."""
    meta = session_with_messages(25)

    result = invoke(cli, "log", meta.session_id, "--follow", "--since", "0", "--max-output", "300")

    assert result.exit_code == vocab.EXIT_BUDGET
    assert "output truncated" in result.stdout
    assert "--max-output 300 exhausted" in result.stderr
    assert f"acpc log {meta.session_id} --follow --since" in result.stderr


def test_follow_cursor_covers_exactly_what_was_printed(cli: CliRunner) -> None:
    """A caller resuming at the footer's cursor sees no gap and no repeat."""
    meta = session_with_messages(25)

    cut = invoke(cli, "log", meta.session_id, "--follow", "--since", "0", "--max-output", "300")
    assert cut.exit_code == vocab.EXIT_BUDGET
    cursor = int(cut.stderr.rsplit("cursor:", 1)[1].strip())
    printed = [
        line
        for line in cut.stdout.splitlines()
        if line.startswith("[") and "output truncated" not in line
    ]
    assert f'"event-{cursor - 1}"' in printed[-1]

    rest = invoke(cli, "log", meta.session_id, "--follow", "--since", str(cursor))
    assert rest.exit_code == vocab.EXIT_OK
    assert f'"event-{cursor - 1}"' not in rest.stdout
    assert f'"event-{cursor}"' in rest.stdout


def test_follow_budget_spans_the_whole_stream(cli: CliRunner) -> None:
    """--max-output budgets the follow, not each page it happens to read.

    Events arrive slower than the poll interval, so every page here is one
    event and sits far inside the budget on its own: only a budget carried
    across pages can ever cut this stream.
    """
    meta = running_session("streaming")
    transcript_file = transcript.Transcript(sessions.transcript_path(meta.session_id))

    def write_events() -> None:
        for index in range(20):
            transcript_file.append("msg", text=f"chunk-{index}")
            time.sleep(0.08)

    writer = threading.Thread(target=write_events, daemon=True)
    writer.start()
    try:
        result = invoke(
            cli,
            "log",
            meta.session_id,
            "--follow",
            "--tail",
            "0",
            "--max-output",
            "200",
            "--timeout",
            "3",
        )
    finally:
        writer.join(timeout=5)

    assert result.exit_code == vocab.EXIT_BUDGET
    printed = [line for line in result.stdout.splitlines() if "chunk-" in line]
    assert 0 < len(printed) < 20


def test_follow_json_stays_valid_ndjson_under_the_budget(cli: CliRunner) -> None:
    """The cut appears as the typed `truncated` event, never a bare marker."""
    meta = session_with_messages(25)

    result = invoke(
        cli,
        "log",
        meta.session_id,
        "--follow",
        "--since",
        "0",
        "--json",
        "--max-output",
        "400",
    )

    assert result.exit_code == vocab.EXIT_BUDGET
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert all(isinstance(event, dict) for event in events)
    assert events[-1]["type"] == "truncated"


def test_follow_prose_renders_full_messages(cli: CliRunner) -> None:
    """--prose --follow is allowed, and renders the content view."""
    long_text = "x" * 400
    meta = session_with_texts(long_text)

    result = invoke(cli, "log", meta.session_id, "--follow", "--prose")

    assert result.exit_code == vocab.EXIT_OK
    assert long_text in result.stdout


def test_follow_and_wait_new_are_mutually_exclusive(cli: CliRunner) -> None:
    """One waiting mode per call."""
    meta = session_with_messages(1)

    result = invoke(cli, "log", meta.session_id, "--follow", "--wait-new")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "mutually exclusive" in result.stderr


def test_follow_accepts_the_short_flag(cli: CliRunner) -> None:
    """-f is a real flag on `log` now, not an alias hint."""
    meta = session_with_messages(3)

    result = invoke(cli, "log", meta.session_id, "-f")

    assert result.exit_code == vocab.EXIT_OK
    assert '"event-2"' in result.stdout


def test_follow_quiet_suppresses_the_footer(cli: CliRunner) -> None:
    """--quiet keeps stderr empty, as everywhere else."""
    meta = session_with_messages(3)

    result = invoke(cli, "log", meta.session_id, "--follow", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert result.stderr == ""
