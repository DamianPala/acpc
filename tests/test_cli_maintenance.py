"""Behavioral tests for stop, rm, prune, and daemon idle retirement."""

import asyncio
import contextlib
import functools
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

import acpc.cli as cli_module
from acpc import daemon, daemon_client, proc, runner, sessions, vocab
from acpc.cli import main
from acpc.registry import AgentRegistry

MOCK_AGENT_SCRIPT = str(Path(__file__).with_name("mock_agent.py"))

MOCK_ENTRY = f"""
name = "Mock Agent"
command = "{sys.executable} {MOCK_AGENT_SCRIPT}"
install_command = "true"
home = "~/.mock"
home_env = "MOCK_HOME"
efforts = ["low", "medium", "high", "xhigh"]

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


def _finished_session(state: str = "done") -> str:
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
        payload["finished_at"] -= finished
    path.write_text(json.dumps(payload), encoding="utf-8")


def _wait_until_running(cli: CliRunner, session_id: str) -> None:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        result = invoke(cli, "status", session_id, "--json")
        if json.loads(result.stdout)["state"] == "running":
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
    return runner.call_target(AgentRegistry().resolve_call(agent))


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
def test_stop_is_a_successful_noop_for_every_finished_state(cli: CliRunner, state: str) -> None:
    session_id = _finished_session(state)

    result = invoke(cli, "stop", session_id)

    assert result.exit_code == vocab.EXIT_OK
    assert sessions.read_meta(session_id).state == state


def test_stop_unknown_session_is_a_usage_error(cli: CliRunner) -> None:
    result = invoke(cli, "stop", "does-not-exist")

    assert result.exit_code == vocab.EXIT_USAGE


def test_daemon_status_with_no_daemons_is_a_successful_empty_report(cli: CliRunner) -> None:
    """No daemons is a normal state, not a failure — scripted cleanliness
    checks (`acpc daemon status && …`) depend on the zero exit."""
    result = invoke(cli, "daemon", "status")

    assert result.exit_code == vocab.EXIT_OK
    assert result.stdout == ""
    assert "no daemons running" in result.stderr


def _finished_target_session(target: str, *, finished_at: float = 110.0) -> str:
    meta = sessions.create_session(
        entry="mock",
        base_adapter="mock",
        prompt="idle age",
        target=target,
        clock=lambda: 100.0,
    )
    sessions.transition(meta.session_id, "done", clock=lambda: finished_at, exit_code=0)
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
        first = invoke(cli, "daemon", "status", "mock")
        second = invoke(cli, "daemon", "status", "mock")
    finally:
        cli_module.time = original_time

    assert "idle 0m10s" in _daemon_status_line(first, target)
    assert "idle 0m20s" in _daemon_status_line(second, target)


def test_daemon_status_running_target_renders_dot_and_json_null(
    cli: CliRunner, live_daemon: None
) -> None:
    _start_slow_session(cli, "slow:5 daemon status running")

    text_result = invoke(cli, "daemon", "status", "mock")
    json_result = invoke(cli, "daemon", "status", "mock", "--json")
    target = _target()
    row = _daemon_status_line(text_result, target)
    entry = json.loads(json_result.stdout)["daemons"][0]

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

    text_result = invoke(cli, "daemon", "status", "mock")
    json_result = invoke(cli, "daemon", "status", "mock", "--json")
    row = _daemon_status_line(text_result, target)
    entry = json.loads(json_result.stdout)["daemons"][0]

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
        text_result = invoke(cli, "daemon", "status", "mock")
        json_result = invoke(cli, "daemon", "status", "mock", "--json")
    finally:
        cli_module.time = original_time

    entry = json.loads(json_result.stdout)["daemons"][0]
    assert entry["idle_seconds"] == 10.0
    assert "idle 0m10s" in _daemon_status_line(text_result, target)


def test_daemon_status_never_served_target_has_no_idle_age(
    cli: CliRunner, live_daemon: None
) -> None:
    target = _target()
    _start_daemon(target)

    text_result = invoke(cli, "daemon", "status", "mock")
    json_result = invoke(cli, "daemon", "status", "mock", "--json")
    row = _daemon_status_line(text_result, target)
    entry = json.loads(json_result.stdout)["daemons"][0]

    assert "idle " not in row
    assert "· ·" in row
    assert entry["idle_seconds"] is None


def test_daemon_stop_help_describes_force(cli: CliRunner) -> None:
    result = invoke(cli, "daemon", "stop", "--help")

    assert result.exit_code == vocab.EXIT_OK
    assert "--force" in result.stdout
    assert (
        "Stop even when the target has running or starting sessions; they are failed, not orphaned."
        in " ".join(result.stdout.split())
    )


def test_daemon_stop_refuses_a_running_session_and_leaves_daemon_alive(
    cli: CliRunner, live_daemon: None
) -> None:
    session_id = _start_slow_session(cli)

    result = invoke(cli, "daemon", "stop", "mock")

    assert result.exit_code == vocab.EXIT_USAGE
    assert result.stdout == ""
    assert result.stderr == (
        f"Error: daemon stop mock: 1 active session ({session_id}) — wait or stop them first, "
        f"or pass --force\n"
    )
    assert sessions.load(session_id).state == "running"
    assert _daemon_is_reachable(_target())


def test_daemon_stop_refuses_a_starting_session(cli: CliRunner, live_daemon: None) -> None:
    target = _target()
    _start_daemon(target)
    session = sessions.create_session(
        entry="mock", base_adapter="mock", prompt="still starting", target=target
    )

    result = invoke(cli, "daemon", "stop", "mock")

    assert result.exit_code == vocab.EXIT_USAGE
    assert result.stderr == (
        f"Error: daemon stop mock: 1 active session ({session.session_id}) — wait or stop them "
        f"first, or pass --force\n"
    )
    assert sessions.read_meta(session.session_id).state == "starting"
    assert _daemon_is_reachable(target)


def test_daemon_stop_force_fails_active_sessions_with_the_existing_reason(
    cli: CliRunner, live_daemon: None
) -> None:
    session_id = _start_slow_session(cli)

    result = invoke(cli, "daemon", "stop", "mock", "--force")

    assert result.exit_code == vocab.EXIT_OK
    assert result.stdout == ""
    assert result.stderr == "-- stopped 1 daemon(s)\n"
    _wait_until_state(session_id, "failed")
    meta = sessions.read_meta(session_id)
    assert meta.state == "failed"
    assert meta.stop_reason == "the daemon was stopped"


def test_idle_daemon_stop_keeps_its_existing_output(cli: CliRunner, live_daemon: None) -> None:
    _start_daemon(_target())

    result = invoke(cli, "daemon", "stop", "mock")

    assert result.exit_code == vocab.EXIT_OK
    assert result.stdout == ""
    assert result.stderr == "-- stopped 1 daemon(s)\n"


def test_orphaned_session_does_not_block_daemon_stop(cli: CliRunner, live_daemon: None) -> None:
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

    assert sessions.load(session.session_id).state == "orphaned"
    result = invoke(cli, "daemon", "stop", "mock")

    assert result.exit_code == vocab.EXIT_OK
    assert result.stderr == "-- stopped 1 daemon(s)\n"
    assert sessions.read_meta(session.session_id).state == "orphaned"


def test_multi_target_daemon_stop_refuses_before_stopping_any_target(
    cli: CliRunner, state_root: Path, live_daemon: None
) -> None:
    other_entry = state_root / "agents" / "other.toml"
    other_entry.write_text(MOCK_ENTRY.replace('home = "~/.mock"', 'home = "~/.mock-other"'))
    other_target = _target("other")
    _start_daemon(other_target)
    session_id = _start_slow_session(cli)

    result = invoke(cli, "daemon", "stop")

    assert result.exit_code == vocab.EXIT_USAGE
    assert result.stderr == (
        f"Error: daemon stop: 1 active session ({session_id}) — wait or stop them first, "
        f"or pass --force\n"
    )
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
    assert result.exit_code == vocab.EXIT_USAGE
    assert f"daemon stop mock: 2 active sessions ({second}, {first})" in result.stderr
    assert "1 active session" not in result.stderr


def test_stop_running_session_cancels_daemon_and_preserves_artifacts(
    cli: CliRunner, state_root: Path, live_daemon: None
) -> None:
    result = invoke(cli, "run", "mock", "run the slow scenario", "--bg", "--quiet")
    assert result.exit_code == vocab.EXIT_OK, result.stderr
    session_id = result.stdout.strip().splitlines()[0]
    _wait_until_running(cli, session_id)

    stopped = invoke(cli, "stop", session_id)

    assert stopped.exit_code == vocab.EXIT_OK
    assert sessions.read_meta(session_id).state == "cancelled"
    assert sessions.transcript_path(session_id).exists()
    answer = sessions.answer_path(session_id)
    assert answer.exists()


def test_stop_json_is_one_object_on_stdout(cli: CliRunner) -> None:
    session_id = _finished_session()

    result = invoke(cli, "stop", session_id, "--json")

    assert result.exit_code == vocab.EXIT_OK
    assert json.loads(result.stdout) == {
        "session_id": session_id,
        "state": "done",
        "stop_reason": "test",
    }
    assert result.stderr.startswith("-- stop ")


def test_rm_deletes_a_finished_session(cli: CliRunner) -> None:
    session_id = _finished_session()

    result = invoke(cli, "rm", session_id)

    assert result.exit_code == vocab.EXIT_OK
    assert not sessions.session_dir(session_id).exists()


def test_rm_unknown_session_is_a_usage_error(cli: CliRunner) -> None:
    result = invoke(cli, "rm", "does-not-exist")

    assert result.exit_code == vocab.EXIT_USAGE


def test_rm_rejects_a_running_session_and_suggests_stop(cli: CliRunner) -> None:
    meta = sessions.create_session(entry="mock", base_adapter="mock", prompt="active")
    sessions.mark_running(
        meta.session_id,
        pid=os.getpid(),
        process_start_time=proc.process_start_time(),
    )

    result = invoke(cli, "rm", meta.session_id)

    assert result.exit_code == vocab.EXIT_USAGE
    assert "stop" in result.stderr
    assert sessions.session_dir(meta.session_id).exists()


def test_rm_json_reports_the_removed_session(cli: CliRunner) -> None:
    session_id = _finished_session()

    result = invoke(cli, "rm", session_id, "--json")

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
    payload["created_at"] -= 200 * 86400
    payload["started_at"] -= 200 * 86400
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = invoke(cli, "prune", "--older-than", "100d")

    assert result.exit_code == vocab.EXIT_OK
    assert sessions.session_dir(session_id).exists()


def test_prune_never_deletes_an_active_session(cli: CliRunner, state_root: Path) -> None:
    meta = sessions.create_session(entry="mock", base_adapter="mock", prompt="active")
    sessions.mark_running(
        meta.session_id,
        pid=os.getpid(),
        process_start_time=proc.process_start_time(),
    )
    result = invoke(cli, "prune", "--older-than", "1s")

    assert result.exit_code == vocab.EXIT_OK
    assert sessions.session_dir(meta.session_id).exists()


def test_prune_uses_configured_retention_by_default(cli: CliRunner, state_root: Path) -> None:
    (state_root / "config.toml").write_text('retention = "7d"\n', encoding="utf-8")
    session_id = _finished_session()
    _backdate(state_root, session_id, finished=8 * 86400)

    result = invoke(cli, "prune")

    assert result.exit_code == vocab.EXIT_OK
    assert not sessions.session_dir(session_id).exists()


def test_bare_prune_zero_retention_is_safe_but_explicit_zero_deletes(
    cli: CliRunner, state_root: Path
) -> None:
    (state_root / "config.toml").write_text('retention = "0d"\n', encoding="utf-8")
    session_id = _finished_session()

    result = invoke(cli, "prune")

    assert result.exit_code == vocab.EXIT_USAGE
    assert result.stderr == (
        "Error: config retention '0d' resolves to zero — bare prune would delete every finished "
        "session; pass --older-than 0d to do that explicitly\n"
    )
    assert sessions.session_dir(session_id).exists()

    explicit = invoke(cli, "prune", "--older-than", "0d")

    assert explicit.exit_code == vocab.EXIT_OK
    assert not sessions.session_dir(session_id).exists()


def test_zero_retention_disables_the_auto_prune_sweep(cli: CliRunner, state_root: Path) -> None:
    (state_root / "config.toml").write_text('retention = "0d"\n', encoding="utf-8")
    session_id = _finished_session()

    result = invoke(cli, "run", "mock", "echo:hello", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert sessions.session_dir(session_id).exists()


def test_bare_prune_default_retention_keeps_a_fresh_finished_session(cli: CliRunner) -> None:
    session_id = _finished_session()

    result = invoke(cli, "prune")

    assert result.exit_code == vocab.EXIT_OK
    assert sessions.session_dir(session_id).exists()


def test_prune_json_is_one_object_on_stdout(cli: CliRunner, state_root: Path) -> None:
    session_id = _finished_session()
    _backdate(state_root, session_id, finished=200 * 86400)

    result = invoke(cli, "prune", "--older-than", "100d", "--dry-run", "--json")

    assert result.exit_code == vocab.EXIT_OK
    assert json.loads(result.stdout) == {"sessions": [session_id], "dry_run": True}
    assert result.stderr.startswith("-- prune ")


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
