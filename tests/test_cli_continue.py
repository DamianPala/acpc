"""Behavioral tests for the ``continue`` verb."""

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from acpc import daemon_client, runner, sessions, vocab
from acpc.cli import main
from acpc.registry import AgentRegistry

MOCK_AGENT_SCRIPT = str(Path(__file__).with_name("mock_agent.py"))
NO_LOAD_AGENT_SCRIPT = str(Path(__file__).with_name("no_load_session_agent.py"))

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
    root = tmp_path / "state"
    agents = root / "agents"
    agents.mkdir(parents=True)
    (agents / "mock.toml").write_text(MOCK_ENTRY, encoding="utf-8")
    monkeypatch.setenv("ACPC_HOME", str(root))
    return root


@pytest.fixture
def cli() -> CliRunner:
    return CliRunner()


def invoke(cli: CliRunner, *args: str, stdin: str | None = None):
    return cli.invoke(main, list(args), input=stdin, catch_exceptions=False)


def start_session(cli: CliRunner, prompt: str = "turn one") -> str:
    result = invoke(cli, "run", "mock", prompt, "--quiet", "--json")
    assert result.exit_code == vocab.EXIT_OK
    return json.loads(result.stdout)["session_id"]


def test_continue_rotates_the_previous_turn_artifacts(cli: CliRunner) -> None:
    session_id = start_session(cli, "turn one")

    result = invoke(cli, "continue", session_id, "turn two", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert sessions.turn_path(session_id, "prompt", 1).is_file()
    assert sessions.turn_path(session_id, "answer", 1).is_file()
    assert sessions.prompt_path(session_id).read_text(encoding="utf-8") == "turn two"
    assert sessions.answer_path(session_id).is_file()


def test_continue_uses_the_stored_resolution_after_entry_changes(
    cli: CliRunner, state_root: Path
) -> None:
    first = invoke(
        cli,
        "run",
        "mock",
        "settings",
        "--model",
        "max",
        "--permissions",
        "write",
        "--quiet",
        "--json",
    )
    session_id = json.loads(first.stdout)["session_id"]
    (state_root / "agents" / "mock.toml").write_text(
        'name = "Changed"\ncommand = "missing-after-first-turn"\n', encoding="utf-8"
    )

    result = invoke(cli, "continue", session_id, "settings", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert "mock-opus-5/xhigh" in result.stdout


def test_continue_preserves_the_stored_home_environment(cli: CliRunner) -> None:
    session_id = start_session(cli)

    result = invoke(cli, "continue", session_id, "env:MOCK_HOME", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert str(Path("~/.mock").expanduser()) in result.stdout


def test_continue_rejects_an_adapter_without_load_session(
    cli: CliRunner, state_root: Path, live_daemon: None
) -> None:
    (state_root / "agents" / "mock.toml").write_text(
        MOCK_ENTRY.replace(MOCK_AGENT_SCRIPT, NO_LOAD_AGENT_SCRIPT), encoding="utf-8"
    )
    session_id = start_session(cli)

    async def stop_daemon() -> None:
        connection = await daemon_client.connect(
            runner.call_target(AgentRegistry().resolve_call("mock"))
        )
        assert connection is not None
        try:
            await connection.stop()
        finally:
            await connection.close()

    asyncio.run(stop_daemon())

    result = invoke(cli, "continue", session_id, "turn two", "--quiet")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    assert "continue requires an adapter with the loadSession capability" in result.stderr


def test_continue_cold_resume_does_not_replay_adapter_history(cli: CliRunner) -> None:
    session_id = start_session(cli, "turn one of the conversation")

    result = invoke(cli, "continue", session_id, "turn two", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert "turn one of the conversation" not in result.stdout


def test_continue_by_name_uses_the_session_alias(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "turn one", "--name", "research", "--quiet", "--json")
    session_id = json.loads(result.stdout)["session_id"]

    continued = invoke(cli, "continue", "research", "turn two", "--quiet", "--json")

    assert continued.exit_code == vocab.EXIT_OK
    assert json.loads(continued.stdout)["session_id"] == session_id


def test_continue_rejects_run_only_permissions_with_the_rule(cli: CliRunner) -> None:
    session_id = start_session(cli)

    result = invoke(cli, "continue", session_id, "turn two", "--permissions", "write")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "continue reuses the session's permissions" in result.stderr
    assert "--permissions" in result.stderr


def test_continue_on_a_running_session_is_a_usage_error(cli: CliRunner, live_daemon: None) -> None:
    result = invoke(cli, "run", "mock", "chunkslow:5 hold", "--bg", "--json")
    session_id = json.loads(result.stdout)["session_id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if sessions.load(session_id).state == "running":
            break
        time.sleep(0.05)
    else:
        pytest.fail("background session did not become running")

    continued = invoke(cli, "continue", session_id, "turn two")

    assert continued.exit_code == vocab.EXIT_USAGE
    assert "running" in continued.stderr


def test_continue_preserves_the_global_transcript_cursor(cli: CliRunner) -> None:
    session_id = start_session(cli, "turn one")
    first_lines = sessions.transcript_path(session_id).read_text(encoding="utf-8").splitlines()
    first_last = json.loads(first_lines[-1])["i"]
    result = invoke(cli, "continue", session_id, "turn two", "--quiet")
    assert result.exit_code == vocab.EXIT_OK

    lines = sessions.transcript_path(session_id).read_text(encoding="utf-8").splitlines()
    indices = [json.loads(line)["i"] for line in lines[1:]]

    assert indices == list(range(1, len(indices) + 1))
    assert indices[-1] > first_last


def test_continue_keeps_the_run_prompt_source_rules(cli: CliRunner) -> None:
    session_id = start_session(cli)

    result = invoke(cli, "continue", session_id)

    assert result.exit_code == vocab.EXIT_USAGE
    assert "exactly one prompt source" in result.stderr


def test_continue_last_is_rejected_without_a_tty(cli: CliRunner) -> None:
    start_session(cli)

    result = invoke(cli, "continue", "last", "turn two")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "TTY" in result.stderr


def test_a_warm_continue_keeps_the_adapter_history(cli: CliRunner, live_daemon: None) -> None:
    session_id = start_session(cli, "turn one of the conversation")

    result = invoke(cli, "continue", session_id, "turn two", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert "turn one of the conversation" in result.stdout
