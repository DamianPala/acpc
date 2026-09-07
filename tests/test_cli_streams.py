"""Each stream classified on its own: prompts, defaults, `NO_INPUT`, `last`.

Redirecting one stream must never reclassify another. Every test here moves
exactly the streams it is about and leaves the rest where the runner put them,
so a rule that quietly reads the wrong `isatty` shows up as a failure.
"""

import json
import os
import pty
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from click.testing import CliRunner

from acpc import interaction, sessions, vocab
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
yolo = {{ grants = "all", delegates = false }}
"""


@pytest.fixture(autouse=True)
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "state"
    agents = root / "agents"
    agents.mkdir(parents=True)
    (agents / "mock.toml").write_text(MOCK_ENTRY, encoding="utf-8")
    monkeypatch.setenv("ACPC_HOME", str(root))
    monkeypatch.delenv("NO_INPUT", raising=False)
    return root


@pytest.fixture
def cli() -> CliRunner:
    # Neither stream is a terminal under CliRunner; each test says which ones
    # it wants to be one.
    return CliRunner()


def invoke(cli: CliRunner, *args: str, stdin: str | None = None):
    return cli.invoke(main, list(args), input=stdin, catch_exceptions=False)


def error_envelope(result) -> dict:
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    return json.loads(lines[-1])["error"]


def streams(monkeypatch: pytest.MonkeyPatch, *, stdin: bool, stdout: bool) -> None:
    """Classify the two streams the permission rules read."""
    monkeypatch.setattr(interaction, "stdin_is_tty", lambda: stdin)
    monkeypatch.setattr(interaction, "stdout_is_tty", lambda: stdout)


def forbid_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn any question acpc puts to the caller into a loud failure."""

    def refuse(*args: object, **kwargs: object) -> str:
        raise AssertionError("acpc asked a question it was not entitled to ask")

    monkeypatch.setattr(interaction, "_ask_on_tty", refuse)


def resolved_policy(cli: CliRunner, *args: str) -> str:
    """The policy `run` settles on, read off the preview that starts nothing."""
    result = invoke(cli, "run", "mock", "probe", "--resolve", *args)
    assert result.exit_code == vocab.EXIT_OK, result.stderr
    for line in result.stdout.splitlines():
        if line.startswith("permissions"):
            return line.split()[1]
    raise AssertionError(f"no permissions line in:\n{result.stdout}")


# --- the default policy, one table row per test ------------------------------


