"""Tests for the daemon client protocol and readiness behavior."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from acpc import __version__
from acpc.cli import cli
from acpc.daemon import Daemon
from acpc.daemon_client import (
    DaemonClient,
    DaemonUnavailableError,
    cancel_daemon_prompt,
    daemon_status,
    shutdown_daemon,
)
from acpc.ipc import Connection, UnixSocketTransport, lock_path_for_target, socket_path_for_target
from acpc.output import OutputHandler, OutputMode
from acpc.sessions import load_last_session, load_session_cwd, process_cmdline, process_start_time


@pytest.fixture()
def daemon_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Give every client test an isolated state directory."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("ACPC_STATE_DIR", str(state_dir))
    return state_dir


class MockDaemon:
    """Small Unix-socket server whose frames are controlled by each test."""

    def __init__(self, target: str, responses: list[dict[str, Any]]) -> None:
        self.target = target
        self.transport = UnixSocketTransport(target)
        self.responses = responses
        self.received: list[dict[str, Any]] = []
        self._task: asyncio.Task[None] | None = None
        self._lock_file: Any | None = None

    async def start(self) -> None:
        await self._acquire_lock()
        await self.transport.bind()
        self._task = asyncio.create_task(self._serve())

    async def wait(self) -> None:
        if self._task is not None:
            await self._task

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self.transport.cleanup()
        self._release_lock()

    async def start_listener(self) -> None:
        """Bind a listener while holding the same lock as a real daemon."""
        await self._acquire_lock()
        await self.transport.bind()

    async def _acquire_lock(self) -> None:
        if self._lock_file is not None:
            return
        path = lock_path_for_target(self.target)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file = path.open("a+", encoding="utf-8")
        fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        json.dump(
            {
                "pid": os.getpid(),
                "socket": str(socket_path_for_target(self.target)),
                "acpc_version": __version__,
                "target": self.target,
            },
            self._lock_file,
        )
        self._lock_file.flush()

    def _release_lock(self) -> None:
        if self._lock_file is None:
            return
        path = Path(self._lock_file.name)
        fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
        self._lock_file.close()
        self._lock_file = None
        with contextlib.suppress(FileNotFoundError):
            path.unlink()

    async def _serve(self) -> None:
        connection = await self.transport.accept()
        try:
            self.received.append(await self.transport.receive(connection))
            for response in self.responses:
                await self.transport.send(connection, response)
        finally:
            with contextlib.suppress(ConnectionError, OSError, ValueError):
                await self.transport.close_connection(connection)


class CancelDaemon(MockDaemon):
    """Daemon fixture that handles cancellation and can serve a follow-up call."""

    def __init__(self, target: str, *, ignore_cancel: bool = False) -> None:
        super().__init__(target, [])
        self.ignore_cancel = ignore_cancel
        self.cancel_received = asyncio.Event()
        self.started = asyncio.Event()
        self._serve_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self._acquire_lock()
        await self.transport.bind()
        self._serve_task = asyncio.create_task(self._serve_cancellable())

    async def close(self) -> None:
        if self._serve_task is not None:
            self._serve_task.cancel()
            await asyncio.gather(self._serve_task, return_exceptions=True)
        await self.transport.cleanup()
        self._release_lock()

    async def _serve_cancellable(self) -> None:
        turn = 0
        try:
            while True:
                connection = await self.transport.accept()
                self.received.append(await self.transport.receive(connection))
                turn += 1
                await self.transport.send(
                    connection,
                    {"type": "session_started", "session_id": "sess-cancel"},
                )
                self.started.set()
                if turn > 1:
                    await self.transport.send(
                        connection,
                        {
                            "type": "prompt_done",
                            "session_id": "sess-cancel",
                            "stop_reason": "end_turn",
                        },
                    )
                    continue
                cancel = await self.transport.receive(connection)
                self.received.append(cancel)
                if cancel.get("type") == "cancel":
                    self.cancel_received.set()
                    if not self.ignore_cancel:
                        await self.transport.send(
                            connection,
                            {
                                "type": "prompt_done",
                                "session_id": "sess-cancel",
                                "stop_reason": "cancelled",
                                "cancel_source": "client",
                            },
                        )
                else:
                    await self.transport.send(
                        connection,
                        {
                            "type": "prompt_done",
                            "session_id": "sess-cancel",
                            "stop_reason": "end_turn",
                        },
                    )
        except asyncio.CancelledError:
            raise


def _update(text: str, session_id: str = "sess-1") -> dict[str, Any]:
    return {
        "type": "session_update",
        "session_id": session_id,
        "update": {
            "session_update": "agent_message_chunk",
            "content": {"type": "text", "text": text},
        },
    }


def _round_trip_responses() -> list[dict[str, Any]]:
    return [
        {"type": "session_started", "session_id": "sess-1", "reused": "new"},
        _update("hello"),
        {"type": "prompt_done", "session_id": "sess-1", "stop_reason": "end_turn"},
    ]


@pytest.mark.parametrize("mode", [OutputMode.TEXT, OutputMode.JSON, OutputMode.QUIET])
def test_prompt_round_trip_preserves_output_modes(
    daemon_environment: Path,
    capsys: pytest.CaptureFixture[str],
    mode: OutputMode,
) -> None:
    async def scenario() -> None:
        daemon = MockDaemon("mock", _round_trip_responses())
        await daemon.start()
        try:
            output = OutputHandler(mode)
            exit_code = await asyncio.wait_for(
                DaemonClient("mock").prompt(
                    text="say hello",
                    cwd=str(daemon_environment),
                    permissions="read",
                    output=output,
                    output_mode=mode.value,
                ),
                timeout=1,
            )
            assert exit_code == 0
            assert daemon.received[0]["output_mode"] == mode.value
        finally:
            await daemon.close()

    asyncio.run(scenario())
    captured = capsys.readouterr()
    if mode is OutputMode.TEXT:
        assert captured.out == "hello"
    elif mode is OutputMode.QUIET:
        assert captured.out == "hello"
    else:
        events = [json.loads(line) for line in captured.out.splitlines()]
        assert events == [
            {"acpc": "session_started", "session_id": "sess-1"},
            {
                "_meta": None,
                "content": {
                    "_meta": None,
                    "annotations": None,
                    "text": "hello",
                    "type": "text",
                },
                "sessionUpdate": "agent_message_chunk",
            },
            {
                "acpc": "session_ended",
                "session_id": "sess-1",
                "stop_reason": "end_turn",
                "exit_code": 0,
            },
        ]


def test_queued_is_progress_on_stderr_not_agent_output(
    daemon_environment: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def scenario() -> None:
        daemon = MockDaemon(
            "mock",
            [
                {"type": "session_started", "session_id": "sess-1"},
                {"type": "queued", "position": 2},
                _update("answer"),
                {"type": "prompt_done", "session_id": "sess-1", "stop_reason": "end_turn"},
            ],
        )
        await daemon.start()
        try:
            exit_code = await asyncio.wait_for(
                DaemonClient("mock").prompt(
                    text="queued",
                    cwd=str(daemon_environment),
                    permissions="read",
                    output=OutputHandler(OutputMode.TEXT),
                ),
                timeout=1,
            )
            assert exit_code == 0
        finally:
            await daemon.close()

    asyncio.run(scenario())
    captured = capsys.readouterr()
    assert captured.out == "answer"
    assert "queued (position 2)" in captured.err


def test_error_frame_returns_frame_exit_code(
    daemon_environment: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def scenario() -> int:
        daemon = MockDaemon(
            "mock",
            [
                {"type": "session_started", "session_id": "sess-1"},
                {
                    "type": "error",
                    "session_id": "sess-1",
                    "message": "adapter failed",
                    "exit_code": 17,
                },
            ],
        )
        await daemon.start()
        try:
            return await asyncio.wait_for(
                DaemonClient("mock").prompt(
                    text="fail",
                    cwd=str(daemon_environment),
                    permissions="read",
                    output=OutputHandler(OutputMode.JSON),
                ),
                timeout=1,
            )
        finally:
            await daemon.close()

    assert asyncio.run(scenario()) == 17
    captured = capsys.readouterr()
    assert "adapter failed" in captured.err
    assert [json.loads(line) for line in captured.out.splitlines()] == [
        {"acpc": "session_started", "session_id": "sess-1"},
        {
            "acpc": "session_error",
            "session_id": "sess-1",
            "error": "adapter failed",
        },
    ]


def test_shutting_down_retries_directly_once(
    daemon_environment: Path,
) -> None:
    calls: list[str] = []

    async def direct_retry() -> int:
        calls.append("direct")
        return 23

    async def scenario() -> int:
        daemon = MockDaemon("mock", [{"type": "shutting_down"}])
        await daemon.start()
        try:
            return await asyncio.wait_for(
                DaemonClient("mock").prompt(
                    text="retry",
                    cwd=str(daemon_environment),
                    permissions="read",
                    output=OutputHandler(OutputMode.TEXT),
                    direct_retry=direct_retry,
                ),
                timeout=1,
            )
        finally:
            await daemon.close()

    assert asyncio.run(scenario()) == 23
    assert calls == ["direct"]


def test_real_cold_start_cli_terminates_with_captured_output(
    daemon_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_agent_dir: Path,
) -> None:
    """A real auto-started daemon must not keep the CLI's capture pipes open."""
    monkeypatch.setenv("ACPC_USER_AGENTS_DIR", str(mock_agent_dir))
    environment = os.environ.copy()
    command = [
        sys.executable,
        "-c",
        "from acpc.cli import cli; cli()",
        "prompt",
        "mock",
        "--cwd",
        str(daemon_environment),
        "cold-start",
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    try:
        try:
            stdout, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired as error:
            pytest.fail(f"cold-start CLI did not terminate: {error}")
        assert process.returncode == 0, stderr
        assert stdout == "cold-start"
        status = asyncio.run(daemon_status("mock"))
        assert len(status["sessions"]) == 1
        assert status["sessions"][0]["state"] == "idle"
    finally:
        with contextlib.suppress(DaemonUnavailableError, OSError):
            asyncio.run(shutdown_daemon("mock"))
        if process.poll() is None:
            process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.communicate(timeout=3)


def test_auto_start_spawns_and_polls_until_socket_appears(
    daemon_environment: Path,
) -> None:
    popen_calls: list[tuple[list[str], dict[str, Any]]] = []

    async def scenario() -> None:
        daemon = MockDaemon("mock", _round_trip_responses())

        def fake_popen(command: list[str], **kwargs: Any) -> SimpleNamespace:
            popen_calls.append((command, kwargs))
            asyncio.create_task(_start_later(daemon))
            return SimpleNamespace(pid=1234)

        try:
            exit_code = await asyncio.wait_for(
                DaemonClient(
                    "mock",
                    readiness_timeout=1,
                    poll_interval=0.02,
                    popen_factory=fake_popen,
                ).prompt(
                    text="start",
                    cwd=str(daemon_environment),
                    permissions="read",
                    output=OutputHandler(OutputMode.QUIET),
                ),
                timeout=2,
            )
            assert exit_code == 0
            await daemon.wait()
        finally:
            await daemon.close()

    async def _start_later(daemon: MockDaemon) -> None:
        await asyncio.sleep(0.08)
        await daemon.start()

    asyncio.run(scenario())
    assert len(popen_calls) == 1
    assert popen_calls[0][0] == [sys.executable, "-m", "acpc.daemon", "mock"]
    assert popen_calls[0][1] == {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "start_new_session": True,
    }


def test_auto_start_timeout_is_bounded_and_clean(
    daemon_environment: Path,
) -> None:
    popen_calls: list[list[str]] = []

    async def scenario() -> None:
        def fake_popen(command: list[str], **kwargs: Any) -> SimpleNamespace:
            del kwargs
            popen_calls.append(command)
            return SimpleNamespace(pid=1234)

        with pytest.raises(DaemonUnavailableError, match="not ready"):
            await asyncio.wait_for(
                DaemonClient(
                    "mock",
                    readiness_timeout=0.12,
                    poll_interval=0.02,
                    popen_factory=fake_popen,
                ).connect(),
                timeout=1,
            )

    asyncio.run(scenario())
    assert len(popen_calls) == 1


@pytest.mark.parametrize(
    "state",
    [
        "empty",
        "orphan_socket",
        "stale_lock",
        "orphan_socket_stale_lock",
        "malformed_lock",
        "orphan_socket_malformed_lock",
        "dead_pid_with_socket_and_lock",
        "dead_pid_with_socket_and_old_version",
        "live_pid_with_unheld_lock",
        "current_listener",
    ],
)
def test_stale_state_matrix_reaches_a_real_working_listener(
    daemon_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    """Reachability states use real sockets and a real prompt round trip.

    The raw 16-row boolean cross-product is not a faithful state matrix:
    PID and version have no meaning without a lock, a version mismatch requires
    a live listener, and a held lock naming an unverified live process must take
    the safe direct-mode fallback instead of becoming a working connection.
    """

    async def scenario() -> None:
        socket_path = socket_path_for_target("mock")
        lock_path = lock_path_for_target("mock")
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        existing: MockDaemon | None = None
        if state == "current_listener":
            existing = MockDaemon("mock", _round_trip_responses())
            await existing.start()
        elif state in {"orphan_socket", "orphan_socket_stale_lock", "orphan_socket_malformed_lock"}:
            socket_path.touch()
        if state in {
            "stale_lock",
            "orphan_socket_stale_lock",
            "dead_pid_with_socket_and_lock",
            "dead_pid_with_socket_and_old_version",
            "live_pid_with_unheld_lock",
        }:
            pid = os.getpid() if state == "live_pid_with_unheld_lock" else 999999
            lock_path.write_text(
                json.dumps(
                    {
                        "pid": pid,
                        "socket": str(socket_path),
                        "acpc_version": (
                            "old"
                            if state == "dead_pid_with_socket_and_old_version"
                            else __version__
                        ),
                        "target": "mock",
                    }
                )
            )
        elif state in {"malformed_lock", "orphan_socket_malformed_lock"}:
            lock_path.write_text("not json")

        spawned: list[MockDaemon] = []
        spawn_tasks: list[asyncio.Task[None]] = []

        async def spawn() -> None:
            daemon = MockDaemon("mock", _round_trip_responses())
            spawned.append(daemon)
            await daemon.start()

        def fake_spawn() -> None:
            spawn_tasks.append(asyncio.create_task(spawn()))

        client = DaemonClient("mock", readiness_timeout=1, poll_interval=0.01)
        monkeypatch.setattr(client, "_spawn_daemon", fake_spawn)
        try:
            transport, connection = await client.connect()
            await transport.send(
                connection,
                {
                    "type": "prompt",
                    "text": "real round trip",
                    "cwd": str(daemon_environment),
                    "session_id": None,
                    "permissions": "read",
                    "model": None,
                    "mode": None,
                    "output_mode": "quiet",
                },
            )
            frames: list[dict[str, Any]] = []
            while True:
                frame = await transport.receive(connection)
                frames.append(frame)
                if frame.get("type") == "prompt_done":
                    break
            assert frames[-1]["stop_reason"] == "end_turn"
            await transport.close_connection(connection)
            await transport.cleanup()
            if state != "current_listener":
                assert spawned
        finally:
            for task in spawn_tasks:
                await asyncio.gather(task, return_exceptions=True)
            if existing is not None:
                await existing.close()
            for daemon in spawned:
                await daemon.close()

    asyncio.run(scenario())


def test_version_mismatch_replaces_process_and_completes_real_prompt(
    daemon_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_agent_dir: Path,
) -> None:
    monkeypatch.setenv("ACPC_USER_AGENTS_DIR", str(mock_agent_dir))

    async def wait_for_path(path: Path) -> None:
        for _ in range(200):
            if path.exists():
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"listener did not create {path}")

    async def scenario() -> None:
        old = subprocess.Popen(
            [sys.executable, "-m", "acpc.daemon", "mock"],
            start_new_session=True,
        )
        new: subprocess.Popen[Any] | None = None
        await wait_for_path(socket_path_for_target("mock"))
        await wait_for_path(lock_path_for_target("mock"))
        metadata = json.loads(lock_path_for_target("mock").read_text())
        metadata["acpc_version"] = "old"
        with lock_path_for_target("mock").open("r+", encoding="utf-8") as lock_file:
            lock_file.seek(0)
            lock_file.truncate()
            json.dump(metadata, lock_file)
            lock_file.flush()
        client = DaemonClient("mock", readiness_timeout=2, poll_interval=0.01)

        def capture_popen(command: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
            nonlocal new
            new = subprocess.Popen(command, **kwargs)
            return new

        monkeypatch.setattr(client, "_popen_factory", capture_popen)
        try:
            result = await client.prompt(
                text="real prompt",
                cwd=str(daemon_environment),
                permissions="read",
                output=OutputHandler(OutputMode.QUIET),
            )
            assert result == 0
            assert old.poll() is not None
            assert new is not None and new.pid != old.pid
            metadata = json.loads(lock_path_for_target("mock").read_text())
            assert metadata["pid"] == new.pid
            assert metadata["acpc_version"] == __version__
        finally:
            if new is not None and new.poll() is None:
                new.terminate()
                new.wait(timeout=2)
            if old.poll() is None:
                old.terminate()
                old.wait(timeout=2)

    asyncio.run(scenario())


def test_timeout_cancels_and_the_same_session_can_be_used_again(
    daemon_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        daemon = CancelDaemon("mock")
        await daemon.start()
        try:
            monkeypatch.setattr("acpc.daemon_client.CANCEL_GRACE", 0.2)
            first = await DaemonClient("mock").prompt(
                text="slow",
                cwd=str(daemon_environment),
                permissions="read",
                output=OutputHandler(OutputMode.QUIET),
                timeout=0.01,
            )
            assert first == 124
            assert daemon.cancel_received.is_set()

            second = await DaemonClient("mock").prompt(
                text="follow-up",
                cwd=str(daemon_environment),
                permissions="read",
                output=OutputHandler(OutputMode.QUIET),
            )
            assert second == 0
        finally:
            await daemon.close()

    asyncio.run(scenario())


def test_ctrl_c_sends_interrupt_cancel_and_returns_130(
    daemon_environment: Path,
) -> None:
    async def scenario() -> None:
        daemon = CancelDaemon("mock")
        await daemon.start()
        try:
            task = asyncio.create_task(
                DaemonClient("mock").prompt(
                    text="interrupt",
                    cwd=str(daemon_environment),
                    permissions="read",
                    output=OutputHandler(OutputMode.QUIET),
                )
            )
            await daemon.started.wait()
            task.cancel()
            assert await task == 130
            assert daemon.cancel_received.is_set()
        finally:
            await daemon.close()

    asyncio.run(scenario())


def test_unresponsive_cancel_does_not_signal_unverified_daemon_pid(
    daemon_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed: list[object] = []

    async def scenario() -> None:
        daemon = CancelDaemon("mock", ignore_cancel=True)
        await daemon.start()
        try:
            monkeypatch.setattr("acpc.daemon_client.CANCEL_GRACE", 0.01)
            monkeypatch.setattr("acpc.daemon_client._terminate_pid", killed.append)
            assert (
                await DaemonClient("mock").prompt(
                    text="timeout",
                    cwd=str(daemon_environment),
                    permissions="read",
                    output=OutputHandler(OutputMode.QUIET),
                    timeout=0.01,
                )
                == 124
            )
        finally:
            await daemon.close()

    asyncio.run(scenario())
    assert killed == []


@pytest.mark.parametrize("recovery_path", ["timeout", "prepare", "recover", "replace"])
def test_live_non_daemon_pid_is_never_signalled_on_recovery(
    daemon_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
    recovery_path: str,
) -> None:
    """A live process named by a lock is not enough to authorize SIGTERM."""
    import acpc.daemon_client as daemon_client_module

    sleeper = subprocess.Popen(["sleep", "30"])
    try:
        cmdline = process_cmdline(sleeper.pid)
        start_time = process_start_time(sleeper.pid)
        assert cmdline is not None and start_time is not None
        metadata = {
            "pid": sleeper.pid,
            "process_start_time": start_time,
            "cmdline": cmdline,
            "target": "mock",
            "acpc_version": "old",
        }
        lock_path = lock_path_for_target("mock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(json.dumps(metadata))
        monkeypatch.setattr(daemon_client_module, "_lock_is_held", lambda path: True)
        client = DaemonClient("mock", poll_interval=0.01, stale_shutdown_timeout=0.01)
        timeout_metadata = {**metadata, "acpc_version": __version__}

        async def scenario() -> None:
            if recovery_path == "timeout":
                daemon = CancelDaemon("mock", ignore_cancel=True)
                await daemon.start()
                try:
                    monkeypatch.setattr(client, "_read_lock_metadata", lambda: timeout_metadata)
                    monkeypatch.setattr(daemon_client_module, "CANCEL_GRACE", 0.01)
                    assert (
                        await client.prompt(
                            text="timeout",
                            cwd=str(daemon_environment),
                            permissions="read",
                            output=OutputHandler(OutputMode.QUIET),
                            timeout=0.01,
                        )
                        == 124
                    )
                finally:
                    await daemon.close()
                return
            monkeypatch.setattr(client, "_read_lock_metadata", lambda: metadata)
            socket_path = socket_path_for_target("mock")
            if recovery_path in {"recover", "replace"}:
                socket_path.touch()
            if recovery_path == "prepare":
                with pytest.raises(DaemonUnavailableError, match="unverified"):
                    await client._prepare_endpoint()
            elif recovery_path == "recover":
                with pytest.raises(DaemonUnavailableError, match="unverified"):
                    await client._recover_after_connection_failure()
            else:
                with pytest.raises(DaemonUnavailableError, match="unverified"):
                    await client._replace_version_mismatched_daemon(metadata)

        asyncio.run(scenario())
        assert sleeper.poll() is None
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=2)


def test_stop_session_cancels_only_the_selected_live_daemon_session(
    daemon_environment: Path,
    mock_agent_dir: Path,
) -> None:
    del mock_agent_dir

    async def receive_until_done(
        transport: UnixSocketTransport,
        connection: Connection,
    ) -> list[dict[str, Any]]:
        frames: list[dict[str, Any]] = []
        while True:
            frame = await asyncio.wait_for(transport.receive(connection), timeout=5)
            frames.append(frame)
            if frame.get("type") == "prompt_done":
                return frames

    async def start_prompt(
        transport: UnixSocketTransport,
        connection: Connection,
    ) -> str:
        await transport.send(
            connection,
            {
                "type": "prompt",
                "text": "slow:10",
                "cwd": str(daemon_environment),
                "session_id": None,
                "permissions": "read",
                "model": None,
                "mode": None,
                "output_mode": "quiet",
            },
        )
        frame = await transport.receive(connection)
        assert frame["type"] == "session_started"
        return str(frame["session_id"])

    async def scenario() -> None:
        daemon = Daemon("mock")
        daemon_task = asyncio.create_task(daemon.run())
        await asyncio.sleep(0.05)
        first_transport = UnixSocketTransport("mock")
        second_transport = UnixSocketTransport("mock")
        first_connection = await first_transport.connect()
        second_connection = await second_transport.connect()
        try:
            first_id = await start_prompt(first_transport, first_connection)
            second_id = await start_prompt(second_transport, second_connection)
            result = await asyncio.to_thread(
                CliRunner().invoke,
                cli,
                ["stop", "-s", first_id],
            )
            assert result.exit_code == 0, result.output
            assert f"cancel requested for daemon session {first_id}" in result.output
            first_frames = await receive_until_done(first_transport, first_connection)
            assert first_frames[-1]["stop_reason"] == "cancelled"
            status = await daemon_status("mock")
            states = {session["id"]: session["state"] for session in status["sessions"]}
            assert states[first_id] == "idle"
            assert states[second_id] == "active"
            assert await cancel_daemon_prompt("mock", session_id=second_id)
            second_frames = await receive_until_done(second_transport, second_connection)
            assert second_frames[-1]["stop_reason"] == "cancelled"
            assert not daemon._stopping
        finally:
            await first_transport.close_connection(first_connection)
            await second_transport.close_connection(second_connection)
            await first_transport.cleanup()
            await second_transport.cleanup()
            await daemon.stop()
            await asyncio.wait_for(daemon_task, timeout=2)

    asyncio.run(scenario())


def test_continuation_without_cwd_uses_the_daemon_session_cwd(
    daemon_environment: Path,
    mock_agent_dir: Path,
) -> None:
    del mock_agent_dir
    work_a = daemon_environment / "work-a"
    work_b = daemon_environment / "work-b"
    work_a.mkdir()
    work_b.mkdir()

    async def scenario() -> None:
        daemon = Daemon("mock")
        daemon_task = asyncio.create_task(daemon.run())
        for _ in range(100):
            if socket_path_for_target("mock").exists():
                break
            await asyncio.sleep(0.01)
        try:
            output = OutputHandler(OutputMode.QUIET)
            assert (
                await DaemonClient("mock").prompt(
                    text="first",
                    cwd=str(work_a),
                    permissions="read",
                    output=output,
                )
                == 0
            )
            session_id = load_last_session("mock")
            assert session_id is not None
            assert load_session_cwd("mock", session_id) == str(work_a.resolve())
            assert (
                await DaemonClient("mock").prompt(
                    text="continue",
                    cwd=None,
                    permissions="read",
                    output=OutputHandler(OutputMode.QUIET),
                    session_id=session_id,
                )
                == 0
            )
            assert daemon.sessions[session_id].cwd == str(work_a.resolve())
            assert work_b.exists()
        finally:
            await daemon.stop()
            await asyncio.wait_for(daemon_task, timeout=2)

    asyncio.run(scenario())


def test_print_session_id_is_first_stdout_line(
    daemon_environment: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def scenario() -> None:
        daemon = MockDaemon("mock", _round_trip_responses())
        await daemon.start()
        try:
            output_path = daemon_environment / "result.txt"
            assert (
                await DaemonClient("mock").prompt(
                    text="say hello",
                    cwd=str(daemon_environment),
                    permissions="read",
                    output=OutputHandler(OutputMode.QUIET, str(output_path)),
                    print_session_id=True,
                )
                == 0
            )
        finally:
            await daemon.close()

    asyncio.run(scenario())
    assert capsys.readouterr().out.splitlines()[0] == "sess-1"


def test_three_clients_can_race_start_without_error_output(
    daemon_environment: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    popen_count = 0

    async def scenario() -> None:
        nonlocal popen_count
        daemon = MockDaemon("mock", [])

        async def serve_three() -> None:
            await daemon.start_listener()
            connections = [await daemon.transport.accept() for _ in range(3)]
            try:
                await asyncio.gather(
                    *(_serve_one(daemon, connection) for connection in connections)
                )
            finally:
                for connection in connections:
                    with contextlib.suppress(ConnectionError, OSError, ValueError):
                        await daemon.transport.close_connection(connection)

        async def _serve_one(server: MockDaemon, connection: Connection) -> None:
            frame = await server.transport.receive(connection)
            await server.transport.send(
                connection,
                {"type": "session_started", "session_id": frame["text"]},
            )
            await server.transport.send(
                connection,
                _update(frame["text"], frame["text"]),
            )
            await server.transport.send(
                connection,
                {
                    "type": "prompt_done",
                    "session_id": frame["text"],
                    "stop_reason": "end_turn",
                },
            )

        async def start_later() -> None:
            await asyncio.sleep(0.05)
            await serve_three()

        def fake_popen(command: list[str], **kwargs: Any) -> SimpleNamespace:
            nonlocal popen_count
            del command, kwargs
            popen_count += 1
            if popen_count == 1:
                asyncio.create_task(start_later())
            return SimpleNamespace(pid=1234 + popen_count)

        try:
            clients = [
                DaemonClient(
                    "mock", readiness_timeout=1, poll_interval=0.01, popen_factory=fake_popen
                )
                for _ in range(3)
            ]
            results = await asyncio.wait_for(
                asyncio.gather(
                    *(
                        client.prompt(
                            text=f"client-{index}",
                            cwd=str(daemon_environment),
                            permissions="read",
                            output=OutputHandler(OutputMode.QUIET),
                        )
                        for index, client in enumerate(clients)
                    )
                ),
                timeout=2,
            )
            assert results == [0, 0, 0]
        finally:
            await daemon.close()

    asyncio.run(scenario())
    assert popen_count == 3
    assert "error" not in capsys.readouterr().err.lower()


@pytest.mark.parametrize(
    ("env_value", "flag", "expected"),
    [(None, False, "enabled"), ("1", False, "disabled"), ("0", True, "disabled")],
)
def test_no_daemon_flag_beats_environment_and_default(
    daemon_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
    env_value: str | None,
    flag: bool,
    expected: str,
) -> None:
    del daemon_environment
    if env_value is None:
        monkeypatch.delenv("ACPC_NO_DAEMON", raising=False)
    else:
        monkeypatch.setenv("ACPC_NO_DAEMON", env_value)
    args = ["prompt", "--dry-run"]
    if flag:
        args.append("--no-daemon")
    args.extend(["codex", "hello"])

    result = CliRunner().invoke(cli, args)

    assert result.exit_code == 0
    assert f"daemon: {expected}" in result.output


def test_daemon_environment_is_inherited_by_auto_start(
    daemon_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ACPC_DAEMON_TTL", "12")
    monkeypatch.setenv("ACPC_DAEMON_MAX_AGE", "34")
    captured_environment: dict[str, str] = {}

    async def scenario() -> None:
        def fake_popen(command: list[str], **kwargs: Any) -> SimpleNamespace:
            del command, kwargs
            captured_environment.update(
                {
                    key: os.environ[key]
                    for key in ("ACPC_STATE_DIR", "ACPC_DAEMON_TTL", "ACPC_DAEMON_MAX_AGE")
                }
            )
            return SimpleNamespace(pid=1234)

        with pytest.raises(DaemonUnavailableError):
            await DaemonClient(
                "mock",
                readiness_timeout=0.02,
                poll_interval=0.01,
                popen_factory=fake_popen,
            ).connect()

    asyncio.run(scenario())
    assert captured_environment == {
        "ACPC_STATE_DIR": str(daemon_environment),
        "ACPC_DAEMON_TTL": "12",
        "ACPC_DAEMON_MAX_AGE": "34",
    }
