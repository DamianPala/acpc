"""Behavioral tests for the ``steer`` verb: cancel, then redirect."""

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from acpc import daemon_client, runner, sessions, transcript, vocab
from acpc.cli import STEER_PREAMBLE, main
from acpc.permissions import select_mode
from acpc.registry import AgentRegistry

MOCK_AGENT_SCRIPT = str(Path(__file__).with_name("mock_agent.py"))

MOCK_ENTRY = f"""
name = "Mock Agent"
command = "{sys.executable} {MOCK_AGENT_SCRIPT}"
install_command = "true"
home = "~/.mock"
home_env = "MOCK_HOME"

[modes]
default = {{ grants = "read", delegates = true }}
plan = {{ grants = "read", delegates = true }}
yolo = {{ grants = "all", delegates = false }}

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
    meta = sessions.read_meta(session_id)
    adapter_session_id = meta.adapter_session_id
    assert adapter_session_id is not None
    store_path = Path(os.environ["ACPC_HOME"]) / "mock-sessions.json"
    store = json.loads(store_path.read_text(encoding="utf-8"))
    store[adapter_session_id]["history"].append("the instruction being interrupted")
    store_path.write_text(json.dumps(store), encoding="utf-8")
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


def wait_for_prompt_in_flight(session_id: str, timeout: float = 30.0) -> None:
    """Wait until the adapter has the turn's prompt, which is what in-place needs.

    `running` alone is not enough: the daemon writes it when it claims the
    turn, before the adapter has been told anything, and a steer sent in that
    window has no prompt to reach.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        meta = sessions.read_meta(session_id)
        delivered = meta.extra.get("delivered_prompts")
        if (
            meta.state == "running"
            and isinstance(delivered, list)
            and any(
                isinstance(entry, dict) and entry.get("turn") == meta.turns for entry in delivered
            )
        ):
            return
        time.sleep(0.05)
    pytest.fail("the turn's prompt never reached the adapter")


def wait_for_log_line(path: Path, needle: str, timeout: float = 10.0) -> None:
    """Wait for one line the mock writes outside the protocol."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            lines = path.read_text(encoding="utf-8").splitlines()
            if any(needle in line for line in lines):
                return
        time.sleep(0.02)
    pytest.fail(f"{path} never recorded {needle!r}")


def running_daemon_turn(cli: CliRunner, prompt: str) -> str:
    """A daemon-owned turn whose prompt has reached the adapter."""
    dispatched = invoke(cli, "run", "mock", prompt, "--bg", "--json")
    assert dispatched.exit_code == vocab.EXIT_OK, dispatched.stderr
    session_id = json.loads(dispatched.stdout)["session_id"]
    wait_for_prompt_in_flight(session_id)
    return session_id


def steer_error(result: Any) -> dict[str, Any]:
    return json.loads(result.stderr.splitlines()[-1])["error"]


def steer_events(session_id: str) -> list[dict[str, Any]]:
    events = transcript.Transcript(sessions.transcript_path(session_id)).read().events
    return [event for event in events if event.get("type") == "steer"]


def running_direct_session(prompt: str) -> tuple[str, threading.Thread]:
    """Start a real direct-child turn in this process, in flight.

    A direct session is the one route that can never steer in place — nothing
    in this process has a channel to the adapter it spawned — so it is built
    here rather than borrowed from a daemon.
    """
    resolution = AgentRegistry().resolve_call("mock", permissions="read")
    mode, mode_spec = select_mode(resolution.entry.modes, "read", resolution.mode)
    resolution = replace(resolution, mode=mode, mode_spec=mode_spec)
    meta = sessions.create_session(
        entry="mock",
        base_adapter="mock",
        prompt=prompt,
        resolution=runner.resolution_payload(resolution, cwd=None),
        target=runner.call_target(resolution),
    )
    request = runner.TurnRequest(resolution=resolution, prompt=prompt)
    thread = threading.Thread(
        target=lambda: runner.execute_turn(meta.session_id, request), daemon=True
    )
    thread.start()
    wait_for_prompt_in_flight(meta.session_id)
    return meta.session_id, thread


def test_steer_cancels_the_turn_and_runs_the_instruction(cli: CliRunner) -> None:
    """One verb: the session is cancelled, then the next turn carries the
    instruction — the caller never sees the session between the two."""
    session_id = mid_turn_session(cli)
    turns_before = sessions.read_meta(session_id).turns

    result = invoke(cli, "steer", session_id, "stop editing; diagnose only")

    assert result.exit_code == vocab.EXIT_OK, result.stderr
    assert "stop editing; diagnose only" in result.stdout
    assert sessions.load(session_id).state == "succeeded"
    assert sessions.read_meta(session_id).turns == turns_before + 1


def test_steer_accepts_a_suffixed_timeout(cli: CliRunner) -> None:
    session_id = mid_turn_session(cli)

    result = invoke(
        cli,
        "steer",
        session_id,
        "diagnose only",
        "--timeout",
        "1m",
        "--quiet",
    )

    assert result.exit_code == vocab.EXIT_OK
    assert "Working through:" in result.stdout


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

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    envelope = json.loads(result.stderr.splitlines()[-1])["error"]
    assert envelope["kind"] == "conflict"
    assert "there is no turn to interrupt" in envelope["message"]
    assert envelope["hint"] == f"Run: acpc continue {session_id}"
    assert envelope["context"]["capabilities"] == {"steer_mode": "cancel-then-start"}


def test_steer_degrades_to_a_plain_continue_when_the_turn_finished_first(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SPEC steer: a turn that ended by itself before the cancel landed was
    never interrupted, so the preamble would lie — and the caller is told."""
    session_id = mid_turn_session(cli)

    async def finish_instead_of_cancelling(target: str, selector: str) -> dict[str, Any]:
        # Stands in for the daemon — another process — reporting a cancel that
        # reached a turn which had already ended on its own.
        sessions.transition(selector, "succeeded", exit_code=0, stop_reason="end_turn")
        return {"ok": True, "turn_token": sessions.read_meta(selector).turns}

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

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    assert json.loads(result.stderr)["error"]["kind"] == "not_found"


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