def test_a_person_at_a_terminal_gets_ask(cli: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    streams(monkeypatch, stdin=True, stdout=True)

    assert resolved_policy(cli) == "ask"


def test_redirecting_stdout_lowers_the_default_to_read(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    streams(monkeypatch, stdin=True, stdout=False)

    assert resolved_policy(cli) == "read"


def test_json_lowers_the_default_to_read(cli: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    streams(monkeypatch, stdin=True, stdout=True)

    result = invoke(cli, "run", "mock", "probe", "--resolve", "--json")

    assert result.exit_code == vocab.EXIT_OK
    assert json.loads(result.stdout)["resolved"]["permissions"]["value"] == "read"


def test_a_prompt_arriving_on_stdin_lowers_the_default_to_read(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    streams(monkeypatch, stdin=False, stdout=True)

    assert resolved_policy(cli) == "read"


def test_an_agent_behind_a_shell_tool_gets_read(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    streams(monkeypatch, stdin=False, stdout=False)

    assert resolved_policy(cli) == "read"


def test_no_input_lowers_the_default_to_read(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    streams(monkeypatch, stdin=True, stdout=True)
    monkeypatch.setenv("NO_INPUT", "1")

    assert resolved_policy(cli) == "read"


def test_an_empty_no_input_changes_nothing(cli: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    streams(monkeypatch, stdin=True, stdout=True)
    monkeypatch.setenv("NO_INPUT", "")

    assert resolved_policy(cli) == "ask"


# --- explicit `ask`, the same rows -------------------------------------------


def test_explicit_ask_is_accepted_at_a_terminal(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    streams(monkeypatch, stdin=True, stdout=True)

    assert resolved_policy(cli, "--permissions", "ask") == "ask"


def test_explicit_ask_survives_a_redirected_stdout(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The heart of it: stdout says nothing about who can answer a question."""
    streams(monkeypatch, stdin=True, stdout=False)

    assert resolved_policy(cli, "--permissions", "ask") == "ask"


def test_explicit_ask_under_json_names_json(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    streams(monkeypatch, stdin=True, stdout=True)

    result = invoke(cli, "run", "mock", "probe", "--permissions", "ask", "--json")

    assert result.exit_code == vocab.EXIT_USAGE
    message = error_envelope(result)["message"]
    assert "--json" in message
    assert "terminal" not in message


def test_explicit_ask_without_a_terminal_on_stdin_names_the_terminal(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    streams(monkeypatch, stdin=False, stdout=True)

    result = invoke(cli, "run", "mock", "probe", "--permissions", "ask")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "needs a terminal to ask on" in result.stderr


def test_explicit_ask_under_no_input_names_no_input(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    streams(monkeypatch, stdin=True, stdout=True)
    monkeypatch.setenv("NO_INPUT", "1")

    result = invoke(cli, "run", "mock", "probe", "--permissions", "ask")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "NO_INPUT" in result.stderr


def test_explicit_ask_with_bg_names_bg(cli: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    streams(monkeypatch, stdin=True, stdout=True)

    result = invoke(cli, "run", "mock", "probe", "--permissions", "ask", "--bg")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "--bg" in result.stderr


# --- one stream never reclassifies another -----------------------------------


def test_a_terminal_on_stdin_does_not_change_the_output_format(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    streams(monkeypatch, stdin=True, stdout=False)

    result = invoke(cli, "run", "mock", "probe", "--resolve", "--json")

    assert result.exit_code == vocab.EXIT_OK
    assert json.loads(result.stdout)["entry"] == "mock"


# --- `--bg` asks for a policy instead of lowering one -------------------------


def answer_policy_prompt(monkeypatch: pytest.MonkeyPatch, answer: str | None) -> list[str]:
    """Stand in for the terminal and record what it was offered."""
    seen: list[str] = []

    def ask(question: str, *, choices: Sequence[str], default: str) -> str | None:
        seen.append(question)
        assert "ask" not in choices
        assert default == "read"
        return answer

    monkeypatch.setattr(interaction, "ask_choice", ask)
    return seen


def test_bg_at_a_terminal_asks_which_policy_to_detach_with(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    streams(monkeypatch, stdin=True, stdout=True)
    asked = answer_policy_prompt(monkeypatch, "edit")

    assert resolved_policy(cli, "--bg") == "edit"
    assert len(asked) == 1
    assert "--bg" in asked[0]


def test_the_policy_chosen_for_bg_reaches_the_session(
    cli: CliRunner, live_daemon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    streams(monkeypatch, stdin=True, stdout=True)
    answer_policy_prompt(monkeypatch, "edit")

    # No `--json` here: it would revoke the interactive context and with it
    # the prompt, so the session is found by the name it was dispatched under.
    result = invoke(cli, "run", "mock", "write-file:background.md", "--bg", "--name", "detached")

    assert result.exit_code == vocab.EXIT_OK
    session_id = sessions.resolve_selector("detached")
    stored = sessions.read_meta(session_id).resolution["resolved"]["permissions"]["value"]
    assert stored == "edit"


def test_bg_without_a_terminal_asks_nothing_and_defaults_to_read(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    streams(monkeypatch, stdin=False, stdout=False)
    forbid_prompt(monkeypatch)

    assert resolved_policy(cli, "--bg") == "read"


def test_an_unanswered_policy_prompt_fails_instead_of_defaulting(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    streams(monkeypatch, stdin=True, stdout=True)
    answer_policy_prompt(monkeypatch, None)

    result = invoke(cli, "run", "mock", "probe", "--bg", "--resolve")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "--permissions" in result.stderr


def _choose_under_a_pty(answer: str) -> tuple[str, str]:
    """Exercise the real `/dev/tty` path; the prompt ignores stdin by design."""
    child = (
        "import sys; from acpc.interaction import ask_choice as ask; "
        "print(ask('policy? ', choices=['read', 'edit'], default='read'))"
    )
    pid, master = pty.fork()
    if pid == 0:
        os.execv(sys.executable, [sys.executable, "-c", child])
    with os.fdopen(master, "r+b", buffering=0) as terminal:
        if answer:
            terminal.write(answer.encode())
        else:
            terminal.write(b"\x04")
        seen = b""
        try:
            while chunk := terminal.read(1024):
                seen += chunk
        except OSError:
            pass
    os.waitpid(pid, 0)
    return answer, seen.decode(errors="replace")


def test_an_answer_outside_the_offered_policies_is_not_a_choice() -> None:
    _, output = _choose_under_a_pty("all\n")

    assert "None" in output


def test_end_of_input_at_the_policy_prompt_is_not_a_choice() -> None:
    _, output = _choose_under_a_pty("")

    assert "None" in output


# --- the `last` selector ------------------------------------------------------


def start_session(cli: CliRunner) -> str:
    result = invoke(cli, "run", "mock", "probe", "--quiet", "--json")
    assert result.exit_code == vocab.EXIT_OK, result.stderr
    return json.loads(result.stdout)["session_id"]


def test_last_resolves_in_an_interactive_context(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = start_session(cli)
    streams(monkeypatch, stdin=True, stdout=True)

    result = invoke(cli, "status", "last")

    assert result.exit_code == vocab.EXIT_OK
    assert session_id in result.stdout


def test_last_survives_a_redirected_stdout(cli: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """A person collecting the output in a file is still a person."""
    session_id = start_session(cli)
    streams(monkeypatch, stdin=True, stdout=False)

    result = invoke(cli, "status", "last")

    assert result.exit_code == vocab.EXIT_OK
    assert session_id in result.stdout


def test_last_is_refused_without_a_terminal_on_stdin(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    start_session(cli)
    streams(monkeypatch, stdin=False, stdout=True)

    result = invoke(cli, "status", "last", "--json")

    assert result.exit_code == vocab.EXIT_USAGE
    failure = error_envelope(result)
    assert failure["kind"] == "invalid_input"
    assert "--name" in failure["message"]


def test_last_is_refused_under_json_at_a_terminal(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    start_session(cli)
    streams(monkeypatch, stdin=True, stdout=True)

    result = invoke(cli, "status", "last", "--json")

    assert result.exit_code == vocab.EXIT_USAGE
    assert error_envelope(result)["kind"] == "invalid_input"


def test_last_is_refused_under_no_input(cli: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    start_session(cli)
    streams(monkeypatch, stdin=True, stdout=True)
    monkeypatch.setenv("NO_INPUT", "1")

    result = invoke(cli, "status", "last")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "--name" in result.stderr
