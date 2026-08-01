"""Persistent ACP adapter daemon for one configured agent target."""

from __future__ import annotations

import asyncio
import contextlib
import shlex
import sys
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import acp
from acp import RequestError
from acp.client import ClientSideConnection

from acpc.agents import load_agent
from acpc.client import AcpcClient, PermissionLevel
from acpc.ipc import Connection, DaemonTransport, UnixSocketTransport
from acpc.output import OutputHandler, OutputMode
from acpc.runner import _drain_notifications, _spawn_agent, _try_set_model
from acpc.sessions import state_dir

SessionState = Literal["idle", "active"]
SessionUpdateSink = Callable[[dict[str, Any]], Awaitable[None]]
_CANCEL_TIMEOUT = 2.0


@dataclass
class SessionRecord:
    """All daemon-owned state for one ACP session."""

    session_id: str
    cwd: str
    created_at: float
    last_used: float
    state: SessionState = "idle"
    queue: list[Any] = field(default_factory=list)
    session_response: Any | None = None
    current_model: str | None = None
    mode: str | None = None
    connected_client: Connection | None = None
    output: OutputHandler = field(default_factory=lambda: OutputHandler(mode=OutputMode.QUIET))
    permission_level: PermissionLevel = PermissionLevel.PROMPT
    available: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class PromptContext:
    """Track the connection and session that own one daemon prompt."""

    connection: Connection
    session_id: str | None
    task: asyncio.Task[Any] | None = None
    record: SessionRecord | None = None
    abandoned: bool = False
    quarantined: bool = False
    finished: bool = False


