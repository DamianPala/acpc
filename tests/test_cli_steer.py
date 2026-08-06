"""Behavioral tests for the ``steer`` verb: cancel, then redirect."""

import json
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from acpc import daemon_client, sessions, vocab
from acpc.cli import STEER_PREAMBLE, main

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


def finished_mock_session(cli: CliRunner, prompt: str = "turn one") -> str:
    """Run the real mock once, leaving a continuable session on disk."""
    result = invoke(cli, "run", "mock", prompt, "--quiet", "--json")
    assert result.exit_code == vocab.EXIT_OK, result.stderr
    return json.loads(result.stdout)["session_id"]


PARTIAL_ANSWER = "what the callee had streamed before the cancel"


def mid_turn_session(cli: CliRunner, prompt: str = "turn one") -> str:
    """A real session with a turn open and in flight, built the way one is.

    Cancelling a genuine in-flight turn needs a daemon and a live adapter;
    everything `steer` decides after the cancel is the same either way, and
    this keeps those decisions free of a race against the mock's clock. The
    live-daemon test below covers the real interruption end to end.
    """
    session_id = finished_mock_session(cli, prompt)
    sessions.rotate_turn(session_id)
    sessions.write_prompt(session_id, "the instruction being interrupted")
    sessions.write_answer(session_id, PARTIAL_ANSWER)
    sessions.transition(session_id, "running", pid=None)
    assert sessions.load(session_id).state == "running"
    return session_id


def wait_for_running(session_id: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sessions.load(session_id).state == "running":
            return
        time.sleep(0.05)
    pytest.fail("background session never became running")


def test_steer_cancels_the_turn_and_runs_the_instruction(cli: CliRunner) -> None:
    """One verb: the session is cancelled, then the next turn carries the
    instruction — the caller never sees the session between the two."""
    session_id = mid_turn_session(cli)
    turns_before = sessions.read_meta(session_id).turns

    result = invoke(cli, "steer", session_id, "stop editing; diagnose only")

    assert result.exit_code == vocab.EXIT_OK, result.stderr
    assert "stop editing; diagnose only" in result.stdout
    assert sessions.load(session_id).state == "done"
    assert sessions.read_meta(session_id).turns == turns_before + 1


def test_steer_stores_the_wrapped_instruction_verbatim(cli: CliRunner) -> None:
    """SPEC steer: what was sent is what is on disk, preamble included."""
    session_id = mid_turn_session(cli)

    invoke(cli, "steer", session_id, "diagnose only")

    stored = sessions.prompt_path(session_id).read_text(encoding="utf-8")
    assert stored == f"{STEER_PREAMBLE}\n\ndiagnose only"
    assert stored.startswith("Your previous turn was interrupted by the operator;")


def test_steer_parks_the_interrupted_turns_answer(cli: CliRunner) -> None:
    """Nothing is lost: the interrupted turn's answer stays as its own file."""
    session_id = mid_turn_session(cli)
    interrupted_turn = sessions.read_meta(session_id).turns

    invoke(cli, "steer", session_id, "diagnose only")

    parked = sessions.turn_path(session_id, "answer", interrupted_turn)
    assert parked.read_text(encoding="utf-8") == PARTIAL_ANSWER


def test_steer_on_a_finished_session_names_continue(cli: CliRunner) -> None:
    """There is no turn to interrupt, and the error says which verb there is."""
    session_id = finished_mock_session(cli)

    result = invoke(cli, "steer", session_id, "diagnose only")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "there is no turn to interrupt" in result.stderr
    assert f"acpc continue {session_id}" in result.stderr


def test_steer_degrades_to_a_plain_continue_when_the_turn_finished_first(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SPEC steer: a turn that ended by itself before the cancel landed was
    never interrupted, so the preamble would lie — and the caller is told."""
    session_id = mid_turn_session(cli)

    async def finish_instead_of_cancelling(target: str, selector: str) -> bool:
        # Stands in for the daemon — another process — reporting a cancel that
        # reached a turn which had already ended on its own.
        sessions.transition(selector, "done", exit_code=0, stop_reason="end_turn")
        return True

    monkeypatch.setattr(daemon_client, "cancel_turn", finish_instead_of_cancelling)

    result = invoke(cli, "steer", session_id, "diagnose only")

    assert result.exit_code == vocab.EXIT_OK, result.stderr
    assert "finished on its own before the cancel landed" in result.stderr
    assert sessions.prompt_path(session_id).read_text(encoding="utf-8") == "diagnose only"


def test_steer_quiet_suppresses_the_stderr_lines(cli: CliRunner) -> None:
    """--quiet behaves as it does on continue: no summary, no early line."""
    session_id = mid_turn_session(cli)

    result = invoke(cli, "steer", session_id, "diagnose only", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert result.stderr == ""


def test_steer_reads_the_instruction_from_a_file(cli: CliRunner, tmp_path: Path) -> None:
    """The prompt sources are `run`'s, so a long instruction needs no quoting."""
    session_id = mid_turn_session(cli)
    instruction_file = tmp_path / "instruction.md"
    instruction_file.write_text("diagnose only, from a file", encoding="utf-8")

    result = invoke(cli, "steer", session_id, "--prompt-file", str(instruction_file))

    assert result.exit_code == vocab.EXIT_OK, result.stderr
    stored = sessions.prompt_path(session_id).read_text(encoding="utf-8")
    assert stored == f"{STEER_PREAMBLE}\n\ndiagnose only, from a file"


def test_steer_rejects_two_instruction_sources(cli: CliRunner, tmp_path: Path) -> None:
    """Exactly one source, as everywhere else."""
    session_id = mid_turn_session(cli)
    instruction_file = tmp_path / "instruction.md"
    instruction_file.write_text("from a file", encoding="utf-8")

    result = invoke(cli, "steer", session_id, "inline", "--prompt-file", str(instruction_file))

    assert result.exit_code == vocab.EXIT_USAGE
    assert "exactly one prompt source" in result.stderr


def test_steer_rejects_an_unknown_session(cli: CliRunner) -> None:
    result = invoke(cli, "steer", "does-not-exist", "diagnose only")

    assert result.exit_code == vocab.EXIT_USAGE


def test_steer_bg_returns_the_session_id(cli: CliRunner, live_daemon: None) -> None:
    """--bg is part of continue's output family, and steer inherits it."""
    session_id = mid_turn_session(cli)

    result = invoke(cli, "steer", session_id, "diagnose only", "--bg", "--json")

    assert result.exit_code == vocab.EXIT_OK, result.stderr
    assert json.loads(result.stdout)["session_id"] == session_id


def test_steer_interrupts_a_real_running_turn(cli: CliRunner, live_daemon: None) -> None:
    """The whole point, against a live adapter mid-turn: the in-flight work is
    cancelled, its partial answer is kept, and the redirect runs."""
    dispatched = invoke(cli, "run", "mock", "chunkslow:20 hold", "--bg", "--json")
    session_id = json.loads(dispatched.stdout)["session_id"]
    wait_for_running(session_id)

    result = invoke(cli, "steer", session_id, "stop editing; diagnose only")

    assert result.exit_code == vocab.EXIT_OK, result.stderr
    assert "stop editing; diagnose only" in result.stdout
    parked = sessions.turn_path(session_id, "answer", 1)
    assert "started" in parked.read_text(encoding="utf-8")
    assert sessions.prompt_path(session_id).read_text(encoding="utf-8").startswith(STEER_PREAMBLE)
