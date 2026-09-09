"""Behavioral tests for stop, rm, prune, and daemon idle retirement."""

import asyncio
import contextlib
import functools
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

import acpc.cli as cli_module
from acpc import __version__, daemon, daemon_client, proc, runner, sessions, vocab
from acpc.cli import main
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

[presets]
standard = {{ model = "mock-sonnet-5", effort = "high" }}
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


def invoke(cli: CliRunner, *args: str):
    return cli.invoke(main, list(args), catch_exceptions=False)


def _finished_session(state: str = "succeeded") -> str:
    meta = sessions.create_session(entry="mock", base_adapter="mock", prompt="maintenance")
    sessions.mark_running(
        meta.session_id,
        pid=os.getpid(),
        process_start_time=proc.process_start_time(),
    )
    sessions.transition(meta.session_id, state, exit_code=0, stop_reason="test")
    return meta.session_id


def _backdate(root: Path, session_id: str, *, finished: float | None = None) -> None:
    path = root / "sessions" / session_id / "meta.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if finished is not None and payload["finished_at"] is not None:
        timestamp = sessions.parse_timestamp(payload["finished_at"], "finished_at", path)
        assert timestamp is not None
        payload["finished_at"] = sessions.format_timestamp(timestamp - finished)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _wait_until_running(cli: CliRunner, session_id: str) -> None:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        result = invoke(cli, "status", session_id, "--json")
        if json.loads(result.stdout)["status"] == "running":
            return
        time.sleep(0.05)
    pytest.fail(f"session {session_id} never became running")


def _wait_until_state(session_id: str, state: str) -> None:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if sessions.load(session_id).state == state:
            return
        time.sleep(0.05)
    pytest.fail(f"session {session_id} never became {state}")


def _target(agent: str = "mock") -> str:
    resolution = AgentRegistry().resolve_call(agent, permissions="read")
    return runner.call_target(resolution)


def _start_daemon(target: str) -> None:
    async def start() -> None:
        connection = await daemon_client.ensure_daemon(target)
        assert not isinstance(connection, daemon_client.DaemonUnavailable)
        await connection.close()

    asyncio.run(start())


def _daemon_is_reachable(target: str) -> bool:
    async def check() -> bool:
        connection = await daemon_client.connect(target)
        if connection is None:
            return False
        try:
            return bool((await connection.status()).get("ok"))
        finally:
            await connection.close()

    return asyncio.run(check())


def _start_slow_session(cli: CliRunner, prompt: str = "slow:5 daemon stop probe") -> str:
    result = invoke(cli, "run", "mock", prompt, "--bg", "--quiet")
    assert result.exit_code == vocab.EXIT_OK, result.stderr
    session_id = result.stdout.strip().splitlines()[0]
    _wait_until_running(cli, session_id)
    return session_id


@pytest.mark.parametrize("state", sorted(vocab.FINISHED_STATES))
def test_cancel_is_a_successful_noop_for_every_finished_state(cli: CliRunner, state: str) -> None:
    session_id = _finished_session(state)

    result = invoke(cli, "cancel", session_id)

    assert result.exit_code == vocab.EXIT_OK
    assert sessions.read_meta(session_id).state == state


def test_cancel_unknown_session_is_not_found(cli: CliRunner) -> None:
    result = invoke(cli, "cancel", "does-not-exist")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    assert json.loads(result.stderr)["error"]["kind"] == "not_found"


def test_daemon_status_with_no_daemons_is_a_successful_empty_report(cli: CliRunner) -> None:
    """No daemons is a normal state, not a failure — scripted cleanliness
    checks (`acpc daemon status && …`) depend on the zero exit."""
    result = invoke(cli, "daemon", "status", "--format", "text")

    assert result.exit_code == vocab.EXIT_OK
    assert result.stdout == ""
    assert "no daemons running" in result.stderr