def test_steer_interleaved_with_an_in_flight_restore(
    cli: CliRunner, live_daemon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = finished_mock_session(cli, "turn one")
    resolution = runner.resolution_from_session(sessions.read_meta(session_id))

    async def stop_target() -> None:
        connection = await daemon_client.connect(runner.call_target(resolution))
        assert connection is not None
        try:
            await connection.stop()
        finally:
            await connection.close()

    asyncio.run(stop_target())
    restore_release = Path(os.environ["ACPC_HOME"]) / "restore-release"
    restore_ready = Path(os.environ["ACPC_HOME"]) / "restore-ready"
    event_file = Path(os.environ["ACPC_HOME"]) / "adapter-events.ndjson"
    monkeypatch.setenv("ACPC_MOCK_BLOCK_DURING_RESTORE", str(restore_release))
    monkeypatch.setenv("ACPC_MOCK_BLOCK_DURING_RESTORE_READY", str(restore_ready))
    monkeypatch.setenv("ACPC_MOCK_EVENT_FILE", str(event_file))
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from acpc.cli import main; raise SystemExit(main())",
            "continue",
            session_id,
            "restore must be cancelled",
            "--quiet",
        ],
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    steer_process: subprocess.Popen[str] | None = None
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not restore_ready.exists():
            time.sleep(0.02)
        if not restore_ready.exists():
            process.kill()
            process.wait(timeout=10)
            pytest.fail("resume never reached the deterministic restore barrier")

        steer_process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from acpc.cli import main; raise SystemExit(main())",
                "steer",
                session_id,
                "echo:plain steering",
            ],
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            meta = sessions.read_meta(session_id)
            if meta.turns == 3 and meta.state == "running":
                break
            time.sleep(0.02)
        else:
            steer_process.kill()
            steer_process.wait(timeout=10)
            pytest.fail("steer did not claim its follow-up turn")

        restore_release.touch()
        steer_stdout, steer_stderr = steer_process.communicate(timeout=10)
        process.wait(timeout=10)

        assert steer_process.returncode == vocab.EXIT_OK, steer_stderr
        assert "plain steering" in steer_stdout
        assert "nothing was interrupted" in steer_stderr
        assert STEER_PREAMBLE not in sessions.prompt_path(session_id).read_text(encoding="utf-8")
        assert sessions.prompt_path(session_id).read_text(encoding="utf-8") == "echo:plain steering"

        events = event_file.read_text(encoding="utf-8").splitlines()
        first_end = next(
            index for index, value in enumerate(events) if value.startswith("restore-end:")
        )
        second_start = next(
            index
            for index, value in enumerate(events[first_end + 1 :], first_end + 1)
            if value.startswith("restore-start:")
        )
        prompt = next(
            index
            for index, value in enumerate(events[second_start + 1 :], second_start + 1)
            if value.endswith(":echo:plain steering")
        )
        assert first_end < second_start < prompt
        assert process.returncode != vocab.EXIT_OK
    finally:
        if steer_process is not None and steer_process.poll() is None:
            steer_process.kill()
            steer_process.wait(timeout=10)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def test_steer_before_outgoing_prompt_frame_is_a_plain_follow_up(
    cli: CliRunner, live_daemon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = finished_mock_session(cli, "turn one")
    resolution = runner.resolution_from_session(sessions.read_meta(session_id))

    async def stop_target() -> None:
        connection = await daemon_client.connect(runner.call_target(resolution))
        assert connection is not None
        try:
            await connection.stop()
        finally:
            await connection.close()

    asyncio.run(stop_target())
    release = Path(os.environ["ACPC_HOME"]) / "prompt-options-release"
    ready = Path(os.environ["ACPC_HOME"]) / "prompt-options-ready"
    monkeypatch.setenv("ACPC_MOCK_BLOCK_BEFORE_PROMPT", str(release))
    monkeypatch.setenv("ACPC_MOCK_BLOCK_BEFORE_PROMPT_READY", str(ready))
    continue_process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from acpc.cli import main; raise SystemExit(main())",
            "continue",
            session_id,
            "turn to redirect",
            "--quiet",
        ],
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    steer_process: subprocess.Popen[str] | None = None
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not ready.exists():
            time.sleep(0.02)
        assert ready.exists()

        steer_process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from acpc.cli import main; raise SystemExit(main())",
                "steer",
                session_id,
                "echo:plain before frame",
            ],
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            meta = sessions.read_meta(session_id)
            if meta.turns == 3 and meta.state == "running":
                break
            time.sleep(0.02)
        else:
            pytest.fail("steer did not claim its plain follow-up")

        release.touch()
        steer_stdout, steer_stderr = steer_process.communicate(timeout=10)
        continue_process.wait(timeout=10)
        assert steer_process.returncode == vocab.EXIT_OK, steer_stderr
        assert "plain before frame" in steer_stdout
        assert "nothing was interrupted" in steer_stderr
        assert STEER_PREAMBLE not in sessions.prompt_path(session_id).read_text(encoding="utf-8")
        assert sessions.prompt_path(session_id).read_text(encoding="utf-8") == (
            "echo:plain before frame"
        )
    finally:
        if steer_process is not None and steer_process.poll() is None:
            steer_process.kill()
            steer_process.wait(timeout=10)
        if continue_process.poll() is None:
            continue_process.kill()
            continue_process.wait(timeout=10)


