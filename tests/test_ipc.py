"""Tests for acpc.ipc transport primitives."""

import asyncio
import contextlib
import stat
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import pytest

import acpc.ipc as ipc
from acpc.ipc import (
    Connection,
    FrameTooLargeError,
    InvalidFrameError,
    UnixSocketTransport,
    short_target_hash,
    socket_path_for_target,
)


@pytest.fixture()
def connected_pair(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Return an async context manager for one connected listener/dialer pair."""
    monkeypatch.setenv("ACPC_STATE_DIR", str(tmp_path))

    @asynccontextmanager
    async def pair(
        target: str = "pair",
    ) -> AsyncIterator[
        tuple[
            UnixSocketTransport,
            UnixSocketTransport,
            Connection,
            Connection,
        ]
    ]:
        server = UnixSocketTransport(target)
        client = UnixSocketTransport(target)
        await server.bind()
        accepting = asyncio.create_task(server.accept())
        client_connection = await client.connect()
        server_connection = await accepting
        try:
            yield server, client, server_connection, client_connection
        finally:
            await client.close_connection(client_connection)
            await server.close_connection(server_connection)
            await server.cleanup()
            await client.cleanup()

    return pair


def test_socket_path_uses_state_dir_and_target_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ACPC_STATE_DIR", str(tmp_path))

    path = socket_path_for_target("codex")

    assert path == tmp_path / "run" / "codex.sock"


@pytest.mark.parametrize("target", ["", "a/b", "../../escape"])
def test_socket_path_rejects_targets_that_could_escape_run_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    target: str,
) -> None:
    monkeypatch.setenv("ACPC_STATE_DIR", str(tmp_path))

    with pytest.raises(ValueError):
        socket_path_for_target(target)


def test_socket_path_uses_full_path_hash_with_readable_prefix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = "target-" + "x" * 200
    first_state = tmp_path / "first"
    second_state = tmp_path / "second"

    monkeypatch.setenv("ACPC_STATE_DIR", str(first_state))
    first_path = socket_path_for_target(target)
    monkeypatch.setenv("ACPC_STATE_DIR", str(second_state))
    second_path = socket_path_for_target(target)

    assert first_path.name != second_path.name
    assert first_path.name.startswith(target[:8])
    assert short_target_hash(str(first_state / "run" / f"{target}.sock")) in first_path.name
    assert first_path.name.endswith(".sock")
    assert len(str(first_path).encode()) < ipc._SOCKET_PATH_LIMIT


def test_socket_path_reports_when_hashed_path_still_does_not_fit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ACPC_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(ipc, "_SOCKET_PATH_LIMIT", 20)

    with pytest.raises(ValueError, match="still exceeds the 20-byte Unix limit"):
        socket_path_for_target("codex")


def test_bind_creates_private_run_dir_and_socket(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ACPC_STATE_DIR", str(tmp_path))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    run_dir.chmod(0o755)
    transport = UnixSocketTransport("permissions")

    async def scenario() -> None:
        await transport.bind()
        try:
            assert stat.S_IMODE(run_dir.stat().st_mode) == 0o700
            assert stat.S_IMODE(transport.path.stat().st_mode) == 0o600
        finally:
            await transport.cleanup()

    asyncio.run(scenario())


def test_path_is_resolved_lazily(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ACPC_STATE_DIR", str(tmp_path))
    transport = UnixSocketTransport("lazy")
    monkeypatch.setenv("ACPC_STATE_DIR", str(tmp_path / "changed"))

    async def scenario() -> None:
        await transport.bind()
        try:
            assert transport.path == tmp_path / "changed" / "run" / "lazy.sock"
        finally:
            await transport.cleanup()

    asyncio.run(scenario())


def test_accept_before_bind_raises() -> None:
    transport = UnixSocketTransport("unbound")

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match=r"call bind\(\) first"):
            await transport.accept()

    asyncio.run(scenario())


def test_bind_connect_round_trip_large_non_ascii_frame(connected_pair) -> None:
    async def scenario() -> None:
        async with connected_pair("roundtrip") as (
            server,
            client,
            server_connection,
            client_connection,
        ):
            frame = {"type": "prompt", "text": "zażółć gęślą jaźń " + "x" * 100_000}
            await client.send(client_connection, frame)
            assert await server.receive(server_connection) == frame

            response = {"type": "prompt_done", "text": "received " + "✓" * 100_000}
            await server.send(server_connection, response)
            assert await client.receive(client_connection) == response

    asyncio.run(scenario())


def test_cleanup_with_connected_client_closes_everything(connected_pair) -> None:
    async def scenario() -> None:
        async with connected_pair("cleanup-connected") as (
            server,
            client,
            server_connection,
            client_connection,
        ):
            await client.close_connection(client_connection)
            await asyncio.wait_for(server.cleanup(), timeout=2)
            assert not server.path.exists()
            assert not server._writers
            assert not client._writers

            # The fixture cleanup is intentionally idempotent after the assertion.
            await server.close_connection(server_connection)

    asyncio.run(scenario())


@pytest.mark.parametrize("yields", [0, 1, 2, 3, 5])
def test_cleanup_does_not_wait_for_pending_connection_callback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    yields: int,
) -> None:
    monkeypatch.setenv("ACPC_STATE_DIR", str(tmp_path))

    async def scenario() -> None:
        server = UnixSocketTransport("cleanup-race")
        client = UnixSocketTransport("cleanup-race")
        await server.bind()
        client_connection = await client.connect()
        try:
            for _ in range(yields):
                await asyncio.sleep(0)

            # Close the dialer before the listener teardown so asyncio.run does not
            # leave its client-side transport for the event loop finalizer.
            await client.close_connection(client_connection)
            await asyncio.wait_for(server.cleanup(), timeout=2)
            assert not server.path.exists()
            assert not server._writers
        finally:
            await client.close_connection(client_connection)
            await client.cleanup()

    asyncio.run(scenario())


def test_cleanup_closes_connection_with_callback_in_flight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ACPC_STATE_DIR", str(tmp_path))

    async def scenario() -> None:
        callback_started = asyncio.Event()
        allow_callback = asyncio.Event()
        callback_finished = asyncio.Event()
        server = UnixSocketTransport("cleanup-in-flight")
        client = UnixSocketTransport("cleanup-in-flight")
        original_callback = server._queue_connection

        async def delayed_callback(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            callback_started.set()
            await allow_callback.wait()
            try:
                await original_callback(reader, writer)
            finally:
                callback_finished.set()

        monkeypatch.setattr(server, "_queue_connection", delayed_callback)
        await server.bind()
        client_connection = await client.connect()
        try:
            await asyncio.wait_for(callback_started.wait(), timeout=2)
            cleanup_task = asyncio.create_task(server.cleanup())
            await asyncio.sleep(0)
            allow_callback.set()
            await asyncio.wait_for(cleanup_task, timeout=2)
            await asyncio.wait_for(callback_finished.wait(), timeout=2)

            assert not server.path.exists()
            assert not server._writers
        finally:
            allow_callback.set()
            await client.close_connection(client_connection)
            await client.cleanup()

    asyncio.run(scenario())


def test_cleanup_wakes_pending_accept(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ACPC_STATE_DIR", str(tmp_path))
    server = UnixSocketTransport("cleanup-accept")

    async def scenario() -> None:
        await server.bind()
        accepting = asyncio.create_task(server.accept())
        await asyncio.sleep(0)
        await asyncio.wait_for(server.cleanup(), timeout=2)
        with pytest.raises(RuntimeError, match="transport is closing"):
            await asyncio.wait_for(accepting, timeout=2)

    asyncio.run(scenario())


def test_dialer_cleanup_does_not_remove_listener_socket(connected_pair) -> None:
    async def scenario() -> None:
        async with connected_pair("dialer-cleanup") as (server, client, _, client_connection):
            await client.close_connection(client_connection)
            await client.cleanup()
            assert server.path.exists()

    asyncio.run(scenario())


def test_close_connection_drops_writers_after_twenty_cycles(connected_pair) -> None:
    async def scenario() -> None:
        async with connected_pair("bookkeeping") as (
            server,
            client,
            initial_server_connection,
            initial_client_connection,
        ):
            await client.close_connection(initial_client_connection)
            await server.close_connection(initial_server_connection)
            for _ in range(20):
                accepting = asyncio.create_task(server.accept())
                client_connection = await client.connect()
                server_connection = await accepting
                await client.close_connection(client_connection)
                await server.close_connection(server_connection)

            assert not server._writers
            assert not client._writers

    asyncio.run(scenario())


def test_oversized_frame_is_reported_and_next_frame_is_read(
    connected_pair,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ipc, "_FRAME_STREAM_LIMIT", 64 * 1024)

    async def scenario() -> None:
        async with connected_pair("oversized") as (
            server,
            client,
            server_connection,
            client_connection,
        ):
            oversized = b'{"text":"' + b"x" * (ipc._FRAME_STREAM_LIMIT + 1) + b'"}\n'
            following = b'{"type":"valid"}\n'
            client_connection.writer.write(oversized + following)
            await client_connection.writer.drain()

            with pytest.raises(FrameTooLargeError, match="frame exceeds"):
                await server.receive(server_connection)
            assert await server.receive(server_connection) == {"type": "valid"}

    asyncio.run(scenario())


@pytest.mark.parametrize("payload", [b"\n", b"[1,2]\n", b"not json\n"])
def test_malformed_frames_raise_invalid_frame_error(connected_pair, payload: bytes) -> None:
    async def scenario() -> None:
        async with connected_pair("malformed") as (
            server,
            client,
            server_connection,
            client_connection,
        ):
            client_connection.writer.write(payload)
            await client_connection.writer.drain()
            with pytest.raises(InvalidFrameError):
                await server.receive(server_connection)

    asyncio.run(scenario())


def test_disconnect_messages_distinguish_clean_eof_and_partial_line(connected_pair) -> None:
    async def scenario() -> None:
        async with connected_pair("disconnect") as (
            server,
            client,
            server_connection,
            client_connection,
        ):
            client_connection.writer.write(b'{"partial":')
            await client_connection.writer.drain()
            await client.close_connection(client_connection)
            with pytest.raises(ConnectionError, match="incomplete NDJSON frame"):
                await server.receive(server_connection)
            await server.close_connection(server_connection)

        async with connected_pair("clean-eof") as (
            server,
            client,
            server_connection,
            client_connection,
        ):
            await client.close_connection(client_connection)
            with pytest.raises(ConnectionError, match="before sending an NDJSON frame"):
                await server.receive(server_connection)

    asyncio.run(scenario())


def test_five_concurrent_clients_receive_their_own_replies(connected_pair) -> None:
    async def scenario() -> None:
        async with connected_pair("concurrent") as (
            server,
            client,
            first_server_connection,
            first_client_connection,
        ):
            server_connections = [first_server_connection]
            client_connections = [first_client_connection]
            accepting = [asyncio.create_task(server.accept()) for _ in range(4)]
            client_connections.extend(await asyncio.gather(*(client.connect() for _ in range(4))))
            server_connections.extend(await asyncio.gather(*accepting))

            await asyncio.gather(
                *(
                    client.send(connection, {"index": index})
                    for index, connection in enumerate(client_connections)
                )
            )
            requests = await asyncio.gather(
                *(server.receive(connection) for connection in server_connections)
            )
            await asyncio.gather(
                *(
                    server.send(connection, {"reply": request["index"]})
                    for connection, request in zip(server_connections, requests)
                )
            )
            replies = await asyncio.gather(
                *(client.receive(connection) for connection in client_connections)
            )
            assert {reply["reply"] for reply in replies} == set(range(5))
            assert len({connection.id for connection in client_connections}) == 5

    asyncio.run(scenario())


def test_cleanup_suppresses_already_closed_writer_errors(connected_pair) -> None:
    async def scenario() -> None:
        async with connected_pair("closed-writer") as (
            server,
            client,
            server_connection,
            client_connection,
        ):
            server_connection.writer.close()
            with contextlib.suppress(OSError):
                await server_connection.writer.wait_closed()
            await client.close_connection(client_connection)
            await asyncio.wait_for(server.cleanup(), timeout=2)

    asyncio.run(scenario())