def test_daemon_targets_are_sorted_and_repeatable(state_root: Path) -> None:
    daemon_dir = state_root / "daemon"
    daemon_dir.mkdir()
    (daemon_dir / "zeta.lock").write_text("", encoding="utf-8")
    (daemon_dir / "alpha.lock").write_text("", encoding="utf-8")

    first = runner.all_daemon_targets()
    second = runner.all_daemon_targets()

    assert first == ["alpha", "zeta"]
    assert second == first


def _finished_target_session(target: str, *, finished_at: float = 110.0) -> str:
    meta = sessions.create_session(
        entry="mock",
        base_adapter="mock",
        prompt="idle age",
        target=target,
        clock=lambda: 100.0,
    )
    sessions.transition(meta.session_id, "succeeded", clock=lambda: finished_at, exit_code=0)
    return meta.session_id


def _daemon_status_line(result: Any, target: str) -> str:
    return next(line for line in result.stdout.splitlines() if line.startswith(target))


def test_daemon_status_idle_age_grows_between_clock_reads(
    cli: CliRunner, live_daemon: None
) -> None:
    target = _target()
    _start_daemon(target)
    _finished_target_session(target)
    clock_values = iter((120.0, 130.0))
    monkey_time = SimpleNamespace(time=lambda: next(clock_values))

    original_time = cli_module.time
    cli_module.time = monkey_time
    try:
        first = invoke(cli, "daemon", "status", "mock", "--format", "text")
        second = invoke(cli, "daemon", "status", "mock", "--format", "text")
    finally:
        cli_module.time = original_time

    assert "idle 0m10s" in _daemon_status_line(first, target)
    assert "idle 0m20s" in _daemon_status_line(second, target)


def test_daemon_status_renders_the_acpc_version(cli: CliRunner, live_daemon: None) -> None:
    target = _target()
    _start_daemon(target)

    text_result = invoke(cli, "daemon", "status", "mock", "--format", "text")
    json_result = invoke(cli, "daemon", "status", "mock", "--json")
    row = _daemon_status_line(text_result, target)
    entry = json.loads(json_result.stdout)["items"][0]

    assert row.startswith(f"{target}  acpc {__version__}  pid ")
    assert row.count(f"acpc {__version__}") == 1
    assert entry["version"] == __version__


def test_daemon_status_aligns_rows_without_a_header(
    cli: CliRunner, state_root: Path, live_daemon: None
) -> None:
    long_agent = "daemon-target-with-a-long-name"
    (state_root / "agents" / f"{long_agent}.toml").write_text(
        'extends = "mock"\nhome = "~/.daemon-home-with-a-long-name"\n',
        encoding="utf-8",
    )
    short_target = _target()
    long_target = _target(long_agent)
    _start_daemon(short_target)
    _start_daemon(long_target)

    result = invoke(cli, "daemon", "status", "--format", "text")
    assert result.exit_code == vocab.EXIT_OK
    rows = [_daemon_status_line(result, target) for target in (short_target, long_target)]
    assert result.stdout.splitlines()[0].split()[0] != "target"
    assert [row.index("pid ") for row in rows] == [rows[0].index("pid ")] * 2
    assert [row.index("up ") for row in rows] == [rows[0].index("up ")] * 2


def test_daemon_status_running_target_renders_dot_and_json_null(
    cli: CliRunner, live_daemon: None
) -> None:
    _start_slow_session(cli, "slow:5 daemon status running")

    text_result = invoke(cli, "daemon", "status", "mock", "--format", "text")
    json_result = invoke(cli, "daemon", "status", "mock", "--json")
    target = _target()
    row = _daemon_status_line(text_result, target)
    entry = json.loads(json_result.stdout)["items"][0]

    assert "idle " not in row
    assert "· ·" in row
    assert entry["idle_seconds"] is None


def test_daemon_status_starting_target_renders_dot_and_json_null(
    cli: CliRunner, live_daemon: None
) -> None:
    target = _target()
    _start_daemon(target)
    _finished_target_session(target)
    sessions.create_session(
        entry="mock", base_adapter="mock", prompt="daemon status starting", target=target
    )

    text_result = invoke(cli, "daemon", "status", "mock", "--format", "text")
    json_result = invoke(cli, "daemon", "status", "mock", "--json")
    row = _daemon_status_line(text_result, target)
    entry = json.loads(json_result.stdout)["items"][0]

    assert "idle " not in row
    assert "· ·" in row
    assert entry["idle_seconds"] is None