# --- in-place steering -------------------------------------------------------


def test_steer_in_place_keeps_the_turn_and_queues_the_instruction(
    cli: CliRunner, live_daemon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SPEC steer: the correction joins the turn in flight, so the turn keeps
    its number, its prompt file and its answer file, and the adapter shows it."""
    monkeypatch.setenv("ACPC_MOCK_STEERING", "1")
    session_id = running_daemon_turn(cli, "chunkslow:6 hold")

    result = invoke(cli, "steer", session_id, "X", "--bg", "--json")

    assert result.exit_code == vocab.EXIT_OK, result.stderr
    document = json.loads(result.stdout)
    assert document["status"] == "running"
    assert document["turn"] == 1
    assert document["correction_result"] == {
        "steer_mode": "in-place",
        "target_turn": 1,
        "target_status": "running",
        "message_state": "accepted",
    }
    assert document["capabilities"] == {"steer_mode": "in-place"}
    assert "partial" not in document

    waited = invoke(cli, "wait", session_id, "--json")
    assert waited.exit_code == vocab.EXIT_OK, waited.stderr
    assert "steered: X" in json.loads(waited.stdout)["answer"]

    assert sessions.read_meta(session_id).turns == 1
    assert not sessions.turn_path(session_id, "prompt", 1).exists()
    assert not sessions.turn_path(session_id, "answer", 1).exists()
    events = steer_events(session_id)
    assert [(event["mode"], event["text"], event["outcome"]) for event in events] == [
        ("in-place", "X", "injected")
    ]


def test_steer_in_place_blocking_prints_the_observed_turns_answer(
    cli: CliRunner, live_daemon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPC_MOCK_STEERING", "1")
    session_id = running_daemon_turn(cli, "chunkslow:6 hold")

    result = invoke(cli, "steer", session_id, "X", "--json", "--quiet")

    assert result.exit_code == vocab.EXIT_OK, result.stderr
    document = json.loads(result.stdout)
    assert document["status"] == "succeeded"
    assert "steered: X" in document["answer"]
    assert document["turn"] == 1
    assert document["correction_result"]["target_status"] == "succeeded"
    assert document["correction_result"]["message_state"] == "accepted"


def test_two_in_place_corrections_arrive_in_the_order_they_were_sent(
    cli: CliRunner, live_daemon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPC_MOCK_STEERING", "1")
    session_id = running_daemon_turn(cli, "chunkslow:8 hold")

    first = invoke(cli, "steer", session_id, "X", "--bg", "--json")
    second = invoke(cli, "steer", session_id, "Y", "--bg", "--json")

    assert first.exit_code == vocab.EXIT_OK, first.stderr
    assert second.exit_code == vocab.EXIT_OK, second.stderr
    waited = invoke(cli, "wait", session_id, "--json")
    assert waited.exit_code == vocab.EXIT_OK, waited.stderr
    answer = json.loads(waited.stdout)["answer"]
    assert "steered: X" in answer and "steered: Y" in answer
    assert answer.index("steered: X") < answer.index("steered: Y")
    assert [event["outcome"] for event in steer_events(session_id)] == ["injected", "injected"]


def test_steer_in_place_preserves_the_turn_context_and_artifacts(
    cli: CliRunner, live_daemon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No second prompt is sent: the adapter's history shows one entry, the
    transcript one turn, and the dispatched prompt file is untouched."""
    monkeypatch.setenv("ACPC_MOCK_STEERING", "1")
    prompt = "chunkslow:6 hold"
    session_id = running_daemon_turn(cli, prompt)

    steered = invoke(cli, "steer", session_id, "X", "--bg", "--json")
    assert steered.exit_code == vocab.EXIT_OK, steered.stderr
    waited = invoke(cli, "wait", session_id, "--json")
    assert waited.exit_code == vocab.EXIT_OK, waited.stderr

    meta = sessions.read_meta(session_id)
    assert meta.turns == 1
    assert sessions.prompt_path(session_id).read_text(encoding="utf-8") == prompt
    answer = json.loads(waited.stdout)["answer"]
    started = answer.index("started")
    assert started < answer.index("steered: X") < answer.index("finished")
    store_path = Path(os.environ["ACPC_HOME"]) / "mock-sessions.json"
    store = json.loads(store_path.read_text(encoding="utf-8"))
    assert store[meta.adapter_session_id]["history"] == [prompt]


def test_steer_without_in_place_support_still_cancels_and_redirects(
    cli: CliRunner,
) -> None:
    """SPEC steer: no capability means the other mode, chosen without asking."""
    session_id = mid_turn_session(cli)
    target_turn = sessions.read_meta(session_id).turns

    result = invoke(cli, "steer", session_id, "diagnose only", "--json", "--quiet")

    assert result.exit_code == vocab.EXIT_OK, result.stderr
    document = json.loads(result.stdout)
    assert document["turn"] == target_turn + 1
    assert document["correction_result"] == {
        "steer_mode": "cancel-then-start",
        "target_turn": target_turn,
        "target_status": "canceled",
        "message_state": "accepted",
    }
    assert sessions.read_meta(session_id).turns == target_turn + 1
    assert sessions.prompt_path(session_id).read_text(encoding="utf-8").startswith(STEER_PREAMBLE)


def test_an_explicit_in_place_mode_on_a_session_without_it_changes_nothing(
    cli: CliRunner,
) -> None:
    session_id = mid_turn_session(cli)
    before = sessions.read_meta(session_id)

    result = invoke(cli, "steer", session_id, "diagnose only", "--steer-mode", "in-place")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    envelope = json.loads(result.stderr.splitlines()[-1])["error"]
    assert envelope["kind"] == "not_supported"
    assert envelope["context"]["capabilities"] == {"steer_mode": "cancel-then-start"}
    assert envelope["hint"] == (f"Run: acpc steer {session_id} ... --steer-mode cancel-then-start")
    meta = sessions.load(session_id)
    assert meta.turns == before.turns
    assert meta.state == "running"


def test_a_daemon_session_without_a_steering_adapter_shows_one_mode(
    cli: CliRunner, live_daemon: None
) -> None:
    """SPEC steer: the modes are published per session, and the daemon records
    what its adapter actually declared."""
    session_id = running_daemon_turn(cli, "chunkslow:6 plain")

    detail = json.loads(invoke(cli, "status", session_id, "--json").stdout)

    assert detail["capabilities"] == {"steer_mode": "cancel-then-start"}
    result = invoke(cli, "steer", session_id, "X", "--bg", "--json")
    assert result.exit_code == vocab.EXIT_OK, result.stderr
    assert json.loads(result.stdout)["correction_result"]["steer_mode"] == "cancel-then-start"


def test_a_direct_session_supports_only_cancel_then_start(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No channel reaches a direct child's process, so in-place is not offered."""
    session_id, thread = running_direct_session("chunkslow:6 direct steer")
    try:
        detail = json.loads(invoke(cli, "status", session_id, "--json").stdout)
        assert detail["capabilities"] == {"steer_mode": "cancel-then-start"}
        assert (
            "steer: cancel-then-start"
            in invoke(cli, "status", session_id, "--format", "text").stdout
        )

        result = invoke(cli, "steer", session_id, "X", "--steer-mode", "in-place")

        assert result.exit_code == vocab.EXIT_AGENT_ERROR
        envelope = steer_error(result)
        assert envelope["kind"] == "not_supported"
        assert envelope["context"]["capabilities"] == {"steer_mode": "cancel-then-start"}
        assert sessions.read_meta(session_id).turns == 1
    finally:
        thread.join(timeout=30)


def test_status_says_unknown_until_a_session_has_reached_an_adapter(
    cli: CliRunner,
) -> None:
    session_id = sessions.create_session(
        entry="mock", base_adapter="mock", prompt="fresh"
    ).session_id

    detail = json.loads(invoke(cli, "status", session_id, "--json").stdout)

    assert detail["capabilities"] == {"steer_mode": "cancel-then-start"}
    assert (
        "steer: cancel-then-start" in invoke(cli, "status", session_id, "--format", "text").stdout
    )


def test_an_adapter_that_starts_its_own_turn_is_reported_as_unknown(
    cli: CliRunner,
    live_daemon: None,
    monkeypatch: pytest.MonkeyPatch,
    state_root: Path,
) -> None:
    """SPEC steer: an adapter that starts a turn of its own is cancelled and
    never presented as an in-place correction."""
    monkeypatch.setenv("ACPC_MOCK_STEERING", "1")
    monkeypatch.setenv("ACPC_MOCK_STEERING_RACE", "1")
    steering_log = state_root / "steering.log"
    monkeypatch.setenv("ACPC_MOCK_STEERING_LOG", str(steering_log))
    session_id = running_daemon_turn(cli, "chunkslow:10 race")

    result = invoke(cli, "steer", session_id, "X", "--bg", "--json")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    envelope = steer_error(result)
    assert envelope["kind"] == "outcome_unknown"
    assert envelope["context"]["session_id"] == session_id
    assert envelope["context"]["correction_result"] == {
        "steer_mode": "in-place",
        "target_turn": 1,
        "target_status": "running",
        "message_state": "unknown",
    }
    assert [event["outcome"] for event in steer_events(session_id)] == ["startedNewTurn"]
    wait_for_log_line(steering_log, "cancel:")


def test_a_correction_the_adapter_refuses_to_place_is_not_delivered(
    cli: CliRunner,
    live_daemon: None,
    monkeypatch: pytest.MonkeyPatch,
    state_root: Path,
) -> None:
    """SPEC steer: `promptRequired` means the turn ended before the instruction
    arrived, and acpc reports exactly that instead of starting a new turn. It
    is the answer acpc asked for: every request carries `idleBehavior:
    promptRequired`, so the adapter never starts a turn of its own on purpose."""
    monkeypatch.setenv("ACPC_MOCK_STEERING", "1")
    monkeypatch.setenv("ACPC_MOCK_STEERING_FORCE", "promptRequired")
    steering_log = state_root / "steering.log"
    monkeypatch.setenv("ACPC_MOCK_STEERING_LOG", str(steering_log))
    session_id = running_daemon_turn(cli, "chunkslow:6 forced idle")

    result = invoke(cli, "steer", session_id, "X", "--bg", "--json")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    envelope = steer_error(result)
    assert envelope["kind"] == "conflict"
    assert envelope["context"]["correction_result"]["message_state"] == "not_delivered"
    assert [event["outcome"] for event in steer_events(session_id)] == ["promptRequired"]
    assert sessions.read_meta(session_id).turns == 1
    wait_for_log_line(steering_log, "idle:promptRequired")


def test_an_adapter_that_declares_steering_and_denies_it_is_not_supported(
    cli: CliRunner, live_daemon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPC_MOCK_STEERING", "declared-only")
    session_id = running_daemon_turn(cli, "chunkslow:6 declared")

    result = invoke(cli, "steer", session_id, "X", "--bg", "--json")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    envelope = steer_error(result)
    assert envelope["kind"] == "not_supported"
    assert envelope["context"]["correction_result"]["message_state"] == "not_delivered"
    assert sessions.read_meta(session_id).turns == 1


def test_a_blocking_in_place_steer_times_out_without_a_document(
    cli: CliRunner, live_daemon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SPEC steer: --timeout bounds this client's wait only, and emits nothing."""
    monkeypatch.setenv("ACPC_MOCK_STEERING", "1")
    session_id = running_daemon_turn(cli, "chunkslow:8 hold")

    result = invoke(cli, "steer", session_id, "X", "--json", "--timeout", "0.5")

    assert result.exit_code == vocab.EXIT_TIMEOUT
    assert result.stdout == ""
    envelope = steer_error(result)
    assert envelope["kind"] == "timeout"
    assert envelope["context"]["status"] == "running"
    assert envelope["context"]["correction_result"]["message_state"] == "accepted"
    assert sessions.read_meta(session_id).state == "running"


def test_cancel_after_needs_an_explicit_cancel_then_start(cli: CliRunner) -> None:
    """SPEC steer: the rule is static, so it holds on an untouched session too."""
    for args in (
        ("steer", "x7k2", "diagnose", "--cancel-after", "5"),
        ("steer", "x7k2", "diagnose", "--steer-mode", "in-place", "--cancel-after", "5"),
    ):
        result = invoke(cli, *args)

        assert result.exit_code == vocab.EXIT_USAGE
        assert "--steer-mode cancel-then-start" in result.stderr
        assert "Usage:" not in result.stdout


def test_in_place_on_a_session_with_no_prompt_in_flight_is_a_conflict(
    cli: CliRunner,
) -> None:
    """SPEC steer: a starting session has no turn for the adapter to reach."""
    starting = sessions.create_session(
        entry="mock", base_adapter="mock", prompt="fresh", target="mock~starting"
    ).session_id
    sessions.update_meta(starting, steer_mode="in-place")

    result = invoke(cli, "steer", starting, "X", "--steer-mode", "in-place")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    envelope = steer_error(result)
    assert envelope["kind"] == "conflict"
    assert "no prompt is in flight" in envelope["message"]
    assert f"Run: acpc wait {starting}" in envelope["hint"]
    assert sessions.read_meta(starting).turns == 1


def test_in_place_on_a_finished_session_names_continue(cli: CliRunner) -> None:
    """The answer is the same in both modes: there is no turn to correct."""
    session_id = finished_mock_session(cli)
    sessions.update_meta(session_id, steer_mode="in-place")

    result = invoke(cli, "steer", session_id, "X", "--steer-mode", "in-place")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    envelope = steer_error(result)
    assert envelope["kind"] == "conflict"
    assert envelope["hint"] == f"Run: acpc continue {session_id}"


def test_log_renders_a_steer_record_without_passing_escape_codes_through(
    cli: CliRunner,
) -> None:
    """SPEC `log`: a correction reads as its mode and its instruction, and an
    instruction is caller-controlled text, so it is escaped like any other."""
    session_id = sessions.create_session(
        entry="mock", base_adapter="mock", prompt="logged"
    ).session_id
    transcript.Transcript(sessions.transcript_path(session_id)).append(
        "steer", mode="in-place", text="stop \x1b[31mediting\x1b[0m now", outcome="injected"
    )

    result = invoke(cli, "log", session_id)

    assert result.exit_code == vocab.EXIT_OK, result.stderr
    assert "steer  in-place: stop ^[[31mediting^[[0m now" in result.stdout
    assert "\x1b" not in result.stdout


def test_an_unreachable_daemon_is_reported_as_nothing_delivered(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SPEC steer: a daemon that cannot be reached sent nothing, and the result
    says so rather than leaving the caller to guess."""
    session_id = mid_turn_session(cli)
    sessions.update_meta(session_id, steer_mode="in-place")

    async def gone(target: str, selector: str, text: str) -> daemon_client.DaemonUnavailable:
        return daemon_client.DaemonUnavailable("no live daemon")

    monkeypatch.setattr(daemon_client, "steer_turn", gone)

    result = invoke(cli, "steer", session_id, "X", "--steer-mode", "in-place")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    envelope = steer_error(result)
    assert envelope["kind"] == "unavailable"
    assert envelope["context"]["correction_result"]["message_state"] == "not_delivered"
    assert sessions.load(session_id).state == "running"


def test_a_daemon_that_never_answers_is_reported_as_unknown(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SPEC steer: a request that may have crossed is never reported as
    delivered or as refused."""
    session_id = mid_turn_session(cli)
    sessions.update_meta(session_id, steer_mode="in-place")

    async def silent(target: str, selector: str, text: str) -> None:
        return None

    monkeypatch.setattr(daemon_client, "steer_turn", silent)

    result = invoke(cli, "steer", session_id, "X", "--steer-mode", "in-place")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    envelope = steer_error(result)
    assert envelope["kind"] == "outcome_unknown"
    assert envelope["context"]["correction_result"]["message_state"] == "unknown"


def test_ctrl_c_during_a_blocking_in_place_steer_leaves_the_turn_running(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SPEC steer: an in-place steer owns no turn, so Ctrl-C behaves like `wait`
    (exit 130, the turn runs on) and still names the correction it had made."""
    session_id = mid_turn_session(cli)
    sessions.update_meta(session_id, steer_mode="in-place")

    async def accepted(target: str, selector: str, text: str) -> dict[str, Any]:
        return {"ok": True, "outcome": "injected", "turn_token": 1}

    def interrupted(*args: Any, **kwargs: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(daemon_client, "steer_turn", accepted)
    monkeypatch.setattr(runner, "wait_for_session", interrupted)

    result = invoke(cli, "steer", session_id, "X", "--steer-mode", "in-place", "--json")

    assert result.exit_code == vocab.EXIT_CANCELLED
    assert result.stdout == ""
    envelope = steer_error(result)
    assert envelope["kind"] == "interrupted"
    assert envelope["context"]["session_id"] == session_id
    assert envelope["context"]["correction_result"]["message_state"] == "accepted"
    assert sessions.load(session_id).state == "running"


def test_a_turn_that_ends_canceled_after_an_accepted_correction_is_a_failure(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SPEC steer: the instruction was accepted; what the turn then did is the
    session's own result, reported as `operation_failed` with no document."""
    session_id = mid_turn_session(cli)
    sessions.update_meta(session_id, steer_mode="in-place")

    async def accepted(target: str, selector: str, text: str) -> dict[str, Any]:
        return {"ok": True, "outcome": "injected", "turn_token": 1}

    def ends_canceled(selector: str, *, timeout: float | None = None) -> str:
        runner._finalize(
            session_id,
            runner.TurnOutcome(state="canceled", stop_reason="cancelled", answer="partial"),
        )
        return "canceled"

    monkeypatch.setattr(daemon_client, "steer_turn", accepted)
    monkeypatch.setattr(runner, "wait_for_session", ends_canceled)

    result = invoke(cli, "steer", session_id, "X", "--steer-mode", "in-place", "--json")

    assert result.exit_code == vocab.EXIT_CANCELLED
    assert result.stdout == ""
    envelope = steer_error(result)
    assert envelope["kind"] == "operation_failed"
    assert envelope["context"]["status"] == "canceled"
    assert envelope["context"]["correction_result"] == {
        "steer_mode": "in-place",
        "target_turn": 2,
        "target_status": "canceled",
        "message_state": "accepted",
    }
