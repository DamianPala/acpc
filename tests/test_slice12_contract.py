"""Direct behavioral coverage for slice 12's result-on-failure contract.

Every test here separates the two streams: the result document on stdout, the
structured error on stderr, and the exit code on its own.  A test that only
checked the exit code, or the merged output, would pass against exactly the
regression this slice removes.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from acpc import errors, sessions, transcript, vocab
from acpc.cli import main

MOCK_AGENT_SCRIPT = str(Path(__file__).with_name("mock_agent.py"))
MOCK_ENTRY = f'''
name = "Mock Agent"
command = "{sys.executable} {MOCK_AGENT_SCRIPT}"
install_command = "true"
home = "~/.mock"
home_env = "MOCK_HOME"

[modes]
default = {{ grants = "read", delegates = true }}
'''

RAW_INVOKE = CliRunner.invoke


@pytest.fixture(autouse=True)
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "state"
    agents = root / "agents"
    agents.mkdir(parents=True)
    (agents / "mock.toml").write_text(MOCK_ENTRY, encoding="utf-8")
    monkeypatch.setenv("ACPC_HOME", str(root))
    return root


@pytest.fixture
def cli() -> CliRunner:
    return CliRunner()


def invoke(cli: CliRunner, *args: str):
    return RAW_INVOKE(cli, main, list(args), catch_exceptions=False)


def document(result: Any) -> dict[str, Any]:
    """The result document on stdout, with the stderr envelope left alone."""
    return json.loads(result.stdout)


def envelope(result: Any) -> dict[str, Any]:
    """The structured error: the last non-empty stderr line."""
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert lines, result.stderr
    assert set(json.loads(lines[-1])) == {"error"}
    return json.loads(lines[-1])["error"]


def finished(state: str) -> str:
    meta = sessions.create_session(entry="mock", base_adapter="mock", prompt=f"fixture {state}")
    sessions.mark_running(meta.session_id, pid=os.getpid())
    sessions.transition(meta.session_id, state, exit_code=0)
    return meta.session_id


def running() -> str:
    meta = sessions.create_session(entry="mock", base_adapter="mock", prompt="fixture running")
    sessions.mark_running(meta.session_id, pid=os.getpid())
    return meta.session_id


def wait_for_alias(alias: str, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return sessions.resolve_selector(alias)
        except sessions.SessionError:
            time.sleep(0.02)
    pytest.fail(f"session alias {alias!r} was not created")


def wait_for_message(session_id: str, text: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = transcript.Transcript(sessions.transcript_path(session_id)).read().events
        if any(event.get("type") == "msg" and text in event.get("text", "") for event in events):
            return
        time.sleep(0.02)
    pytest.fail(f"transcript for {session_id} did not contain {text!r}")


def run_in_thread(target: Any) -> tuple[threading.Thread, dict[str, Any]]:
    holder: dict[str, Any] = {}

    def invoke_call() -> None:
        try:
            holder["result"] = target()
        except BaseException as error:  # noqa: BLE001 - re-raised in the test thread
            holder["error"] = error

    thread = threading.Thread(target=invoke_call, daemon=True)
    thread.start()
    return thread, holder


def test_a_failed_turn_returns_the_complete_result_and_the_error(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "fail this turn", "--json", "--quiet")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    payload = document(result)
    error = envelope(result)
    assert payload["status"] == "failed"
    assert payload["partial"] is False
    assert "Unable to complete" in payload["answer"]
    assert error["kind"] == "operation_failed"
    assert error["context"]["status"] == payload["status"]


def test_a_canceled_turn_returns_what_was_produced_as_partial(cli: CliRunner) -> None:
    result = invoke(
        cli,
        "run",
        "mock",
        "chunkslow:5 canceled",
        "--cancel-after",
        "0.5",
        "--json",
        "--quiet",
    )

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    payload = document(result)
    error = envelope(result)
    assert payload["status"] == "canceled"
    assert payload["partial"] is True
    assert payload["answer"] == "started"
    assert error["kind"] == "operation_failed"
    assert error["context"]["status"] == payload["status"]


def test_a_canceled_follow_up_returns_what_was_produced_as_partial(cli: CliRunner) -> None:
    base = invoke(cli, "run", "mock", "echo:base", "--json", "--quiet")
    session_id = document(base)["session_id"]

    result = invoke(
        cli,
        "continue",
        session_id,
        "chunkslow:5 canceled follow-up",
        "--cancel-after",
        "0.5",
        "--json",
        "--quiet",
    )

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    payload = document(result)
    error = envelope(result)
    assert payload["status"] == "canceled"
    assert payload["partial"] is True
    assert payload["answer"] == "started"
    assert error["kind"] == "operation_failed"
    assert error["context"]["status"] == payload["status"]


def test_an_expired_deadline_returns_no_result_even_with_a_partial_answer(
    cli: CliRunner, tmp_path: Path, live_daemon: None
) -> None:
    # The deadline now includes cold-daemon startup. Warm the target so this
    # test isolates the timeout contract after the turn is observed.
    warm = invoke(cli, "run", "mock", "echo:warm timeout target", "--json", "--quiet")
    assert warm.exit_code == vocab.EXIT_OK, warm.stderr
    release = tmp_path / "release-run"
    thread, holder = run_in_thread(
        lambda: invoke(
            cli,
            "run",
            "mock",
            f"chunkhold:{release}",
            "--name",
            "timeout-run",
            "--timeout",
            "0.5",
            "--json",
            "--quiet",
        )
    )
    session_id = wait_for_alias("timeout-run")
    wait_for_message(session_id, "holding")
    thread.join(timeout=10)
    release.touch()
    thread.join(timeout=30)
    assert not thread.is_alive(), holder
    assert "error" not in holder, holder
    result = holder["result"]

    assert result.exit_code == vocab.EXIT_TIMEOUT
    # V5a: no result document, even though "holding" was already recorded.
    assert result.stdout == ""
    error = envelope(result)
    assert error["kind"] == "timeout"
    assert error["retryable"] is False
    assert error["context"]["turn"] == 1
    assert error["context"]["status"] == "running"
    assert error["next"] == ["acpc", "status", session_id]
    # The session was neither canceled nor changed by the deadline.
    assert sessions.read_meta(session_id).is_active
    # V5a: `log --tail` reads the fragment the deadline did not return.
    log = invoke(cli, "log", session_id, "--tail", "20", "--json", "--quiet")
    assert "holding" in log.stdout


def test_continue_timeout_returns_no_result_even_with_a_partial_answer(
    cli: CliRunner, tmp_path: Path, live_daemon: None
) -> None:
    base = invoke(cli, "run", "mock", "echo:base", "--json", "--quiet")
    session_id = document(base)["session_id"]
    release = tmp_path / "release-continue"
    thread, holder = run_in_thread(
        lambda: invoke(
            cli,
            "continue",
            session_id,
            f"chunkhold:{release}",
            "--timeout",
            "0.5",
            "--json",
            "--quiet",
        )
    )
    wait_for_message(session_id, "holding")
    thread.join(timeout=10)
    release.touch()
    thread.join(timeout=30)
    assert not thread.is_alive(), holder
    assert "error" not in holder, holder
    result = holder["result"]
    assert result.exit_code == vocab.EXIT_TIMEOUT
    assert result.stdout == ""
    error = envelope(result)
    assert error["kind"] == "timeout"
    assert error["retryable"] is False
    assert error["context"]["turn"] == 2
    assert error["context"]["status"] == "running"


def test_wait_timeout_returns_no_result_even_with_a_partial_answer(cli: CliRunner) -> None:
    session_id = running()
    events = transcript.Transcript(sessions.transcript_path(session_id))
    events.append("state", **{"from": "starting", "to": "running"})
    events.append("msg", text="holding")

    result = invoke(cli, "wait", session_id, "--timeout", "0", "--json", "--quiet")

    assert result.exit_code == vocab.EXIT_TIMEOUT
    assert result.stdout == ""
    error = envelope(result)
    assert error["kind"] == "timeout"
    assert error["retryable"] is True
    assert error["context"]["turn"] == 1
    assert error["context"]["status"] == "running"
    assert "retry_after_ms" in error["context"]
    # `log --tail` reads the recorded fragment the deadline did not return.
    log = invoke(cli, "log", session_id, "--tail", "20", "--json", "--quiet")
    assert "holding" in log.stdout


def test_wait_reports_a_lost_turn_as_partial_and_matches_its_error(cli: CliRunner) -> None:
    session_id = sessions.create_session(
        entry="mock", base_adapter="mock", prompt="lost"
    ).session_id
    sessions.mark_running(session_id, pid=999999, process_start_time="gone")

    result = invoke(cli, "wait", session_id, "--json", "--quiet")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    payload = document(result)
    error = envelope(result)
    assert payload["status"] == "unknown"
    assert payload["partial"] is True
    assert error["kind"] == "operation_failed"
    assert error["context"]["status"] == payload["status"]


@pytest.mark.parametrize(
    ("state", "exit_code", "partial", "error_kind"),
    [
        ("succeeded", vocab.EXIT_OK, False, None),
        ("failed", vocab.EXIT_AGENT_ERROR, False, "operation_failed"),
        ("canceled", vocab.EXIT_CANCELLED, True, "operation_failed"),
        ("unknown", vocab.EXIT_AGENT_ERROR, True, "operation_failed"),
    ],
)
def test_wait_returns_every_terminal_state_it_observed(
    cli: CliRunner, state: str, exit_code: int, partial: bool, error_kind: str | None
) -> None:
    result = invoke(cli, "wait", finished(state), "--json", "--quiet")

    assert result.exit_code == exit_code
    payload = document(result)
    assert payload["status"] == state
    assert payload["partial"] is partial
    assert result.stdout
    if error_kind is None:
        assert result.stderr == ""
    else:
        error = envelope(result)
        assert error["kind"] == error_kind
        assert error["context"]["status"] == payload["status"]


def test_wait_started_after_rotation_picks_the_current_turn_not_the_old_one(
    cli: CliRunner,
) -> None:
    """M1g: a `wait` that starts after the session already rotated selects
    `meta.turns` as it stands then, never the turn that finished earlier."""
    session_id = finished("succeeded")
    sessions.write_answer(session_id, "first answer")
    sessions.rotate_turn(session_id)
    sessions.mark_running(session_id, pid=os.getpid())
    sessions.transition(session_id, "failed", exit_code=1)
    sessions.write_answer(session_id, "second answer")

    result = invoke(cli, "wait", session_id, "--json", "--quiet")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    payload = document(result)
    assert payload["turn"] == 2
    assert payload["status"] == "failed"
    assert payload["answer"] == "second answer"


def test_wait_pins_the_turn_selected_at_call_start(cli: CliRunner, live_daemon: None) -> None:
    """M1g: once `wait` has observed turn 1, a later `continue` opening turn 2
    does not change what a fresh `wait` on the running turn reports."""
    first = invoke(cli, "run", "mock", "slow:1", "--bg", "--json", "--quiet")
    assert first.exit_code == vocab.EXIT_OK, first.stderr
    session_id = document(first)["session_id"]

    finished_wait = invoke(cli, "wait", session_id, "--json", "--quiet")
    assert finished_wait.exit_code == vocab.EXIT_OK, finished_wait.stderr
    assert document(finished_wait)["turn"] == 1

    second = invoke(cli, "continue", session_id, "slow:6", "--bg", "--json", "--quiet")
    assert second.exit_code == vocab.EXIT_OK, second.stderr

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if sessions.read_meta(session_id).state == "running":
            break
        time.sleep(0.05)
    else:
        pytest.fail("second turn never started running")

    timed_out = invoke(cli, "wait", session_id, "--timeout", "1", "--json", "--quiet")

    assert timed_out.exit_code == vocab.EXIT_TIMEOUT
    assert timed_out.stdout == ""
    error = envelope(timed_out)
    assert error["kind"] == "timeout"
    assert error["context"]["turn"] == 2
    assert error["context"]["status"] == "running"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal semantics")
def test_wait_via_the_no_daemon_fallback_pins_the_turn_across_a_rotation(
    cli: CliRunner,
) -> None:
    """M1g/V5a via `_await_session`'s meta-only branch: no daemon runs in this
    test at all, so a background `wait` here polls `meta.json` directly. It
    still pins to turn 1 when this process rotates the session onward from
    under it, reading the rotated-past turn's parked outcome exactly as the
    daemon path's `stale` reply does."""
    session_id = running()

    wait_process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from acpc.cli import main; raise SystemExit(main())",
            "wait",
            session_id,
            "--json",
        ],
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    try:
        # Give the subprocess time to start Python, import the CLI and read
        # turn 1 as its selection before this process rotates the session
        # onward — a fresh interpreter's own startup cost dwarfs one
        # WAIT_POLL_INTERVAL cycle, so the margin has to cover that instead.
        time.sleep(2.0)

        # Frozen across the rotation: its next poll must see turn 1 already
        # parked, never the finished-but-not-yet-rotated record in between,
        # which is the current-turn case another test covers.
        wait_process.send_signal(signal.SIGSTOP)
        sessions.write_answer(session_id, "first answer")
        sessions.transition(session_id, "canceled", stop_reason="cancelled")
        sessions.rotate_turn(session_id)
        sessions.mark_running(session_id, pid=os.getpid())
        wait_process.send_signal(signal.SIGCONT)

        wait_stdout, wait_stderr = wait_process.communicate(timeout=15)
    finally:
        if wait_process.poll() is None:
            wait_process.kill()
            wait_process.wait(timeout=10)

    assert wait_process.returncode == vocab.EXIT_CANCELLED
    payload = json.loads(wait_stdout)
    assert payload["turn"] == 1
    assert payload["status"] == "canceled"
    assert payload["answer"] == "first answer"
    # SPEC `State on disk`: a parked-turn document names that turn's own
    # files, never the session's current ones (turn 2's, by now).
    assert payload["paths"]["answer"] == str(sessions.turn_path(session_id, "answer", 1))
    assert payload["paths"]["prompt"] == str(sessions.turn_path(session_id, "prompt", 1))
    error = json.loads(wait_stderr.splitlines()[-1])["error"]
    assert error["kind"] == "operation_failed"
    assert error["context"]["status"] == "canceled"
    assert sessions.read_meta(session_id).turns == 2


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal semantics")
def test_wait_via_the_no_daemon_fallback_names_the_parked_turns_file_when_truncated(
    cli: CliRunner,
) -> None:
    """Same rotation as above, but with `--max-output` small enough to force
    truncation: the marker and `output_file` must both name the parked
    turn's own `answer.1.md`, not the session's current `answer.md`."""
    session_id = running()

    wait_process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from acpc.cli import main; raise SystemExit(main())",
            "wait",
            session_id,
            "--json",
            "--max-output",
            "64",
        ],
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    try:
        time.sleep(2.0)

        wait_process.send_signal(signal.SIGSTOP)
        sessions.write_answer(session_id, "first answer " * 20)
        sessions.transition(session_id, "canceled", stop_reason="cancelled")
        sessions.rotate_turn(session_id)
        sessions.mark_running(session_id, pid=os.getpid())
        wait_process.send_signal(signal.SIGCONT)

        wait_stdout, wait_stderr = wait_process.communicate(timeout=15)
    finally:
        if wait_process.poll() is None:
            wait_process.kill()
            wait_process.wait(timeout=10)

    assert wait_process.returncode == vocab.EXIT_CANCELLED
    payload = json.loads(wait_stdout)
    assert payload["turn"] == 1
    assert payload["truncated"] is True
    parked_answer = str(sessions.turn_path(session_id, "answer", 1))
    assert payload["output_file"] == parked_answer
    assert payload["paths"]["answer"] == parked_answer
    assert payload["paths"]["prompt"] == str(sessions.turn_path(session_id, "prompt", 1))
    assert parked_answer in payload["answer"]
    assert wait_stderr


