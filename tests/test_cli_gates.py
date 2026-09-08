"""Behavioral tests for effect metadata, confirmation gates and the prompt limit.

Every gate here is exercised from a non-interactive context — `CliRunner` gives
the command a pipe for stdin — except the `install` prompt, which needs a real
controlling terminal and gets one through `pty.fork`.
"""

import json
import os
import pty
import re
import sys
from collections.abc import Iterator
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from acpc import cli as cli_module
from acpc import daemon_client, effects, proc, sessions, vocab
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
    "agents check": effects.READ_ONLY,
    "agents create": effects.NON_IDEMPOTENT,
    "agents delete": effects.NON_IDEMPOTENT,
    "agents get": effects.READ_ONLY,
    "agents list": effects.READ_ONLY,
    "continue": effects.NON_IDEMPOTENT,
    "daemon": effects.READ_ONLY,
    "daemon status": effects.READ_ONLY,
    "daemon stop": effects.IDEMPOTENT,
    "install": effects.NON_IDEMPOTENT,
    "log": effects.READ_ONLY,
    "probe": effects.READ_ONLY,
    "prune": effects.NON_IDEMPOTENT,
    "delete": effects.NON_IDEMPOTENT,
    "resolve": effects.READ_ONLY,
    "run": effects.NON_IDEMPOTENT,
    "schema": effects.READ_ONLY,
    "skills": effects.READ_ONLY,
    "skills get": effects.READ_ONLY,
    "skills list": effects.READ_ONLY,
    "status": effects.READ_ONLY,
    "steer": effects.NON_IDEMPOTENT,
    "cancel": effects.IDEMPOTENT,
    "wait": effects.READ_ONLY,
}


def test_every_command_in_the_tree_declares_one_effects_value() -> None:
    undeclared = [path or "acpc" for path, command in walk(main) if effects.of(command) is None]

    assert undeclared == []


def test_agent_and_skill_subcommands_declare_their_effects() -> None:
    assert effects.of(cli_module.agents_get_command) == effects.READ_ONLY
    assert effects.of(cli_module.skills_get_command) == effects.READ_ONLY


def test_each_command_declares_the_published_classification() -> None:
    assert {path: effects.of(command) for path, command in walk(main)} == DECLARED_EFFECTS


# --- delete -----------------------------------------------------------------


def test_delete_without_yes_stops_before_deleting_anything(cli: CliRunner) -> None:
    session_id = finished_session()

    result = invoke(cli, "delete", session_id)

    assert result.exit_code != vocab.EXIT_OK
    assert envelope(result)["kind"] == "confirmation_required"
    assert "--yes" in envelope(result)["hint"]
    assert sessions.session_dir(session_id).exists()


def test_delete_of_an_unknown_session_is_not_found_rather_than_a_gate(cli: CliRunner) -> None:
    result = invoke(cli, "delete", "does-not-exist")

    assert envelope(result)["kind"] == "not_found"


def test_delete_of_a_running_session_is_a_conflict_rather_than_a_gate(cli: CliRunner) -> None:
    session_id = running_session()

    result = invoke(cli, "delete", session_id)

    assert envelope(result)["kind"] == "conflict"
    assert sessions.session_dir(session_id).exists()


def test_delete_reports_that_it_changed_something(cli: CliRunner) -> None:
    session_id = finished_session()

    result = invoke(cli, "delete", session_id, "--yes", "--json")

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

    assert preview == {
        "targets": [session_id],
        "changed": False,
        "requires_confirmation": True,
    }
    assert real == {
        "targets": [session_id],
        "changed": True,
        "requires_confirmation": True,
    }
    assert not sessions.session_dir(session_id).exists()


def test_prune_refuses_an_unreadable_age_rather_than_asking_for_yes(cli: CliRunner) -> None:
    """The gate is last: no confirmation could make `nonsense` an age."""
    finished_session()

    result = invoke(cli, "prune", "--older-than", "nonsense")

    assert result.exit_code == vocab.EXIT_USAGE
    assert envelope(result)["kind"] == "invalid_input"


