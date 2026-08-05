"""Tests for acpc.ipc transport primitives."""

import asyncio
import contextlib
import os
import stat
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

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
    monkeypatch.setenv("ACPC_HOME", str(tmp_path))

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


def test_socket_path_uses_daemon_dir_and_target_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path))

    path = socket_path_for_target("codex")

    assert path == tmp_path / "daemon" / "codex.sock"


@pytest.mark.parametrize("target", ["", "a/b", "../../escape"])
def test_socket_path_rejects_targets_that_could_escape_daemon_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    target: str,
) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path))

    with pytest.raises(ValueError):
        socket_path_for_target(target)


def test_socket_path_uses_full_path_hash_with_readable_prefix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = "target-" + "x" * 200
    first_state = tmp_path / "first"
    second_state = tmp_path / "second"

    monkeypatch.setenv("ACPC_HOME", str(first_state))
    first_path = socket_path_for_target(target)
    monkeypatch.setenv("ACPC_HOME", str(second_state))
    second_path = socket_path_for_target(target)

    assert first_path.name != second_path.name
    # How much of the target survives depends on how long the state directory
    # is, so pinning a fixed number of characters fails under any longer path,
    # such as the per-worker temporary directories a parallel run uses.
    prefix = first_path.name.split("-", 1)[0]
    assert prefix and target.startswith(prefix)
    # The digest may itself be truncated to fit the byte budget, so assert a
    # prefix match against the full hash rather than full containment.
    digest_part = first_path.name.rsplit("-", 1)[1].removesuffix(".sock")
    full_hash = short_target_hash(str(first_state / "daemon" / f"{target}.sock"))
    assert digest_part and full_hash.startswith(digest_part)
    assert first_path.name.endswith(".sock")
    # The OS contract: sockaddr_un caps Unix socket paths at 104 bytes on
    # macOS and 108 on Linux.
    unix_socket_path_limit = 104 if sys.platform == "darwin" else 108
    assert len(str(first_path).encode()) < unix_socket_path_limit


def test_socket_path_reports_when_hashed_path_still_does_not_fit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # A state root deeper than the OS socket-path limit leaves no room for
    # even a hashed socket name.
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / ("x" * 120)))

    with pytest.raises(ValueError, match="still exceeds the "):
        socket_path_for_target("codex")


def test_bind_creates_private_daemon_dir_and_socket(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path))
    daemon_dir = tmp_path / "daemon"
    daemon_dir.mkdir()
    daemon_dir.chmod(0o755)
    transport = UnixSocketTransport("permissions")

    async def scenario() -> None:
        await transport.bind()
        try:
            assert stat.S_IMODE(daemon_dir.stat().st_mode) == 0o700
            assert stat.S_IMODE(transport.path.stat().st_mode) == 0o600
        finally:
            await transport.cleanup()

    asyncio.run(scenario())


def test_path_is_resolved_lazily(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path))
    transport = UnixSocketTransport("lazy")
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "changed"))

    async def scenario() -> None:
        await transport.bind()
        try:
            assert transport.path == tmp_path / "changed" / "daemon" / "lazy.sock"
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
            assert server_connection.writer.is_closing()
            assert client_connection.writer.is_closing()

            # The fixture cleanup is intentionally idempotent after the assertion.
            await server.close_connection(server_connection)

    asyncio.run(scenario())


@pytest.mark.parametrize("yields", [0, 1, 2, 3, 5])
def test_cleanup_does_not_wait_for_pending_connection_callback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    yields: int,
) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path))

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
        finally:
            await client.close_connection(client_connection)
            await client.cleanup()

    asyncio.run(scenario())


def test_cleanup_closes_connection_with_callback_in_flight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path))

    async def scenario() -> None:
        callback_started = asyncio.Event()
        allow_callback = asyncio.Event()
        callback_finished = asyncio.Event()
        server = UnixSocketTransport("cleanup-in-flight")
        client = UnixSocketTransport("cleanup-in-flight")
        # Scheduling instrumentation, not a stub: the real callback still runs;
        # the wrapper only holds it open to force the cleanup race window.
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
            # The client observes the termination: its read completes with EOF
            # or a reset instead of hanging on a leaked server-side writer.
            with contextlib.suppress(ConnectionError):
                await asyncio.wait_for(client_connection.reader.read(), timeout=2)
        finally:
            allow_callback.set()
            await client.close_connection(client_connection)
            await client.cleanup()

    asyncio.run(scenario())


def test_cleanup_wakes_pending_accept(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path))
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


def test_twenty_connect_close_cycles_do_not_leak_file_descriptors(connected_pair) -> None:
    if not Path("/proc/self/fd").exists():
        pytest.skip("/proc-based descriptor accounting is unavailable")

    def open_fd_count() -> int:
        return len(os.listdir("/proc/self/fd"))

    async def cycle(server: UnixSocketTransport, client: UnixSocketTransport) -> None:
        accepting = asyncio.create_task(server.accept())
        client_connection = await client.connect()
        server_connection = await accepting
        await client.close_connection(client_connection)
        # The peer must observe the close as EOF; a writer left open by
        # close_connection would make this receive hang instead.
        with pytest.raises(ConnectionError, match="before sending an NDJSON frame"):
            await asyncio.wait_for(server.receive(server_connection), timeout=2)
        await server.close_connection(server_connection)

    async def scenario() -> None:
        async with connected_pair("bookkeeping") as (
            server,
            client,
            initial_server_connection,
            initial_client_connection,
        ):
            await client.close_connection(initial_client_connection)
            await server.close_connection(initial_server_connection)
            # Baseline after one full cycle so one-time allocations do not count.
            await cycle(server, client)
            baseline = open_fd_count()
            for _ in range(20):
                await cycle(server, client)

            assert open_fd_count() <= baseline

    asyncio.run(scenario())


def test_oversized_frame_is_reported_and_next_frame_is_read(connected_pair) -> None:
    async def scenario() -> None:
        async with connected_pair("oversized") as (
            server,
            _client,
            server_connection,
            client_connection,
        ):
            # The transport contract caps one NDJSON frame at 10 MiB.
            oversized = b'{"text":"' + b"x" * 10_485_761 + b'"}\n'
            following = b'{"type":"valid"}\n'

            async def write_all() -> None:
                # A frame this large overfills the socket buffer, so the write
                # must run concurrently with the reads that consume it.
                client_connection.writer.write(oversized + following)
                await client_connection.writer.drain()

            write_task = asyncio.create_task(write_all())
            try:
                with pytest.raises(FrameTooLargeError, match="frame exceeds"):
                    await server.receive(server_connection)
                assert await server.receive(server_connection) == {"type": "valid"}
            finally:
                await asyncio.wait_for(write_task, timeout=5)

    asyncio.run(scenario())


@pytest.mark.parametrize("payload", [b"\n", b"[1,2]\n", b"not json\n"])
def test_malformed_frames_raise_invalid_frame_error(connected_pair, payload: bytes) -> None:
    async def scenario() -> None:
        async with connected_pair("malformed") as (
            server,
            _client,
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