def test_wait_deadline_returns_no_result_for_the_running_session(cli: CliRunner) -> None:
    result = invoke(cli, "wait", running(), "--timeout", "0", "--json", "--quiet")

    assert result.exit_code == vocab.EXIT_TIMEOUT
    assert result.stdout == ""
    error = envelope(result)
    assert error["kind"] == "timeout"
    assert error["context"]["status"] == "running"


def test_a_call_that_observed_no_turn_writes_nothing_to_stdout(cli: CliRunner) -> None:
    cases = [
        (vocab.EXIT_AGENT_ERROR, errors.NOT_FOUND, ("run", "no-such-agent", "hello", "--json")),
        (
            vocab.EXIT_USAGE,
            errors.INVALID_INPUT,
            ("run", "mock", "hello", "--background", "--timeout", "1", "--json"),
        ),
        (
            vocab.EXIT_AGENT_ERROR,
            errors.AGENT_ERROR,
            ("run", "mock", "hello", "--background", "--json"),
        ),
        (vocab.EXIT_AGENT_ERROR, errors.NOT_FOUND, ("continue", "zzzz", "hello", "--json")),
        (vocab.EXIT_AGENT_ERROR, errors.NOT_FOUND, ("wait", "zzzz", "--json")),
    ]
    for exit_code, error_kind, args in cases:
        result = invoke(cli, *args, "--quiet")
        assert result.exit_code == exit_code, (args, result.stdout, result.stderr)
        assert result.stdout == "", args
        assert envelope(result)["kind"] == error_kind, args
    starting = sessions.create_session(entry="mock", base_adapter="mock", prompt="starting")
    result = invoke(cli, "wait", starting.session_id, "--timeout", "0", "--json", "--quiet")
    assert result.exit_code == vocab.EXIT_TIMEOUT
    assert result.stdout == ""
    error = envelope(result)
    assert error["kind"] == "timeout"
    assert error["context"]["status"] == "starting"
    assert error["context"]["turn"] == 1


