"""Tests for the Phase 1 daemon core."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import time
from pathlib import Path
from typing import Any, Iterator

import pytest
from acp.schema import PermissionOption, ToolCallUpdate

from acpc.client import PermissionLevel
from acpc.daemon import Daemon, read_mem_available_mb, rss_ceiling_mb
from acpc.ipc import (
    Connection,
    UnixSocketTransport,
    lock_path_for_target,
    socket_path_for_target,
)


async def _wait_for_socket(path: Path) -> None:
    for _ in range(100):
        if path.exists():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"daemon socket did not appear: {path}")


async def _receive_prompt_result(
    transport: UnixSocketTransport,
    connection: Connection,
) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    while True:
        frame = await asyncio.wait_for(transport.receive(connection), timeout=5)
        frames.append(frame)
        if frame["type"] in {"prompt_done", "error", "capacity", "shutting_down"}:
            return frames


async def _send_prompt(
    transport: UnixSocketTransport,
    connection: Connection,
    cwd: Path,
    text: str,
    session_id: str | None = None,
    *,
    permissions: str = "all",
    model: str | None = None,
    mode: str | None = None,
) -> list[dict[str, Any]]:
    await transport.send(
        connection,
        {
            "type": "prompt",
            "text": text,
            "cwd": str(cwd),
            "session_id": session_id,
            "permissions": permissions,
            "model": model,
            "mode": mode,
            "output_mode": "text",
        },
    )
    return await _receive_prompt_result(transport, connection)


@pytest.fixture()
def daemon_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mock_agent_dir: Path,
) -> Iterator[tuple[Path, Path]]:
    del mock_agent_dir
    state = tmp_path / "state"
    caller_cwd = tmp_path / "caller"
    state.mkdir()
    caller_cwd.mkdir()
    monkeypatch.setenv("ACPC_STATE_DIR", str(state))
    yield state, caller_cwd
    assert not socket_path_for_target("mock").exists()
    assert not lock_path_for_target("mock").exists()


def test_daemon_sessions_are_fresh_and_explicit_ids_load_or_reuse(daemon_environment) -> None:
    async def scenario() -> None:
        state, cwd = daemon_environment
        daemon = Daemon("mock")
        daemon_task = asyncio.create_task(daemon.run())
        await _wait_for_socket(socket_path_for_target("mock"))
        transport = UnixSocketTransport("mock")
        connection = await transport.connect()
        try:
            first = await _send_prompt(transport, connection, cwd, "first")
            second = await _send_prompt(transport, connection, cwd, "second")
            first_id = first[0]["session_id"]
            second_id = second[0]["session_id"]
            assert first[0]["reused"] == "new"
            assert second[0]["reused"] == "new"
            assert first_id != second_id

            live = await _send_prompt(transport, connection, cwd, "multi:live", first_id)
            loaded = await _send_prompt(transport, connection, cwd, "multi:loaded", "load-only")
            assert live[0]["reused"] == "live"
            assert live[0]["session_id"] == first_id
            assert live[-1]["session_id"] == first_id
            assert live[-1]["stop_reason"] == "end_turn"
            assert [
                frame["update"]["content"]["text"]
                for frame in live
                if frame["type"] == "session_update"
            ] == ["turn 2: live"]
            assert loaded[0]["reused"] == "loaded"
            assert loaded[0]["session_id"] == "load-only"
            assert loaded[-1]["session_id"] == "load-only"
            assert loaded[-1]["stop_reason"] == "end_turn"
            assert [
                frame["update"]["content"]["text"]
                for frame in loaded
                if frame["type"] == "session_update"
            ] == ["turn 2: loaded"]
            refusal = await _send_prompt(transport, connection, cwd, "error")
            assert refusal[-1]["session_id"] == refusal[0]["session_id"]
            assert refusal[-1]["stop_reason"] == "refusal"
            assert set(daemon.sessions) == {
                first_id,
                second_id,
                "load-only",
                refusal[0]["session_id"],
            }
            assert not hasattr(daemon.sessions["load-only"], "replaying")
        finally:
            await transport.close_connection(connection)
            await transport.cleanup()
            await daemon.stop()
            await asyncio.wait_for(daemon_task, timeout=1)
            assert not state.joinpath("run", "mock.sock").exists()

    asyncio.run(scenario())


def test_disconnected_prompt_does_not_leak_into_reused_session(daemon_environment) -> None:
    async def scenario() -> None:
        state, cwd = daemon_environment
        daemon = Daemon("mock")
        daemon_task = asyncio.create_task(daemon.run())
        await _wait_for_socket(socket_path_for_target("mock"))
        first_transport = UnixSocketTransport("mock")
        first_connection = await first_transport.connect()
        second_transport = UnixSocketTransport("mock")
        second_connection = None
        try:
            await first_transport.send(
                first_connection,
                {
                    "type": "prompt",
                    "text": "slow:1",
                    "cwd": str(cwd),
                    "session_id": None,
                    "permissions": "all",
                },
            )
            started = await asyncio.wait_for(first_transport.receive(first_connection), timeout=1)
            session_id = started["session_id"]
            await first_transport.close_connection(first_connection)
            await first_transport.cleanup()

            second_connection = await second_transport.connect()
            frames = await _send_prompt(
                second_transport,
                second_connection,
                cwd,
                "slow:2",
                session_id,
            )
            assert [
                frame["update"]["content"]["text"]
                for frame in frames
                if frame["type"] == "session_update"
            ] == ["waited 2s"]
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(second_transport.receive(second_connection), timeout=0.4)
        finally:
            if second_connection is not None:
                await second_transport.close_connection(second_connection)
            await second_transport.cleanup()
            await daemon.stop()
            await asyncio.wait_for(daemon_task, timeout=1)

    asyncio.run(scenario())


def test_adapter_death_fails_prompt_and_stops_daemon(daemon_environment) -> None:
    async def scenario() -> None:
        _, cwd = daemon_environment
        daemon = Daemon("mock")
        daemon_task = asyncio.create_task(daemon.run())
        await _wait_for_socket(socket_path_for_target("mock"))
        transport = UnixSocketTransport("mock")
        connection = await transport.connect()
        try:
            await transport.send(
                connection,
                {
                    "type": "prompt",
                    "text": "slow:10",
                    "cwd": str(cwd),
                    "session_id": None,
                    "permissions": "all",
                },
            )
            started = await asyncio.wait_for(transport.receive(connection), timeout=1)
            assert started["type"] == "session_started"
            assert daemon.process is not None
            daemon.process.kill()
            await asyncio.wait_for(daemon.process.wait(), timeout=1)
            error = await asyncio.wait_for(transport.receive(connection), timeout=1)
            assert error == {
                "type": "error",
                "message": "adapter connection lost",
                "exit_code": 1,
                "session_id": started["session_id"],
            }
            await asyncio.wait_for(daemon_task, timeout=1)
        finally:
            with contextlib.suppress(ConnectionError, OSError, ValueError):
                await transport.close_connection(connection)
            await transport.cleanup()
            await daemon.stop()

    asyncio.run(scenario())


def test_resume_rejects_cwd_mismatch_and_evicts_deleted_cwd(daemon_environment) -> None:
    async def scenario() -> None:
        _, cwd = daemon_environment
        other_cwd = cwd.parent / "other"
        other_cwd.mkdir()
        daemon = Daemon("mock")
        daemon_task = asyncio.create_task(daemon.run())
        await _wait_for_socket(socket_path_for_target("mock"))
        transport = UnixSocketTransport("mock")
        connection = await transport.connect()
        try:
            first = await _send_prompt(transport, connection, cwd, "first")
            session_id = first[0]["session_id"]
            mismatch = await _send_prompt(transport, connection, other_cwd, "wrong", session_id)
            assert mismatch[-1] == {
                "type": "error",
                "message": f"session cwd is {cwd.resolve()}, --cwd says {other_cwd.resolve()}",
                "exit_code": 2,
                "session_id": session_id,
            }
            cwd.rmdir()
            deleted = await _send_prompt(transport, connection, other_cwd, "gone", session_id)
            assert deleted[-1] == {
                "type": "error",
                "message": f"session cwd no longer exists: {cwd.resolve()}",
                "exit_code": 2,
                "session_id": session_id,
            }
            assert session_id not in daemon.sessions
            assert session_id not in daemon.client.session_ids
        finally:
            await transport.close_connection(connection)
            await transport.cleanup()
            await daemon.stop()
            await asyncio.wait_for(daemon_task, timeout=1)

    asyncio.run(scenario())


def test_load_is_not_published_until_replay_finishes(daemon_environment) -> None:
    async def scenario() -> None:
        _, cwd = daemon_environment
        daemon = Daemon("mock")
        daemon_task = asyncio.create_task(daemon.run())
        await _wait_for_socket(socket_path_for_target("mock"))
        first_transport = UnixSocketTransport("mock")
        first_connection = await first_transport.connect()
        second_transport = UnixSocketTransport("mock")
        second_connection = await second_transport.connect()
        try:
            await first_transport.send(
                first_connection,
                {
                    "type": "prompt",
                    "text": "multi:first",
                    "cwd": str(cwd),
                    "session_id": "load-slow",
                    "permissions": "all",
                },
            )
            await asyncio.sleep(0.05)
            assert "load-slow" not in daemon.sessions
            await second_transport.send(
                second_connection,
                {
                    "type": "prompt",
                    "text": "multi:second",
                    "cwd": str(cwd),
                    "session_id": "load-slow",
                    "permissions": "all",
                },
            )
            first = await _receive_prompt_result(first_transport, first_connection)
            second = await _receive_prompt_result(second_transport, second_connection)
            assert first[0]["reused"] == "loaded"
            assert second[0]["reused"] == "live"
            assert all(
                not (
                    frame["type"] == "session_update"
                    and frame["update"]["content"]["text"] == "history"
                )
                for frame in first + second
            )
        finally:
            await first_transport.close_connection(first_connection)
            await second_transport.close_connection(second_connection)
            await first_transport.cleanup()
            await second_transport.cleanup()
            await daemon.stop()
            await asyncio.wait_for(daemon_task, timeout=1)

    asyncio.run(scenario())


def test_failed_load_rolls_back_client_registration(daemon_environment) -> None:
    async def scenario() -> None:
        _, cwd = daemon_environment
        daemon = Daemon("mock")
        daemon_task = asyncio.create_task(daemon.run())
        await _wait_for_socket(socket_path_for_target("mock"))
        transport = UnixSocketTransport("mock")
        connection = await transport.connect()
        try:
            frames = await _send_prompt(transport, connection, cwd, "ignored", "load-fail")
            assert frames[-1]["type"] == "error"
            assert "load-fail" not in daemon.sessions
            assert "load-fail" not in daemon.client.session_ids
        finally:
            await transport.close_connection(connection)
            await transport.cleanup()
            await daemon.stop()
            await asyncio.wait_for(daemon_task, timeout=1)

    asyncio.run(scenario())


def test_daemon_has_no_artificial_session_cap_and_drains_all_chunks(daemon_environment) -> None:
    async def scenario() -> None:
        _, cwd = daemon_environment
        daemon = Daemon("mock")
        daemon_task = asyncio.create_task(daemon.run())
        await _wait_for_socket(socket_path_for_target("mock"))
        transport = UnixSocketTransport("mock")
        connection = await transport.connect()
        try:
            results = [
                await _send_prompt(
                    transport,
                    connection,
                    cwd,
                    f"burst:{i}|{i + 1}|{i + 2}|{i + 3}|{i + 4}",
                )
                for i in range(4)
            ]
            assert len(daemon.sessions) == 4
            for i, frames in enumerate(results):
                assert [
                    frame["update"]["content"]["text"]
                    for frame in frames
                    if frame["type"] == "session_update"
                ] == [str(i), str(i + 1), str(i + 2), str(i + 3), str(i + 4)]
        finally:
            await transport.close_connection(connection)
            await transport.cleanup()
            await daemon.stop()
            await asyncio.wait_for(daemon_task, timeout=1)

    asyncio.run(scenario())


def test_stop_retries_after_cleanup_failure_and_finishes_all_stages(
    daemon_environment,
) -> None:
    async def scenario() -> None:
        assert daemon_environment
        daemon = Daemon("mock")
        cleanup_calls = 0
        stack_closed = False

        async def cleanup() -> None:
            nonlocal cleanup_calls
            cleanup_calls += 1
            if cleanup_calls == 1:
                raise TimeoutError("transport cleanup failed")

        class ExitStack:
            async def aclose(self) -> None:
                nonlocal stack_closed
                stack_closed = True

        async def waiting_task() -> None:
            await asyncio.Event().wait()

        daemon.transport.cleanup = cleanup
        daemon._exit_stack = ExitStack()  # type: ignore[assignment]
        task = asyncio.create_task(waiting_task())
        daemon._client_tasks.add(task)

        with pytest.raises(TimeoutError, match="transport cleanup failed"):
            await daemon.stop()
        assert cleanup_calls == 1
        assert stack_closed
        assert task.done()
        assert not daemon._stopping

        await daemon.stop()
        assert cleanup_calls == 2
        assert daemon._shutdown_complete

    asyncio.run(scenario())


def test_daemon_uses_state_cwd_and_survives_deleted_caller_cwd(daemon_environment) -> None:
    async def scenario() -> None:
        state, first_cwd = daemon_environment
        second_cwd = first_cwd.parent / "second"
        second_cwd.mkdir()
        daemon = Daemon("mock")
        daemon_task = asyncio.create_task(daemon.run())
        await _wait_for_socket(socket_path_for_target("mock"))
        transport = UnixSocketTransport("mock")
        connection = await transport.connect()
        try:
            first = await _send_prompt(transport, connection, first_cwd, "before delete")
            assert first[-1]["type"] == "prompt_done"
            assert daemon.process is not None and daemon.process.pid is not None
            assert Path(os.readlink(f"/proc/{daemon.process.pid}/cwd")) == state.resolve()
            first_cwd.rmdir()

            second = await _send_prompt(transport, connection, second_cwd, "after delete")
            assert second[-1]["type"] == "prompt_done"
        finally:
            await transport.close_connection(connection)
            await transport.cleanup()
            await daemon.stop()
            await asyncio.wait_for(daemon_task, timeout=1)

    asyncio.run(scenario())


def test_daemon_routes_concurrent_session_streams_to_their_clients(daemon_environment) -> None:
    async def scenario() -> None:
        _, cwd = daemon_environment
        daemon = Daemon("mock")
        daemon_task = asyncio.create_task(daemon.run())
        await _wait_for_socket(socket_path_for_target("mock"))
        first_transport = UnixSocketTransport("mock")
        second_transport = UnixSocketTransport("mock")
        first_connection, second_connection = await asyncio.gather(
            first_transport.connect(),
            second_transport.connect(),
        )
        try:
            first_frames, second_frames = await asyncio.gather(
                _send_prompt(first_transport, first_connection, cwd, "barrier:first stream"),
                _send_prompt(second_transport, second_connection, cwd, "barrier:second stream"),
            )
            first_id = first_frames[0]["session_id"]
            second_id = second_frames[0]["session_id"]
            assert first_id != second_id
            assert [frame["update"]["content"]["text"] for frame in first_frames[1:-1]] == [
                "first stream"
            ]
            assert [frame["update"]["content"]["text"] for frame in second_frames[1:-1]] == [
                "second stream"
            ]
            assert all(frame["session_id"] == first_id for frame in first_frames[:-1])
            assert all(frame["session_id"] == second_id for frame in second_frames[:-1])
        finally:
            await first_transport.close_connection(first_connection)
            await second_transport.close_connection(second_connection)
            await first_transport.cleanup()
            await second_transport.cleanup()
            await daemon.stop()
            await asyncio.wait_for(daemon_task, timeout=1)

    asyncio.run(scenario())


def test_same_session_fifo_lock_catches_concurrent_prompt_mutation(daemon_environment) -> None:
    async def scenario() -> None:
        _, cwd = daemon_environment
        daemon = Daemon("mock")
        daemon_task = asyncio.create_task(daemon.run())
        await _wait_for_socket(socket_path_for_target("mock"))
        first_transport = UnixSocketTransport("mock")
        second_transport = UnixSocketTransport("mock")
        first = await first_transport.connect()
        second = await second_transport.connect()
        try:
            await first_transport.send(
                first,
                {"type": "prompt", "text": "chunkslow:1", "cwd": str(cwd), "permissions": "all"},
            )
            started = await asyncio.wait_for(first_transport.receive(first), timeout=1)
            await asyncio.wait_for(first_transport.receive(first), timeout=1)
            session_id = started["session_id"]
            assert daemon.sessions[session_id].running
            await second_transport.send(
                second,
                {
                    "type": "prompt",
                    "text": "slow:1",
                    "cwd": str(cwd),
                    "session_id": session_id,
                    "permissions": "all",
                },
            )
            started_at = time.monotonic()
            second_frames = await asyncio.wait_for(
                _receive_prompt_result(second_transport, second), timeout=3
            )
            elapsed = time.monotonic() - started_at
            first_result = await asyncio.wait_for(
                _receive_prompt_result(first_transport, first), timeout=1
            )
            assert any(frame["type"] == "queued" for frame in second_frames), second_frames
            assert first_result[-1]["stop_reason"] == "end_turn"
            assert second_frames[-1]["stop_reason"] == "end_turn"
            assert elapsed >= 1.6
        finally:
            await first_transport.close_connection(first)
            await second_transport.close_connection(second)
            await first_transport.cleanup()
            await second_transport.cleanup()
            await daemon.stop()
            await asyncio.wait_for(daemon_task, timeout=1)
            assert not socket_path_for_target("mock").exists()
            assert not lock_path_for_target("mock").exists()

    asyncio.run(scenario())


def test_superseded_cancelled_prompt_is_an_error(daemon_environment) -> None:
    async def scenario() -> None:
        _, cwd = daemon_environment
        daemon = Daemon("mock")
        daemon_task = asyncio.create_task(daemon.run())
        await _wait_for_socket(socket_path_for_target("mock"))
        transport = UnixSocketTransport("mock")
        connection = await transport.connect()
        try:
            frames = await asyncio.wait_for(
                _send_prompt(transport, connection, cwd, "supersede:external"), timeout=1
            )
            assert frames[-1]["type"] == "error"
            assert frames[-1]["message"] == "prompt superseded on this session"
            assert frames[-1]["exit_code"] == 1
        finally:
            await transport.close_connection(connection)
            await transport.cleanup()
            await daemon.stop()
            await asyncio.wait_for(daemon_task, timeout=1)
            assert not lock_path_for_target("mock").exists()

    asyncio.run(scenario())


def test_global_concurrency_cap_queues_arriving_prompt(daemon_environment, monkeypatch) -> None:
    async def scenario() -> None:
        _, cwd = daemon_environment
        monkeypatch.setenv("ACPC_DAEMON_MAX_CONCURRENT", "1")
        daemon = Daemon("mock")
        daemon_task = asyncio.create_task(daemon.run())
        await _wait_for_socket(socket_path_for_target("mock"))
        first_transport = UnixSocketTransport("mock")
        second_transport = UnixSocketTransport("mock")
        first = await first_transport.connect()
        second = await second_transport.connect()
        try:
            await first_transport.send(
                first,
                {"type": "prompt", "text": "chunkslow:1", "cwd": str(cwd), "permissions": "all"},
            )
            await asyncio.wait_for(first_transport.receive(first), timeout=1)
            await asyncio.wait_for(first_transport.receive(first), timeout=1)
            await second_transport.send(
                second,
                {"type": "prompt", "text": "slow:1", "cwd": str(cwd), "permissions": "all"},
            )
            second_frames = await asyncio.wait_for(
                _receive_prompt_result(second_transport, second), timeout=3
            )
            first_frames = await asyncio.wait_for(
                _receive_prompt_result(first_transport, first), timeout=1
            )
            assert any(frame["type"] == "queued" for frame in second_frames)
            assert first_frames[-1]["stop_reason"] == "end_turn"
            assert second_frames[-1]["stop_reason"] == "end_turn"
        finally:
            await first_transport.close_connection(first)
            await second_transport.close_connection(second)
            await first_transport.cleanup()
            await second_transport.cleanup()
            await daemon.stop()
            await asyncio.wait_for(daemon_task, timeout=1)
            assert not lock_path_for_target("mock").exists()

    asyncio.run(scenario())


def test_model_and_mode_switch_once_per_changed_value(daemon_environment) -> None:
    async def scenario() -> None:
        _, cwd = daemon_environment
        daemon = Daemon("mock")
        daemon_task = asyncio.create_task(daemon.run())
        await _wait_for_socket(socket_path_for_target("mock"))
        transport = UnixSocketTransport("mock")
        connection = await transport.connect()
        try:
            first = await _send_prompt(
                transport,
                connection,
                cwd,
                "settings",
                model="model-a",
                mode="plan",
            )
            session_id = first[0]["session_id"]
            second = await _send_prompt(
                transport,
                connection,
                cwd,
                "settings",
                session_id,
                model="model-a",
                mode="plan",
            )
            third = await _send_prompt(
                transport,
                connection,
                cwd,
                "settings",
                session_id,
                model="model-b",
                mode="execute",
            )

            def text(frames: list[dict[str, Any]]) -> str:
                return next(
                    frame["update"]["content"]["text"]
                    for frame in frames
                    if frame["type"] == "session_update"
                )

            assert text(first).endswith("/1/1")
            assert text(second).endswith("/1/1")
            assert text(third).endswith("/2/2")
        finally:
            await transport.close_connection(connection)
            await transport.cleanup()
            await daemon.stop()
            await asyncio.wait_for(daemon_task, timeout=1)
            assert not daemon.process
            assert not socket_path_for_target("mock").exists()
            assert not lock_path_for_target("mock").exists()

    asyncio.run(scenario())


def test_permissions_are_captured_per_prompt_request(daemon_environment, monkeypatch) -> None:
    async def scenario() -> None:
        state, cwd = daemon_environment
        daemon = Daemon("mock")
        registrations: list[PermissionLevel] = []
        register_session = daemon.client.register_session

        def record_registration(*args: Any, **kwargs: Any) -> None:
            registrations.append(kwargs["permission_level"])
            register_session(*args, **kwargs)

        monkeypatch.setattr(daemon.client, "register_session", record_registration)
        daemon_task = asyncio.create_task(daemon.run())
        await _wait_for_socket(socket_path_for_target("mock"))
        transport = UnixSocketTransport("mock")
        connection = await transport.connect()
        try:
            allowed_frames = await _send_prompt(
                transport,
                connection,
                cwd,
                "allowed",
                permissions="all",
            )
            denied_frames = await _send_prompt(
                transport,
                connection,
                cwd,
                "denied",
                permissions="none",
            )
            assert registrations == [PermissionLevel.ALL, PermissionLevel.NONE]
            options = [
                PermissionOption(
                    option_id="allow-once",
                    name="Allow once",
                    kind="allow_once",
                ),
                PermissionOption(
                    option_id="reject-once",
                    name="Reject once",
                    kind="reject_once",
                ),
            ]
            tool_call = ToolCallUpdate(
                tool_call_id="test-tool",
                kind="edit",
                title="Edit file",
            )
            allowed = await daemon.client.request_permission(
                options,
                allowed_frames[0]["session_id"],
                tool_call,
            )
            denied = await daemon.client.request_permission(
                options,
                denied_frames[0]["session_id"],
                tool_call,
            )
            assert allowed.outcome.outcome == "selected"
            assert denied.outcome.outcome == "cancelled"
        finally:
            await transport.close_connection(connection)
            await transport.cleanup()
            await daemon.stop()
            await asyncio.wait_for(daemon_task, timeout=1)
            assert not daemon.process
            assert not state.joinpath("run", "mock.sock").exists()
            assert not lock_path_for_target("mock").exists()

    asyncio.run(scenario())


def test_session_queue_overflow_has_no_direct_spawn_signal(daemon_environment) -> None:
    async def scenario() -> None:
        _, cwd = daemon_environment
        daemon = Daemon("mock")
        daemon_task = asyncio.create_task(daemon.run())
        await _wait_for_socket(socket_path_for_target("mock"))
        transports = [UnixSocketTransport("mock") for _ in range(6)]
        connections = [await transport.connect() for transport in transports]
        try:
            await transports[0].send(
                connections[0],
                {"type": "prompt", "text": "chunkslow:1", "cwd": str(cwd), "permissions": "all"},
            )
            started = await asyncio.wait_for(transports[0].receive(connections[0]), timeout=1)
            await asyncio.wait_for(transports[0].receive(connections[0]), timeout=1)
            session_id = started["session_id"]
            for index in range(1, 6):
                await transports[index].send(
                    connections[index],
                    {
                        "type": "prompt",
                        "text": f"queued:{index}",
                        "cwd": str(cwd),
                        "session_id": session_id,
                        "permissions": "all",
                    },
                )
            results = await asyncio.wait_for(
                asyncio.gather(
                    *(
                        _receive_prompt_result(transport, connection)
                        for transport, connection in zip(transports, connections)
                    ),
                ),
                timeout=4,
            )
            overflow = results[5][-1]
            assert overflow == {
                "type": "error",
                "message": "session queue full (4 waiting)",
                "exit_code": 1,
            }, results
            assert all(result[-1]["type"] == "prompt_done" for result in results[:5])
        finally:
            for transport, connection in zip(transports, connections):
                await transport.close_connection(connection)
                await transport.cleanup()
            await daemon.stop()
            await asyncio.wait_for(daemon_task, timeout=1)
            assert not socket_path_for_target("mock").exists()
            assert not lock_path_for_target("mock").exists()

    asyncio.run(scenario())


def test_idle_ttl_cleans_socket_and_lock(daemon_environment, monkeypatch) -> None:
    async def scenario() -> None:
        state, _ = daemon_environment
        monkeypatch.setenv("ACPC_DAEMON_TTL", "0.1")
        daemon = Daemon("mock")
        await asyncio.wait_for(daemon.run(), timeout=2)
        assert not socket_path_for_target("mock").exists()
        assert not lock_path_for_target("mock").exists()
        assert state.joinpath("log", "mock.log").is_file()

    asyncio.run(scenario())


def test_sigterm_rejects_queue_and_drains_active_prompt(daemon_environment) -> None:
    async def scenario() -> None:
        _, cwd = daemon_environment
        daemon = Daemon("mock")
        daemon_task = asyncio.create_task(daemon.run())
        await _wait_for_socket(socket_path_for_target("mock"))
        first_transport = UnixSocketTransport("mock")
        second_transport = UnixSocketTransport("mock")
        first = await first_transport.connect()
        second = await second_transport.connect()
        try:
            await first_transport.send(
                first,
                {"type": "prompt", "text": "chunkslow:1", "cwd": str(cwd), "permissions": "all"},
            )
            started = await asyncio.wait_for(first_transport.receive(first), timeout=1)
            await asyncio.wait_for(first_transport.receive(first), timeout=1)
            session_id = started["session_id"]
            await second_transport.send(
                second,
                {
                    "type": "prompt",
                    "text": "slow:1",
                    "cwd": str(cwd),
                    "session_id": session_id,
                    "permissions": "all",
                },
            )
            await asyncio.wait_for(second_transport.receive(second), timeout=1)
            await asyncio.wait_for(second_transport.receive(second), timeout=1)
            os.kill(os.getpid(), signal.SIGTERM)
            queued = await asyncio.wait_for(
                _receive_prompt_result(second_transport, second), timeout=1
            )
            active = await asyncio.wait_for(
                _receive_prompt_result(first_transport, first), timeout=2
            )
            assert queued[-1] == {"type": "shutting_down"}
            assert active[-1]["type"] == "prompt_done"
            await asyncio.wait_for(daemon_task, timeout=2)
        finally:
            await first_transport.close_connection(first)
            await second_transport.close_connection(second)
            await first_transport.cleanup()
            await second_transport.cleanup()
            await daemon.stop()
            assert not socket_path_for_target("mock").exists()
            assert not lock_path_for_target("mock").exists()

    asyncio.run(scenario())


def test_losing_daemon_exits_without_output(daemon_environment, capsys) -> None:
    async def scenario() -> None:
        _, _ = daemon_environment
        winner = Daemon("mock")
        assert await winner.start()
        metadata = json.loads(lock_path_for_target("mock").read_text())
        assert metadata["pid"] == os.getpid()
        assert metadata["target"] == "mock"
        assert metadata["socket"] == str(socket_path_for_target("mock"))
        assert "acpc_version" in metadata
        assert "start_time" in metadata
        loser = Daemon("mock")
        assert not await loser.start()
        await winner.stop()

    asyncio.run(scenario())
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_capacity_refuses_new_sessions_but_serves_existing(daemon_environment, monkeypatch) -> None:
    async def scenario() -> None:
        _, cwd = daemon_environment
        daemon = Daemon("mock")
        daemon_task = asyncio.create_task(daemon.run())
        await _wait_for_socket(socket_path_for_target("mock"))
        transport = UnixSocketTransport("mock")
        connection = await transport.connect()
        try:
            first = await _send_prompt(transport, connection, cwd, "existing")
            session_id = first[0]["session_id"]
            monkeypatch.setattr("acpc.daemon.rss_ceiling_mb", lambda: 1)
            monkeypatch.setattr("acpc.daemon.sample_process_tree_rss_mb", lambda pid: 2)
            refused = await _send_prompt(transport, connection, cwd, "new")
            assert refused[-1] == {"type": "capacity", "rss_mb": 2}
            served = await _send_prompt(transport, connection, cwd, "served", session_id)
            assert served[-1]["type"] == "prompt_done"
        finally:
            await transport.close_connection(connection)
            await transport.cleanup()
            await daemon.stop()
            await asyncio.wait_for(daemon_task, timeout=1)

    asyncio.run(scenario())


def test_adapter_stderr_is_truncated_and_written_to_target_log(daemon_environment) -> None:
    async def scenario() -> None:
        state, cwd = daemon_environment
        log_path = state / "log" / "mock.log"
        log_path.parent.mkdir(parents=True)
        log_path.write_text("old\n")
        daemon = Daemon("mock")
        daemon_task = asyncio.create_task(daemon.run())
        await _wait_for_socket(socket_path_for_target("mock"))
        transport = UnixSocketTransport("mock")
        connection = await transport.connect()
        try:
            frames = await _send_prompt(transport, connection, cwd, "stderr:adapter noise")
            assert frames[-1]["type"] == "prompt_done"
            await asyncio.sleep(0.05)
        finally:
            await transport.close_connection(connection)
            await transport.cleanup()
            await daemon.stop()
            await asyncio.wait_for(daemon_task, timeout=1)
        text = log_path.read_text()
        assert "old" not in text
        assert "adapter noise" in text

    asyncio.run(scenario())


def test_rss_ceiling_resolution_env_default_and_missing_procfs(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("ACPC_DAEMON_RSS_MAX", raising=False)
    monkeypatch.setattr("acpc.daemon._MEMINFO_PATH", tmp_path / "meminfo")
    (tmp_path / "meminfo").write_text("MemAvailable: 4194304 kB\n")
    assert read_mem_available_mb(tmp_path / "meminfo") == 4096
    assert rss_ceiling_mb() == 2048
    monkeypatch.setenv("ACPC_DAEMON_RSS_MAX", "1234")
    assert rss_ceiling_mb() == 1234
    monkeypatch.delenv("ACPC_DAEMON_RSS_MAX")
    (tmp_path / "meminfo").unlink()
    assert rss_ceiling_mb() == 2048


def test_max_age_recycles_after_active_prompt_finishes(daemon_environment, monkeypatch) -> None:
    async def scenario() -> None:
        _, cwd = daemon_environment
        monkeypatch.setenv("ACPC_DAEMON_MAX_AGE", "0.2")
        daemon = Daemon("mock")
        daemon_task = asyncio.create_task(daemon.run())
        await _wait_for_socket(socket_path_for_target("mock"))
        transport = UnixSocketTransport("mock")
        connection = await transport.connect()
        try:
            frames = await _send_prompt(transport, connection, cwd, "slow:1")
            assert frames[-1]["type"] == "prompt_done"
        finally:
            await transport.close_connection(connection)
            await transport.cleanup()
        await asyncio.wait_for(daemon_task, timeout=2)
        assert not socket_path_for_target("mock").exists()
        assert not lock_path_for_target("mock").exists()

    asyncio.run(scenario())
