"""Behavioral tests for the `run` verb: flags, usage errors, TTY rules."""

import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from acpc import sessions, vocab
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

PHANTOM_ENTRY = """
name = "Phantom Agent"
command = "definitely-not-installed-phantom-xyz"
install_command = "false"
"""


@pytest.fixture(autouse=True)
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "state"
    agents = root / "agents"
    agents.mkdir(parents=True)
    (agents / "mock.toml").write_text(MOCK_ENTRY, encoding="utf-8")
    (agents / "phantom.toml").write_text(PHANTOM_ENTRY, encoding="utf-8")
    monkeypatch.setenv("ACPC_HOME", str(root))
    return root


@pytest.fixture
def cli() -> CliRunner:
    # stdout is not a tty under CliRunner, which is the non-interactive branch
    # of every TTY rule below.
    return CliRunner()


def invoke(cli: CliRunner, *args: str, stdin: str | None = None):
    return cli.invoke(main, list(args), input=stdin, catch_exceptions=False)


# --- prompt sources ---------------------------------------------------------


def test_a_prompt_argument_runs_the_turn(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "echo:hello from the argument", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert "hello from the argument" in result.stdout


def test_a_dash_reads_the_prompt_from_stdin(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "-", "--quiet", stdin="echo:hello from stdin")

    assert result.exit_code == vocab.EXIT_OK
    assert "hello from stdin" in result.stdout


def test_prompt_file_reads_the_prompt_from_disk(cli: CliRunner, tmp_path: Path) -> None:
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("echo:hello from the file", encoding="utf-8")

    result = invoke(cli, "run", "mock", "--prompt-file", str(prompt_file), "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert "hello from the file" in result.stdout


def test_no_prompt_source_is_a_usage_error(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock")

    assert result.exit_code == vocab.EXIT_USAGE


def test_two_prompt_sources_are_a_usage_error(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "arg", "--prompt-file", "/dev/null")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "exactly one prompt source" in result.stderr


def test_an_unreadable_prompt_file_is_a_usage_error(cli: CliRunner, tmp_path: Path) -> None:
    result = invoke(cli, "run", "mock", "--prompt-file", str(tmp_path / "nope.txt"))

    assert result.exit_code == vocab.EXIT_USAGE
    assert "nope.txt" in result.stderr


# --- unknown agents and missing binaries ------------------------------------


def test_an_unknown_agent_is_a_usage_error(cli: CliRunner) -> None:
    result = invoke(cli, "run", "no-such-agent", "hello")

    assert result.exit_code == vocab.EXIT_USAGE


def test_a_missing_adapter_binary_exits_1_and_names_the_install(cli: CliRunner) -> None:
    result = invoke(cli, "run", "phantom", "hello")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    assert "acpc install phantom" in result.stderr


def test_a_missing_adapter_binary_creates_no_session(cli: CliRunner, state_root: Path) -> None:
    invoke(cli, "run", "phantom", "hello")

    sessions_dir = state_root / "sessions"
    assert not sessions_dir.exists() or not list(sessions_dir.iterdir())


# --- --dry-run --------------------------------------------------------------


def test_dry_run_shows_the_resolved_model_and_its_source(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "probe", "--dry-run")

    assert result.exit_code == vocab.EXIT_OK
    assert "mock-sonnet-5" in result.stdout
    assert "adapter default" in result.stdout


def test_dry_run_runs_nothing(cli: CliRunner, state_root: Path) -> None:
    invoke(cli, "run", "mock", "probe", "--dry-run")

    sessions_dir = state_root / "sessions"
    assert not sessions_dir.exists() or not list(sessions_dir.iterdir())


def test_dry_run_json_is_machine_readable(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "probe", "--dry-run", "--json")

    payload = json.loads(result.stdout)
    assert payload["entry"] == "mock"
    assert payload["resolved"]["model"]["value"] == "mock-sonnet-5"
    assert "entry_definition" not in payload


def test_dry_run_reports_the_permission_policy_it_would_use(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "probe", "--dry-run", "--json")

    # Non-interactive stdout, no --permissions: SPEC's default is `read`.
    assert json.loads(result.stdout)["resolved"]["permissions"]["value"] == "read"


def test_a_raw_model_id_on_the_call_is_labelled_as_a_call_flag(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "probe", "--model", "mock-opus-5", "--dry-run", "--json")

    resolved = json.loads(result.stdout)["resolved"]
    assert resolved["model"]["value"] == "mock-opus-5"
    assert resolved["model"]["source"] == "call flag"


def test_a_tier_name_resolves_through_the_entry_preset_that_defines_it(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "probe", "--model", "max", "--dry-run", "--json")

    resolved = json.loads(result.stdout)["resolved"]
    assert resolved["model"]["value"] == "mock-opus-5"
    # The tier is a call flag, but the value behind it comes from the entry.
    assert resolved["model"]["source"].startswith("entry")


# --- TTY rules --------------------------------------------------------------


def test_permissions_prompt_without_a_terminal_is_a_usage_error(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "probe", "--permissions", "prompt")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "terminal" in result.stderr


def test_an_unknown_permission_value_is_a_usage_error(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "probe", "--permissions", "sudo-everything")

    assert result.exit_code == vocab.EXIT_USAGE


# --- the bypass-mode guard --------------------------------------------------


def test_a_bypass_mode_is_rejected_unless_permissions_are_all(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "probe", "--mode", "yolo", "--permissions", "write")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "yolo" in result.stderr


def test_a_bypass_mode_is_accepted_with_permissions_all(cli: CliRunner) -> None:
    result = invoke(
        cli, "run", "mock", "settings", "--mode", "yolo", "--permissions", "all", "--quiet"
    )

    assert result.exit_code == vocab.EXIT_OK
    assert "/yolo/" in result.stdout


def test_a_non_bypass_mode_needs_no_special_permissions(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "settings", "--mode", "plan", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert "/plan/" in result.stdout


# --- output contract --------------------------------------------------------


def test_quiet_suppresses_the_stderr_summary(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "echo:quiet please", "--quiet")

    assert result.stderr == ""


def test_without_quiet_the_summary_is_exactly_one_stderr_line(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "echo:summarize me")

    summary_lines = [line for line in result.stderr.splitlines() if line.startswith("-- ")]
    assert len(summary_lines) == 1
    assert "exit 0" in summary_lines[0]


def test_the_direct_child_note_rides_the_one_summary_line(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "echo:route me")

    summary_lines = [line for line in result.stderr.splitlines() if line.startswith("-- ")]
    assert len(summary_lines) == 1
    assert "direct child" in summary_lines[0]


def test_output_file_receives_the_answer(cli: CliRunner, tmp_path: Path) -> None:
    target = tmp_path / "answer.md"

    result = invoke(cli, "run", "mock", "echo:written to a file", "-o", str(target), "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert "written to a file" in target.read_text(encoding="utf-8")


def test_json_output_carries_the_session_id_and_paths(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "echo:as json", "--json", "--quiet")

    payload = json.loads(result.stdout)
    assert payload["session_id"]
    assert payload["paths"]["answer"].endswith("answer.md")
    assert "as json" in payload["answer"]


def test_max_output_caps_stdout(cli: CliRunner) -> None:
    result = invoke(
        cli, "run", "mock", "trigger the huge scenario", "--quiet", "--max-output", "512"
    )

    assert len(result.stdout) < 4096


def test_max_output_zero_disables_the_cap(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "trigger the huge scenario", "--quiet", "--max-output", "0")

    assert len(result.stdout) > 131072


def test_a_negative_max_output_is_a_usage_error(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "probe", "--max-output", "-1")

    assert result.exit_code == vocab.EXIT_USAGE


# --- exit codes at the CLI boundary -----------------------------------------


def test_a_refusal_exits_1(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "please fail this on purpose", "--quiet")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR


def test_a_timeout_exits_124(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "slow:30 cli timeout probe", "--timeout", "1", "--quiet")

    assert result.exit_code == vocab.EXIT_TIMEOUT


# --- session naming ---------------------------------------------------------


def test_a_name_can_be_claimed_for_a_session(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "echo:named run", "--name", "my-run", "--json", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    session_id = json.loads(result.stdout)["session_id"]
    assert sessions.read_meta(session_id).name == "my-run"


def test_a_name_still_active_on_another_session_is_refused(cli: CliRunner) -> None:
    invoke(cli, "run", "mock", "echo:first", "--name", "taken", "--quiet")

    # The first holder finished, so the name rebinds with a warning, not an error.
    result = invoke(cli, "run", "mock", "echo:second", "--name", "taken", "--quiet")

    assert result.exit_code == vocab.EXIT_OK


def test_the_reserved_name_last_is_refused(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "echo:nope", "--name", "last", "--quiet")

    assert result.exit_code == vocab.EXIT_USAGE
