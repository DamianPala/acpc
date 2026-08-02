"""Integration tests for daemon lifecycle, failure recovery, and contention."""

from __future__ import annotations

import json
import os
import select
import signal
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
POLL_SECONDS = DAEMON_TEST_TIMEOUT / 1000
SHORT_SLEEP_SECONDS = DAEMON_TEST_TIMEOUT / 200
CLIENT_CANCEL_SECONDS = max(1, DAEMON_TEST_TIMEOUT // 15)

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


def _descendants(pid: int) -> set[int]:
    children_path = Path(f"/proc/{pid}/task/{pid}/children")
    try:
        children = [int(value) for value in children_path.read_text().split()]
    except (FileNotFoundError, OSError, ValueError):
        return set()
    descendants = set(children)
    for child in children:
        descendants.update(_descendants(child))
    return descendants


def _cmdline(pid: int) -> list[str]:
    try:
        data = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (FileNotFoundError, OSError):
        return []
    return [part.decode(errors="replace") for part in data.split(b"\0") if part]


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


@pytest.fixture()
def integration_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Give each lifecycle test an isolated state dir and mock agent registry."""
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


def _stop_daemon_if_present(state_dir: Path) -> None:
    socket_path, lock_path = _daemon_paths(state_dir)
    if socket_path.exists() or lock_path.exists():
        _run_acpc("daemon", "stop", "mock")


def test_idle_ttl_cleans_daemon_files(
    integration_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPC_DAEMON_TTL", str(TTL_SECONDS))
    result = _run_acpc("prompt", "mock", "ttl", "--quiet")
    assert result.returncode == 0, result.stderr
    assert "[acpc] daemon: unavailable" not in result.stderr
    socket_path, lock_path = _daemon_paths(integration_state)
    daemon_pid = _read_lock(integration_state)["pid"]
    assert _pid_alive(daemon_pid)
    _wait_for(lambda: not _pid_alive(daemon_pid))
    _wait_for(lambda: not socket_path.exists() and not lock_path.exists())


def test_killed_daemon_is_respawned_for_next_prompt(integration_state: Path) -> None:
    first = _run_acpc("prompt", "mock", "before kill", "--quiet")
    assert first.returncode == 0, first.stderr
    assert "[acpc] daemon: unavailable" not in first.stderr
    old_pid = _read_lock(integration_state)["pid"]
    assert _pid_alive(old_pid)
    old_adapter_pids = {
        pid for pid in _descendants(old_pid) if str(MOCK_AGENT_SCRIPT) in _cmdline(pid)
    }
    assert old_adapter_pids
    os.kill(old_pid, signal.SIGKILL)
    _wait_for(lambda: not _pid_alive(old_pid))
    _wait_for(lambda: all(not _pid_alive(pid) for pid in old_adapter_pids))

    second = _run_acpc("prompt", "mock", "after kill", "--quiet")
    assert second.returncode == 0, second.stderr
    assert "[acpc] daemon: unavailable" not in second.stderr
    assert second.stdout == "after kill"
    new_pid = _read_lock(integration_state)["pid"]
    assert new_pid != old_pid
    _stop_daemon(integration_state)
    assert all(not _pid_alive(pid) for pid in old_adapter_pids)


def test_no_daemon_flag_does_not_create_socket(integration_state: Path) -> None:
    result = _run_acpc("prompt", "mock", "direct", "--quiet", "--no-daemon")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "direct"
    socket_path, lock_path = _daemon_paths(integration_state)
    assert not socket_path.exists()
    assert not lock_path.exists()


def test_daemon_stop_kills_adapter_process_tree(integration_state: Path) -> None:
    result = _run_acpc("prompt", "mock", "start tree", "--quiet")
    assert result.returncode == 0, result.stderr
    assert "[acpc] daemon: unavailable" not in result.stderr
    daemon_pid = _read_lock(integration_state)["pid"]
    assert _pid_alive(daemon_pid)
    adapter_pids = {
        pid for pid in _descendants(daemon_pid) if str(MOCK_AGENT_SCRIPT) in _cmdline(pid)
    }
    assert adapter_pids, f"no mock adapter descendants under daemon {daemon_pid}"

    _stop_daemon(integration_state)
    _wait_for(lambda: not _pid_alive(daemon_pid))
    _wait_for(lambda: all(not _pid_alive(pid) for pid in adapter_pids))


def test_version_mismatch_restarts_daemon(integration_state: Path) -> None:
    first = _run_acpc("prompt", "mock", "old version", "--quiet")
    assert first.returncode == 0, first.stderr
    assert "[acpc] daemon: unavailable" not in first.stderr
    old_pid = _read_lock(integration_state)["pid"]
    assert _pid_alive(old_pid)
    _, lock_path = _daemon_paths(integration_state)
    metadata = _read_lock(integration_state)
    metadata["acpc_version"] = "0.0.0-test-mismatch"
    lock_path.write_text(json.dumps(metadata), encoding="utf-8")

    second = _run_acpc("prompt", "mock", "new version", "--quiet")
    assert second.returncode == 0, second.stderr
    assert "[acpc] daemon: unavailable" not in second.stderr
    assert second.stdout == "new version"
    new_pid = _read_lock(integration_state)["pid"]
    assert new_pid != old_pid
    _stop_daemon(integration_state)


def test_permissions_are_per_request_and_deny_effect(integration_state: Path) -> None:
    sentinel = integration_state / "sentinel.txt"
    denied = _run_acpc(
        "prompt",
        "mock",
        "write-file:sentinel.txt",
        "--quiet",
        "--permissions",
        "none",
        "--cwd",
        str(integration_state),
    )
    assert denied.returncode == 0, denied.stderr
    assert "[acpc] daemon: unavailable" not in denied.stderr
    assert "[acpc] permission: edit write sentinel.txt -> deny" in denied.stderr
    assert not sentinel.exists()
    daemon_pid = _read_lock(integration_state)["pid"]
    assert _pid_alive(daemon_pid)

    allowed = _run_acpc(
        "prompt",
        "mock",
        "write-file:sentinel.txt",
        "--quiet",
        "--permissions",
        "all",
        "--cwd",
        str(integration_state),
    )
    assert allowed.returncode == 0, allowed.stderr
    assert "[acpc] daemon: unavailable" not in allowed.stderr
    assert "[acpc] permission: edit write sentinel.txt -> allow" in allowed.stderr
    assert sentinel.read_text(encoding="utf-8") == "written by mock agent: sentinel.txt\n"
    assert _read_lock(integration_state)["pid"] == daemon_pid
    _stop_daemon(integration_state)


def _install_mock_event_probe(state_dir: Path) -> Path:
    probe_script = state_dir / "probed_mock_agent.py"
    events_path = state_dir / "mock-events.log"
    probe_script.write_text(
        "import asyncio\n"
        "import importlib.util\n"
        "import sys\n"
        "from pathlib import Path\n"
        "\n"
        f"events_path = Path({str(events_path)!r})\n"
        f"spec = importlib.util.spec_from_file_location('probed_mock_agent', {str(MOCK_AGENT_SCRIPT)!r})\n"
        "if spec is None or spec.loader is None:\n"
        "    raise RuntimeError('cannot load mock agent')\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "sys.modules[spec.name] = module\n"
        "spec.loader.exec_module(module)\n"
        "\n"
        "def record(event):\n"
        "    with events_path.open('a', encoding='utf-8') as event_file:\n"
        "        event_file.write(event + '\\n')\n"
        "\n"
        "class ProbedMockAgent(module.MockAgent):\n"
        "    async def cancel(self, session_id, **kwargs):\n"
        "        record(f'cancel:{session_id}')\n"
        "        await super().cancel(session_id, **kwargs)\n"
        "\n"
        "    async def _send_text(self, session_id, text):\n"
        "        record(f'text:{session_id}:{text}')\n"
        "        await super()._send_text(session_id, text)\n"
        "\n"
        "asyncio.run(module.run_agent(ProbedMockAgent()))\n",
        encoding="utf-8",
    )
    agents_path = state_dir / "agents" / "mock.toml"
    agents_path.write_text(
        f'''identity = "mock"
name = "Mock Agent"
author = "Test"
run_command = "{sys.executable} {probe_script}"
install_command = "true"
''',
        encoding="utf-8",
    )
    return events_path


def test_client_timeout_cancels_prompt_without_cross_client_output(
    integration_state: Path,
) -> None:
    events_path = _install_mock_event_probe(integration_state)
    first = subprocess.Popen(
        [
            *ACPC_COMMAND,
            "prompt",
            "mock",
            f"chunkslow:{PROMPT_SECONDS}",
            "--timeout",
            str(CLIENT_CANCEL_SECONDS),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
    )
    try:
        assert first.stdout is not None
        _read_until(first.stdout, "started")
        _wait_for(
            lambda: (
                events_path.exists()
                and any(
                    line.endswith(":started")
                    for line in events_path.read_text(encoding="utf-8").splitlines()
                )
            )
        )
        started_events = [
            line
            for line in events_path.read_text(encoding="utf-8").splitlines()
            if line.endswith(":started")
        ]
        assert len(started_events) == 1
        session_id = started_events[0].removeprefix("text:").removesuffix(":started")
        socket_path, lock_path = _daemon_paths(integration_state)
        assert socket_path.exists() and lock_path.exists()
        sibling = subprocess.run(
            [*ACPC_COMMAND, "prompt", "mock", "sibling", "--quiet"],
            capture_output=True,
            text=True,
            timeout=DAEMON_TEST_TIMEOUT,
            env=os.environ.copy(),
        )
        first.wait(timeout=DAEMON_TEST_TIMEOUT)
        assert first.returncode == 124
        _wait_for(
            lambda: (
                events_path.exists()
                and f"cancel:{session_id}\n" in events_path.read_text(encoding="utf-8")
            )
        )
        assert sibling.returncode == 0
        assert sibling.stdout == "sibling"
        assert "finished" not in sibling.stdout
        assert "[acpc] daemon: unavailable" not in sibling.stderr
        time.sleep(PROMPT_SECONDS + SHORT_SLEEP_SECONDS)
        events = events_path.read_text(encoding="utf-8")
        assert f"text:{session_id}:finished\n" not in events
    finally:
        if first.poll() is None:
            first.kill()
        first.wait(timeout=DAEMON_TEST_TIMEOUT)
    _stop_daemon(integration_state)


def test_queued_client_disconnect_is_removed_from_queue(integration_state: Path) -> None:
    setup = _run_acpc(
        "prompt",
        "mock",
        "create session",
        "--quiet",
        "-o",
        str(integration_state / "setup.txt"),
        "--print-session-id",
    )
    assert setup.returncode == 0
    session_id = setup.stdout.splitlines()[0]
    assert session_id
    socket_path, lock_path = _daemon_paths(integration_state)
    assert socket_path.exists() and lock_path.exists()
    caller_cwd = str(Path.cwd())

    active = subprocess.Popen(
        [
            *ACPC_COMMAND,
            "prompt",
            "mock",
            f"chunkslow:{PROMPT_SECONDS}",
            "--json",
            "-s",
            session_id,
            "--cwd",
            caller_cwd,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
    )
    assert active.stdout is not None
    _read_until(active.stdout, "started")
    queued: subprocess.Popen[str] | None = None
    remaining = None
    try:
        queued = subprocess.Popen(
            [
                *ACPC_COMMAND,
                "prompt",
                "mock",
                f"slow:{PROMPT_SECONDS}",
                "--quiet",
                "-o",
                str(integration_state / "queued.txt"),
                "-s",
                session_id,
                "--cwd",
                caller_cwd,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ.copy(),
        )
        assert queued.stderr is not None
        _read_until(queued.stderr, "queued (position 1)")

        remaining = subprocess.Popen(
            [
                *ACPC_COMMAND,
                "prompt",
                "mock",
                f"slow:{PROMPT_SECONDS}",
                "--quiet",
                "-o",
                str(integration_state / "remaining.txt"),
                "-s",
                session_id,
                "--cwd",
                caller_cwd,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ.copy(),
        )
        assert remaining.stderr is not None
        _read_until(remaining.stderr, "queued (position 2)")
        queued.kill()
        queued.wait(timeout=DAEMON_TEST_TIMEOUT)
        _read_until(remaining.stderr, "queued (position 1)")
        active.wait(timeout=DAEMON_TEST_TIMEOUT)
        remaining.wait(timeout=DAEMON_TEST_TIMEOUT)
        assert remaining.returncode == 0
        assert (integration_state / "remaining.txt").read_text() == f"waited {PROMPT_SECONDS}s"
        assert not (integration_state / "queued.txt").exists()
    finally:
        for process in (queued, remaining, active):
            if process is not None and process.poll() is None:
                process.kill()
            if process is not None:
                process.wait(timeout=DAEMON_TEST_TIMEOUT)
    _stop_daemon(integration_state)


def _install_daemon_pid_probe(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    probe_dir = state_dir / "probe"
    probe_dir.mkdir()
    log_path = probe_dir / "daemon-pids.log"
    adapter_log_path = probe_dir / "adapter-pids.log"
    probe_dir.joinpath("sitecustomize.py").write_text(
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "\n"
        "cmdline = Path('/proc/self/cmdline')\n"
        "parts = cmdline.read_bytes().split(b'\\0') if cmdline.exists() else []\n"
        "if 'acpc.daemon' in sys.argv or b'acpc.daemon' in parts:\n"
        f"    with Path({str(log_path)!r}).open('a', encoding='ascii') as marker:\n"
        "        marker.write(f'{os.getpid()}\\n')\n"
        f"if sys.argv and sys.argv[0] == {str(MOCK_AGENT_SCRIPT)!r}:\n"
        f"    with Path({str(adapter_log_path)!r}).open('a', encoding='ascii') as marker:\n"
        "        marker.write(f'{os.getpid()}\\n')\n",
        encoding="utf-8",
    )
    old_pythonpath = os.environ.get("PYTHONPATH")
    pythonpath = str(probe_dir)
    if old_pythonpath:
        pythonpath += os.pathsep + old_pythonpath
    monkeypatch.setenv("PYTHONPATH", pythonpath)
    return log_path


def test_concurrent_cold_starts_have_one_daemon_and_no_stderr(
    integration_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_path = _install_daemon_pid_probe(integration_state, monkeypatch)
    processes = [
        subprocess.Popen(
            [*ACPC_COMMAND, "prompt", "mock", f"cold answer {index}", "--quiet"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ.copy(),
        )
        for index in range(3)
    ]
    results = [process.communicate(timeout=DAEMON_TEST_TIMEOUT) for process in processes]
    for index, (stdout, stderr) in enumerate(results):
        assert processes[index].returncode == 0
        assert stdout == f"cold answer {index}"
        assert stderr == ""

    pid_lines = log_path.read_text(encoding="ascii").splitlines()
    daemon_pids = {int(pid) for pid in pid_lines}
    assert daemon_pids

    # A loser exits on its own schedule, unrelated to the winner answering the prompt,
    # so sampling the moment the clients return races with that exit. Bounding the wait
    # keeps the real requirement -- the losers do exit -- without asserting on timing.
    def _survivors() -> set[int]:
        return {pid for pid in daemon_pids if _pid_alive(pid)}

    _wait_for(lambda: len(_survivors()) == 1)
    surviving_daemons = _survivors()
    assert len(surviving_daemons) == 1
    lock_metadata = _read_lock(integration_state)
    assert surviving_daemons == {lock_metadata["pid"]}
    lock_path = _daemon_paths(integration_state)[1]
    assert lock_path.exists()
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        try:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            pytest.fail("surviving daemon does not hold the target lock")

    adapter_log = integration_state / "probe" / "adapter-pids.log"
    adapter_pids = {int(pid) for pid in adapter_log.read_text(encoding="ascii").splitlines()}
    assert len(adapter_pids) == 1
    assert adapter_pids <= _descendants(lock_metadata["pid"])
    _stop_daemon(integration_state)
