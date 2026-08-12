"""Reaching the per-target daemon, and starting one when there isn't one yet.

SPEC.md `daemon`. This module is the only thing that ever starts a daemon:
there is no `start` verb, so `ensure_daemon` is what "auto-managed" means in
practice.

Two contracts are load-bearing and outlive this file's internals:

- `ensure_daemon` never raises for an ordinary failure. It returns
  `DaemonUnavailable` with a human reason and the caller falls back to a direct
  child *visibly* — a restricted sandbox has to degrade, not error.
- Starting is race-safe. Several `acpc run` processes can reach for the same
  cold target at once; the winner is decided by an exclusive lock on the
  target's lock file and the losers connect to what the winner started.

`DaemonConnection` splits starting a turn from awaiting it, which the S06 stub
did not: `--bg` starts a turn and walks away, and a client that takes a SIGTERM
stops awaiting without touching the turn. Both need those to be separate calls.
"""

import asyncio
import contextlib
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Protocol

from acpc import __version__, ipc, paths

# How long to keep reaching for the socket after starting a daemon before
# deciding it will never come up. A cold adapter start is well inside this.
START_TIMEOUT = 10.0
_CONNECT_RETRY_INTERVAL = 0.05


@dataclass(frozen=True, slots=True)
class DaemonUnavailable:
    """Why this call could not use the daemon; `reason` reaches the summary."""

    reason: str


