"""Behavioral tests for first-contact help and CLI hardening."""

import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from acpc import cli, vocab
from acpc.cli import main

MOCK_AGENT_SCRIPT = str(Path(__file__).with_name("mock_agent.py"))

MOCK_ENTRY = f'''
name = "Mock Agent"
command = "{sys.executable} {MOCK_AGENT_SCRIPT}"
install_command = "true"
home = "~/.mock"
home_env = "MOCK_HOME"
efforts = ["low", "medium", "high", "xhigh"]

[presets]
standard = {{ model = "mock-sonnet-5", effort = "high" }}
'''


@pytest.fixture(autouse=True)
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "state"
    agents = root / "agents"
    agents.mkdir(parents=True)
    (agents / "mock.toml").write_text(MOCK_ENTRY, encoding="utf-8")
    monkeypatch.setenv("ACPC_HOME", str(root))
    return root


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def invoke(runner: CliRunner, *args: str):
    return runner.invoke(main, list(args), catch_exceptions=False)


def test_root_help_is_a_compact_cheat_sheet(runner: CliRunner) -> None:
    result = invoke(runner, "--help")

    assert result.exit_code == vocab.EXIT_OK
    assert len(result.stdout.splitlines()) <= 100
    assert "--permissions write" in result.stdout
    assert "request_permission" in result.stdout


def test_short_help_matches_root_help(runner: CliRunner) -> None:
    long_help = invoke(runner, "--help")
    short_help = invoke(runner, "-h")

    assert short_help.stdout == long_help.stdout


def test_run_help_is_a_distinct_reference_page(runner: CliRunner) -> None:
    root_help = invoke(runner, "--help")
    run_help = invoke(runner, "run", "--help")

    assert run_help.stdout != root_help.stdout
    assert "--max-output" in run_help.stdout
    assert "Example" in run_help.stdout


def test_log_help_documents_activity_waiting(runner: CliRunner) -> None:
    result = invoke(runner, "log", "--help")

    assert "--wait-new" in result.stdout
    assert "Example" in result.stdout


@pytest.mark.parametrize("verb", ["stop", "rm", "install"])
def test_short_verbs_reuse_the_root_help_page(runner: CliRunner, verb: str) -> None:
    root_help = invoke(runner, "--help")
    command_help = invoke(runner, verb, "--help")

    assert command_help.stdout == root_help.stdout


def test_short_version_matches_long_version(runner: CliRunner) -> None:
    short_version = invoke(runner, "-V")
    long_version = invoke(runner, "--version")

    assert short_version.stdout == long_version.stdout
    assert "acpc" in short_version.stdout


def test_background_prompt_policy_is_rejected_on_a_tty(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_stdout_is_tty", lambda: True)

    result = invoke(runner, "run", "mock", "hello", "--permissions", "prompt", "--bg")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "--bg" in result.stderr


def test_fresh_nested_state_root_lists_no_sessions(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "nested" / "state"))

    result = invoke(runner, "status")

    assert result.exit_code == vocab.EXIT_OK


def test_last_selector_is_rejected_without_a_tty(runner: CliRunner) -> None:
    invoke(runner, "run", "mock", "echo:hello", "--quiet")

    result = invoke(runner, "status", "last")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "TTY" in result.stderr


def test_unknown_flag_is_a_usage_error(runner: CliRunner) -> None:
    result = invoke(runner, "status", "--bogus")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "--bogus" in result.stderr
    assert len(result.stderr.splitlines()) == 1
    assert "Traceback" not in result.stderr


def test_corrupt_meta_is_a_clean_cli_error(runner: CliRunner, state_root: Path) -> None:
    created = invoke(runner, "run", "mock", "echo:hello", "--quiet", "--json")
    session_id = json.loads(created.stdout)["session_id"]
    (state_root / "sessions" / session_id / "meta.json").write_text(
        "{not valid json", encoding="utf-8"
    )

    result = invoke(runner, "status", session_id)

    assert result.exit_code != vocab.EXIT_OK
    assert "meta.json" in result.stderr
    assert "Traceback" not in result.stderr


def test_truncated_transcript_tail_is_ignored_by_log(runner: CliRunner, state_root: Path) -> None:
    created = invoke(runner, "run", "mock", "echo:hello", "--quiet", "--json")
    session_id = json.loads(created.stdout)["session_id"]
    with (state_root / "sessions" / session_id / "transcript.ndjson").open("ab") as file:
        file.write(b'{"broken')

    result = invoke(runner, "log", session_id)

    assert result.exit_code == vocab.EXIT_OK
    assert "Traceback" not in result.stderr