def test_daemon_status_json_idle_age_matches_text(cli: CliRunner, live_daemon: None) -> None:
    target = _target()
    _start_daemon(target)
    _finished_target_session(target)
    original_time = cli_module.time
    cli_module.time = SimpleNamespace(time=lambda: 120.0)
    try:
        text_result = invoke(cli, "daemon", "status", "mock", "--format", "text")
        json_result = invoke(cli, "daemon", "status", "mock", "--json")
    finally:
        cli_module.time = original_time

    entry = json.loads(json_result.stdout)["items"][0]
    assert entry["idle_seconds"] == 10.0
    assert "idle 0m10s" in _daemon_status_line(text_result, target)
    assert (
        json_result.stdout == json.dumps(json.loads(json_result.stdout), ensure_ascii=False) + "\n"
    )


def test_daemon_status_never_served_target_has_no_idle_age(
    cli: CliRunner, live_daemon: None
) -> None:
    target = _target()
    _start_daemon(target)

    text_result = invoke(cli, "daemon", "status", "mock", "--format", "text")
    json_result = invoke(cli, "daemon", "status", "mock", "--json")
    row = _daemon_status_line(text_result, target)
    entry = json.loads(json_result.stdout)["items"][0]

    assert "idle " not in row
    assert "· ·" in row
    assert entry["idle_seconds"] is None


def test_daemon_stop_help_describes_force(cli: CliRunner) -> None:
    result = invoke(cli, "daemon", "stop", "--help")

    assert result.exit_code == vocab.EXIT_OK
    assert "--force" in result.stdout
    assert (
        "Stop even when the target has running or starting sessions; they are failed, not unknown."
        in " ".join(result.stdout.split())
    )


def test_daemon_stop_refuses_a_running_session_and_leaves_daemon_alive(
    cli: CliRunner, live_daemon: None
) -> None:
    session_id = _start_slow_session(cli)

    result = invoke(cli, "daemon", "stop", "mock", "--json")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    assert result.stdout == ""
    envelope = json.loads(result.stderr)["error"]
    assert envelope["kind"] == "precondition_failed"
    assert f"1 active session ({session_id})" in envelope["message"]
    assert envelope["hint"] == "Run: acpc daemon stop mock --force"
    assert envelope["context"]["sessions"] == [session_id]
    assert sessions.load(session_id).state == "running"
    assert _daemon_is_reachable(_target())


def test_daemon_stop_refuses_a_starting_session(cli: CliRunner, live_daemon: None) -> None:
    target = _target()
    _start_daemon(target)
    session = sessions.create_session(
        entry="mock", base_adapter="mock", prompt="still starting", target=target
    )

    result = invoke(cli, "daemon", "stop", "mock")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    envelope = json.loads(result.stderr)["error"]
    assert envelope["kind"] == "precondition_failed"
    assert f"1 active session ({session.session_id})" in envelope["message"]
    assert sessions.read_meta(session.session_id).state == "starting"
    assert _daemon_is_reachable(target)


def test_daemon_stop_force_fails_active_sessions_with_the_existing_reason(
    cli: CliRunner, live_daemon: None
) -> None:
    session_id = _start_slow_session(cli)

    result = invoke(cli, "daemon", "stop", "mock", "--force")

    assert result.exit_code == vocab.EXIT_OK
    assert json.loads(result.stdout)["targets"] == ["mock~4ac109c6ee44d1e7"]
    assert result.stderr == "-- stopped 1 daemon(s)\n"
    _wait_until_state(session_id, "failed")
    meta = sessions.read_meta(session_id)
    assert meta.state == "failed"
    assert meta.stop_reason == "the daemon was stopped"


