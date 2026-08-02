"""Live end-to-end tests with real ACP agents.

These tests call real agents (codex, claude) and cost API credits.
Skipped by default. Run explicitly:

    uv run pytest tests/test_live.py -v -m live

All tests use the cheapest available model per agent to minimize cost.
Estimated run time: ~3 minutes.

Isolation: each test gets a temporary ACPC state directory. Agent tests
also run with HOME=~/.agent-test-home to prevent loading user skills and
config. The directory must contain only the auth files needed by the
adapters. Known limitation: claude-agent-acp still loads the real
~/.claude/CLAUDE.md regardless of HOME override.
"""

from collections.abc import Callable, Iterator
import contextlib
from contextvars import ContextVar
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

# Use the installed entry point, not python -m (no __main__.py)
_acpc_bin = shutil.which("acpc")
assert _acpc_bin is not None, "acpc not installed in venv. Run: uv sync"
ACPC = [_acpc_bin]

# The project-wide pytest timeout is 5s, which suits unit tests and kills every
# test here mid-inference. Each subprocess call carries its own timeout, so this
# is only a backstop against a hung adapter.
LIVE_TEST_TIMEOUT_SECONDS = 300
DAEMON_REQUEST_TIMEOUT_SECONDS = 60
# A cancelled turn keeps generating on the adapter side, so the session stays
# busy for however long that answer takes. This budget covers the cancelled
# request finishing plus the next one being answered.
DAEMON_POST_CANCEL_TIMEOUT_SECONDS = 60
DAEMON_POLL_SECONDS = 0.1
DAEMON_WAIT_TIMEOUT_SECONDS = 30
DAEMON_IDLE_TTL_SECONDS = 3
DAEMON_SHUTDOWN_GRACE_SECONDS = 10
DAEMON_IDLE_WAIT_TIMEOUT_SECONDS = DAEMON_IDLE_TTL_SECONDS + DAEMON_SHUTDOWN_GRACE_SECONDS
TIMEOUT_REQUEST_SECONDS = 2

pytestmark = [pytest.mark.live, pytest.mark.timeout(LIVE_TEST_TIMEOUT_SECONDS)]

# Models used in live tests. Presets, not raw model ids: the advertised
# model list changes with adapter releases and account tier, so a hardcoded
# id silently rots into "Invalid params".
TEST_MODELS: dict[str, str] = {
    "codex": "fast",
    "claude": "fast",
}

# The second model for the per-request switch test. It has to differ from the
# fast preset to prove anything, and the other presets are the expensive tiers,
# which is a lot to pay for a one-word answer. A raw id is the compromise: if it
# rots, the test's own "failed to set model" assertion says so outright, which is
# the failure mode the comment above is really about.
SWITCH_MODELS: dict[str, str] = {
    "codex": "gpt-5.4-mini",
}

# Isolated agent env: no skills, no user config, just auth.
_AGENT_TEST_HOME = Path(os.environ.get("AGENT_TEST_HOME", Path.home() / ".agent-test-home"))
_CURRENT_TEST_ENV: ContextVar[dict[str, str] | None] = ContextVar("_CURRENT_TEST_ENV", default=None)
_TEST_ENV_KEYS = ("ACPC_STATE_DIR", "ACPC_NO_DAEMON", "HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR")


def _build_test_env(state_dir: Path, *, route_daemon: bool) -> dict[str, str]:
    """Build a per-test env with isolated ACPC and agent state.

    The direct/daemon choice is explicit so routing cannot depend on the
    developer's ambient environment.
    """
    env = dict(os.environ)
    env["ACPC_STATE_DIR"] = str(state_dir)
    if route_daemon:
        env.pop("ACPC_NO_DAEMON", None)
    else:
        env["ACPC_NO_DAEMON"] = "1"

    if _AGENT_TEST_HOME.exists():
        env["HOME"] = str(_AGENT_TEST_HOME)
        codex_dir = _AGENT_TEST_HOME / ".codex"
        if codex_dir.exists():
            env["CODEX_HOME"] = str(codex_dir)
        claude_dir = _AGENT_TEST_HOME / ".claude"
        if claude_dir.exists():
            env["CLAUDE_CONFIG_DIR"] = str(claude_dir)
    return env


