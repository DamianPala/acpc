"""Client for the persistent per-target acpc daemon."""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import errno
import json
import os
import platform
import signal
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised only on Windows
    fcntl = None  # type: ignore[assignment]

from acp.schema import SessionNotification

from acpc import __version__
from acpc.client import AcpcClient, PermissionLevel
from acpc.ipc import (
    Connection,
    UnixSocketTransport,
    lock_path_for_target,
    socket_path_for_target,
)
from acpc.output import OutputHandler, OutputMode, stderr, stderr_error, stderr_permission
from acpc.sessions import (
    load_last_session,
    process_cmdline,
    process_start_time,
    run_dir,
    save_last_session,
)

if TYPE_CHECKING:
    from acpc.runner import RunConfig


DirectRetry = Callable[[], Awaitable[int]]

READINESS_TIMEOUT = 15.0
READINESS_POLL_INTERVAL = 0.1
STALE_SHUTDOWN_TIMEOUT = 10.0
CANCEL_GRACE = 2.0
_PIDFD_SYSCALLS = {
    "aarch64": (434, 424),
    "ppc64le": (434, 424),
    "riscv64": (434, 424),
    "s390x": (434, 424),
    "x86_64": (434, 424),
}

_STOP_REASON_EXIT: dict[str, int] = {
    "end_turn": 0,
    "max_tokens": 1,
    "max_turn_requests": 1,
    "refusal": 1,
    "cancelled": 130,
}


class DaemonUnavailableError(RuntimeError):
    """The daemon could not become usable for this prompt."""


class DaemonProtocolError(RuntimeError):
    """The daemon sent a frame that the client could not process."""


@dataclass
class _ValidatedDaemonProcess:
    """A daemon identity validated strongly enough for one termination."""

    pid: int
    start_time: str
    cmdline: list[str]
    pidfd: int | None = None

    def close(self) -> None:
        if self.pidfd is not None:
            with contextlib.suppress(OSError):
                os.close(self.pidfd)
            self.pidfd = None


