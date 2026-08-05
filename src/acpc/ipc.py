"""Transport primitives for daemon IPC.

The daemon protocol uses newline-delimited JSON frames over a local socket.
The abstract transport keeps the daemon independent from any future Windows
named-pipe implementation. Sockets and lock files live under the state root's
`daemon/` directory, next to the per-target logs SPEC.md names.

Harvested from the 0.3.0.dev1 implementation; only the path layout and the
`ACPC_HOME` guidance in error messages changed.
"""

import asyncio
import contextlib
import hashlib
import json
import os
import sys
import uuid
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from acpc.paths import daemon_dir

type Frame = dict[str, Any]
type TransportRole = Literal["listener", "dialer"]

_FRAME_STREAM_LIMIT = 10_485_760
_SOCKET_PATH_LIMIT = 104 if sys.platform == "darwin" else 108
_TARGET_HASH_LENGTH = 16
_CLEANUP_GRACE_PERIOD = 1.0


class FrameTooLargeError(ValueError):
    """The complete frame exceeded the transport's configured size limit."""


class InvalidFrameError(ValueError):
    """The input was not a valid JSON object frame."""


@dataclass(frozen=True, slots=True)
class Connection:
    """One transport connection and its stable process-local identifier."""

    id: str
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter

    @property
    def connection_id(self) -> str:
        """Return the stable identifier using an explicit descriptive name."""
        return self.id


class DaemonTransport(ABC):
    """Platform-neutral interface for daemon request transport."""

    @abstractmethod
    async def bind(self) -> None:
        """Bind the server endpoint."""

    @abstractmethod
    async def accept(self) -> Connection:
        """Accept and return one connection."""

    @abstractmethod
    async def connect(self) -> Connection:
        """Connect to the server endpoint and return the connection."""

    @abstractmethod
    async def send(self, connection: Connection, frame: Mapping[str, Any]) -> None:
        """Send one protocol frame over a connection."""

    @abstractmethod
    async def receive(self, connection: Connection) -> Frame:
        """Receive one protocol frame from a connection."""

    @abstractmethod
    async def close_connection(self, connection: Connection) -> None:
        """Close one connection and release its bookkeeping entry."""

    @abstractmethod
    async def cleanup(self) -> None:
        """Release the endpoint and all connections owned by this transport."""

    async def stop_accepting(self) -> None:
        """Stop accepting new clients while preserving existing connections."""
        return