def test_idle_daemon_stop_keeps_its_existing_output(cli: CliRunner, live_daemon: None) -> None:
    _start_daemon(_target())

    result = invoke(cli, "daemon", "stop", "mock")

    assert result.exit_code == vocab.EXIT_OK
    assert json.loads(result.stdout)["targets"] == [_target()]
    assert result.stderr == "-- stopped 1 daemon(s)\n"


def test_unknown_session_does_not_block_daemon_stop(cli: CliRunner, live_daemon: None) -> None:
    target = _target()
    _start_daemon(target)
    session = sessions.create_session(
        entry="mock", base_adapter="mock", prompt="orphan me", target=target
    )
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        sessions.mark_running(
            session.session_id,
            pid=child.pid,
            process_start_time=proc.process_start_time(child.pid),
        )
    finally:
        child.terminate()
        child.wait(timeout=5)

    assert sessions.load(session.session_id).state == "unknown"
    result = invoke(cli, "daemon", "stop", "mock")

    assert result.exit_code == vocab.EXIT_OK
    assert result.stderr == "-- stopped 1 daemon(s)\n"
    assert sessions.read_meta(session.session_id).state == "unknown"


def test_multi_target_daemon_stop_refuses_before_stopping_any_target(
    cli: CliRunner, state_root: Path, live_daemon: None
) -> None:
    other_entry = state_root / "agents" / "other.toml"
    other_entry.write_text(MOCK_ENTRY.replace('home = "~/.mock"', 'home = "~/.mock-other"'))
    other_target = _target("other")
    _start_daemon(other_target)
    session_id = _start_slow_session(cli)

    result = invoke(cli, "daemon", "stop")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    envelope = json.loads(result.stderr)["error"]
    assert envelope["kind"] == "precondition_failed"
    assert f"1 active session ({session_id})" in envelope["message"]
    assert _daemon_is_reachable(_target())
    assert _daemon_is_reachable(other_target)


def test_daemon_stop_uses_singular_and_plural_active_session_wording(
    cli: CliRunner, live_daemon: None
) -> None:
    first = _start_slow_session(cli, "slow:5 first daemon stop session")
    second = _start_slow_session(cli, "slow:5 second daemon stop session")
    listed_ids = [
        meta.session_id
        for meta in sessions.list_sessions()
        if meta.target == _target() and meta.state in {"running", "starting"}
    ]

    result = invoke(cli, "daemon", "stop", "mock")

    assert listed_ids == [second, first]
    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    assert f"daemon stop mock: 2 active sessions ({second}, {first})" in result.stderr
    assert "1 active session" not in result.stderr


def test_cancel_running_session_cancels_daemon_and_preserves_artifacts(
    cli: CliRunner, state_root: Path, live_daemon: None
) -> None:
    result = invoke(cli, "run", "mock", "run the slow scenario", "--bg", "--quiet")
    assert result.exit_code == vocab.EXIT_OK, result.stderr
    session_id = result.stdout.strip().splitlines()[0]
    _wait_until_running(cli, session_id)

    stopped = invoke(cli, "cancel", session_id, "--json")

    assert stopped.exit_code == vocab.EXIT_OK
    assert json.loads(stopped.stdout)["changed"] is True
    assert sessions.read_meta(session_id).state == "canceled"
    assert sessions.transcript_path(session_id).exists()
    answer = sessions.answer_path(session_id)
    assert answer.exists()


