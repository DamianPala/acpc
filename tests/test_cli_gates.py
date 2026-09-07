"""Behavioral tests for effect metadata, confirmation gates and the prompt limit.

Every gate here is exercised from a non-interactive context — `CliRunner` gives
the command a pipe for stdin — except the `install` prompt, which needs a real
controlling terminal and gets one through `pty.fork`.
"""

import json
import os
import pty
import sys
from collections.abc import Iterator
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from acpc import cli as cli_module
from acpc import effects, proc, sessions, vocab
from acpc.cli import main

MOCK_AGENT_SCRIPT = str(Path(__file__).with_name("mock_agent.py"))

MOCK_HEAD = f"""
name = "Mock Agent"
command = "{sys.executable} {MOCK_AGENT_SCRIPT}"
home = "~/.mock"
home_env = "MOCK_HOME"
"""

MOCK_TABLES = """
[modes]
default = { grants = "read", delegates = true }

[presets]
standard = { model = "mock-sonnet-5", effort = "high" }
"""

MOCK_ENTRY = MOCK_HEAD + MOCK_TABLES

INSTALLER_SCRIPT = """
import pathlib
import sys

pathlib.Path(sys.argv[1]).write_text("installed", encoding="utf-8")
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
def installer(tmp_path: Path, state_root: Path) -> Path:
    """An entry whose installer leaves a file behind when it actually runs."""
    script = tmp_path / "installer.py"
    script.write_text(INSTALLER_SCRIPT, encoding="utf-8")
    marker = tmp_path / "installed.txt"
    entry = MOCK_HEAD + f'install_command = "{sys.executable} {script} {marker}"\n' + MOCK_TABLES
    (state_root / "agents" / "mock.toml").write_text(entry, encoding="utf-8")
    return marker


@pytest.fixture
def cli() -> CliRunner:
    return CliRunner()


def invoke(cli: CliRunner, *args: str):
    return cli.invoke(main, list(args), catch_exceptions=False)


def envelope(result) -> dict:
    return json.loads(result.stderr.strip().splitlines()[-1])["error"]


def finished_session() -> str:
    meta = sessions.create_session(entry="mock", base_adapter="mock", prompt="gate")
    sessions.mark_running(
        meta.session_id, pid=os.getpid(), process_start_time=proc.process_start_time()
    )
    sessions.transition(meta.session_id, "done", exit_code=0, stop_reason="test")
    return meta.session_id


def running_session(target: str | None = None) -> str:
    meta = sessions.create_session(entry="mock", base_adapter="mock", prompt="gate", target=target)
    sessions.mark_running(
        meta.session_id, pid=os.getpid(), process_start_time=proc.process_start_time()
    )
    return meta.session_id


# --- effect metadata --------------------------------------------------------


def walk(command: click.Command, path: str = "") -> Iterator[tuple[str, click.Command]]:
    yield path, command
    for name, sub in sorted(getattr(command, "commands", {}).items()):
        yield from walk(sub, f"{path} {name}".strip())


DECLARED_EFFECTS = {
    "": effects.READ_ONLY,
    "agents": effects.READ_ONLY,
    "agents delete": effects.NON_IDEMPOTENT,
    "agents init": effects.NON_IDEMPOTENT,
    "continue": effects.NON_IDEMPOTENT,
    "daemon": effects.READ_ONLY,
    "daemon status": effects.READ_ONLY,
    "daemon stop": effects.IDEMPOTENT,
    "install": effects.NON_IDEMPOTENT,
    "log": effects.READ_ONLY,
    "probe": effects.READ_ONLY,
    "prune": effects.NON_IDEMPOTENT,
    "rm": effects.NON_IDEMPOTENT,
    "run": effects.NON_IDEMPOTENT,
    "skills": effects.READ_ONLY,
    "status": effects.READ_ONLY,
    "steer": effects.NON_IDEMPOTENT,
    "stop": effects.IDEMPOTENT,
    "wait": effects.READ_ONLY,
}


def test_every_command_in_the_tree_declares_one_effects_value() -> None:
    undeclared = [path or "acpc" for path, command in walk(main) if effects.of(command) is None]

    assert undeclared == []


def test_the_named_views_declare_their_effects() -> None:
    assert effects.of(cli_module._agent_view_command) == effects.READ_ONLY
    assert effects.of(cli_module._skill_view_command) == effects.READ_ONLY


def test_each_command_declares_the_published_classification() -> None:
    assert {path: effects.of(command) for path, command in walk(main)} == DECLARED_EFFECTS


# --- rm ---------------------------------------------------------------------


def test_rm_without_yes_stops_before_deleting_anything(cli: CliRunner) -> None:
    session_id = finished_session()

    result = invoke(cli, "rm", session_id)

    assert result.exit_code != vocab.EXIT_OK
    assert envelope(result)["kind"] == "confirmation_required"
    assert "--yes" in envelope(result)["hint"]
    assert sessions.session_dir(session_id).exists()


def test_rm_of_an_unknown_session_is_not_found_rather_than_a_gate(cli: CliRunner) -> None:
    result = invoke(cli, "rm", "does-not-exist")

    assert envelope(result)["kind"] == "not_found"


def test_rm_of_a_running_session_is_a_conflict_rather_than_a_gate(cli: CliRunner) -> None:
    session_id = running_session()

    result = invoke(cli, "rm", session_id)

    assert envelope(result)["kind"] == "conflict"
    assert sessions.session_dir(session_id).exists()


def test_rm_reports_that_it_changed_something(cli: CliRunner) -> None:
    session_id = finished_session()

    result = invoke(cli, "rm", session_id, "--yes", "--json")

    assert result.exit_code == vocab.EXIT_OK
    assert json.loads(result.stdout)["changed"] is True


# --- prune ------------------------------------------------------------------


def test_prune_without_yes_stops_before_deleting_anything(cli: CliRunner) -> None:
    session_id = finished_session()

    result = invoke(cli, "prune", "--older-than", "0d")

    assert result.exit_code != vocab.EXIT_OK
    assert envelope(result)["kind"] == "confirmation_required"
    assert "--yes" in envelope(result)["hint"]
    assert sessions.session_dir(session_id).exists()


def test_prune_dry_run_needs_no_yes_and_accepts_one(cli: CliRunner) -> None:
    session_id = finished_session()

    without = invoke(cli, "prune", "--older-than", "0d", "--dry-run", "--json")
    with_flag = invoke(cli, "prune", "--older-than", "0d", "--dry-run", "--yes", "--json")

    assert without.exit_code == vocab.EXIT_OK
    assert with_flag.exit_code == vocab.EXIT_OK
    assert json.loads(without.stdout) == json.loads(with_flag.stdout)
    assert sessions.session_dir(session_id).exists()


def test_prune_preview_and_mutation_share_one_shape_and_differ_in_changed(
    cli: CliRunner,
) -> None:
    session_id = finished_session()

    preview = json.loads(invoke(cli, "prune", "--older-than", "0d", "--dry-run", "--json").stdout)
    real = json.loads(invoke(cli, "prune", "--older-than", "0d", "--yes", "--json").stdout)

    assert preview == {"targets": [session_id], "changed": False}
    assert real == {"targets": [session_id], "changed": True}
    assert not sessions.session_dir(session_id).exists()


def test_prune_that_selects_nothing_reports_no_change(cli: CliRunner) -> None:
    finished_session()

    result = invoke(cli, "prune", "--older-than", "100d", "--yes", "--json")

    assert json.loads(result.stdout) == {"targets": [], "changed": False}


# --- daemon stop ------------------------------------------------------------


def test_bare_daemon_stop_without_yes_stops_before_touching_a_daemon(cli: CliRunner) -> None:
    result = invoke(cli, "daemon", "stop")

    assert result.exit_code != vocab.EXIT_OK
    assert envelope(result)["kind"] == "confirmation_required"
    assert "--yes" in envelope(result)["hint"]


def test_a_named_daemon_stop_needs_no_confirmation(cli: CliRunner) -> None:
    result = invoke(cli, "daemon", "stop", "mock", "--json")

    assert result.exit_code == vocab.EXIT_OK
    assert json.loads(result.stdout) == {"targets": [], "changed": False}


def test_bare_daemon_stop_dry_run_needs_no_yes(cli: CliRunner) -> None:
    result = invoke(cli, "daemon", "stop", "--dry-run", "--json")

    assert result.exit_code == vocab.EXIT_OK
    assert json.loads(result.stdout) == {"targets": [], "changed": False}


def test_force_does_not_confirm_and_confirming_does_not_force(
    cli: CliRunner, state_root: Path
) -> None:
    """`--force` overrides a precondition; `--yes` answers a question."""
    forced = invoke(cli, "daemon", "stop", "--force")

    assert envelope(forced)["kind"] == "confirmation_required"

    # A target is known once its lock file exists; nothing answers on it.
    (state_root / "daemon").mkdir(parents=True, exist_ok=True)
    (state_root / "daemon" / "mock.lock").write_text("", encoding="utf-8")
    session_id = running_session("mock")
    confirmed = invoke(cli, "daemon", "stop", "mock", "--yes")

    assert envelope(confirmed)["kind"] == "precondition_failed"
    assert "--force" in envelope(confirmed)["hint"]
    assert sessions.load(session_id).state == "running"


# --- install ----------------------------------------------------------------


def test_install_without_yes_runs_no_installer(cli: CliRunner, installer: Path) -> None:
    result = invoke(cli, "install", "mock")

    assert result.exit_code != vocab.EXIT_OK
    assert envelope(result)["kind"] == "confirmation_required"
    assert "--yes" in envelope(result)["hint"]
    assert not installer.exists()


def test_install_with_yes_runs_the_installer(cli: CliRunner, installer: Path) -> None:
    result = invoke(cli, "install", "mock", "--yes")

    assert result.exit_code == vocab.EXIT_OK
    assert installer.read_text(encoding="utf-8") == "installed"


def test_install_of_an_unknown_agent_is_not_found_rather_than_a_gate(cli: CliRunner) -> None:
    result = invoke(cli, "install", "no-such-agent")

    assert envelope(result)["kind"] == "not_found"


def test_install_without_an_installer_is_not_supported_rather_than_a_gate(
    cli: CliRunner,
) -> None:
    """`mock` here declares no `install_command`, so no confirmation could help."""
    result = invoke(cli, "install", "mock")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    assert envelope(result)["kind"] == "not_supported"


def _install_under_a_pty(answer: str | None) -> tuple[int, str]:
    """Run `install` in a child that owns a real controlling terminal.

    `pty.fork` is the only way to exercise the `/dev/tty` branch: the prompt
    deliberately ignores stdin.  `answer=None` writes nothing and closes the
    terminal, which is how a caller that never answers looks from inside.
    """
    child = "from acpc.cli import main; main(['install', 'mock'])"
    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - replaced by execv in the child
        os.execv(sys.executable, [sys.executable, "-c", child])
    if answer is not None:
        os.write(fd, answer.encode())
    else:
        os.close(fd)
    asked = b""
    if answer is not None:
        try:
            while chunk := os.read(fd, 1024):
                asked += chunk
        except OSError:
            pass  # EIO is how a pty master reports the child closing its end
        finally:
            os.close(fd)
    return os.waitpid(pid, 0)[1], asked.decode(errors="replace")


# forkpty warns about threads (xdist runs us multi-threaded); the child execs at once.
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_a_person_at_a_terminal_is_asked_and_can_agree(installer: Path) -> None:
    status, asked = _install_under_a_pty("y\n")

    assert status == 0
    assert "Install mock?" in asked
    assert installer.read_text(encoding="utf-8") == "installed"


@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
@pytest.mark.parametrize(
    ("answer", "what"),
    [("n\n", "a refusal"), ("\x04", "end of input"), (None, "a closed terminal")],
)
def test_nothing_but_agreement_lets_the_install_through(
    installer: Path, answer: str | None, what: str
) -> None:
    """`\\x04` ends the read without closing the terminal: EOF, not an answer."""
    status, _ = _install_under_a_pty(answer)

    assert status != 0, what
    assert not installer.exists(), what


@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_a_bare_enter_takes_the_offered_default(installer: Path) -> None:
    status, _ = _install_under_a_pty("\n")

    assert status == 0
    assert installer.exists()


# --- agents delete ----------------------------------------------------------


def test_agents_delete_removes_the_entry_agents_init_wrote(
    cli: CliRunner, state_root: Path
) -> None:
    invoke(cli, "agents", "init", "work", "--extends", "mock")
    target = state_root / "agents" / "work.toml"
    assert target.exists()

    result = invoke(cli, "agents", "delete", "work", "--json")

    assert result.exit_code == vocab.EXIT_OK
    assert json.loads(result.stdout) == {
        "name": "work",
        "path": str(target),
        "changed": True,
    }
    assert not target.exists()


def test_agents_delete_refuses_an_adapter_acpc_ships(cli: CliRunner) -> None:
    result = invoke(cli, "agents", "delete", "codex")

    assert result.exit_code == vocab.EXIT_USAGE
    assert envelope(result)["kind"] == "invalid_input"
    assert "ships" in envelope(result)["message"]


def test_agents_delete_of_an_unknown_entry_is_not_found(cli: CliRunner) -> None:
    result = invoke(cli, "agents", "delete", "never-existed")

    assert envelope(result)["kind"] == "not_found"


def test_agents_init_onto_an_existing_entry_is_a_conflict(cli: CliRunner) -> None:
    invoke(cli, "agents", "init", "work", "--extends", "mock")

    result = invoke(cli, "agents", "init", "work", "--extends", "mock")

    assert envelope(result)["kind"] == "conflict"


def test_agents_init_reports_that_it_changed_something(cli: CliRunner) -> None:
    result = invoke(cli, "agents", "init", "work", "--extends", "mock", "--json")

    assert json.loads(result.stdout)["changed"] is True


# --- the prompt size limit --------------------------------------------------


def oversized() -> str:
    return "x" * (vocab.MAX_PROMPT_BYTES + 1)


def at_the_limit() -> str:
    return "x" * vocab.MAX_PROMPT_BYTES


def test_an_oversized_prompt_argument_creates_no_session(cli: CliRunner, state_root: Path) -> None:
    result = invoke(cli, "run", "mock", oversized())

    assert result.exit_code == vocab.EXIT_USAGE
    failure = envelope(result)
    assert failure["kind"] == "invalid_input"
    assert str(vocab.MAX_PROMPT_BYTES) in failure["message"]
    assert str(vocab.MAX_PROMPT_BYTES + 1) in failure["message"]
    assert not (state_root / "sessions").exists()


def test_an_oversized_prompt_on_stdin_creates_no_session(cli: CliRunner, state_root: Path) -> None:
    result = cli.invoke(main, ["run", "mock", "-"], input=oversized(), catch_exceptions=False)

    assert result.exit_code == vocab.EXIT_USAGE
    assert envelope(result)["kind"] == "invalid_input"
    assert "from -" in envelope(result)["message"]
    assert not (state_root / "sessions").exists()


def test_an_oversized_prompt_file_creates_no_session(
    cli: CliRunner, state_root: Path, tmp_path: Path
) -> None:
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text(oversized(), encoding="utf-8")

    result = invoke(cli, "run", "mock", "--prompt-file", str(prompt_file))

    assert result.exit_code == vocab.EXIT_USAGE
    assert envelope(result)["kind"] == "invalid_input"
    assert "--prompt-file" in envelope(result)["message"]
    assert not (state_root / "sessions").exists()


def test_a_prompt_exactly_at_the_limit_is_accepted(cli: CliRunner) -> None:
    """`continue` reads the prompt first, so this reaches the unknown session."""
    result = invoke(cli, "continue", "does-not-exist", at_the_limit())

    assert envelope(result)["kind"] == "not_found"


def test_the_limit_covers_steer_too(cli: CliRunner) -> None:
    result = invoke(cli, "steer", "does-not-exist", oversized())

    assert envelope(result)["kind"] == "invalid_input"


def test_the_prompt_limit_is_named_in_help(cli: CliRunner) -> None:
    help_text = " ".join(invoke(cli, "run", "--help").stdout.split())

    assert "--prompt-file FILE Read the prompt from a file; at most 1 MiB." in help_text
    assert "at most 1 MiB (1048576 bytes)" in help_text


# --- run --resolve ----------------------------------------------------------


def test_resolve_starts_neither_a_session_nor_a_daemon(cli: CliRunner, state_root: Path) -> None:
    result = invoke(cli, "run", "mock", "probe", "--resolve")

    assert result.exit_code == vocab.EXIT_OK
    assert not (state_root / "sessions").exists()
    assert not (state_root / "daemon").exists()


def test_dry_run_is_no_longer_a_run_flag(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "probe", "--dry-run")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "--dry-run" in envelope(result)["message"]


def test_continue_still_refuses_the_run_only_resolution_flag(cli: CliRunner) -> None:
    result = invoke(cli, "continue", "does-not-exist", "next", "--resolve")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "--resolve" in envelope(result)["message"]
