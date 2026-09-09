"""Direct behavioral coverage for slice 12's result-on-failure contract.

Every test here separates the two streams: the result document on stdout, the
structured error on stderr, and the exit code on its own.  A test that only
checked the exit code, or the merged output, would pass against exactly the
regression this slice removes.
"""

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from acpc import sessions, vocab
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


def test_an_expired_deadline_returns_the_observed_session_as_partial(cli: CliRunner) -> None:
    result = invoke(
        cli, "run", "mock", "slow:30 still running", "--timeout", "0.001", "--json", "--quiet"
    )

    assert result.exit_code == vocab.EXIT_TIMEOUT
    payload = document(result)
    error = envelope(result)
    assert payload["status"] in {"starting", "running"}
    assert payload["partial"] is True
    assert payload["answer"] == ""
    assert error["kind"] == "timeout"
    assert error["context"]["status"] == payload["status"]
    # The session was neither canceled nor changed by the deadline.
    assert sessions.read_meta(payload["session_id"]).is_active


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
    ("state", "exit_code", "partial"),
    [
        ("succeeded", vocab.EXIT_OK, False),
        ("failed", vocab.EXIT_AGENT_ERROR, False),
        ("canceled", vocab.EXIT_CANCELLED, True),
        ("unknown", vocab.EXIT_AGENT_ERROR, True),
    ],
)
def test_wait_returns_every_terminal_state_it_observed(
    cli: CliRunner, state: str, exit_code: int, partial: bool
) -> None:
    result = invoke(cli, "wait", finished(state), "--json", "--quiet")

    assert result.exit_code == exit_code
    payload = document(result)
    assert payload["status"] == state
    assert payload["partial"] is partial
    if exit_code != vocab.EXIT_OK:
        assert envelope(result)["context"]["status"] == payload["status"]


def test_wait_deadline_returns_the_running_session_as_partial(cli: CliRunner) -> None:
    result = invoke(cli, "wait", running(), "--timeout", "0", "--json", "--quiet")

    assert result.exit_code == vocab.EXIT_TIMEOUT
    payload = document(result)
    error = envelope(result)
    assert payload["status"] == "running"
    assert payload["partial"] is True
    assert error["kind"] == "timeout"
    assert error["context"]["status"] == payload["status"]


def test_a_call_that_observed_no_turn_writes_nothing_to_stdout(cli: CliRunner) -> None:
    cases = [
        (vocab.EXIT_AGENT_ERROR, ("run", "no-such-agent", "hello", "--json")),
        (vocab.EXIT_USAGE, ("run", "mock", "hello", "--background", "--timeout", "1", "--json")),
        (vocab.EXIT_AGENT_ERROR, ("run", "mock", "hello", "--background", "--json")),
        (vocab.EXIT_AGENT_ERROR, ("continue", "zzzz", "hello", "--json")),
        (vocab.EXIT_AGENT_ERROR, ("steer", "zzzz", "hello", "--json")),
        (vocab.EXIT_AGENT_ERROR, ("wait", "zzzz", "--json")),
    ]
    for exit_code, args in cases:
        result = invoke(cli, *args, "--quiet")
        assert result.exit_code == exit_code, (args, result.stdout, result.stderr)
        assert result.stdout == "", args
        assert envelope(result)["kind"], args


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
    assert envelope(result)["kind"] == "timeout"
    assert sessions.read_meta(session_id).state == "succeeded"


def test_a_call_without_a_result_creates_no_output_file(cli: CliRunner, tmp_path: Path) -> None:
    cases = [
        ("unknown.json", ("run", "no-such-agent", "hello", "--json", "--quiet")),
        # A dispatch that never started a turn is no result either: the daemon
        # is unavailable here, so `--background` fails before the turn begins.
        ("undispatched.json", ("run", "mock", "hello", "--background", "--json", "--quiet")),
    ]
    for name, args in cases:
        target = tmp_path / name
        result = invoke(cli, *args, "--output-file", str(target))
        assert result.exit_code == vocab.EXIT_AGENT_ERROR, (args, result.stderr)
        assert result.stdout == "", args
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
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["partial"] is False
    assert "Unable to complete" in payload["answer"]


def test_partial_true_always_exits_non_zero(cli: CliRunner) -> None:
    partial_calls = [
        ("run", "mock", "chunkslow:5 partial", "--cancel-after", "0.5", "--json"),
        ("run", "mock", "slow:30 partial", "--timeout", "0.001", "--json"),
        ("wait", finished("canceled"), "--json"),
        ("wait", finished("unknown"), "--json"),
    ]
    for args in partial_calls:
        result = invoke(cli, *args, "--quiet")
        payload = document(result)
        assert payload["partial"] is True, args
        assert result.exit_code != vocab.EXIT_OK, args


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
    assert text.stdout == document(machine)["answer"]
    assert envelope(text)["kind"] == envelope(machine)["kind"]


@pytest.mark.parametrize("name", ["run", "continue", "steer", "wait"])
def test_every_answer_command_declares_partial_and_its_emission_cases(
    cli: CliRunner, name: str
) -> None:
    detail = json.loads(invoke(cli, "schema", *name.split()).stdout)

    assert "partial" in detail["output"]["required"]
    assert detail["output"]["properties"]["partial"] == {"type": "boolean"}
    description = detail["output_description"]
    assert isinstance(description, str) and description
    enum = detail["output"]["properties"]["status"]["enum"]
    assert set(enum) <= set(vocab.SESSION_STATES)
    assert {"succeeded", "failed", "canceled", "unknown"} <= set(enum)