class Daemon:
    """Serve prompt requests over one persistent ACP adapter connection."""

    def __init__(self, target: str, transport: DaemonTransport | None = None) -> None:
        self.target = target
        self.transport = transport or UnixSocketTransport(target)
        self.sessions: dict[str, SessionRecord] = {}
        self.client = AcpcClient(
            output=OutputHandler(mode=OutputMode.QUIET),
            permission_level=PermissionLevel.PROMPT,
            is_tty=False,
            strict_sessions=True,
        )
        self.connection: ClientSideConnection | None = None
        self.process: asyncio.subprocess.Process | None = None
        self._exit_stack: AsyncExitStack | None = None
        self._client_tasks: set[asyncio.Task[Any]] = set()
        self._send_locks: dict[str, asyncio.Lock] = {}
        self._active_prompts: dict[asyncio.Task[Any], PromptContext] = {}
        self._loading_sessions: dict[str, asyncio.Future[SessionRecord]] = {}
        self._process_watch_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._shutdown_lock = asyncio.Lock()
        self._shutdown_complete = False
        self._started = False
        self._stopping = False
        self._adapter_lost = False

    async def start(self) -> None:
        """Bind the transport, spawn the adapter in state_dir, and initialize ACP."""
        if self._started:
            return

        daemon_cwd = state_dir().resolve()
        daemon_cwd.mkdir(parents=True, exist_ok=True)
        agent = load_agent(self.target)
        command_parts = shlex.split(agent.run_command)
        command, args = command_parts[0], command_parts[1:]
        await self.transport.bind()
        stack = AsyncExitStack()
        self._exit_stack = stack
        try:
            await stack.__aenter__()
            connection, process = await stack.enter_async_context(
                _spawn_agent(self.client, command, *args, cwd=str(daemon_cwd))
            )
            self.connection = connection
            self.process = process
            self._process_watch_task = asyncio.create_task(
                self._watch_process(process, connection),
                name="acpc.daemon.process-watch",
            )
            await connection.initialize(protocol_version=acp.PROTOCOL_VERSION)
            self._started = True
        except BaseException:
            if self._process_watch_task is not None:
                self._process_watch_task.cancel()
                await asyncio.gather(self._process_watch_task, return_exceptions=True)
                self._process_watch_task = None
            with contextlib.suppress(BaseException):
                await stack.aclose()
            self._exit_stack = None
            with contextlib.suppress(BaseException):
                await self.transport.cleanup()
            raise

    async def run(self) -> None:
        """Run the accept loop until stop is requested or the transport closes."""
        await self.start()
        try:
            while not self._stop_event.is_set():
                try:
                    connection = await self.transport.accept()
                except RuntimeError:
                    if self._stop_event.is_set():
                        break
                    raise
                task = asyncio.create_task(self._serve_connection(connection))
                self._client_tasks.add(task)
                task.add_done_callback(self._forget_task)
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Close every resource and retry later if one cleanup stage fails."""
        async with self._shutdown_lock:
            if self._shutdown_complete:
                return
            self._stopping = True
            self._stop_event.set()
            first_error: BaseException | None = None

            async def attempt(operation: Awaitable[Any]) -> None:
                nonlocal first_error
                try:
                    await operation
                except BaseException as error:
                    if first_error is None:
                        first_error = error

            await attempt(self.transport.cleanup())
            tasks = tuple(self._client_tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await attempt(asyncio.gather(*tasks, return_exceptions=True))
            self._client_tasks.clear()

            watcher = self._process_watch_task
            if watcher is not None and watcher is not asyncio.current_task():
                watcher.cancel()
                await attempt(asyncio.gather(watcher, return_exceptions=True))
            self._process_watch_task = None

            stack = self._exit_stack
            self._exit_stack = None
            if stack is not None:
                await attempt(stack.aclose())

            for session_id in tuple(self.client.session_ids):
                self.client.unregister_session(session_id)
            self.sessions.clear()
            self._loading_sessions.clear()
            self._send_locks.clear()
            self.connection = None
            self.process = None
            self._started = False
            self._stopping = False
            if first_error is None:
                self._shutdown_complete = True
            else:
                raise first_error

    async def _serve_connection(self, connection: Connection) -> None:
        prompt_tasks: set[asyncio.Task[Any]] = set()
        try:
            while True:
                frame = await self.transport.receive(connection)
                if frame.get("type") != "prompt":
                    await self._send_error(
                        connection,
                        "unsupported daemon frame; expected type 'prompt'",
                    )
                    continue
                context = PromptContext(
                    connection=connection,
                    session_id=frame.get("session_id")
                    if isinstance(frame.get("session_id"), str)
                    else None,
                )
                task = asyncio.create_task(self._handle_prompt(context, frame))
                context.task = task
                prompt_tasks.add(task)
                self._active_prompts[task] = context
                task.add_done_callback(prompt_tasks.discard)
        except (ConnectionError, OSError, ValueError):
            pass
        finally:
            await self._abandon_connection_prompts(connection, prompt_tasks)
            for task in prompt_tasks:
                context = self._active_prompts.get(task)
                if context is None or not context.quarantined:
                    task.cancel()
            if prompt_tasks:
                await asyncio.gather(*prompt_tasks, return_exceptions=True)
            with contextlib.suppress(ConnectionError, OSError, ValueError):
                await self.transport.close_connection(connection)
            self._send_locks.pop(connection.id, None)

    async def _handle_prompt(self, context: PromptContext, frame: dict[str, Any]) -> None:
        session_id = context.session_id
        try:
            permission = self._permission_level(frame)
            record, reused = await self._resolve_session(context, frame, permission)
            session_id = record.session_id
            context.session_id = session_id
            if not context.abandoned:
                await self._send(
                    context.connection,
                    {
                        "type": "session_started",
                        "session_id": session_id,
                        "reused": reused,
                    },
                )
            await self._set_requested_model(record, frame)
            result = await self._prompt(record, frame)
            await self._drain_adapter_notifications()
            context.finished = True
            if not context.abandoned:
                await self._send(
                    context.connection,
                    {
                        "type": "prompt_done",
                        "session_id": session_id,
                        "stop_reason": result.stop_reason,
                    },
                )
        except ValueError as error:
            if not context.abandoned and not self._adapter_lost:
                await self._send_error(
                    context.connection,
                    str(error),
                    session_id=session_id,
                    exit_code=2,
                )
        except (RequestError, OSError, RuntimeError) as error:
            if not context.abandoned and not self._adapter_lost:
                await self._send_error(context.connection, str(error), session_id=session_id)
        finally:
            if context.record is not None and (not context.abandoned or context.quarantined):
                self._release_record(context.record, context.connection)
            task = asyncio.current_task()
            if task is not None:
                self._active_prompts.pop(task, None)

    async def _resolve_session(
        self,
        context: PromptContext,
        frame: dict[str, Any],
        permission: PermissionLevel,
    ) -> tuple[SessionRecord, Literal["new", "live", "loaded"]]:
        cwd = self._request_cwd(frame)
        requested_id = frame.get("session_id")
        if requested_id is not None and not isinstance(requested_id, str):
            raise ValueError("session_id must be a string or null")

        if requested_id is None:
            response = await self._require_connection().new_session(cwd=cwd)
            record = self._new_record(response.session_id, cwd, context.connection, permission)
            record.session_response = response
            context.record = record
            self._claim_record(record)
            return record, "new"

        while True:
            record = self.sessions.get(requested_id)
            if record is not None:
                if record.state == "active":
                    await record.available.wait()
                    continue
                self._validate_resume_cwd(record, cwd)
                self._attach_session(record, context.connection, permission)
                context.record = record
                self._claim_record(record)
                return record, "live"

            loading = self._loading_sessions.get(requested_id)
            if loading is not None:
                await asyncio.shield(loading)
                continue

            return await self._load_session(context, requested_id, cwd, permission)

    def _new_record(
        self,
        session_id: str,
        cwd: str,
        connection: Connection,
        permission: PermissionLevel,
        *,
        publish: bool = True,
    ) -> SessionRecord:
        now = time.time()
        record = SessionRecord(
            session_id=session_id,
            cwd=cwd,
            created_at=now,
            last_used=time.monotonic(),
            connected_client=connection,
            permission_level=permission,
        )
        record.available.set()
        if publish:
            self.sessions[session_id] = record
        self._attach_session(record, connection, permission)
        return record

    def _attach_session(
        self,
        record: SessionRecord,
        connection: Connection,
        permission: PermissionLevel,
    ) -> None:
        record.connected_client = connection
        record.permission_level = permission
        self.client.register_session(
            record.session_id,
            output=record.output,
            permission_level=permission,
            is_tty=False,
            update_sink=self._make_update_sink(record, connection),
        )

    def _claim_record(self, record: SessionRecord) -> None:
        record.state = "active"
        record.available.clear()

    def _release_record(self, record: SessionRecord, connection: Connection) -> None:
        if record.connected_client is not connection:
            return
        self.client.detach_session(record.session_id)
        record.connected_client = None
        record.state = "idle"
        record.last_used = time.monotonic()
        record.available.set()

    async def _load_session(
        self,
        context: PromptContext,
        session_id: str,
        cwd: str,
        permission: PermissionLevel,
    ) -> tuple[SessionRecord, Literal["loaded"]]:
        loop = asyncio.get_running_loop()
        loading = loop.create_future()
        self._loading_sessions[session_id] = loading
        record = self._new_record(
            session_id,
            cwd,
            context.connection,
            permission,
            publish=False,
        )
        context.record = record
        self._claim_record(record)
        try:
            with self.client.replaying_history(session_id):
                record.session_response = await self._require_connection().load_session(
                    cwd=cwd,
                    session_id=session_id,
                )
            self.sessions[session_id] = record
            loading.set_result(record)
            return record, "loaded"
        except BaseException as error:
            self.client.unregister_session(session_id)
            self._release_record(record, context.connection)
            if isinstance(error, asyncio.CancelledError):
                loading.cancel()
            else:
                loading.set_exception(error)
                loading.exception()
            raise
        finally:
            self._loading_sessions.pop(session_id, None)

    async def _abandon_connection_prompts(
        self,
        connection: Connection,
        prompt_tasks: set[asyncio.Task[Any]],
    ) -> None:
        self._detach_connection_sessions(connection)
        for task in tuple(prompt_tasks):
            context = self._active_prompts.get(task)
            if context is not None:
                await self._abandon_prompt(context)

    async def _abandon_prompt(self, context: PromptContext) -> None:
        if context.abandoned:
            return
        context.abandoned = True
        record = context.record
        if record is None:
            return
        if record.connected_client is context.connection:
            self.client.detach_session(record.session_id)
        with contextlib.suppress(RequestError, OSError, RuntimeError, asyncio.TimeoutError):
            await asyncio.wait_for(
                self._require_connection().cancel(session_id=record.session_id),
                timeout=_CANCEL_TIMEOUT,
            )
        if context.task is not None and not context.task.done():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    asyncio.shield(context.task),
                    timeout=_CANCEL_TIMEOUT,
                )
        if context.task is not None and not context.task.done():
            context.quarantined = True
        await self._drain_adapter_notifications()
        if not context.quarantined:
            self._release_record(record, context.connection)

    def _detach_connection_sessions(self, connection: Connection) -> None:
        for record in self.sessions.values():
            if record.connected_client is connection:
                self.client.detach_session(record.session_id)
                if record.state == "idle":
                    record.connected_client = None

    def _make_update_sink(
        self,
        record: SessionRecord,
        connection: Connection,
    ) -> SessionUpdateSink:
        async def send_update(frame: dict[str, Any]) -> None:
            if record.connected_client is not connection:
                return
            await self._send(connection, frame)

        return send_update

    async def _drain_adapter_notifications(self) -> None:
        if self.connection is None:
            return
        await asyncio.sleep(0)
        await _drain_notifications(self.connection)

    async def _set_requested_model(
        self,
        record: SessionRecord,
        frame: dict[str, Any],
    ) -> None:
        model = frame.get("model")
        if model is None:
            return
        if not isinstance(model, str) or not model:
            raise ValueError("model must be a non-empty string or null")
        if model == record.current_model:
            return
        accepted = await _try_set_model(
            self._require_connection(),
            record.session_id,
            model,
            record.session_response,
            self._ignore_model_log,
        )
        if accepted:
            record.current_model = model

    async def _prompt(self, record: SessionRecord, frame: dict[str, Any]) -> Any:
        text = frame.get("text")
        if not isinstance(text, str):
            raise ValueError("prompt text must be a string")
        return await self._require_connection().prompt(
            [acp.text_block(text)],
            session_id=record.session_id,
        )

    def _request_cwd(self, frame: dict[str, Any]) -> str:
        cwd = frame.get("cwd")
        if not isinstance(cwd, str):
            raise ValueError("cwd must be an absolute existing directory")
        return self._canonical_cwd(cwd)

    @staticmethod
    def _canonical_cwd(cwd: str) -> str:
        path = Path(cwd)
        if not path.is_absolute():
            raise ValueError(f"cwd must be an absolute existing directory: {cwd}")
        try:
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise ValueError(f"cwd must be an absolute existing directory: {cwd}") from error
        if not resolved.is_dir():
            raise ValueError(f"cwd must be an absolute existing directory: {cwd}")
        return str(resolved)

    def _validate_resume_cwd(self, record: SessionRecord, requested_cwd: str) -> None:
        try:
            recorded_cwd = self._canonical_cwd(record.cwd)
        except ValueError as error:
            self.sessions.pop(record.session_id, None)
            self.client.unregister_session(record.session_id)
            raise ValueError(f"session cwd no longer exists: {record.cwd}") from error
        record.cwd = recorded_cwd
        if recorded_cwd != requested_cwd:
            raise ValueError(f"session cwd is {recorded_cwd}, --cwd says {requested_cwd}")

    @staticmethod
    def _permission_level(frame: dict[str, Any]) -> PermissionLevel:
        value = frame.get("permissions", PermissionLevel.PROMPT.value)
        if not isinstance(value, str):
            raise ValueError("permissions must be one of all, write, read, none, prompt")
        try:
            return PermissionLevel(value)
        except ValueError as error:
            raise ValueError("permissions must be one of all, write, read, none, prompt") from error

    async def _send_error(
        self,
        connection: Connection,
        message: str,
        *,
        session_id: str | None = None,
        exit_code: int = 1,
    ) -> None:
        frame: dict[str, Any] = {"type": "error", "message": message, "exit_code": exit_code}
        if session_id is not None:
            frame["session_id"] = session_id
        await self._send(connection, frame)

    async def _send(self, connection: Connection, frame: dict[str, Any]) -> None:
        lock = self._send_locks.setdefault(connection.id, asyncio.Lock())
        async with lock:
            with contextlib.suppress(ConnectionError, OSError, ValueError):
                await self.transport.send(connection, frame)

    def _require_connection(self) -> ClientSideConnection:
        if self.connection is None:
            raise RuntimeError("daemon adapter is not initialized")
        return self.connection

    @staticmethod
    def _ignore_model_log(message: str) -> None:
        del message

    def _forget_task(self, task: asyncio.Task[Any]) -> None:
        self._client_tasks.discard(task)
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.exception()

    async def _watch_process(
        self,
        process: asyncio.subprocess.Process,
        connection: ClientSideConnection,
    ) -> None:
        try:
            await process.wait()
        except asyncio.CancelledError:
            return
        if self._stopping or self._adapter_lost:
            return
        if not self._started:
            with contextlib.suppress(Exception):
                await connection.close()
            return
        self._adapter_lost = True
        with contextlib.suppress(Exception):
            await connection.close()
        for context in tuple(self._active_prompts.values()):
            if not context.finished and not context.abandoned:
                await self._send_error(
                    context.connection,
                    "adapter connection lost",
                    session_id=context.session_id,
                )
        await self.stop()


async def _async_main(target: str) -> None:
    daemon = Daemon(target)
    await daemon.run()


def main() -> None:
    """Run the daemon module with one target argument."""
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m acpc.daemon TARGET")
    asyncio.run(_async_main(sys.argv[1]))


if __name__ == "__main__":
    main()