def short_target_hash(value: str) -> str:
    """Return a stable short hash for a full socket path or other input."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:_TARGET_HASH_LENGTH]


def socket_path_for_target(target: str) -> Path:
    """Return the daemon socket path for a target within the daemon dir."""
    _validate_target(target)
    directory = daemon_dir()
    regular_path = directory / f"{target}.sock"
    if _socket_path_length(regular_path) < _SOCKET_PATH_LIMIT:
        return regular_path

    hashed_path = _hashed_socket_path(regular_path, target)
    if hashed_path is None:
        digest = short_target_hash(str(regular_path))
        candidate = directory / f"{target[:1]}-{digest}.sock"
        raise ValueError(
            f"daemon socket path '{candidate}' still exceeds the {_SOCKET_PATH_LIMIT}-byte "
            "Unix limit; set ACPC_HOME to a shorter absolute path"
        )
    return hashed_path


def lock_path_for_target(target: str) -> Path:
    """Return the daemon lock path for a target within the daemon dir."""
    _validate_target(target)
    return daemon_dir() / f"{target}.lock"


class UnixSocketTransport(DaemonTransport):
    """NDJSON transport backed by a Unix domain socket.

    An instance claims either the listener or dialer role on its first socket
    operation. Keeping that role explicit prevents a dialer's cleanup from
    ever owning or removing a listener endpoint.
    """

    def __init__(self, target: str, socket_path: Path | None = None) -> None:
        self._target = target
        self._path = socket_path
        self._role: TransportRole | None = None
        self._server: asyncio.Server | None = None
        self._connections: asyncio.Queue[Connection] = asyncio.Queue()
        self._writers: set[asyncio.StreamWriter] = set()
        self._closing = False
        self._closing_event = asyncio.Event()

    @property
    def path(self) -> Path:
        """Return the resolved socket path after bind or connect."""
        if self._path is None:
            raise RuntimeError(
                f"daemon socket path for target '{self._target}' is unresolved; "
                "call bind() or connect() first"
            )
        return self._path

    async def bind(self) -> None:
        """Bind the socket and restrict its directory and socket permissions."""
        self._claim_role("listener")
        self._resolve_path()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.parent.chmod(0o700)
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()
            self._server = await asyncio.start_unix_server(
                self._queue_connection,
                path=str(self.path),
                limit=_FRAME_STREAM_LIMIT,
            )
            self.path.chmod(0o600)
        except OSError as error:
            await self._close_server()
            raise OSError(
                f"failed to bind daemon socket '{self.path}': {error}. "
                "Check ACPC_HOME permissions and remove a stale socket before retrying"
            ) from error

    async def stop_accepting(self) -> None:
        """Stop accepting new clients while keeping existing writers open."""
        self._closing = True
        self._closing_event.set()
        server = self._server
        self._server = None
        if server is not None:
            server.close()

    async def accept(self) -> Connection:
        """Wait for one client connection accepted by the server."""
        if self._server is None:
            raise RuntimeError(
                f"cannot accept daemon connections for target '{self._target}': call bind() first"
            )
        if self._closing:
            raise RuntimeError(
                f"cannot accept daemon connections for target '{self._target}': "
                "transport is closing"
            )

        connection_task = asyncio.create_task(self._connections.get())
        closing_task = asyncio.create_task(self._closing_event.wait())
        try:
            done, _ = await asyncio.wait(
                (connection_task, closing_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if closing_task in done:
                raise RuntimeError(
                    f"cannot accept daemon connections for target '{self._target}': "
                    "transport is closing"
                )
            return connection_task.result()
        finally:
            for task in (connection_task, closing_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(connection_task, closing_task, return_exceptions=True)

    async def connect(self) -> Connection:
        """Connect to the target daemon socket."""
        self._claim_role("dialer")
        self._resolve_path()
        try:
            reader, writer = await asyncio.open_unix_connection(
                path=str(self.path),
                limit=_FRAME_STREAM_LIMIT,
            )
        except OSError as error:
            raise OSError(
                f"failed to connect to daemon socket '{self.path}': {error}. "
                "Start the target daemon or remove its stale socket and retry"
            ) from error

        self._writers.add(writer)
        return Connection(uuid.uuid4().hex, reader, writer)

    async def send(self, connection: Connection, frame: Mapping[str, Any]) -> None:
        """Encode and send one NDJSON frame."""
        try:
            payload = (
                json.dumps(
                    dict(frame),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"failed to encode NDJSON frame for daemon socket '{self.path}': {error}"
            ) from error

        try:
            connection.writer.write(payload)
            await connection.writer.drain()
        except (ConnectionError, OSError) as error:
            raise ConnectionError(
                f"failed to send NDJSON frame on daemon socket '{self.path}': {error}"
            ) from error

    async def receive(self, connection: Connection) -> Frame:
        """Read, decode, and validate one NDJSON object frame."""
        try:
            line = await connection.reader.readuntil(b"\n")
        except asyncio.LimitOverrunError as error:
            await _drain_oversized_frame(connection.reader)
            raise FrameTooLargeError(
                f"failed to receive NDJSON frame from daemon socket '{self.path}': "
                f"frame exceeds {_FRAME_STREAM_LIMIT} bytes"
            ) from error
        except asyncio.IncompleteReadError as error:
            if error.partial:
                raise ConnectionError(
                    f"daemon socket '{self.path}' closed with an incomplete NDJSON frame"
                ) from error
            raise ConnectionError(
                f"daemon socket '{self.path}' closed before sending an NDJSON frame"
            ) from error
        except (ConnectionError, OSError) as error:
            raise ConnectionError(
                f"failed to receive NDJSON frame from daemon socket '{self.path}': {error}"
            ) from error

        try:
            frame = json.loads(line[:-1].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise InvalidFrameError(
                f"failed to decode NDJSON frame from daemon socket '{self.path}': {error}"
            ) from error
        if not isinstance(frame, dict):
            raise InvalidFrameError(
                f"received a non-object NDJSON frame from daemon socket '{self.path}'; "
                "send a JSON object per line"
            )
        return frame

    async def close_connection(self, connection: Connection) -> None:
        """Close one writer and remove it from this transport's bookkeeping."""
        self._writers.discard(connection.writer)
        await _close_writer(connection.writer)

    async def cleanup(self) -> None:
        """Stop accepting and close every connection within a bounded grace period."""
        server = self._server
        await self.stop_accepting()
        if server is not None:
            server.close_clients()

        writers = tuple(self._writers)
        self._writers.clear()
        operations = []
        if writers:
            operations.append(_close_writers(writers))
        if server is not None:
            operations.append(server.wait_closed())

        try:
            await asyncio.wait_for(
                asyncio.gather(*operations),
                timeout=_CLEANUP_GRACE_PERIOD,
            )
        except TimeoutError as error:
            # Server tracks transports before scheduling _queue_connection. The
            # bounded wait plus abort handles both that callback window and a
            # transport that does not finish closing, so cleanup cannot hang.
            if server is not None:
                server.abort_clients()
            _abort_writers(writers)
            raise TimeoutError(
                f"timed out cleaning up daemon transport for target '{self._target}' "
                f"after {_CLEANUP_GRACE_PERIOD:g}s; aborted active connections"
            ) from error
        if self._role == "listener" and self._path is not None:
            with contextlib.suppress(FileNotFoundError, OSError):
                self._path.unlink()

    async def _queue_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        if self._closing:
            # The server may schedule this callback after cleanup has already
            # returned. Abort here because no caller remains to await a close.
            writer.transport.abort()
            return
        self._writers.add(writer)
        await self._connections.put(Connection(uuid.uuid4().hex, reader, writer))

    async def _close_server(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            await server.wait_closed()

    def _claim_role(self, role: TransportRole) -> None:
        if self._role is None:
            self._role = role
        elif self._role != role:
            raise RuntimeError(
                f"daemon transport for target '{self._target}' is already a {self._role}"
            )

    def _resolve_path(self) -> None:
        if self._path is None:
            self._path = socket_path_for_target(self._target)


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()


async def _close_writers(writers: tuple[asyncio.StreamWriter, ...]) -> None:
    await asyncio.gather(*(_close_writer(writer) for writer in writers))


def _abort_writers(writers: tuple[asyncio.StreamWriter, ...]) -> None:
    for writer in writers:
        writer.transport.abort()


async def _drain_oversized_frame(reader: asyncio.StreamReader) -> None:
    """Discard through the next newline while preserving following frames."""
    # StreamReader has no public limit override, so temporarily widen its
    # private one to consume exactly one oversized line without discarding
    # the next frame.
    original_limit = reader._limit  # pyright: ignore[reportAttributeAccessIssue]
    reader._limit = sys.maxsize  # pyright: ignore[reportAttributeAccessIssue]
    try:
        with contextlib.suppress(asyncio.IncompleteReadError, ConnectionError, OSError):
            await reader.readuntil(b"\n")
    finally:
        reader._limit = original_limit  # pyright: ignore[reportAttributeAccessIssue]


def _hashed_socket_path(regular_path: Path, target: str) -> Path | None:
    """Build a shorter readable hashed path when the regular path is too long."""
    available = min(_SOCKET_PATH_LIMIT - 1, _socket_path_length(regular_path) - 1)
    available -= _socket_path_length(regular_path.parent) + 1
    digest_length = min(_TARGET_HASH_LENGTH, available - 7)
    if digest_length < 1:
        return None

    digest = short_target_hash(str(regular_path))[:digest_length]
    prefix = _prefix_by_bytes(target, available - digest_length - 6)
    if not prefix:
        return None
    candidate = regular_path.parent / f"{prefix}-{digest}.sock"
    if _socket_path_length(candidate) >= _SOCKET_PATH_LIMIT:
        return None
    return candidate


def _prefix_by_bytes(value: str, maximum: int) -> str:
    """Return the longest leading text that fits the byte budget."""
    prefix = ""
    for character in value:
        candidate = prefix + character
        if len(candidate.encode("utf-8")) > maximum:
            break
        prefix = candidate
    return prefix


def _validate_target(target: str) -> None:
    if not target:
        raise ValueError("daemon target must be a non-empty name")
    if Path(target).name != target:
        raise ValueError(f"daemon target '{target}' must not contain path separators")


def _socket_path_length(path: Path) -> int:
    return len(os.fsencode(str(path)))