def test_prune_that_selects_nothing_reports_no_change(cli: CliRunner) -> None:
    finished_session()

    result = invoke(cli, "prune", "--older-than", "100d", "--yes", "--json")

    assert json.loads(result.stdout) == {
        "targets": [],
        "changed": False,
        "requires_confirmation": True,
    }


# --- daemon stop ------------------------------------------------------------


def test_bare_daemon_stop_without_yes_stops_before_touching_a_daemon(cli: CliRunner) -> None:
    result = invoke(cli, "daemon", "stop")

    assert result.exit_code != vocab.EXIT_OK
    assert envelope(result)["kind"] == "confirmation_required"
    assert "--yes" in envelope(result)["hint"]


def test_a_named_daemon_stop_needs_no_confirmation(cli: CliRunner) -> None:
    result = invoke(cli, "daemon", "stop", "mock", "--json")

    assert result.exit_code == vocab.EXIT_OK
    assert json.loads(result.stdout) == {
        "targets": [],
        "changed": False,
        "requires_confirmation": False,
    }


def test_bare_daemon_stop_dry_run_needs_no_yes(cli: CliRunner) -> None:
    result = invoke(cli, "daemon", "stop", "--dry-run", "--json")

    assert result.exit_code == vocab.EXIT_OK
    assert json.loads(result.stdout) == {
        "targets": [],
        "changed": False,
        "requires_confirmation": True,
    }


def _known_daemon_target(state_root: Path, name: str = "mock") -> str:
    """A target is known once its lock file exists; nothing answers on it."""
    (state_root / "daemon").mkdir(parents=True, exist_ok=True)
    (state_root / "daemon" / f"{name}.lock").write_text("", encoding="utf-8")
    return name


