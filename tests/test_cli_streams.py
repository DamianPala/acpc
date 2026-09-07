"""Each stream classified on its own: prompts, defaults, `NO_INPUT`, `last`.

Redirecting one stream must never reclassify another. Every test here moves
exactly the streams it is about and leaves the rest where the runner put them,
so a rule that quietly reads the wrong `isatty` shows up as a failure.
"""

import json
import os
import pty
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from acpc import interaction, sessions, vocab
from acpc.cli import main

pytestmark = pytest.mark.filterwarnings(
    "ignore:This process .* is multi-threaded.*:DeprecationWarning"
)

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


def test_the_published_default_rule_is_the_rule_acpc_applies(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One assertion over both halves: the sentence acpc publishes, and the code.

    Two terminals are not enough on their own, and a descriptor that said so
    would send a person to `--json` expecting to approve edits and get a run
    that denied them without asking.
    """
    described = json.loads(invoke(cli, "schema", "run").stdout)
    permissions = next(flag for flag in described["flags"] if flag["name"] == "permissions")
    assert (
        "stdin and stdout both terminals, no --json, NO_INPUT unset" in permissions["description"]
    )

    streams(monkeypatch, stdin=True, stdout=True)
    machine = invoke(cli, "run", "mock", "probe", "--resolve", "--json")
    assert json.loads(machine.stdout)["resolved"]["permissions"]["value"] == "read"

    monkeypatch.setenv("NO_INPUT", "1")
    assert resolved_policy(cli) == "read"


# --- one stream never reclassifies another -----------------------------------


def test_a_terminal_on_stdin_does_not_change_the_output_format(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `--json` here: with it, the flag and not the classification decides."""
    streams(monkeypatch, stdin=True, stdout=False)
    with_a_terminal = invoke(cli, "run", "mock", "probe", "--resolve")

    streams(monkeypatch, stdin=False, stdout=False)
    without_one = invoke(cli, "run", "mock", "probe", "--resolve")

    assert with_a_terminal.exit_code == vocab.EXIT_OK
    assert without_one.exit_code == vocab.EXIT_OK
    assert with_a_terminal.stdout.startswith("entry")
    assert without_one.stdout.startswith("entry")


# --- `--bg` asks for a policy instead of lowering one -------------------------


def answer_on_the_terminal(monkeypatch: pytest.MonkeyPatch, reply: str | None) -> list[str]:
    """Stand in for `/dev/tty` itself; `None` is end of input, not an answer.

    Substituted at the boundary, so `ask_choice` stays under test: what it
    does with an answer outside the offered set, or with a bare Enter, is part
    of what these tests are checking.
    """
    seen: list[str] = []

    def ask(question: str) -> str | None:
        seen.append(question)
        return reply

    monkeypatch.setattr(interaction, "_ask_on_tty", ask)
    return seen


def no_terminal_to_ask_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """A platform with no `/dev/tty` to open — every Windows console."""

    def unavailable(question: str) -> str | None:
        raise interaction.TerminalUnavailable("no controlling terminal")

    monkeypatch.setattr(interaction, "_ask_on_tty", unavailable)


def dispatched_policy(cli: CliRunner, *args: str) -> tuple[str, str | None]:
    """Dispatch for real and read back the policy and its source from `meta.json`.

    `--bg` is what is under test, so the answer has to come out of the session
    it detached, not out of a preview that no longer resolves one.
    """
    result = invoke(cli, "run", "mock", "write-file:background.md", "--bg", "--name", "det", *args)
    assert result.exit_code == vocab.EXIT_OK, result.stderr
    stored = sessions.read_meta(sessions.resolve_selector("det")).resolution
    return stored["resolved"]["permissions"]["value"], stored.get("permissions_source")


def test_bg_at_a_terminal_asks_which_policy_to_detach_with(
    cli: CliRunner, live_daemon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No `--json`: it would revoke the interactive context and with it the
    # prompt, so the session is found by the name it was dispatched under.
    streams(monkeypatch, stdin=True, stdout=True)
    asked = answer_on_the_terminal(monkeypatch, "edit")

    assert dispatched_policy(cli)[0] == "edit"
    assert len(asked) == 1
    assert "--bg" in asked[0]


def test_a_policy_typed_at_the_prompt_is_not_recorded_as_a_default(
    cli: CliRunner, live_daemon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`default` would claim acpc picked it; a person did."""
    streams(monkeypatch, stdin=True, stdout=True)
    answer_on_the_terminal(monkeypatch, "edit")

    assert dispatched_policy(cli)[1] == "answered"


def test_bg_without_a_terminal_asks_nothing_and_defaults_to_read(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    streams(monkeypatch, stdin=False, stdout=False)
    forbid_prompt(monkeypatch)

    assert resolved_policy(cli, "--bg") == "read"


def test_bg_falls_back_to_read_when_no_terminal_can_be_opened(
    cli: CliRunner, live_daemon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both streams look like terminals, but the platform has none to open.

    The question was never put, so there is no silence to read as consent and
    nothing to refuse the call over: the policy takes its floor, and says so.
    """
    streams(monkeypatch, stdin=True, stdout=True)
    no_terminal_to_ask_on(monkeypatch)

    assert dispatched_policy(cli) == ("read", "default")


def test_an_unanswered_policy_prompt_fails_instead_of_defaulting(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The terminal opened and said nothing — that is a refusal, not `read`."""
    streams(monkeypatch, stdin=True, stdout=True)
    answer_on_the_terminal(monkeypatch, None)

    result = invoke(cli, "run", "mock", "probe", "--bg")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "--permissions" in result.stderr


def test_ask_is_not_on_offer_at_the_policy_prompt(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nobody stays attached to answer, so `ask` is not a choice `--bg` accepts."""
    streams(monkeypatch, stdin=True, stdout=True)
    asked = answer_on_the_terminal(monkeypatch, "ask")

    result = invoke(cli, "run", "mock", "probe", "--bg")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "ask" not in asked[0].split("[", 1)[1].split("]", 1)[0]


def test_bg_with_an_explicit_policy_asks_nothing(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    streams(monkeypatch, stdin=True, stdout=True)
    forbid_prompt(monkeypatch)

    assert resolved_policy(cli, "--bg", "--permissions", "execute") == "execute"


# --- `--resolve` previews the call and asks nobody anything -------------------


def test_resolve_under_bg_asks_nothing_and_reports_the_policy_as_unresolved(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A preview starts nothing, so it has no business holding a terminal."""
    streams(monkeypatch, stdin=True, stdout=True)
    forbid_prompt(monkeypatch)

    result = invoke(cli, "run", "mock", "probe", "--bg", "--resolve")

    assert result.exit_code == vocab.EXIT_OK, result.stderr
    assert "permissions  · (asked at dispatch)" in result.stdout


def test_two_previews_of_the_same_bg_call_agree(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A preview that asked would report whatever it was told, twice over."""
    streams(monkeypatch, stdin=True, stdout=True)
    answer_on_the_terminal(monkeypatch, "edit")

    first = invoke(cli, "run", "mock", "probe", "--bg", "--resolve")
    second = invoke(cli, "run", "mock", "probe", "--bg", "--resolve")

    assert first.exit_code == vocab.EXIT_OK
    assert first.stdout == second.stdout


def test_a_refused_call_is_refused_before_anyone_is_asked(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No point spending an answer on a call that cannot be dispatched."""
    streams(monkeypatch, stdin=True, stdout=True)
    forbid_prompt(monkeypatch)

    result = invoke(cli, "run", "mock", "--prompt-file", "no-such-file.md", "--bg")

    assert result.exit_code != vocab.EXIT_OK
    assert "no-such-file.md" in result.stderr


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