def test_cancel_acknowledged_while_work_is_pending_reports_changed(
    cli: CliRunner, live_daemon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _start_slow_session(cli, "slow:30 cancellation pending")
    current_turn = sessions.read_meta(session_id).turns

    async def accepted_without_finishing(target: str, selected: str) -> dict[str, Any]:
        del target, selected
        return {"ok": True, "turn_token": current_turn}

    monkeypatch.setattr(daemon_client, "cancel_turn", accepted_without_finishing)
    monkeypatch.setattr(cli_module.runner, "CANCEL_ACK_TIMEOUT", 0.05)

    stopped = invoke(cli, "cancel", session_id, "--json")

    assert stopped.exit_code == vocab.EXIT_OK, stopped.stderr
    payload = json.loads(stopped.stdout)
    assert payload["status"] == "running"
    assert payload["changed"] is True


def test_cancel_without_rpc_confirmation_fails_without_killing_work(
    cli: CliRunner, live_daemon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _start_slow_session(cli, "slow:30 cancellation unconfirmed")

    async def never_replies(target: str, selected: str) -> bool:
        del target, selected
        await asyncio.sleep(1)
        return True

    monkeypatch.setattr(daemon_client, "cancel_turn", never_replies)
    monkeypatch.setattr(cli_module.runner, "CANCEL_ACK_TIMEOUT", 0.05)

    result = invoke(cli, "cancel", session_id, "--json")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    error = json.loads(result.stderr.splitlines()[-1])["error"]
    assert error["kind"] == "outcome_unknown"
    assert error["context"] == {"session_id": session_id, "status": "running"}
    assert sessions.load(session_id).state == "running"


def test_cancel_rereads_after_terminal_race_and_preserves_another_session(
    cli: CliRunner, live_daemon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    target_session = _start_slow_session(cli, "slow:1 finishes during cancel")
    other_session = _start_slow_session(cli, "slow:30 unrelated session")
    entered_rpc = threading.Event()
    release_rpc = threading.Event()
    real_cancel = daemon_client.cancel_turn

    async def delayed_cancel(target: str, selected: str) -> Any:
        entered_rpc.set()
        await asyncio.to_thread(release_rpc.wait, 5)
        return await real_cancel(target, selected)

    monkeypatch.setattr(daemon_client, "cancel_turn", delayed_cancel)
    result_holder: list[Any] = []
    cancel_thread = threading.Thread(
        target=lambda: result_holder.append(invoke(cli, "cancel", target_session, "--json")),
        daemon=True,
    )
    cancel_thread.start()
    assert entered_rpc.wait(5)
    _wait_until_state(target_session, "succeeded")
    release_rpc.set()
    cancel_thread.join(timeout=10)

    assert not cancel_thread.is_alive()
    result = result_holder[0]
    assert result.exit_code == vocab.EXIT_OK, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "succeeded"
    assert payload["changed"] is False
    assert _daemon_is_reachable(_target())
    assert sessions.load(other_session).state == "running"


def test_cancel_preparing_waits_for_the_acknowledged_turn_generation(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _target()
    meta = _finished_target_session(target)
    old_turn = sessions.read_meta(meta).turns
    acknowledged = threading.Event()
    first_observation = threading.Event()
    release_observation = threading.Event()
    advanced = threading.Event()

    def show_preparing(current: sessions.SessionMeta) -> sessions.SessionMeta:
        current.state = "preparing"
        return current

    async def accepted_cancel(selected_target: str, selected_id: str) -> dict[str, Any]:
        assert selected_target == target
        assert selected_id == meta
        acknowledged.set()
        return {"ok": True, "turn_token": old_turn + 1}

    def advance_turn() -> None:
        assert first_observation.wait(5)
        sessions.rotate_turn(meta, prompt="replacement", target_from_meta=lambda _: target)
        if sessions.read_meta(meta).state == "starting":
            sessions.transition(meta, "canceled", exit_code=vocab.EXIT_CANCELLED)
        release_observation.set()
        advanced.set()

    real_load = sessions.load

    def controlled_load(session_id: str) -> sessions.SessionMeta:
        current = real_load(session_id)
        if (
            session_id == meta
            and acknowledged.is_set()
            and not first_observation.is_set()
            and current.turns == old_turn
        ):
            first_observation.set()
            if not release_observation.wait(5):
                raise AssertionError("the replacement turn never became observable")
        return current

    monkeypatch.setattr(cli_module, "_status_view_meta", show_preparing)
    monkeypatch.setattr(daemon_client, "cancel_turn", accepted_cancel)
    monkeypatch.setattr(sessions, "load", controlled_load)
    advance_thread = threading.Thread(target=advance_turn, daemon=True)
    advance_thread.start()

    result = invoke(cli, "cancel", meta, "--json")

    advance_thread.join(timeout=5)
    assert advanced.is_set()
    assert result.exit_code == vocab.EXIT_OK, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "canceled"
    assert payload["changed"] is True


def test_cancel_status_enum_contains_only_statuses_reached_by_cancel(
    cli: CliRunner, live_daemon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: set[str] = set()
    for state in sorted(vocab.FINISHED_STATES):
        session_id = _finished_session(state)
        result = invoke(cli, "cancel", session_id, "--json")
        assert result.exit_code == vocab.EXIT_OK, result.stderr
        observed.add(json.loads(result.stdout)["status"])

    running_id = _start_slow_session(cli, "slow:30 reachable running cancel status")
    turn = sessions.read_meta(running_id).turns

    async def accepted_without_finishing(target: str, selected: str) -> dict[str, Any]:
        del target, selected
        return {"ok": True, "turn_token": turn}

    monkeypatch.setattr(daemon_client, "cancel_turn", accepted_without_finishing)
    monkeypatch.setattr(cli_module.runner, "CANCEL_ACK_TIMEOUT", 0.05)
    result = invoke(cli, "cancel", running_id, "--json")
    assert result.exit_code == vocab.EXIT_OK, result.stderr
    observed.add(json.loads(result.stdout)["status"])

    detail = json.loads(invoke(cli, "schema", "cancel").stdout)
    declared = set(detail["output"]["properties"]["status"]["enum"])
    assert declared == observed


def test_cancel_reports_the_observed_terminal_state_before_cancellation(
    cli: CliRunner,
) -> None:
    meta = sessions.create_session(entry="mock", base_adapter="mock", prompt="race")
    sessions.mark_running(
        meta.session_id,
        pid=os.getpid(),
        process_start_time=proc.process_start_time(),
    )
    sessions.transition(meta.session_id, "succeeded", exit_code=0, stop_reason="test")

    result = invoke(cli, "cancel", meta.session_id, "--json")

    assert result.exit_code == vocab.EXIT_OK
    assert json.loads(result.stdout) == {
        "session_id": meta.session_id,
        "status": "succeeded",
        "stop_reason": "test",
        "changed": False,
    }


def test_cancel_json_is_one_object_on_stdout(cli: CliRunner) -> None:
    session_id = _finished_session()

    result = invoke(cli, "cancel", session_id, "--json")

    assert result.exit_code == vocab.EXIT_OK
    assert json.loads(result.stdout) == {
        "session_id": session_id,
        "status": "succeeded",
        "stop_reason": "test",
        "changed": False,
    }
    assert result.stderr.startswith("-- canceled ")


def test_delete_deletes_a_finished_session(cli: CliRunner) -> None:
    session_id = _finished_session()

    result = invoke(cli, "delete", session_id, "--yes")

    assert result.exit_code == vocab.EXIT_OK
    assert sessions.session_dir(session_id).is_dir()
    assert sessions.tombstone_path(session_id).is_file()
    assert not sessions.meta_path(session_id).exists()


def test_delete_unknown_session_is_not_found(cli: CliRunner) -> None:
    result = invoke(cli, "delete", "does-not-exist")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    assert json.loads(result.stderr)["error"]["kind"] == "not_found"


def test_delete_rejects_a_running_session_and_suggests_cancel(cli: CliRunner) -> None:
    meta = sessions.create_session(entry="mock", base_adapter="mock", prompt="active")
    sessions.mark_running(
        meta.session_id,
        pid=os.getpid(),
        process_start_time=proc.process_start_time(),
    )

    result = invoke(cli, "delete", meta.session_id)

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    envelope = json.loads(result.stderr)["error"]
    assert envelope["kind"] == "conflict"
    assert envelope["retryable"] is True
    assert "cancel" in envelope["message"]
    assert envelope["context"]["session_id"] == meta.session_id
    assert sessions.session_dir(meta.session_id).exists()


def test_delete_json_reports_the_removed_session(cli: CliRunner) -> None:
    session_id = _finished_session()

    result = invoke(cli, "delete", session_id, "--yes", "--json")

    assert result.exit_code == vocab.EXIT_OK
    payload = json.loads(result.stdout)
    assert payload["session_id"] == session_id
    assert payload["removed"] is True


def test_prune_dry_run_lists_old_finished_sessions_without_deleting(
    cli: CliRunner, state_root: Path
) -> None:
    session_id = _finished_session()
    _backdate(state_root, session_id, finished=200 * 86400)

    result = invoke(cli, "prune", "--older-than", "100d", "--dry-run")

    assert result.exit_code == vocab.EXIT_OK
    assert session_id in result.stdout
    assert sessions.session_dir(session_id).exists()


def test_prune_measures_age_from_finished_at(cli: CliRunner, state_root: Path) -> None:
    session_id = _finished_session()
    path = state_root / "sessions" / session_id / "meta.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    created_at = sessions.parse_timestamp(payload["created_at"], "created_at", path)
    started_at = sessions.parse_timestamp(payload["started_at"], "started_at", path)
    assert created_at is not None and started_at is not None
    payload["created_at"] = sessions.format_timestamp(created_at - 200 * 86400)
    payload["started_at"] = sessions.format_timestamp(started_at - 200 * 86400)
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = invoke(cli, "prune", "--older-than", "100d", "--yes")

    assert result.exit_code == vocab.EXIT_OK
    assert sessions.session_dir(session_id).exists()


def test_prune_never_deletes_an_active_session(cli: CliRunner, state_root: Path) -> None:
    meta = sessions.create_session(entry="mock", base_adapter="mock", prompt="active")
    sessions.mark_running(
        meta.session_id,
        pid=os.getpid(),
        process_start_time=proc.process_start_time(),
    )
    result = invoke(cli, "prune", "--older-than", "1s", "--yes")

    assert result.exit_code == vocab.EXIT_OK
    assert sessions.session_dir(meta.session_id).exists()


def test_prune_uses_configured_retention_by_default(cli: CliRunner, state_root: Path) -> None:
    (state_root / "config.toml").write_text('retention = "7d"\n', encoding="utf-8")
    session_id = _finished_session()
    _backdate(state_root, session_id, finished=8 * 86400)

    result = invoke(cli, "prune", "--yes")

    assert result.exit_code == vocab.EXIT_OK
    assert sessions.session_dir(session_id).is_dir()
    assert sessions.tombstone_path(session_id).is_file()


def test_bare_prune_zero_retention_is_safe_but_explicit_zero_deletes(
    cli: CliRunner, state_root: Path
) -> None:
    (state_root / "config.toml").write_text('retention = "0d"\n', encoding="utf-8")
    session_id = _finished_session()

    result = invoke(cli, "prune", "--yes")

    assert result.exit_code == vocab.EXIT_USAGE
    envelope = json.loads(result.stderr)["error"]
    assert envelope["kind"] == "invalid_input"
    assert "config retention '0d' resolves to zero" in envelope["message"]
    assert sessions.session_dir(session_id).exists()

    explicit = invoke(cli, "prune", "--older-than", "0d", "--yes")

    assert explicit.exit_code == vocab.EXIT_OK
    assert sessions.session_dir(session_id).is_dir()
    assert sessions.tombstone_path(session_id).is_file()


def test_zero_retention_disables_the_auto_prune_sweep(cli: CliRunner, state_root: Path) -> None:
    (state_root / "config.toml").write_text('retention = "0d"\n', encoding="utf-8")
    session_id = _finished_session()

    result = invoke(cli, "run", "mock", "echo:hello", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert sessions.session_dir(session_id).exists()


def test_bare_prune_default_retention_keeps_a_fresh_finished_session(cli: CliRunner) -> None:
    session_id = _finished_session()

    result = invoke(cli, "prune", "--yes")

    assert result.exit_code == vocab.EXIT_OK
    assert sessions.session_dir(session_id).exists()


def test_prune_json_is_one_object_on_stdout(cli: CliRunner, state_root: Path) -> None:
    session_id = _finished_session()
    _backdate(state_root, session_id, finished=200 * 86400)

    result = invoke(cli, "prune", "--older-than", "100d", "--dry-run", "--json")

    assert result.exit_code == vocab.EXIT_OK
    assert json.loads(result.stdout) == {
        "targets": [session_id],
        "changed": False,
        "requires_confirmation": True,
    }
    assert result.stderr.startswith("-- prune ")


def test_prune_removal_failure_is_reported_as_an_operation_error(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _finished_session()

    def refuse_removal(**kwargs: Any) -> list[Any]:
        del kwargs
        raise OSError("read-only session directory")

    monkeypatch.setattr(sessions, "prune_sessions", refuse_removal)

    result = invoke(cli, "prune", "--older-than", "0d", "--yes", "--json")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    assert result.stdout == ""
    error = json.loads(result.stderr)["error"]
    assert error["kind"] == "operation_failed"
    assert "read-only session directory" in error["message"]
    assert error["context"] == {"operation": "prune"}


def test_prune_rejects_an_invalid_duration(cli: CliRunner) -> None:
    result = invoke(cli, "prune", "--older-than", "not-a-duration")

    assert result.exit_code == vocab.EXIT_USAGE


def test_idle_sweep_survives_a_check_failure_and_retires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = daemon.Daemon("maintenance-idle-test", ttl=3600.0)
    checks = 0

    def flaky_endpoint_check() -> bool:
        nonlocal checks
        checks += 1
        if checks == 1:
            raise RuntimeError("injected idle check failure")
        return True

    monkeypatch.setattr(daemon, "IDLE_CHECK_INTERVAL", 0)
    monkeypatch.setattr(instance, "_endpoint_gone", flaky_endpoint_check)

    asyncio.run(asyncio.wait_for(instance._expire_when_idle(), timeout=1.0))

    assert checks == 2
    assert instance._shutdown.is_set()


def test_a_dead_lifecycle_task_retires_the_daemon() -> None:
    """A loop that should never finish, finishing, must not be survivable.

    The guard inside the idle sweep only covers what it can catch. This is
    the backstop for everything else, so the immortal-daemon failure class
    is impossible regardless of what killed the task.
    """
    instance = daemon.Daemon("maintenance-dead-task-test", ttl=3600.0)

    async def die() -> None:
        raise RuntimeError("injected lifecycle failure")

    async def drive() -> None:
        task = asyncio.ensure_future(die())
        task.add_done_callback(functools.partial(instance._lifecycle_task_ended, "idle"))
        await asyncio.wait_for(instance._shutdown.wait(), timeout=1.0)

    asyncio.run(drive())

    assert instance._stop_reason is not None
    assert "ended unexpectedly" in instance._stop_reason
    assert "injected lifecycle failure" in instance._stop_reason


def test_a_cancelled_lifecycle_task_reports_nothing_to_the_event_loop() -> None:
    """Shutdown cancels both loops; that is the normal path, not a fault.

    Asserting only that no stop reason is recorded would pass either way:
    `task.exception()` on a cancelled task raises inside the callback, and
    asyncio hands that to the loop's exception handler rather than to
    anyone waiting. The observable difference is whether the handler fires,
    so that is what this checks — otherwise every clean shutdown would
    write a spurious traceback into the per-target daemon log.
    """
    instance = daemon.Daemon("maintenance-cancelled-task-test", ttl=3600.0)
    reported: list[dict[str, Any]] = []

    async def drive() -> None:
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: reported.append(context)
        )
        task = asyncio.ensure_future(asyncio.sleep(60))
        task.add_done_callback(functools.partial(instance._lifecycle_task_ended, "accept"))
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(drive())

    assert reported == []
    assert instance._stop_reason is None
    assert not instance._shutdown.is_set()