class DaemonClient:
    """Connect to one target daemon and stream one prompt response."""

    def __init__(
        self,
        target: str,
        *,
        readiness_timeout: float = READINESS_TIMEOUT,
        poll_interval: float = READINESS_POLL_INTERVAL,
        stale_shutdown_timeout: float = STALE_SHUTDOWN_TIMEOUT,
        popen_factory: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        self.target = target
        self.readiness_timeout = readiness_timeout
        self.poll_interval = poll_interval
        self.stale_shutdown_timeout = stale_shutdown_timeout
        self._popen_factory = popen_factory
        self._spawned_daemon = False

    def _report_connected(self) -> None:
        """Say which daemon served this call, and whether we had to start it.

        Reported after connecting rather than after spawning: every client spawns a
        daemon and the losers of the lock exit, so the process we started is often
        not the one answering. The PID here is the one that holds the lock.
        """
        metadata = self._read_lock_metadata() or {}
        pid = metadata.get("pid")
        where = f" (pid {pid})" if isinstance(pid, int) else ""
        stderr(f"daemon: {'started' if self._spawned_daemon else 'connected'}{where}")

    async def connect(self) -> tuple[UnixSocketTransport, Connection]:
        """Connect to a ready daemon, reconciling stale state before polling."""
        if sys.platform == "win32":
            raise DaemonUnavailableError("daemon transport is unavailable on Windows")
        self._spawned_daemon = False
        await self._prepare_endpoint()

        deadline = asyncio.get_running_loop().time() + self.readiness_timeout
        recovered_connection = False
        while True:
            socket_was_present = self._socket_path().exists()
            connection = await self._try_connect()
            if connection is not None:
                self._report_connected()
                return connection
            if socket_was_present and not recovered_connection:
                await self._recover_after_connection_failure()
                recovered_connection = True
                continue
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise DaemonUnavailableError(
                    f"daemon for target '{self.target}' was not ready after "
                    f"{self.readiness_timeout:g}s"
                )
            await asyncio.sleep(min(self.poll_interval, remaining))

    async def prompt(
        self,
        *,
        text: str,
        cwd: str | None,
        permissions: str,
        output: OutputHandler,
        model: str | None = None,
        mode: str | None = None,
        session_id: str | None = None,
        output_mode: str = "text",
        direct_retry: DirectRetry | None = None,
        timeout: float | None = None,
        print_session_id: bool = False,
    ) -> int:
        """Send one prompt and return the daemon-provided exit code."""
        transport, connection = await self.connect()
        cancel_reason: list[str | None] = [None]
        stream_task: asyncio.Task[int] | None = None
        try:
            await transport.send(
                connection,
                self._prompt_frame(
                    text=text,
                    cwd=cwd,
                    permissions=permissions,
                    model=model,
                    mode=mode,
                    session_id=session_id,
                    output_mode=output_mode,
                ),
            )
            stream_task = asyncio.create_task(
                self._stream_response(
                    transport,
                    connection,
                    output,
                    direct_retry,
                    cancel_reason,
                    print_session_id,
                    cwd,
                )
            )
            if timeout is None or timeout <= 0:
                return await asyncio.shield(stream_task)
            completed, _ = await asyncio.wait({stream_task}, timeout=timeout)
            if completed:
                return stream_task.result()
            return await self._cancel_prompt(
                transport,
                connection,
                stream_task,
                reason="timeout",
                cancel_reason=cancel_reason,
            )
        except asyncio.CancelledError:
            current_task = asyncio.current_task()
            if current_task is not None:
                current_task.uncancel()
            if stream_task is None:
                raise
            return await self._cancel_prompt(
                transport,
                connection,
                stream_task,
                reason="interrupt",
                cancel_reason=cancel_reason,
            )
        except KeyboardInterrupt:
            if stream_task is None:
                raise
            return await self._cancel_prompt(
                transport,
                connection,
                stream_task,
                reason="interrupt",
                cancel_reason=cancel_reason,
            )
        except DaemonProtocolError:
            raise
        except (ConnectionError, OSError, ValueError) as error:
            raise DaemonUnavailableError(str(error)) from error
        finally:
            with contextlib.suppress(ConnectionError, OSError, RuntimeError, ValueError):
                await transport.close_connection(connection)
            with contextlib.suppress(ConnectionError, OSError, RuntimeError, ValueError):
                await transport.cleanup()

    async def _stream_response(
        self,
        transport: UnixSocketTransport,
        connection: Connection,
        output: OutputHandler,
        direct_retry: DirectRetry | None,
        cancel_reason: list[str | None] | None = None,
        print_session_id: bool = False,
        cwd: str | None = None,
    ) -> int:
        acp_client = AcpcClient(output, PermissionLevel.NONE, is_tty=False)
        session_id: str | None = None
        printed_session_id = False
        while True:
            frame = await transport.receive(connection)
            frame_type = frame.get("type")
            if frame_type == "session_started":
                session_id = self._session_id(frame)
                output.on_session_started(session_id)
                if print_session_id:
                    print(session_id, flush=True)
                    printed_session_id = True
            elif frame_type == "session_update":
                await self._handle_update(acp_client, frame)
            elif frame_type == "permission":
                self._handle_permission(frame)
            elif frame_type == "queued":
                self._handle_queued(frame)
            elif frame_type == "cancel_ack":
                self._validate_cancel_ack(frame)
            elif frame_type == "prompt_done":
                return self._handle_done(
                    output,
                    frame,
                    session_id,
                    cancel_reason,
                    print_session_id and not printed_session_id,
                    cwd,
                )
            elif frame_type == "error":
                return self._handle_error(output, frame, session_id)
            elif frame_type == "shutting_down":
                return await self._retry_direct(direct_retry)
            elif frame_type == "capacity":
                raise DaemonUnavailableError("daemon is at capacity")
            else:
                raise DaemonProtocolError(f"unsupported daemon frame type: {frame_type!r}")

    async def _handle_update(self, client: AcpcClient, frame: dict[str, Any]) -> None:
        session_id = self._session_id(frame)
        payload = frame.get("update")
        if not isinstance(payload, dict):
            raise DaemonProtocolError("session_update frame has no object update payload")
        try:
            notification = SessionNotification.model_validate(
                {"sessionId": session_id, "update": payload}
            )
        except ValueError as error:
            raise DaemonProtocolError(f"invalid session_update payload: {error}") from error
        await client.session_update(notification.session_id, notification.update)

    @classmethod
    def _handle_permission(cls, frame: dict[str, Any]) -> None:
        cls._session_id(frame)
        kind = frame.get("kind")
        title = frame.get("title")
        outcome = frame.get("outcome")
        if not isinstance(kind, str) or not isinstance(title, str) or not isinstance(outcome, str):
            raise DaemonProtocolError("permission frame has invalid fields")
        if outcome not in {"allow", "deny"}:
            raise DaemonProtocolError(f"permission frame has invalid outcome: {outcome!r}")
        stderr_permission(kind, title, outcome)

    @staticmethod
    def _handle_queued(frame: dict[str, Any]) -> None:
        position = frame.get("position", "?")
        stderr(f"queued (position {position})")

    def _handle_done(
        self,
        output: OutputHandler,
        frame: dict[str, Any],
        session_id: str | None,
        cancel_reason: list[str | None] | None = None,
        print_session_id: bool = False,
        cwd: str | None = None,
    ) -> int:
        resolved_session_id = self._session_id(frame, fallback=session_id)
        stop_reason = frame.get("stop_reason")
        if not isinstance(stop_reason, str):
            raise DaemonProtocolError("prompt_done frame has no stop_reason")
        if print_session_id:
            print(resolved_session_id, flush=True)
        exit_code = self._exit_code_for_done(stop_reason, frame, cancel_reason)
        output.on_session_ended(resolved_session_id, stop_reason, exit_code)
        output.finalize()
        save_last_session(self.target, resolved_session_id, cwd=cwd)
        return exit_code

    @staticmethod
    def _validate_cancel_ack(frame: dict[str, Any]) -> None:
        if not isinstance(frame.get("accepted"), bool):
            raise DaemonProtocolError("cancel_ack frame has no boolean accepted field")

    @staticmethod
    def _exit_code_for_done(
        stop_reason: str,
        frame: dict[str, Any],
        cancel_reason: list[str | None] | None,
    ) -> int:
        if stop_reason != "cancelled":
            return _STOP_REASON_EXIT.get(stop_reason, 1)
        source = frame.get("cancel_source")
        if cancel_reason and cancel_reason[0] == "timeout":
            return 124
        if cancel_reason and cancel_reason[0] == "interrupt":
            return 130
        if source in {"daemon", "unrequested"}:
            return 1
        if source == "client":
            return 130
        return _STOP_REASON_EXIT["cancelled"]

    @staticmethod
    def _handle_error(
        output: OutputHandler,
        frame: dict[str, Any],
        session_id: str | None,
    ) -> int:
        message = frame.get("message", "daemon error")
        if not isinstance(message, str):
            message = str(message)
        resolved_session_id = frame.get("session_id", session_id)
        if not isinstance(resolved_session_id, str):
            resolved_session_id = ""
        output.on_session_error(resolved_session_id, message)
        stderr_error(message)
        exit_code = frame.get("exit_code", 1)
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            raise DaemonProtocolError("error frame has an invalid exit_code")
        return exit_code

    async def _retry_direct(self, direct_retry: DirectRetry | None) -> int:
        if direct_retry is None:
            raise DaemonUnavailableError("daemon is shutting down")
        return await direct_retry()

    async def _cancel_prompt(
        self,
        transport: UnixSocketTransport,
        connection: Connection,
        stream_task: asyncio.Task[int],
        *,
        reason: str,
        cancel_reason: list[str | None],
    ) -> int:
        cancel_reason[0] = reason
        try:
            await transport.send(
                connection,
                {"type": "cancel", "session_id": None, "reason": reason},
            )
            return await asyncio.wait_for(asyncio.shield(stream_task), timeout=CANCEL_GRACE)
        except (asyncio.TimeoutError, ConnectionError, OSError, ValueError):
            self._kill_daemon_from_lock()
            if not stream_task.done():
                stream_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await stream_task
            return 124 if reason == "timeout" else 130

    def _kill_daemon_from_lock(self) -> None:
        process = self._daemon_process_from_lock()
        if process is None:
            return
        try:
            _terminate_pid(process)
        finally:
            process.close()

    async def connect_existing(self) -> tuple[UnixSocketTransport, Connection]:
        """Connect to an already running daemon without starting one."""
        if sys.platform == "win32":
            raise DaemonUnavailableError("daemon transport is unavailable on Windows")
        if not self._socket_path().exists():
            raise DaemonUnavailableError(f"daemon for target '{self.target}' is not running")
        result = await self._try_connect()
        if result is None:
            raise DaemonUnavailableError(f"daemon for target '{self.target}' is not reachable")
        return result

    async def _prepare_endpoint(self) -> None:
        socket_path = self._socket_path()
        lock_path = self._lock_path()
        if not socket_path.exists():
            process = self._daemon_process_from_lock()
            if process is not None:
                try:
                    if not _terminate_pid(process):
                        raise DaemonUnavailableError(
                            f"cannot safely terminate daemon for target '{self.target}'"
                        )
                    await self._wait_for_process_exit(process)
                finally:
                    process.close()
            elif lock_path.exists() and _lock_is_held(lock_path):
                raise DaemonUnavailableError(
                    f"daemon lock for target '{self.target}' names an unverified process"
                )
            self._cleanup_endpoint()
            self._spawn_daemon()
            return
        if not lock_path.exists():
            self._cleanup_endpoint()
            self._spawn_daemon()
            return

        metadata = self._read_lock_metadata()
        if metadata is None:
            self._cleanup_endpoint()
            self._spawn_daemon()
            return
        pid = metadata.get("pid")
        if not _pid_alive(pid) or not _lock_is_held(lock_path):
            self._cleanup_endpoint()
            self._spawn_daemon()
            return
        if metadata.get("acpc_version") != __version__:
            await self._replace_version_mismatched_daemon(metadata)
            self._spawn_daemon()

    async def _recover_after_connection_failure(self) -> None:
        process = self._daemon_process_from_lock()
        if process is not None:
            try:
                if not _terminate_pid(process):
                    raise DaemonUnavailableError(
                        f"cannot safely terminate daemon for target '{self.target}'"
                    )
                await self._wait_for_process_exit(process)
            finally:
                process.close()
        elif self._lock_path().exists() and _lock_is_held(self._lock_path()):
            raise DaemonUnavailableError(
                f"daemon lock for target '{self.target}' names an unverified process"
            )
        self._cleanup_endpoint()
        self._spawn_daemon()

    async def _replace_version_mismatched_daemon(self, metadata: dict[str, Any]) -> None:
        process = self._daemon_process_from_lock()
        if process is None:
            if self._lock_path().exists() and _lock_is_held(self._lock_path()):
                raise DaemonUnavailableError(
                    f"daemon lock for target '{self.target}' names an unverified process"
                )
            self._cleanup_endpoint()
            return
        with contextlib.suppress(DaemonUnavailableError, ConnectionError, OSError, ValueError):
            transport, connection = await self.connect_existing()
            try:
                await transport.send(connection, {"type": "shutdown"})
                with contextlib.suppress(
                    asyncio.TimeoutError, ConnectionError, OSError, ValueError
                ):
                    await asyncio.wait_for(
                        transport.receive(connection), timeout=self.poll_interval
                    )
            finally:
                with contextlib.suppress(ConnectionError, OSError, RuntimeError, ValueError):
                    await transport.close_connection(connection)
                with contextlib.suppress(ConnectionError, OSError, RuntimeError, ValueError):
                    await transport.cleanup()
        try:
            await self._wait_for_process_exit(process)
            if _pid_alive(process.pid) and _process_matches_identity(
                process.pid, process.start_time, process.cmdline
            ):
                if not _terminate_pid(process):
                    raise DaemonUnavailableError(
                        f"cannot safely terminate daemon for target '{self.target}'"
                    )
        finally:
            process.close()
        self._cleanup_endpoint()

    async def _wait_for_process_exit(self, process: _ValidatedDaemonProcess) -> None:
        deadline = asyncio.get_running_loop().time() + self.stale_shutdown_timeout
        while (
            _pid_alive(process.pid)
            and _process_matches_identity(process.pid, process.start_time, process.cmdline)
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(
                min(self.poll_interval, max(0.0, deadline - asyncio.get_running_loop().time()))
            )

    def _daemon_process_from_lock(self) -> _ValidatedDaemonProcess | None:
        metadata = self._read_lock_metadata()
        if metadata is None or metadata.get("target") != self.target:
            return None
        if not _lock_is_held(self._lock_path()):
            return None
        pid = metadata.get("pid")
        start_time = metadata.get("process_start_time")
        cmdline = metadata.get("cmdline")
        if (
            not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 0
            or not isinstance(start_time, str)
            or not isinstance(cmdline, list)
            or not all(isinstance(part, str) for part in cmdline)
            or not _is_daemon_cmdline(cmdline, self.target)
            or not _process_matches_identity(pid, start_time, cmdline)
        ):
            return None
        pidfd: int | None = None
        if sys.platform == "linux":
            try:
                pidfd = _pidfd_open(pid)
            except OSError:
                pidfd = None
            if pidfd is not None and not _process_matches_identity(pid, start_time, cmdline):
                with contextlib.suppress(OSError):
                    os.close(pidfd)
                return None
        return _ValidatedDaemonProcess(pid, start_time, cmdline, pidfd)

    def _read_lock_metadata(self) -> dict[str, Any] | None:
        try:
            with self._lock_path().open(encoding="utf-8") as lock_file:
                metadata = json.load(lock_file)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        return metadata if isinstance(metadata, dict) else None

    def _cleanup_endpoint(self) -> None:
        for path in (self._socket_path(),):
            with contextlib.suppress(FileNotFoundError, OSError):
                path.unlink()
        lock_path = self._lock_path()
        _unlink_unheld_lock(lock_path)

    def _lock_path(self) -> Path:
        return lock_path_for_target(self.target)

    async def _try_connect(self) -> tuple[UnixSocketTransport, Connection] | None:
        transport = UnixSocketTransport(self.target)
        try:
            connection = await asyncio.wait_for(
                transport.connect(),
                timeout=min(self.poll_interval, self.readiness_timeout),
            )
        except (ConnectionError, OSError, RuntimeError, asyncio.TimeoutError):
            with contextlib.suppress(ConnectionError, OSError, RuntimeError, ValueError):
                await transport.cleanup()
            return None
        return transport, connection

    def _spawn_daemon(self) -> None:
        self._spawned_daemon = True
        try:
            self._popen_factory(
                [sys.executable, "-m", "acpc.daemon", self.target],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as error:
            raise DaemonUnavailableError(f"failed to start daemon: {error}") from error

    def _socket_path(self) -> Path:
        return socket_path_for_target(self.target)

    @staticmethod
    def _prompt_frame(
        *,
        text: str,
        cwd: str | None,
        permissions: str,
        model: str | None,
        mode: str | None,
        session_id: str | None,
        output_mode: str,
    ) -> dict[str, Any]:
        return {
            "type": "prompt",
            "text": text,
            "cwd": os.path.abspath(cwd) if cwd is not None else None,
            "permissions": permissions,
            "model": model,
            "mode": mode,
            "session_id": session_id,
            "output_mode": output_mode,
        }

    @staticmethod
    def _session_id(frame: dict[str, Any], fallback: str | None = None) -> str:
        value = frame.get("session_id", fallback)
        if not isinstance(value, str) or not value:
            raise DaemonProtocolError("daemon frame has no session_id")
        return value


def daemon_targets() -> list[str]:
    """Return targets with daemon lock metadata in the current state directory."""
    directory = run_dir()
    if not directory.exists():
        return []
    targets: set[str] = set()
    for lock_path in directory.glob("*.lock"):
        try:
            with lock_path.open(encoding="utf-8") as lock_file:
                metadata = json.load(lock_file)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            continue
        target = metadata.get("target") if isinstance(metadata, dict) else None
        if isinstance(target, str) and target:
            targets.add(target)
    return sorted(targets)


async def daemon_status(target: str) -> dict[str, Any]:
    """Request a status frame from an already running target daemon."""
    client = DaemonClient(target)
    transport, connection = await client.connect_existing()
    try:
        await transport.send(connection, {"type": "status"})
        frame = await transport.receive(connection)
        if frame.get("type") != "status":
            raise DaemonProtocolError("daemon status request returned an unexpected frame")
        return frame
    finally:
        with contextlib.suppress(ConnectionError, OSError, RuntimeError, ValueError):
            await transport.close_connection(connection)
        with contextlib.suppress(ConnectionError, OSError, RuntimeError, ValueError):
            await transport.cleanup()


async def shutdown_daemon(target: str) -> None:
    """Ask one target daemon to shut down through the frozen shutdown frame."""
    client = DaemonClient(target)
    transport, connection = await client.connect_existing()
    try:
        await transport.send(connection, {"type": "shutdown"})
        with contextlib.suppress(ConnectionError, OSError, ValueError, asyncio.TimeoutError):
            await asyncio.wait_for(transport.receive(connection), timeout=CANCEL_GRACE)
    finally:
        with contextlib.suppress(ConnectionError, OSError, RuntimeError, ValueError):
            await transport.close_connection(connection)
        with contextlib.suppress(ConnectionError, OSError, RuntimeError, ValueError):
            await transport.cleanup()


async def cancel_daemon_prompt(
    target: str,
    session_id: str | None = None,
    reason: str = "interrupt",
) -> bool:
    """Send a cancel frame and return whether the daemon accepted it."""
    client = DaemonClient(target)
    transport, connection = await client.connect_existing()
    try:
        await transport.send(
            connection,
            {"type": "cancel", "session_id": session_id, "reason": reason},
        )
        frame = await transport.receive(connection)
        if frame.get("type") != "cancel_ack":
            raise DaemonProtocolError("daemon cancel request returned an unexpected frame")
        accepted = frame.get("accepted")
        if not isinstance(accepted, bool):
            raise DaemonProtocolError("cancel_ack frame has no boolean accepted field")
        return accepted
    finally:
        with contextlib.suppress(ConnectionError, OSError, RuntimeError, ValueError):
            await transport.close_connection(connection)
        with contextlib.suppress(ConnectionError, OSError, RuntimeError, ValueError):
            await transport.cleanup()


def _pid_alive(value: object) -> bool:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return False
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _lock_is_held(path: Path) -> bool:
    if fcntl is None:
        return False
    if not path.exists():
        return False
    try:
        lock_file = path.open("a+")
    except OSError:
        return False
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        return False
    except OSError:
        return False
    finally:
        lock_file.close()


def _unlink_unheld_lock(path: Path) -> None:
    if fcntl is None or not path.exists():
        return
    try:
        lock_file = path.open("a+")
    except OSError:
        return
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        with contextlib.suppress(FileNotFoundError, OSError):
            path.unlink()
    except OSError:
        return
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def _is_daemon_cmdline(cmdline: list[str], target: str) -> bool:
    try:
        module_index = cmdline.index("-m")
    except ValueError:
        return False
    return (
        module_index + 2 < len(cmdline)
        and cmdline[module_index + 1] == "acpc.daemon"
        and cmdline[module_index + 2] == target
    )


def _process_matches_identity(pid: int, start_time: str, cmdline: list[str]) -> bool:
    return process_start_time(pid) == start_time and process_cmdline(pid) == cmdline


def _pidfd_open(pid: int) -> int | None:
    native_open = getattr(os, "pidfd_open", None)
    if native_open is not None:
        return native_open(pid)
    syscalls = _PIDFD_SYSCALLS.get(platform.machine())
    if syscalls is None:
        return None
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.syscall(ctypes.c_long(syscalls[0]), ctypes.c_int(pid), ctypes.c_uint(0))
    if fd < 0:
        error_number = ctypes.get_errno() or errno.EIO
        raise OSError(error_number, os.strerror(error_number))
    return int(fd)


def _pidfd_send_signal(pidfd: int, signum: int) -> None:
    native_send = getattr(signal, "pidfd_send_signal", None)
    if native_send is not None:
        native_send(pidfd, signum)
        return
    syscalls = _PIDFD_SYSCALLS.get(platform.machine())
    if syscalls is None:
        raise OSError(errno.ENOSYS, "pidfd signalling is unavailable on this Linux architecture")
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.syscall(
        ctypes.c_long(syscalls[1]),
        ctypes.c_int(pidfd),
        ctypes.c_int(signum),
        ctypes.c_void_p(),
        ctypes.c_uint(0),
    )
    if result < 0:
        error_number = ctypes.get_errno() or errno.EIO
        raise OSError(error_number, os.strerror(error_number))


def _terminate_pid(process: _ValidatedDaemonProcess) -> bool:
    """Terminate only a process already validated by its lock metadata."""
    if not _process_matches_identity(process.pid, process.start_time, process.cmdline):
        return False
    if sys.platform == "linux":
        if process.pidfd is None:
            return False
        try:
            _pidfd_send_signal(process.pidfd, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            return False
        return True
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.kill(process.pid, signal.SIGTERM)
        return True
    return False


async def run_daemon(
    config: RunConfig,
    *,
    direct_retry: DirectRetry | None = None,
    readiness_timeout: float = READINESS_TIMEOUT,
) -> int:
    """Run a configured prompt through the daemon, falling back to direct ACP."""
    from acpc.runner import run as direct_run

    output = OutputHandler(
        mode=OutputMode(config.output_mode),
        output_file=config.output_file,
    )

    async def retry_direct() -> int:
        return await direct_run(config)

    retry = direct_retry or retry_direct
    if sys.platform == "win32":
        return await retry()
    session_id = config.session_id
    if config.use_last and session_id is None:
        session_id = load_last_session(config.agent_identity)
        if session_id is None:
            stderr_error("no previous session found")
            return 2

    client = DaemonClient(
        config.agent_identity,
        readiness_timeout=readiness_timeout,
    )
    try:
        return await client.prompt(
            text=config.prompt_text,
            cwd=config.cwd or os.getcwd(),
            permissions=config.permission_level,
            output=output,
            model=config.model,
            mode=config.mode,
            session_id=session_id,
            output_mode=config.output_mode,
            direct_retry=retry,
            timeout=config.timeout,
            print_session_id=bool(getattr(config, "print_session_id", False)),
        )
    except (DaemonUnavailableError, DaemonProtocolError, ConnectionError, OSError) as error:
        if str(error) == "daemon is at capacity":
            stderr("daemon: at capacity, running direct")
        else:
            stderr(f"daemon: unavailable ({error}), running direct")
        return await retry()