class DaemonConnection(Protocol):
    """What callers need from a live daemon, independent of the transport."""

    async def start_turn(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Hand a turn to the daemon and return as soon as it is accepted."""
        ...

    async def await_turn(self, session_id: str) -> dict[str, Any]:
        """Block until the turn finishes and return its outcome."""
        ...

    async def await_preparation(self, session_id: str) -> dict[str, Any]:
        """Block until a deferred continuation has claimed its next turn."""
        ...

    async def cancel(self, session_id: str) -> dict[str, Any]:
        """Ask the daemon to cancel a session's in-flight turn."""
        ...

    async def status(self) -> dict[str, Any]:
        """Report pid, uptime, log path and the sessions this daemon holds."""
        ...

    async def stop(self) -> dict[str, Any]:
        """Stop the daemon; it finishes its sessions before exiting."""
        ...

    async def close(self) -> None:
        """Release this client's handle on the daemon."""
        ...


class _SocketDaemon:
    """A live connection to one target's daemon."""

    def __init__(self, target: str, transport: ipc.UnixSocketTransport, conn: ipc.Connection):
        self.target = target
        self._transport = transport
        self._conn = conn
        self._lock = asyncio.Lock()

    async def call(self, frame: dict[str, Any]) -> dict[str, Any]:
        """Send one frame and read its reply.

        Serialized: the protocol is strictly request/response, so two
        overlapping calls on one connection would read each other's replies.
        """
        async with self._lock:
            await self._transport.send(self._conn, frame)
            return await self._transport.receive(self._conn)

    async def start_turn(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.call({"op": "start", "session_id": session_id, "payload": payload})

    async def await_turn(self, session_id: str) -> dict[str, Any]:
        return await self.call({"op": "await", "session_id": session_id})

    async def await_preparation(self, session_id: str) -> dict[str, Any]:
        return await self.call({"op": "await_preparation", "session_id": session_id})

    async def cancel(self, session_id: str) -> dict[str, Any]:
        return await self.call({"op": "cancel", "session_id": session_id})

    async def status(self) -> dict[str, Any]:
        return await self.call({"op": "status"})

    async def stop(self) -> dict[str, Any]:
        return await self.call({"op": "stop"})

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._transport.close_connection(self._conn)
        with contextlib.suppress(Exception):
            await self._transport.cleanup()


async def connect(target: str) -> _SocketDaemon | None:
    """Connect to a running daemon of this build, or return None."""
    transport = ipc.UnixSocketTransport(target)
    try:
        conn = await transport.connect()
    except (ConnectionError, OSError, ValueError):
        with contextlib.suppress(Exception):
            await transport.cleanup()
        return None

    daemon = _SocketDaemon(target, transport, conn)
    try:
        hello = await daemon.call({"op": "hello", "version": __version__})
    except (ConnectionError, OSError):
        await daemon.close()
        return None
    if not hello.get("ok"):
        # Version skew: the daemon is standing down for us, so this connection
        # is dead. The caller starts a matching one.
        await daemon.close()
        return None
    return daemon


async def ensure_daemon(target: str) -> DaemonConnection | DaemonUnavailable:
    """Connect to the daemon serving `target`, starting it if needed.

    Returns a live connection, or `DaemonUnavailable` naming why the direct
    path has to be used instead.
    """
    existing = await connect(target)
    if existing is not None:
        return existing

    try:
        lock_path = ipc.lock_path_for_target(target)
        paths.ensure_private_dir(lock_path.parent)
    except (OSError, ValueError) as error:
        return DaemonUnavailable(f"no daemon socket for {target}: {error}")

    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        holder = _try_lock(lock_path)
        if holder is None:
            # Someone else is starting this daemon; wait for their socket.
            await asyncio.sleep(_CONNECT_RETRY_INTERVAL)
            candidate = await connect(target)
            if candidate is not None:
                return candidate
            continue
        try:
            # Re-check under the lock: the previous holder may have finished
            # starting between our failed connect and taking the lock.
            candidate = await connect(target)
            if candidate is not None:
                return candidate
            problem = _spawn_daemon(target)
            if problem is not None:
                return DaemonUnavailable(problem)
            candidate = await _await_socket(target, deadline)
            if candidate is not None:
                return candidate
            return DaemonUnavailable(f"the daemon for {target} did not come up")
        finally:
            _unlock(holder)
    return DaemonUnavailable(f"timed out waiting for the daemon for {target}")


async def _await_socket(target: str, deadline: float) -> _SocketDaemon | None:
    while time.monotonic() < deadline:
        await asyncio.sleep(_CONNECT_RETRY_INTERVAL)
        candidate = await connect(target)
        if candidate is not None:
            return candidate
    return None


def _spawn_daemon(target: str) -> str | None:
    """Start the daemon detached, with its output in the per-target log.

    The log is where adapter stderr ends up too: the daemon inherits these
    handles and `spawn_adapter` forwards the adapter's stderr to its own.
    """
    from acpc import daemon as daemon_module

    try:
        log_file = daemon_module.log_path_for_target(target)
        paths.ensure_private_dir(log_file.parent)
        handle: IO[bytes] = log_file.open("ab")
    except OSError as error:
        return f"cannot open the daemon log for {target}: {error}"

    try:
        extra: dict[str, Any] = {}
        if sys.platform != "win32":
            extra["start_new_session"] = True
        subprocess.Popen(
            [sys.executable, "-m", "acpc.daemon", target],
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=handle,
            env=dict(os.environ),
            **extra,
        )
    except OSError as error:
        return f"cannot start a daemon for {target}: {error}"
    finally:
        handle.close()
    return None


def _try_lock(path: Path) -> IO[bytes] | None:
    """Take the target's start lock, or return None when someone else holds it."""
    try:
        # Deliberately not a context manager: the lock lives exactly as long as
        # this handle, and _unlock is what closes it.
        handle = open(path, "a+b")  # noqa: SIM115
    except OSError:
        return None
    if sys.platform == "win32":  # pragma: no cover - POSIX is the tested path
        return handle

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _unlock(handle: IO[bytes]) -> None:
    if sys.platform != "win32":
        import fcntl

        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    with contextlib.suppress(OSError):
        handle.close()


async def cancel_turn(target: str, session_id: str) -> bool:
    """Cancel a turn over a connection of its own.

    A cancel has to overtake the `await` it is interrupting, and one
    connection serves one request at a time — sending it down the awaiting
    connection would queue behind the very reply it is meant to prevent.
    """
    daemon = await connect(target)
    if daemon is None:
        return False
    try:
        reply = await daemon.cancel(session_id)
    except (ConnectionError, OSError):
        return False
    finally:
        await daemon.close()
    return bool(reply.get("ok"))
