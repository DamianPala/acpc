"""Tests for the daemon client protocol and readiness behavior."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from acpc.cli import cli
from acpc.daemon_client import DaemonClient, DaemonUnavailableError
from acpc.ipc import Connection, UnixSocketTransport
from acpc.output import OutputHandler, OutputMode


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
        self.transport = UnixSocketTransport(target)
        self.responses = responses
        self.received: list[dict[str, Any]] = []
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
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

    async def _serve(self) -> None:
        connection = await self.transport.accept()
        try:
            self.received.append(await self.transport.receive(connection))
            for response in self.responses:
                await self.transport.send(connection, response)
        finally:
            with contextlib.suppress(ConnectionError, OSError, ValueError):
                await self.transport.close_connection(connection)


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
    assert popen_calls[0][1] == {"start_new_session": True}


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


def test_three_clients_can_race_start_without_error_output(
    daemon_environment: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    popen_count = 0

    async def scenario() -> None:
        nonlocal popen_count
        daemon = MockDaemon("mock", [])

        async def serve_three() -> None:
            await daemon.transport.bind()
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
