"""Integration tests for daemon operational behaviour."""

from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest


DAEMON_TEST_TIMEOUT = 15
PROMPT_SECONDS = DAEMON_TEST_TIMEOUT // 5
TTL_SECONDS = DAEMON_TEST_TIMEOUT // 7
IN_FLIGHT_SECONDS = PROMPT_SECONDS + TTL_SECONDS
POLL_SECONDS = DAEMON_TEST_TIMEOUT / 1000
SHORT_SLEEP_SECONDS = DAEMON_TEST_TIMEOUT / 200
REQUEST_TIMEOUT_SECONDS = max(1, DAEMON_TEST_TIMEOUT // 15)
CAPACITY_RSS_CEILING_MB = 128
CAPACITY_HELD_MEMORY_MB = 256

pytestmark = pytest.mark.timeout(DAEMON_TEST_TIMEOUT)

ACPC_COMMAND = [sys.executable, "-c", "from acpc.cli import cli; cli()"]
MOCK_AGENT_SCRIPT = Path(__file__).with_name("mock_agent.py")


def _run_acpc(
    *args: str,
    timeout: float = DAEMON_TEST_TIMEOUT,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    process_env = dict(os.environ)
    if env is not None:
        process_env.update(env)
    return subprocess.run(
        [*ACPC_COMMAND, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=process_env,
    )


def _daemon_paths(state_dir: Path) -> tuple[Path, Path]:
    run_dir = state_dir / "run"
    return run_dir / "mock.sock", run_dir / "mock.lock"


def _read_lock(state_dir: Path) -> dict[str, Any]:
    _, lock_path = _daemon_paths(state_dir)
    with lock_path.open(encoding="utf-8") as lock_file:
        metadata = json.load(lock_file)
    assert isinstance(metadata, dict)
    assert metadata.get("target") == "mock"
    pid = metadata.get("pid")
    cmdline = metadata.get("cmdline")
    assert isinstance(pid, int) and pid != os.getpid()
    assert isinstance(cmdline, list)
    assert "acpc.daemon" in cmdline
    assert "mock" in cmdline
    return metadata


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, OSError, IndexError):
        return True
    return state != "Z"


def _wait_for(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + DAEMON_TEST_TIMEOUT
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(POLL_SECONDS)
    assert condition(), "condition did not become true before the test budget expired"


def _stop_daemon(state_dir: Path) -> None:
    socket_path, lock_path = _daemon_paths(state_dir)
    if not socket_path.exists() and not lock_path.exists():
        return
    result = _run_acpc("daemon", "stop", "mock")
    assert result.returncode == 0, result.stderr
    _wait_for(lambda: not socket_path.exists() and not lock_path.exists())


def _stop_daemon_if_present(state_dir: Path) -> None:
    socket_path, lock_path = _daemon_paths(state_dir)
    if socket_path.exists() or lock_path.exists():
        _run_acpc("daemon", "stop", "mock")


def _read_until(stream: Any, text: str) -> list[str]:
    lines: list[str] = []
    deadline = time.monotonic() + DAEMON_TEST_TIMEOUT
    while time.monotonic() < deadline:
        ready, _, _ = select.select([stream], [], [], POLL_SECONDS)
        if not ready:
            continue
        line = stream.readline()
        if not line:
            break
        lines.append(line)
        if text in line:
            return lines
    raise AssertionError(f"did not receive {text!r}; received {lines!r}")


def _capacity_rss_values(log_path: Path) -> list[int]:
    if not log_path.exists():
        return []
    marker = "capacity: rss="
    values: list[int] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if marker not in line:
            continue
        value = line.split(marker, 1)[1].split("MB", 1)[0]
        try:
            values.append(int(value))
        except ValueError:
            continue
    return values


def _create_session(state_dir: Path, cwd: Path, prompt: str) -> tuple[str, int]:
    output_path = state_dir / "session-output.txt"
    result = _run_acpc(
        "prompt",
        "mock",
        prompt,
        "--quiet",
        "-o",
        str(output_path),
        "--print-session-id",
        "--cwd",
        str(cwd),
    )
    assert result.returncode == 0, result.stderr
    assert "[acpc] daemon: unavailable" not in result.stderr
    session_id = result.stdout.splitlines()[0].strip()
    assert session_id
    assert output_path.read_text(encoding="utf-8") == prompt
    daemon_pid = _read_lock(state_dir)["pid"]
    assert isinstance(daemon_pid, int)
    assert _pid_alive(daemon_pid)
    return session_id, daemon_pid


@pytest.fixture()
def integration_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Give each operational test an isolated state dir and mock agent registry."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    agents_dir.joinpath("mock.toml").write_text(
        f'''identity = "mock"
name = "Mock Agent"
author = "Test"
run_command = "{sys.executable} {MOCK_AGENT_SCRIPT}"
install_command = "true"
''',
        encoding="utf-8",
    )
    monkeypatch.setenv("ACPC_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ACPC_USER_AGENTS_DIR", str(agents_dir))
    yield tmp_path
    _stop_daemon_if_present(tmp_path)


def test_timeout_on_live_session(integration_state: Path) -> None:
    session_id, daemon_pid = _create_session(
        integration_state,
        Path.cwd().resolve(),
        "before-timeout",
    )

    timed_out = _run_acpc(
        "prompt",
        "mock",
        f"slow:{PROMPT_SECONDS}",
        "--timeout",
        str(REQUEST_TIMEOUT_SECONDS),
        "--quiet",
        "-s",
        session_id,
    )
    assert timed_out.returncode == 124, timed_out.stderr
    assert _pid_alive(daemon_pid)

    next_prompt = _run_acpc(
        "prompt",
        "mock",
        "after-timeout",
        "--quiet",
        "-s",
        session_id,
    )
    assert next_prompt.returncode == 0, next_prompt.stderr
    assert "[acpc] daemon: unavailable" not in next_prompt.stderr
    assert next_prompt.stdout == "after-timeout"
    assert _read_lock(integration_state)["pid"] == daemon_pid
    _stop_daemon(integration_state)


def test_resume_against_deleted_cwd(integration_state: Path) -> None:
    session_cwd = integration_state / "session-cwd"
    session_cwd.mkdir()
    session_id, _ = _create_session(integration_state, session_cwd, "before-delete")

    status_before = _run_acpc("daemon", "status", "mock")
    assert status_before.returncode == 0, status_before.stderr
    assert session_id in status_before.stdout
    assert str(session_cwd) in status_before.stdout
    shutil.rmtree(session_cwd)

    resumed = _run_acpc(
        "prompt",
        "mock",
        "after-delete",
        "--quiet",
        "-s",
        session_id,
    )
    assert resumed.returncode == 2
    assert "session cwd no longer exists" in resumed.stderr

    status_after = _run_acpc("daemon", "status", "mock")
    assert status_after.returncode == 0, status_after.stderr
    assert session_id not in status_after.stdout
    _stop_daemon(integration_state)


def test_log_file_contains_adapter_stderr_and_status_path(integration_state: Path) -> None:
    marker = "daemon-log-marker"
    result = _run_acpc("prompt", "mock", f"stderr:{marker}", "--quiet")
    assert result.returncode == 0, result.stderr
    assert "[acpc] daemon: unavailable" not in result.stderr

    status = _run_acpc("daemon", "status", "mock")
    assert status.returncode == 0, status.stderr
    log_path = integration_state / "log" / "mock.log"
    assert str(log_path) in status.stdout
    _wait_for(lambda: log_path.exists() and marker in log_path.read_text(encoding="utf-8"))
    assert marker in log_path.read_text(encoding="utf-8")
    _stop_daemon(integration_state)


def test_capacity_fallback_preserves_live_session(
    integration_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ACPC_DAEMON_RSS_MAX", str(CAPACITY_RSS_CEILING_MB))
    session_id, daemon_pid = _create_session(
        integration_state,
        Path.cwd().resolve(),
        f"hold:{CAPACITY_HELD_MEMORY_MB}",
    )
    existing = subprocess.Popen(
        [
            *ACPC_COMMAND,
            "prompt",
            "mock",
            f"chunkslow:{IN_FLIGHT_SECONDS}",
            "-s",
            session_id,
            "--json",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
    )
    try:
        assert existing.stdout is not None
        started_lines = _read_until(existing.stdout, '"text":"started"')
        assert any(f'"session_id":"{session_id}"' in line for line in started_lines)
        assert existing.poll() is None
        live_status = _run_acpc("daemon", "status", "mock")
        assert live_status.returncode == 0, live_status.stderr
        assert f"{session_id}  active" in live_status.stdout

        direct = _run_acpc("prompt", "mock", "new-session", "--quiet")
        assert direct.returncode == 0, direct.stderr
        assert direct.stdout == "new-session"
        assert "[acpc] daemon: at capacity, running direct" in direct.stderr
        assert "[acpc] daemon: unavailable" not in direct.stderr

        completed_stdout, completed_stderr = existing.communicate(timeout=DAEMON_TEST_TIMEOUT)
        assert existing.returncode == 0, completed_stderr
        assert "finished" in completed_stdout
        assert "[acpc] daemon: unavailable" not in completed_stderr
        capacity_log = integration_state / "log" / "mock.log"
        _wait_for(
            lambda: any(
                rss_mb > CAPACITY_RSS_CEILING_MB for rss_mb in _capacity_rss_values(capacity_log)
            )
        )
        socket_path, lock_path = _daemon_paths(integration_state)
        _wait_for(lambda: not socket_path.exists() and not lock_path.exists())
        _wait_for(lambda: not _pid_alive(daemon_pid))
        assert not _pid_alive(daemon_pid)
    finally:
        if existing.poll() is None:
            existing.kill()
            existing.wait(timeout=DAEMON_TEST_TIMEOUT)


def test_max_age_recycles_after_draining_in_flight_prompt(
    integration_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ACPC_DAEMON_MAX_AGE", str(TTL_SECONDS))
    inflight = subprocess.Popen(
        [
            *ACPC_COMMAND,
            "prompt",
            "mock",
            f"slow:{PROMPT_SECONDS}",
            "--quiet",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
    )
    try:
        socket_path, lock_path = _daemon_paths(integration_state)
        _wait_for(lambda: socket_path.exists() and lock_path.exists())
        daemon_pid = _read_lock(integration_state)["pid"]
        assert isinstance(daemon_pid, int)
        assert _pid_alive(daemon_pid)
        start_time = _read_lock(integration_state)["start_time"]
        assert isinstance(start_time, (int, float))
        _wait_for(lambda: time.time() - start_time >= TTL_SECONDS)
        assert inflight.poll() is None

        stdout, stderr = inflight.communicate(timeout=DAEMON_TEST_TIMEOUT)
        assert inflight.returncode == 0, stderr
        assert stdout == f"waited {PROMPT_SECONDS}s"
        _wait_for(lambda: not socket_path.exists() and not lock_path.exists())
        _wait_for(lambda: not _pid_alive(daemon_pid))
        assert not _pid_alive(daemon_pid)

        fresh = _run_acpc("prompt", "mock", "fresh-daemon", "--quiet")
        assert fresh.returncode == 0, fresh.stderr
        assert "[acpc] daemon: unavailable" not in fresh.stderr
        assert fresh.stdout == "fresh-daemon"
        fresh_pid = _read_lock(integration_state)["pid"]
        assert isinstance(fresh_pid, int)
        assert fresh_pid != daemon_pid
        _stop_daemon(integration_state)
    finally:
        if inflight.poll() is None:
            inflight.kill()
            inflight.wait(timeout=DAEMON_TEST_TIMEOUT)