def test_a_follow_up_deadline_before_its_turn_starts_returns_no_result(cli: CliRunner) -> None:
    base = invoke(cli, "run", "mock", "echo:base", "--json", "--quiet")
    session_id = json.loads(base.stdout)["session_id"]

    result = invoke(
        cli, "continue", session_id, "slow:5 follow-up", "--timeout", "0.001", "--json", "--quiet"
    )

    # The deadline expired before the follow-up rotated in: the session record
    # still describes the previous turn, which is not this call's result.
    assert result.exit_code == vocab.EXIT_TIMEOUT
    assert result.stdout == ""
    error = envelope(result)
    assert error["kind"] == "timeout"
    assert error["context"]["turn"] == 2
    # SPEC `continue`: the unobserved follow-up is `starting`, not the
    # previous turn's `succeeded`.
    assert error["context"]["status"] == "starting"
    assert sessions.read_meta(session_id).state == "succeeded"


def test_a_call_without_a_result_creates_no_output_file(cli: CliRunner, tmp_path: Path) -> None:
    base = invoke(cli, "run", "mock", "echo:base", "--json", "--quiet")
    base_session_id = json.loads(base.stdout)["session_id"]
    cases = [
        (
            "unknown.json",
            vocab.EXIT_AGENT_ERROR,
            errors.NOT_FOUND,
            ("run", "no-such-agent", "hello", "--json", "--quiet"),
        ),
        # A dispatch that never started a turn is no result either: the daemon
        # is unavailable here, so `--background` fails before the turn begins.
        (
            "undispatched.json",
            vocab.EXIT_AGENT_ERROR,
            errors.AGENT_ERROR,
            ("run", "mock", "hello", "--background", "--json", "--quiet"),
        ),
        # V5a: a client deadline never produces a result document either, so
        # `--output-file` gets nothing, the same as the empty stdout it mirrors
        # (`_emit_wait_timeout`'s deadline path, reached here because the
        # follow-up's own deadline expires before it rotates in).
        (
            "deadline.json",
            vocab.EXIT_TIMEOUT,
            errors.TIMEOUT,
            (
                "continue",
                base_session_id,
                "slow:5 follow-up",
                "--timeout",
                "0.001",
                "--json",
                "--quiet",
            ),
        ),
    ]
    for name, exit_code, error_kind, args in cases:
        target = tmp_path / name
        result = invoke(cli, *args, "--output-file", str(target))
        assert result.exit_code == exit_code, (args, result.stderr)
        assert result.stdout == "", args
        assert envelope(result)["kind"] == error_kind, args
        assert not target.exists(), args


