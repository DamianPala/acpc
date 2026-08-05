"""Behavioral tests for stop, rm, prune, and daemon idle retirement."""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from acpc import daemon, proc, sessions, vocab
from acpc.cli import main

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


@pytest.mark.parametrize("state", sorted(vocab.FINISHED_STATES))
def test_stop_is_a_successful_noop_for_every_finished_state(cli: CliRunner, state: str) -> None:
    session_id = _finished_session(state)

    result = invoke(cli, "stop", session_id)

    assert result.exit_code == vocab.EXIT_OK
    assert sessions.read_meta(session_id).state == state


def test_stop_unknown_session_is_a_usage_error(cli: CliRunner) -> None:
    result = invoke(cli, "stop", "does-not-exist")

    assert result.exit_code == vocab.EXIT_USAGE


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