def _install_test_env(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    """Install only the test-controlled environment keys in the current process."""
    for key in _TEST_ENV_KEYS:
        if key in env:
            monkeypatch.setenv(key, env[key])
        else:
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _isolated_test_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Give every live test isolated ACPC state and direct routing."""
    state_dir = tmp_path / "acpc-state"
    state_dir.mkdir()
    test_env = _build_test_env(state_dir, route_daemon=False)
    _install_test_env(test_env, monkeypatch)

    token = _CURRENT_TEST_ENV.set(test_env)
    try:
        yield
    finally:
        _CURRENT_TEST_ENV.reset(token)


@pytest.fixture()
def daemon_test_env(
    _isolated_test_env: None,
    agent: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[dict[str, str]]:
    """Opt a test into daemon routing while retaining autouse isolation."""
    direct_env = _active_test_env()
    state_dir = Path(direct_env["ACPC_STATE_DIR"])
    assert state_dir.is_relative_to(tmp_path)
    daemon_env = _build_test_env(state_dir, route_daemon=True)
    _install_test_env(daemon_env, monkeypatch)
    token = _CURRENT_TEST_ENV.set(daemon_env)
    try:
        yield daemon_env
    finally:
        _stop_daemon_if_present(agent, state_dir)
        _CURRENT_TEST_ENV.reset(token)


def _active_test_env() -> dict[str, str]:
    """Return the environment installed by the autouse fixture."""
    env = _CURRENT_TEST_ENV.get()
    if env is None:
        raise RuntimeError("live test helpers must run inside pytest")
    return env


def _run_acpc(
    *args: str,
    input_text: str | None = None,
    timeout: float = DAEMON_REQUEST_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run acpc with the environment selected for the current test."""
    return subprocess.run(
        [*ACPC, *args],
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_active_test_env(),
    )


def _run_acpc_direct(
    *args: str,
    input_text: str | None = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    """Run acpc directly with the current test's isolated environment."""
    return _run_acpc(*args, input_text=input_text, timeout=timeout)


def _run_acpc_cheap(
    agent: str,
    *args: str,
    input_text: str | None = None,
    timeout: float = DAEMON_REQUEST_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run a prompt with the configured cheapest model in the current route."""
    return _run_acpc(
        "prompt",
        agent,
        *args,
        "--model",
        TEST_MODELS[agent],
        input_text=input_text,
        timeout=timeout,
    )


def _run_acpc_cheap_direct(
    agent: str,
    *args: str,
    input_text: str | None = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    """Run a direct prompt with the cheapest model for the given agent."""
    model = TEST_MODELS.get(agent)
    model_args = ("--model", model) if model else ()
    return _run_acpc_direct(
        "prompt", agent, *args, *model_args, input_text=input_text, timeout=timeout
    )


def _extract_session_id(stderr: str) -> str | None:
    """Extract session ID from [acpc] session: <id> line."""
    match = re.search(r"\[acpc\] session: (.+)", stderr)
    return match.group(1).strip() if match else None


def _wait_for(
    condition: Callable[[], bool], *, timeout: float = DAEMON_WAIT_TIMEOUT_SECONDS
) -> None:
    """Poll a live-process condition until it becomes true."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(DAEMON_POLL_SECONDS)
    assert condition(), "condition did not become true before the test budget expired"


def _daemon_paths(agent: str) -> tuple[Path, Path]:
    """Return the isolated daemon socket and lock paths for one target."""
    state_dir = Path(_active_test_env()["ACPC_STATE_DIR"])
    run_dir = state_dir / "run"
    return run_dir / f"{agent}.sock", run_dir / f"{agent}.lock"


def _read_daemon_lock(agent: str) -> dict[str, object]:
    """Read and validate the daemon lock contract for one target."""
    _, lock_path = _daemon_paths(agent)
    metadata = json.loads(lock_path.read_text(encoding="utf-8"))
    assert isinstance(metadata, dict)
    assert metadata.get("target") == agent
    pid = metadata.get("pid")
    cmdline = metadata.get("cmdline")
    assert isinstance(pid, int) and pid != os.getpid()
    assert isinstance(cmdline, list)
    assert "acpc.daemon" in cmdline
    assert agent in cmdline
    return metadata


def _daemon_pid(agent: str) -> int:
    """Return the validated daemon PID from its lock metadata."""
    pid = _read_daemon_lock(agent).get("pid")
    assert isinstance(pid, int)
    return pid


def _daemon_status(agent: str) -> Path:
    """Check daemon status output and return its contract-advertised log path."""
    result = _run_acpc("daemon", "status", agent)
    assert result.returncode == 0, result.stderr
    status_line = next(
        (line for line in result.stdout.splitlines() if line.startswith(f"{agent}: pid ")),
        None,
    )
    assert status_line is not None, f"Missing daemon status line: {result.stdout!r}"
    prefix, separator, raw_log_path = status_line.partition(", log ")
    assert separator and prefix.startswith(f"{agent}: pid ")
    log_path = Path(raw_log_path)
    expected_path = Path(_active_test_env()["ACPC_STATE_DIR"]) / "log" / f"{agent}.log"
    assert log_path == expected_path
    return log_path


def _log_contains(log_path: Path, marker: str) -> bool:
    """Return whether a daemon log currently contains a marker."""
    try:
        return marker in log_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return False


def _assert_daemon_log(agent: str, marker: str = "daemon started") -> Path:
    """Assert the status/log contract and wait for one log marker."""
    log_path = _daemon_status(agent)
    _wait_for(lambda: _log_contains(log_path, marker))
    assert _log_contains(log_path, marker)
    return log_path


def _assert_daemon_routed(result: subprocess.CompletedProcess[str], agent: str) -> None:
    """Reject a successful direct fallback when a test requires daemon routing."""
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "[acpc] daemon: unavailable" not in result.stderr
    _, lock_path = _daemon_paths(agent)
    assert lock_path.exists(), f"Daemon lock was not created: {lock_path}"


def _process_stat(pid: int) -> tuple[str, int, int] | None:
    """Read Linux process state, parent PID, and process-group ID."""
    try:
        raw_stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    try:
        fields = raw_stat.rsplit(")", 1)[1].split()
        return fields[0], int(fields[1]), int(fields[2])
    except (IndexError, ValueError):
        return None


def _cmdline(pid: int) -> list[str] | None:
    """Return one process command line as exact argv elements."""
    try:
        raw_cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (FileNotFoundError, OSError):
        return None
    return [part.decode("utf-8", errors="replace") for part in raw_cmdline.split(b"\0") if part]


def _pid_alive(pid: int) -> bool:
    """Return whether a process exists and is not a zombie."""
    info = _process_stat(pid)
    if info is not None:
        return info[0] != "Z"
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def _process_snapshot() -> dict[int, tuple[str, int, int]]:
    """Return process metadata available through Linux procfs."""
    proc_dir = Path("/proc")
    if not proc_dir.exists():
        return {}
    snapshot: dict[int, tuple[str, int, int]] = {}
    for entry in proc_dir.iterdir():
        if not entry.name.isdecimal():
            continue
        pid = int(entry.name)
        info = _process_stat(pid)
        if info is not None:
            snapshot[pid] = info
    return snapshot


def _process_tree(root_pid: int) -> set[int]:
    """Return the root and all descendants visible in procfs."""
    snapshot = _process_snapshot()
    children: dict[int, list[int]] = {}
    for pid, (_, parent_pid, _) in snapshot.items():
        children.setdefault(parent_pid, []).append(pid)
    tree = {root_pid}
    pending = [root_pid]
    while pending:
        parent_pid = pending.pop()
        for child_pid in children.get(parent_pid, []):
            if child_pid not in tree:
                tree.add(child_pid)
                pending.append(child_pid)
    return tree


def _has_path_component(cmdline: list[str] | None, component: str) -> bool:
    """Match an executable path component without relying on list membership."""
    return bool(cmdline) and any(component in Path(argument).parts for argument in cmdline)


def _adapter_pids(pids: set[int], agent: str) -> set[int]:
    """Return adapter processes in a daemon tree by executable path."""
    adapter_name = f"{agent}-acp"
    return {pid for pid in pids if _has_path_component(_cmdline(pid), adapter_name)}


def _capture_daemon_tree(agent: str) -> tuple[int, set[int], set[int]]:
    """Capture a daemon tree after its real adapter child has appeared."""
    daemon_pid = _daemon_pid(agent)
    _wait_for(lambda: bool(_adapter_pids(_process_tree(daemon_pid), agent)))
    tree = _process_tree(daemon_pid)
    adapter_pids = _adapter_pids(tree, agent)
    assert adapter_pids, f"No {agent}-acp process in daemon tree: {tree}"
    groups = {info[2] for pid, info in _process_snapshot().items() if pid in tree}
    return daemon_pid, tree, groups


def _live_pids_in_groups(groups: set[int]) -> set[int]:
    """Return non-zombie processes still in captured process groups."""
    return {
        pid
        for pid, (state, _, process_group) in _process_snapshot().items()
        if state != "Z" and process_group in groups
    }


def _tree_is_gone(pids: set[int], groups: set[int]) -> bool:
    """Return whether captured PIDs and their process groups have exited."""
    return not any(_pid_alive(pid) for pid in pids) and not _live_pids_in_groups(groups)


def _daemon_endpoints_gone(agent: str) -> bool:
    """Return whether a daemon has removed both of its state endpoints."""
    socket_path, lock_path = _daemon_paths(agent)
    return not socket_path.exists() and not lock_path.exists()


def _stop_daemon_if_present(agent: str, state_dir: Path) -> None:
    """Best-effort fixture cleanup for a daemon left by a failed test."""
    socket_path = state_dir / "run" / f"{agent}.sock"
    lock_path = state_dir / "run" / f"{agent}.lock"
    if not socket_path.exists() and not lock_path.exists():
        return
    with contextlib.suppress(Exception):
        result = _run_acpc("daemon", "stop", agent)
        if result.returncode == 0:
            _wait_for(lambda: not socket_path.exists() and not lock_path.exists())


def _new_session(agent: str, tmp_path: Path, label: str, prompt: str) -> str:
    """Create one routed session and obtain its ID without another billed turn."""
    output_path = tmp_path / f"{label}.txt"
    result = _run_acpc_cheap(
        agent,
        prompt,
        "--permissions",
        "none",
        "--quiet",
        "-o",
        str(output_path),
        "--print-session-id",
    )
    _assert_daemon_routed(result, agent)
    session_id = result.stdout.splitlines()[0].strip() if result.stdout.splitlines() else ""
    assert session_id, f"No session ID in stdout: {result.stdout!r}"
    assert output_path.exists() and output_path.read_text(encoding="utf-8").strip()
    return session_id


def _run_concurrent_prompts(
    agent: str,
    requests: tuple[tuple[str, ...], tuple[str, ...]],
    observer: Callable[[], None],
) -> list[subprocess.CompletedProcess[str]]:
    """Run two routed prompts at once, calling observer while they are in flight."""
    processes = [
        subprocess.Popen(
            [*ACPC, "prompt", agent, *args, "--model", TEST_MODELS[agent]],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_active_test_env(),
        )
        for args in requests
    ]
    deadline = time.perf_counter() + DAEMON_REQUEST_TIMEOUT_SECONDS
    try:
        while any(process.poll() is None for process in processes):
            if time.perf_counter() > deadline:
                raise AssertionError("Concurrent prompts did not finish in time")
            observer()
            time.sleep(DAEMON_POLL_SECONDS)
        return [
            subprocess.CompletedProcess(process.args, process.returncode, *process.communicate())
            for process in processes
        ]
    except BaseException:
        for process in processes:
            if process.poll() is None:
                process.kill()
        for process in processes:
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.communicate(timeout=DAEMON_POST_CANCEL_TIMEOUT_SECONDS)
        raise


@pytest.mark.parametrize("agent", ["codex"], ids=["codex"])
class TestDaemonLive:
    """Phase 4 coverage for one live, daemon-routed adapter."""

    @pytest.fixture(autouse=True)
    def _route_daemon_for_class(self, daemon_test_env: dict[str, str]) -> None:
        """Make every test in this class explicit about the safe isolated route."""
        assert Path(daemon_test_env["ACPC_STATE_DIR"]).is_absolute()
        assert "ACPC_NO_DAEMON" not in daemon_test_env

    def test_second_prompt_reuses_daemon_and_adapter(
        self,
        agent: str,
    ) -> None:
        """A second prompt is served by the same daemon and adapter processes.

        Reuse is asserted on process identity rather than on the clock. The
        regression worth catching -- a daemon or adapter respawned per call --
        changes these PIDs outright. Wall-clock does not discriminate here:
        after the adapter migration, startup is roughly a second, and measured
        against inference that varies by more than that in both directions. Two
        consecutive runs gave cold 4.11s / warm 2.91s and then cold 3.70s /
        warm 5.05s, with the daemon provably reused in both.
        """
        cold = _run_acpc_cheap(
            agent,
            "Respond with exactly one word: cold",
            "--permissions",
            "none",
            "--quiet",
        )
        _assert_daemon_routed(cold, agent)
        cold_daemon_pid, cold_tree, _ = _capture_daemon_tree(agent)
        cold_adapter_pids = _adapter_pids(cold_tree, agent)

        warm = _run_acpc_cheap(
            agent,
            "--last",
            "Respond with exactly one word: warm",
            "--permissions",
            "none",
            "--quiet",
        )
        _assert_daemon_routed(warm, agent)

        warm_daemon_pid, warm_tree, _ = _capture_daemon_tree(agent)
        assert warm_daemon_pid == cold_daemon_pid, "Daemon was respawned between calls"
        assert _adapter_pids(warm_tree, agent) == cold_adapter_pids, (
            "Adapter was respawned between calls"
        )
        assert warm.stdout.strip()
        _assert_daemon_log(agent)

    def test_two_sessions_run_concurrently(
        self,
        agent: str,
        tmp_path: Path,
    ) -> None:
        """The daemon holds two sessions in flight at once instead of queueing one.

        Asserted by observing both sessions active at the same instant, not by
        comparing wall-clock against a solo baseline. Two simultaneous requests
        on one account stagger by seconds even on the direct path, where the
        adapters are separate processes and cannot serialize each other, so a
        timing ratio here measures the account, not the daemon.
        """
        session_a = _new_session(agent, tmp_path, "session-a", "Reply with exactly: ready-a")
        session_b = _new_session(agent, tmp_path, "session-b", "Reply with exactly: ready-b")

        both_active_seen = False

        def _observe_status() -> None:
            nonlocal both_active_seen
            if both_active_seen:
                return
            status = _run_acpc("daemon", "status", agent, timeout=DAEMON_WAIT_TIMEOUT_SECONDS)
            both_active_seen = all(
                f"{session_id}  active" in status.stdout for session_id in (session_a, session_b)
            )

        concurrent = _run_concurrent_prompts(
            agent,
            (
                (
                    "-s",
                    session_a,
                    "Reply with exactly: concurrent-a",
                    "--permissions",
                    "none",
                    "--quiet",
                ),
                (
                    "-s",
                    session_b,
                    "Reply with exactly: concurrent-b",
                    "--permissions",
                    "none",
                    "--quiet",
                ),
            ),
            _observe_status,
        )
        for result, response in zip(concurrent, ("concurrent-a", "concurrent-b"), strict=True):
            _assert_daemon_routed(result, agent)
            assert response in result.stdout

        assert both_active_seen, "The daemon never had both sessions active at once"
        _assert_daemon_log(agent)

    def test_timeout_keeps_live_session_usable(
        self,
        agent: str,
        tmp_path: Path,
    ) -> None:
        """A timed-out real request cancels one turn without killing the session."""
        session_id = _new_session(agent, tmp_path, "timeout-session", "Reply with exactly: ready")
        daemon_pid, tree_pids, process_groups = _capture_daemon_tree(agent)
        adapter_pids = _adapter_pids(tree_pids, agent)
        _assert_daemon_log(agent)

        timed_out = _run_acpc_cheap(
            agent,
            "-s",
            session_id,
            # Long enough to still be generating at the timeout, short enough
            # that the session frees up again quickly. Cancelling does not stop
            # the adapter generating, so the size of this answer sets how long
            # the session stays busy: a 5000-word request wedged it for 151s.
            "Write one paragraph of about 80 words explaining what a compiler does.",
            "--permissions",
            "none",
            "--quiet",
            "--timeout",
            str(TIMEOUT_REQUEST_SECONDS),
            timeout=DAEMON_POST_CANCEL_TIMEOUT_SECONDS,
        )
        assert timed_out.returncode == 124, timed_out.stderr
        assert _pid_alive(daemon_pid)
        assert any(_pid_alive(pid) for pid in adapter_pids), (
            f"Adapter exited after timeout: {sorted(adapter_pids)}"
        )

        next_prompt = _run_acpc_cheap(
            agent,
            "-s",
            session_id,
            "Respond with exactly one word: recovered",
            "--permissions",
            "none",
            "--quiet",
            timeout=DAEMON_POST_CANCEL_TIMEOUT_SECONDS,
        )
        _assert_daemon_routed(next_prompt, agent)
        assert "recovered" in next_prompt.stdout
        _assert_daemon_log(agent)
        assert not _tree_is_gone(tree_pids, process_groups)

    def test_last_preserves_context_on_routed_session(self, agent: str) -> None:
        """The daemon-routed --last path keeps the live session's conversation."""
        first = _run_acpc_cheap(
            agent,
            "For this test, remember the code word is 'Saffron'. Reply with just the code word.",
            "--permissions",
            "none",
            "--quiet",
        )
        _assert_daemon_routed(first, agent)

        second = _run_acpc_cheap(
            agent,
            "--last",
            "What code word did I give you? Reply with just the code word.",
            "--permissions",
            "none",
            "--quiet",
        )
        _assert_daemon_routed(second, agent)
        assert "saffron" in second.stdout.lower(), f"Context was lost: {second.stdout!r}"
        _assert_daemon_log(agent)

    def test_model_switch_is_applied_per_request(
        self,
        agent: str,
        tmp_path: Path,
    ) -> None:
        """A live session accepts a new model request without stale-model warnings."""
        session_id = _new_session(agent, tmp_path, "model-session", "Reply with exactly: first")
        switched = _run_acpc(
            "prompt",
            agent,
            "-s",
            session_id,
            "Respond with exactly one word: switched",
            "--model",
            SWITCH_MODELS[agent],
            "--permissions",
            "none",
            "--quiet",
        )
        _assert_daemon_routed(switched, agent)
        assert "switched" in switched.stdout
        log_path = _assert_daemon_log(agent)
        assert "warning: failed to set model" not in log_path.read_text(encoding="utf-8")

    def test_idle_ttl_removes_daemon_process_tree(
        self,
        agent: str,
        daemon_test_env: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """After the configured idle TTL, daemon and adapter processes are gone."""
        monkeypatch.setenv("ACPC_DAEMON_TTL", str(DAEMON_IDLE_TTL_SECONDS))
        daemon_test_env["ACPC_DAEMON_TTL"] = str(DAEMON_IDLE_TTL_SECONDS)
        result = _run_acpc_cheap(
            agent,
            "Respond with exactly one word: expire",
            "--permissions",
            "none",
            "--quiet",
        )
        _assert_daemon_routed(result, agent)
        daemon_pid, tree_pids, process_groups = _capture_daemon_tree(agent)
        assert _pid_alive(daemon_pid)
        _assert_daemon_log(agent)

        _wait_for(
            lambda: _daemon_endpoints_gone(agent) and _tree_is_gone(tree_pids, process_groups),
            timeout=DAEMON_IDLE_WAIT_TIMEOUT_SECONDS,
        )
        assert _daemon_endpoints_gone(agent)
        assert _tree_is_gone(tree_pids, process_groups)

    def test_daemon_stop_removes_all_adapter_processes(
        self,
        agent: str,
    ) -> None:
        """Explicit daemon stop removes the daemon, node adapter, and binary tree."""
        result = _run_acpc_cheap(
            agent,
            "Respond with exactly one word: stop",
            "--permissions",
            "none",
            "--quiet",
        )
        _assert_daemon_routed(result, agent)
        daemon_pid, tree_pids, process_groups = _capture_daemon_tree(agent)
        _assert_daemon_log(agent)

        stopped = _run_acpc("daemon", "stop", agent)
        assert stopped.returncode == 0, stopped.stderr
        assert f"stopped daemon {agent}" in stopped.stdout
        _wait_for(
            lambda: _daemon_endpoints_gone(agent) and _tree_is_gone(tree_pids, process_groups)
        )
        assert not _pid_alive(daemon_pid)
        assert _daemon_endpoints_gone(agent)
        assert _tree_is_gone(tree_pids, process_groups)

        status_after = _run_acpc("daemon", "status", agent)
        assert status_after.returncode == 1
        assert "not running" in status_after.stderr.lower()


# ---------------------------------------------------------------------------
# Tier 1: Tests that catch real bugs (mock agent can't find these)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("agent", ["codex", "claude"])
class TestHelloWorld:
    """Minimal smoke test: agent responds to a trivial prompt."""

    def test_hello_response(self, agent: str) -> None:
        """Agent returns a non-empty response."""
        result = _run_acpc_cheap_direct(
            agent, "Respond with exactly one word: hello", "--permissions", "none", "--quiet"
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert len(result.stdout.strip()) > 0, "Expected non-empty response"

    def test_session_id_emitted(self, agent: str) -> None:
        """Session ID and resume hint appear on stderr."""
        result = _run_acpc_cheap_direct(
            agent, "Respond with exactly one word: hi", "--permissions", "none", "--quiet"
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "[acpc] session:" in result.stderr
        assert "[acpc] resume:" in result.stderr


@pytest.mark.parametrize("agent", ["codex", "claude"])
class TestMultiTurn:
    """Multi-turn session: does the agent actually remember context?"""

    def test_context_preserved_with_session_id(self, agent: str) -> None:
        """Agent remembers context when resuming by session ID."""
        model = TEST_MODELS.get(agent)
        model_args = ("--model", model) if model else ()

        # Turn 1: give agent a fact to remember
        r1 = _run_acpc_direct(
            "prompt",
            agent,
            "For this test session, the project name we are working on is 'FizzBuzz'. "
            "Please confirm by responding with just the project name.",
            *model_args,
            "--permissions",
            "none",
            "--quiet",
        )
        assert r1.returncode == 0, f"Turn 1 failed: {r1.stderr}"
        session_id = _extract_session_id(r1.stderr)
        assert session_id, f"No session ID in stderr: {r1.stderr}"

        # Turn 2: ask for the fact back (same model, resume session)
        r2 = _run_acpc_direct(
            "prompt",
            agent,
            "-s",
            session_id,
            "What project name did I mention earlier in this session?",
            *model_args,
            "--permissions",
            "none",
            "--quiet",
        )
        assert r2.returncode == 0, f"Turn 2 failed: {r2.stderr}"
        assert "fizzbuzz" in r2.stdout.lower(), f"Agent forgot context. Got: {r2.stdout!r}"


@pytest.mark.parametrize("agent", ["codex", "claude"])
class TestJsonOutput:
    """JSON output mode produces valid NDJSON with required events."""

    def test_ndjson_structure(self, agent: str) -> None:
        """JSON output is valid NDJSON with session lifecycle events."""
        result = _run_acpc_cheap_direct(
            agent, "Respond with exactly: json test ok", "--permissions", "none", "--json"
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

        lines = []
        for line in result.stdout.strip().split("\n"):
            if line.strip():
                parsed = json.loads(line)
                lines.append(parsed)

        assert len(lines) >= 3, f"Expected at least 3 NDJSON lines, got {len(lines)}"

        acpc_events = [ln.get("acpc") for ln in lines if "acpc" in ln]
        assert "session_started" in acpc_events, f"Missing session_started. Events: {acpc_events}"
        assert "session_ended" in acpc_events, f"Missing session_ended. Events: {acpc_events}"

        updates = [ln.get("sessionUpdate") for ln in lines if "sessionUpdate" in ln]
        assert "agent_message_chunk" in updates, f"No agent_message_chunk. Updates: {updates}"


@pytest.mark.parametrize("agent", ["codex", "claude"])
class TestToolCallPermissions:
    """Permission enforcement with real agent tool calls."""

    def test_permissions_all_allows_read(self, agent: str) -> None:
        """With --permissions all, agent can read files."""
        result = _run_acpc_cheap_direct(
            agent,
            "Read the file pyproject.toml in the current directory "
            "and tell me the project name. Reply with just the name.",
            "--permissions",
            "all",
            "--quiet",
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "acpc" in result.stdout.lower(), f"Expected 'acpc' in response: {result.stdout!r}"

    def test_permissions_none_denies_tools(self, agent: str) -> None:
        """With --permissions none, tool calls are denied."""
        result = _run_acpc_cheap_direct(
            agent,
            "Read the file pyproject.toml and tell me the version.",
            "--permissions",
            "none",
            "--quiet",
        )
        assert result.returncode in (0, 1), f"Unexpected exit: {result.returncode}, {result.stderr}"
        if "[acpc] permission:" in result.stderr:
            assert "deny" in result.stderr.lower()


# ---------------------------------------------------------------------------
# Tier 2: Edge cases and secondary features
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "agent,model,expected",
    [
        ("claude", "haiku", "haiku"),
    ],
)
class TestModelSelection:
    """Verify --model flag changes the model (uses specific models, not TEST_MODELS defaults)."""

    def test_model_identifies_itself(self, agent: str, model: str, expected: str) -> None:
        """Agent reports using the requested model."""
        result = _run_acpc_direct(
            "prompt",
            agent,
            "What AI model are you? Reply with just your model name/identifier, nothing else.",
            "--model",
            model,
            "--permissions",
            "none",
            "--quiet",
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert expected in result.stdout.lower(), (
            f"Expected '{expected}' in model self-identification. Got: {result.stdout!r}"
        )


@pytest.mark.parametrize("agent", ["codex"])
class TestModelSelectionUnsupported:
    """Verify --model with unsupported adapter doesn't crash."""

    def test_unsupported_model_warns(self, agent: str) -> None:
        """Agent that doesn't support set_model still works (warns on stderr)."""
        result = _run_acpc_direct(
            "prompt",
            agent,
            "Respond with exactly one word: hello",
            "--model",
            "o3-mini",
            "--permissions",
            "none",
            "--quiet",
        )
        assert result.returncode in (0, 1), f"Unexpected exit: {result.returncode}"
        if result.returncode == 1:
            assert "error" in result.stderr.lower()


@pytest.mark.parametrize("agent", ["codex", "claude"])
class TestOutputFile:
    """Output file (-o) with real agent."""

    def test_output_written_to_file(self, agent: str, tmp_path: Path) -> None:
        """Agent response is written to output file."""
        out_file = tmp_path / "response.txt"
        result = _run_acpc_cheap_direct(
            agent,
            "Respond with exactly: file output works",
            "--permissions",
            "none",
            "-o",
            str(out_file),
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert out_file.exists(), "Output file not created"
        content = out_file.read_text()
        assert len(content.strip()) > 0, "Output file is empty"


@pytest.mark.parametrize("agent", ["codex", "claude"])
class TestStdinPipe:
    """Stdin pipe input with real agent."""

    def test_stdin_pipe_input(self, agent: str) -> None:
        """Piped stdin is used as prompt text."""
        result = _run_acpc_cheap_direct(
            agent,
            "-",
            "--permissions",
            "none",
            "--quiet",
            input_text="Respond with exactly one word: piped",
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert len(result.stdout.strip()) > 0, "Expected non-empty response from piped input"


@pytest.mark.parametrize("agent", ["codex", "claude"])
class TestInputFile:
    """Input file (--input-file) with real agent."""

    def test_reads_prompt_from_file(self, agent: str, tmp_path: Path) -> None:
        """Prompt text read from file."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Respond with exactly one word: filed")
        result = _run_acpc_cheap_direct(
            agent, "--input-file", str(prompt_file), "--permissions", "none", "--quiet"
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert len(result.stdout.strip()) > 0, "Expected non-empty response from file input"


@pytest.mark.parametrize("agent", ["codex", "claude"])
class TestTimeout:
    """Timeout flag with real agent."""

    def test_timeout_exits_124(self, agent: str) -> None:
        """Very short timeout causes exit 124."""
        result = _run_acpc_cheap_direct(
            agent,
            "Write a detailed 5000-word analysis of the history of computing, "
            "covering every decade from the 1940s to 2020s with specific examples.",
            "--permissions",
            "none",
            "--quiet",
            "--timeout",
            "2",
        )
        assert result.returncode == 124, (
            f"Expected exit 124 (timeout), got {result.returncode}. stderr: {result.stderr}"
        )


# ---------------------------------------------------------------------------
# Tier 3: Process lifecycle and session management
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("agent", ["codex", "claude"])
class TestResumeWithLast:
    """--last flag resumes the most recent session."""

    def test_last_resumes_session(self, agent: str) -> None:
        """--last picks up context from the previous session."""
        model = TEST_MODELS.get(agent)
        model_args = ("--model", model) if model else ()

        # Turn 1: establish a fact
        r1 = _run_acpc_direct(
            "prompt",
            agent,
            "For this test, the color is 'Magenta'. Confirm by saying just the color.",
            *model_args,
            "--permissions",
            "none",
            "--quiet",
        )
        assert r1.returncode == 0, f"Turn 1 failed: {r1.stderr}"

        # Turn 2: resume with --last
        r2 = _run_acpc_direct(
            "prompt",
            agent,
            "--last",
            "What color did I mention? Reply with just the color.",
            *model_args,
            "--permissions",
            "none",
            "--quiet",
        )
        assert r2.returncode == 0, f"Turn 2 failed: {r2.stderr}"
        assert "magenta" in r2.stdout.lower(), f"Agent forgot context. Got: {r2.stdout!r}"


@pytest.mark.parametrize("agent", ["codex", "claude"])
class TestProcessCleanup:
    """Adapter processes are cleaned up after exit (no orphans)."""

    def test_no_orphans_after_normal_exit(self, agent: str) -> None:
        """No adapter processes remain after a normal prompt completes."""
        result = _run_acpc_cheap_direct(
            agent, "Respond with exactly one word: cleanup", "--permissions", "none", "--quiet"
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        time.sleep(2)

        # Check for orphaned adapter processes
        ps = subprocess.run(
            ["pgrep", "-f", f"{agent}-acp|{agent}-agent-acp"],
            capture_output=True,
            text=True,
        )
        # pgrep exit 1 = no matches (good), exit 0 = matches found (bad)
        orphan_pids = ps.stdout.strip().split("\n") if ps.stdout.strip() else []
        assert len(orphan_pids) == 0, f"Orphan {agent} adapter processes found: {orphan_pids}"


class TestStatus:
    """acpc status command."""

    def test_status_no_sessions(self) -> None:
        """Status with no running sessions exits cleanly."""
        result = _run_acpc_direct("status")
        assert result.returncode == 0, f"stderr: {result.stderr}"