def test_output_file_holds_the_result_of_a_failure_and_empties_stdout(
    cli: CliRunner, tmp_path: Path
) -> None:
    target = tmp_path / "failed.json"

    result = invoke(
        cli, "run", "mock", "fail this turn", "--json", "--quiet", "--output-file", str(target)
    )

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    assert result.stdout == ""
    error = envelope(result)
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["partial"] is False
    assert "Unable to complete" in payload["answer"]
    assert error["kind"] == "operation_failed"
    assert error["context"]["status"] == payload["status"]


def test_partial_true_always_exits_non_zero(cli: CliRunner) -> None:
    partial_calls = [
        (
            ("run", "mock", "chunkslow:5 partial", "--cancel-after", "0.5", "--json"),
            vocab.EXIT_AGENT_ERROR,
            "operation_failed",
        ),
        (("wait", finished("canceled"), "--json"), vocab.EXIT_CANCELLED, "operation_failed"),
        (("wait", finished("unknown"), "--json"), vocab.EXIT_AGENT_ERROR, "operation_failed"),
    ]
    for args, exit_code, error_kind in partial_calls:
        result = invoke(cli, *args, "--quiet")
        payload = document(result)
        assert payload["partial"] is True, args
        assert result.exit_code == exit_code, args
        assert result.stdout, args
        error = envelope(result)
        assert error["kind"] == error_kind, args
        assert error["context"]["status"] == payload["status"], args