def test_a_daemon_stop_preview_opens_no_connection(
    cli: CliRunner, state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Greeting a daemon can stand it down, so a preview greets nothing."""
    target = _known_daemon_target(state_root)

    async def refuse(name: str) -> None:
        raise AssertionError(f"the preview reached out to {name}")

    monkeypatch.setattr(daemon_client, "connect", refuse)
    monkeypatch.setattr(daemon_client, "observe", refuse)

    result = invoke(cli, "daemon", "stop", "--dry-run", "--json")

    assert result.exit_code == vocab.EXIT_OK
    assert json.loads(result.stdout) == {
        "targets": [target],
        "changed": False,
        "requires_confirmation": True,
    }


def test_daemon_stop_preview_and_call_share_one_shape(cli: CliRunner, state_root: Path) -> None:
    target = _known_daemon_target(state_root)

    preview = json.loads(invoke(cli, "daemon", "stop", "--dry-run", "--json").stdout)
    real = json.loads(invoke(cli, "daemon", "stop", "--yes", "--json").stdout)

    assert preview.keys() == real.keys()
    assert preview == {
        "targets": [target],
        "changed": False,
        "requires_confirmation": True,
    }
    # Nothing answers on that socket, so the call reached nothing and changed
    # nothing — the preview still had to name what it would have addressed.
    assert real["changed"] is False


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


def _spelled_install_calls(text: str) -> list[str]:
    """Lines that spell out an `acpc install` call, as opposed to naming the command.

    A placeholder in angle brackets or a closing backtick right after the verb
    is prose about the command; anything else is a line someone can copy.
    """
    calls = []
    for line in text.splitlines():
        for match in re.finditer(r"acpc install\s*(\S*)", line):
            argument = match.group(1)
            if not argument or argument[0] in "<`\"'":
                continue
            calls.append(line.strip())
    return calls


def test_nothing_acpc_ships_tells_a_caller_to_install_without_yes() -> None:
    """D1: the tool's own hints and its shipped skills lead to calls that can succeed.

    Outside a terminal `acpc install <name>` fails `confirmation_required`, so
    a hint or a skill step without `--yes` sends its reader into a dead end.
    """
    package = Path(cli_module.__file__).parent
    offenders = [
        f"{path.relative_to(package)}: {line}"
        for path in sorted(package.rglob("*"))
        if path.is_file() and path.suffix in {".py", ".md", ".toml"}
        for line in _spelled_install_calls(path.read_text(encoding="utf-8"))
        if "--yes" not in line
    ]

    assert offenders == []


def _install_under_a_pty(answer: str | None) -> tuple[int, str, str]:
    """Run `install` in a child that owns a real controlling terminal.

    `pty.fork` is the only way to exercise the `/dev/tty` branch: the prompt
    deliberately ignores stdin.  `answer=None` writes nothing and closes the
    terminal, which is how a caller that never answers looks from inside.

    The child's stderr is a pipe rather than the terminal, so a failure comes
    back as the envelope a caller matches on while stdin stays a real
    terminal and the prompt still happens.  Returns the wait status, what the
    terminal saw, and what stderr carried.
    """
    child = "from acpc.cli import main; main(['install', 'mock'])"
    reading, writing = os.pipe()
    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - replaced by execv in the child
        os.dup2(writing, 2)
        os.execv(sys.executable, [sys.executable, "-c", child])
    os.close(writing)
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
    failure = b""
    while chunk := os.read(reading, 1024):
        failure += chunk
    os.close(reading)
    return (
        os.waitpid(pid, 0)[1],
        asked.decode(errors="replace"),
        failure.decode(errors="replace"),
    )


# forkpty warns about threads (xdist runs us multi-threaded); the child execs at once.
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_a_person_at_a_terminal_is_asked_and_can_agree(installer: Path) -> None:
    status, asked, _ = _install_under_a_pty("y\n")

    assert status == 0
    assert "Install mock?" in asked
    assert installer.read_text(encoding="utf-8") == "installed"


@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
@pytest.mark.parametrize(
    ("answer", "what"),
    [("n\n", "a refusal"), ("\x04", "end of input")],
)
def test_nothing_but_agreement_lets_the_install_through(
    installer: Path, answer: str | None, what: str
) -> None:
    """`\\x04` ends the read without closing the terminal: EOF, not an answer."""
    status, _, failure = _install_under_a_pty(answer)

    assert status != 0, what
    assert not installer.exists(), what
    # The same failure a missing `--yes` produces, not merely a non-zero exit.
    assert json.loads(failure.splitlines()[-1])["error"]["kind"] == "confirmation_required", what


@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_a_closed_terminal_installs_nothing(installer: Path) -> None:
    """A terminal that is gone before the child starts leaves nothing to report to.

    The kind cannot be asserted here: with the terminal closed the child does
    not live long enough to say anything.  What has to hold is that the
    installer never ran.
    """
    status, _, _ = _install_under_a_pty(None)

    assert status != 0
    assert not installer.exists()


@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_a_bare_enter_takes_the_offered_default(installer: Path) -> None:
    status, _, _ = _install_under_a_pty("\n")

    assert status == 0
    assert installer.exists()


# --- agents delete ----------------------------------------------------------


def test_agents_delete_removes_the_entry_agents_create_wrote(
    cli: CliRunner, state_root: Path
) -> None:
    invoke(cli, "agents", "create", "work", "--extends", "mock")
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

    assert envelope(result)["kind"] == "not_found"
    assert "ships" in envelope(result)["message"]


def test_agents_delete_takes_back_an_override_of_a_shipped_adapter(
    cli: CliRunner, state_root: Path
) -> None:
    invoke(cli, "agents", "create", "codex", "--extends", "mock")
    target = state_root / "agents" / "codex.toml"
    assert target.exists()

    result = invoke(cli, "agents", "delete", "codex", "--json")

    assert result.exit_code == vocab.EXIT_OK
    assert json.loads(result.stdout)["changed"] is True
    assert not target.exists()


def test_agents_delete_refuses_a_name_that_climbs_out_of_the_agents_directory(
    cli: CliRunner, state_root: Path
) -> None:
    victim = state_root / "config.toml"
    victim.write_text("keep me\n", encoding="utf-8")

    result = invoke(cli, "agents", "delete", "../config", "--json")

    assert result.exit_code == vocab.EXIT_USAGE
    assert envelope(result)["kind"] == "invalid_input"
    assert victim.exists()


def test_agents_delete_refuses_an_absolute_name(cli: CliRunner, tmp_path: Path) -> None:
    victim = tmp_path / "victim.toml"
    victim.write_text("keep me\n", encoding="utf-8")

    result = invoke(cli, "agents", "delete", str(tmp_path / "victim"))

    assert envelope(result)["kind"] == "invalid_input"
    assert victim.exists()


def test_agents_delete_refuses_a_link_that_points_out_of_the_directory(
    cli: CliRunner, state_root: Path, tmp_path: Path
) -> None:
    """The character check cannot see this one; resolving the path can."""
    victim = tmp_path / "victim.toml"
    victim.write_text("keep me\n", encoding="utf-8")
    (state_root / "agents").mkdir(parents=True, exist_ok=True)
    (state_root / "agents" / "escape.toml").symlink_to(victim)

    result = invoke(cli, "agents", "delete", "escape")

    assert envelope(result)["kind"] == "invalid_input"
    assert victim.exists()


def test_agents_create_refuses_a_name_that_climbs_out_of_the_agents_directory(
    cli: CliRunner, state_root: Path
) -> None:
    result = invoke(cli, "agents", "create", "../escapee", "--extends", "mock")

    assert envelope(result)["kind"] == "invalid_input"
    assert not (state_root / "escapee.toml").exists()


def test_agents_delete_of_an_unknown_entry_is_not_found(cli: CliRunner) -> None:
    result = invoke(cli, "agents", "delete", "never-existed")

    assert envelope(result)["kind"] == "not_found"


def test_agents_create_onto_an_existing_entry_is_a_conflict(cli: CliRunner) -> None:
    invoke(cli, "agents", "create", "work", "--extends", "mock")

    result = invoke(cli, "agents", "create", "work", "--extends", "mock")

    assert envelope(result)["kind"] == "conflict"


def test_agents_create_reports_that_it_changed_something(cli: CliRunner) -> None:
    result = invoke(cli, "agents", "create", "work", "--extends", "mock", "--json")

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
    # A capped read never measured the rest of the stream, so the size it
    # reports is a floor and the message says so instead of inventing one.
    assert "at least" in envelope(result)["message"]
    assert not (state_root / "sessions").exists()


@pytest.mark.skipif(not Path("/dev/zero").exists(), reason="needs an endless character device")
def test_a_prompt_file_that_never_ends_is_refused_without_buffering_it(
    cli: CliRunner, state_root: Path
) -> None:
    """`stat` reports zero bytes for a device, a FIFO and a `/proc` entry alike."""
    result = invoke(cli, "run", "mock", "--prompt-file", "/dev/zero")

    assert result.exit_code == vocab.EXIT_USAGE
    assert envelope(result)["kind"] == "invalid_input"
    assert "--prompt-file" in envelope(result)["message"]
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
    """The number has to be there; how Click lays the page out is its business."""
    help_text = " ".join(invoke(cli, "run", "--help").stdout.split())

    assert "--prompt-file" in help_text
    assert vocab.MAX_PROMPT_LABEL in help_text
    assert str(vocab.MAX_PROMPT_BYTES) in help_text


# --- resolve -----------------------------------------------------------------


def test_resolve_starts_neither_a_session_nor_a_daemon(cli: CliRunner, state_root: Path) -> None:
    result = invoke(cli, "resolve", "mock")

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
