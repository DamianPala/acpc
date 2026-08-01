"""Tests for the Phase 1 daemon core."""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
from typing import Any

import pytest

from acpc.daemon import Daemon
from acpc.ipc import Connection, UnixSocketTransport, socket_path_for_target


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
        frame = await transport.receive(connection)
        frames.append(frame)
        if frame["type"] in {"prompt_done", "error"}:
            return frames


async def _send_prompt(
    transport: UnixSocketTransport,
    connection: Connection,
    cwd: Path,
    text: str,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    await transport.send(
        connection,
        {
            "type": "prompt",
            "text": text,
            "cwd": str(cwd),
            "session_id": session_id,
            "permissions": "all",
            "output_mode": "text",
        },
    )
    return await _receive_prompt_result(transport, connection)


@pytest.fixture()
def daemon_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mock_agent_dir: Path,
) -> tuple[Path, Path]:
    del mock_agent_dir
    state = tmp_path / "state"
    caller_cwd = tmp_path / "caller"
    state.mkdir()
    caller_cwd.mkdir()
    monkeypatch.setenv("ACPC_STATE_DIR", str(state))
    return state, caller_cwd


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
            await daemon_task
            assert not state.joinpath("run", "mock.sock").exists()

    asyncio.run(scenario())


def test_disconnected_prompt_does_not_leak_into_reused_session(daemon_environment) -> None:
    async def scenario() -> None:
        _, cwd = daemon_environment
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
            started = await first_transport.receive(first_connection)
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
            await daemon_task

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
            await daemon.process.wait()
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
            await daemon_task

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
            await daemon_task

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
            await daemon_task

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
            await daemon_task

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
            await daemon_task

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
            await daemon_task

    asyncio.run(scenario())
