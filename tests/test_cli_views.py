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
bypass_modes = ["yolo"]
efforts = ["low", "medium", "high", "xhigh"]

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

    assert json.loads(result.stdout)["state"] == "done"


def test_status_json_without_an_id_returns_a_session_list(cli: CliRunner) -> None:
    """Status JSON without an id returns the list envelope."""
    meta = finished_session()

    result = invoke(cli, "status", "--json")

    assert json.loads(result.stdout)["sessions"][0]["session_id"] == meta.session_id


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


def test_log_defaults_to_the_last_twenty_events(cli: CliRunner) -> None:
    """A log without --since selects the last twenty events."""
    meta = session_with_messages(21)

    result = invoke(cli, "log", meta.session_id)

    assert "event-0" not in result.stdout and "event-20" in result.stdout


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


def test_finished_log_footer_carries_exit_code_and_answer_path(cli: CliRunner) -> None:
    """A finished log footer identifies the exit code and answer artifact."""
    session_id = run_mock(cli)

    result = invoke(cli, "log", session_id)

    assert (
        "exit 0" in result.stderr and f"answer: {sessions.answer_path(session_id)}" in result.stderr
    )


def test_running_log_footer_carries_the_event_count(cli: CliRunner) -> None:
    """A running log footer reports its transcript event count."""
    meta = running_session()
    transcript.Transcript(sessions.transcript_path(meta.session_id)).append(
        "msg", text="one running event"
    )

    result = invoke(cli, "log", meta.session_id)

    assert "-- running" in result.stderr and "1 events" in result.stderr


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
    assert result.stderr.lstrip().startswith("-- done")
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
    assert result.stderr.lstrip().startswith("-- done")


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
