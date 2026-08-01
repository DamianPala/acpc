"""Client for the persistent per-target acpc daemon."""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from acp.schema import SessionNotification

from acpc.client import AcpcClient, PermissionLevel
from acpc.ipc import Connection, UnixSocketTransport, socket_path_for_target
from acpc.output import OutputHandler, OutputMode, stderr, stderr_error
from acpc.sessions import load_last_session, save_last_session

if TYPE_CHECKING:
    from acpc.runner import RunConfig


DirectRetry = Callable[[], Awaitable[int]]

READINESS_TIMEOUT = 15.0
READINESS_POLL_INTERVAL = 0.1

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


class DaemonClient:
    """Connect to one target daemon and stream one prompt response."""

    def __init__(
        self,
        target: str,
        *,
        readiness_timeout: float = READINESS_TIMEOUT,
        poll_interval: float = READINESS_POLL_INTERVAL,
        popen_factory: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        self.target = target
        self.readiness_timeout = readiness_timeout
        self.poll_interval = poll_interval
        self._popen_factory = popen_factory

    async def connect(self) -> tuple[UnixSocketTransport, Connection]:
        """Connect to a ready daemon, starting one when its socket is absent."""
        socket_path = self._socket_path()
        if not socket_path.exists():
            self._spawn_daemon()

        deadline = asyncio.get_running_loop().time() + self.readiness_timeout
        while True:
            connection = await self._try_connect()
            if connection is not None:
                return connection
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
        cwd: str,
        permissions: str,
        output: OutputHandler,
        model: str | None = None,
        mode: str | None = None,
        session_id: str | None = None,
        output_mode: str = "text",
        direct_retry: DirectRetry | None = None,
    ) -> int:
        """Send one prompt and return the daemon-provided exit code."""
        transport, connection = await self.connect()
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
            return await self._stream_response(
                transport,
                connection,
                output,
                direct_retry,
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
    ) -> int:
        acp_client = AcpcClient(output, PermissionLevel.NONE, is_tty=False)
        session_id: str | None = None
        while True:
            frame = await transport.receive(connection)
            frame_type = frame.get("type")
            if frame_type == "session_started":
                session_id = self._session_id(frame)
                output.on_session_started(session_id)
            elif frame_type == "session_update":
                await self._handle_update(acp_client, frame)
            elif frame_type == "queued":
                self._handle_queued(frame)
            elif frame_type == "prompt_done":
                return self._handle_done(output, frame, session_id)
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

    @staticmethod
    def _handle_queued(frame: dict[str, Any]) -> None:
        position = frame.get("position", "?")
        stderr(f"queued (position {position})")

    def _handle_done(
        self,
        output: OutputHandler,
        frame: dict[str, Any],
        session_id: str | None,
    ) -> int:
        resolved_session_id = self._session_id(frame, fallback=session_id)
        stop_reason = frame.get("stop_reason")
        if not isinstance(stop_reason, str):
            raise DaemonProtocolError("prompt_done frame has no stop_reason")
        exit_code = _STOP_REASON_EXIT.get(stop_reason, 1)
        output.on_session_ended(resolved_session_id, stop_reason, exit_code)
        output.finalize()
        save_last_session(self.target, resolved_session_id)
        return exit_code

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
        try:
            self._popen_factory(
                [sys.executable, "-m", "acpc.daemon", self.target],
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
        cwd: str,
        permissions: str,
        model: str | None,
        mode: str | None,
        session_id: str | None,
        output_mode: str,
    ) -> dict[str, Any]:
        return {
            "type": "prompt",
            "text": text,
            "cwd": os.path.abspath(cwd),
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
        )
    except (DaemonUnavailableError, DaemonProtocolError, ConnectionError, OSError) as error:
        stderr(f"warning: daemon unavailable ({error}), running direct")
        return await retry()