def test_truncation_and_partial_are_separate_markers(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "huge", "--max-output", "512", "--json", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    payload = document(result)
    assert payload["truncated"] is True
    assert payload["partial"] is False
    assert payload["output_file"].endswith("answer.md")


def test_text_mode_prints_the_same_answer_the_document_carries(cli: CliRunner) -> None:
    text = invoke(cli, "run", "mock", "fail this turn", "--format", "text", "--quiet")
    machine = invoke(cli, "run", "mock", "fail this turn", "--json", "--quiet")

    assert text.exit_code == machine.exit_code == vocab.EXIT_AGENT_ERROR
    assert text.stdout
    assert machine.stdout
    assert text.stdout == document(machine)["answer"]
    assert envelope(text)["kind"] == envelope(machine)["kind"] == "operation_failed"


@pytest.mark.parametrize("name", ["run", "continue", "wait"])
def test_every_answer_command_declares_partial_and_its_emission_cases(
    cli: CliRunner, name: str
) -> None:
    detail = json.loads(invoke(cli, "schema", *name.split()).stdout)

    assert "partial" in detail["output"]["required"]
    assert detail["output"]["properties"]["partial"] == {"type": "boolean"}
    description = detail["output_description"]
    expected = {
        "run": (
            "Returns the answer result for a turn this call observed the end of — including a "
            "failed or canceled turn — and returns no result for a call that observed no turn, "
            "including one whose --timeout deadline expired or whose watch ended in a detach. "
            "`stop_reason`, `cost` and `answer` are present on every foreground result and "
            "omitted by `--background`."
        ),
        "continue": (
            "Returns the answer result for a turn this call observed the end of — including a "
            "failed or canceled turn — and returns no result for a call that observed no turn, "
            "including one whose --timeout deadline expired or whose watch ended in a detach. "
            "`stop_reason`, `cost` and `answer` are present on every foreground result and "
            "omitted by `--background`."
        ),
        "wait": (
            "Selects the session's current turn when the call starts and keeps observing that "
            "turn even if the session rotates to a newer one meanwhile; returns the answer "
            "result once that turn has ended — including a failed or canceled turn — and "
            "returns no result when a --timeout deadline expires first."
        ),
    }
    assert description == expected[name]
    enum = detail["output"]["properties"]["status"]["enum"]
    assert set(enum) <= set(vocab.SESSION_STATES)
    if name == "wait":
        # A client deadline no longer returns a document, so only the
        # terminal states remain reachable.
        assert set(enum) == {"succeeded", "failed", "canceled", "unknown"}
    assert {"succeeded", "failed", "canceled", "unknown"} <= set(enum)


@pytest.mark.parametrize("name", ["run", "continue", "wait"])
def test_answer_command_help_mentions_observed_result(cli: CliRunner, name: str) -> None:
    result = invoke(cli, name, "--help")

    assert result.exit_code == vocab.EXIT_OK
    assert "observed result" in result.stdout


@pytest.mark.parametrize("name", ["run", "continue", "steer", "wait"])
def test_timeout_flag_help_is_unbounded_with_no_result(cli: CliRunner, name: str) -> None:
    result = invoke(cli, name, "--help")
    flat = " ".join(result.stdout.split())

    assert result.exit_code == vocab.EXIT_OK
    assert "Unbounded by default" in flat
    assert "with no result" in flat
