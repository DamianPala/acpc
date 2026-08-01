"""Persistent ACP adapter daemon for one configured agent target."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import acp
from acp import RequestError
from acp.client import ClientSideConnection

from acpc import __version__
from acpc.agents import load_agent
from acpc.client import AcpcClient, PermissionLevel
from acpc.ipc import (
    Connection,
    DaemonTransport,
    UnixSocketTransport,
    lock_path_for_target,
    socket_path_for_target,
)
from acpc.output import OutputHandler, OutputMode
from acpc.runner import _drain_notifications, _spawn_agent, _try_set_model
from acpc.sessions import process_cmdline, process_start_time, state_dir

SessionState = Literal["idle", "active"]
SessionUpdateSink = Callable[[dict[str, Any]], Awaitable[None]]

_MAX_QUEUE_DEPTH = 4
_DEFAULT_CONCURRENT = 4
_DEFAULT_TTL = 300.0
_DEFAULT_MAX_AGE = 4 * 60 * 60.0
_RSS_FLOOR_MB = 2048
_CANCEL_TIMEOUT = 2.0
_DRAIN_TIMEOUT = 5.0
_MEMINFO_PATH = Path("/proc/meminfo")
_AUTH_MARKERS = ("auth", "credential", "unauthorized", "401", "token", "login")


class CapacityError(RuntimeError):
    """The adapter process tree is above the daemon's RSS ceiling."""

    def __init__(self, rss_mb: int) -> None:
        super().__init__(f"daemon capacity exceeded at {rss_mb} MB")
        self.rss_mb = rss_mb


class RecyclingError(RuntimeError):
    """The daemon is draining and cannot create another adapter session."""


@dataclass
class PromptContext:
    """Track one client's request, including its connection lifetime."""

    connection: Connection
    abandoned: bool = False
    request: PromptRequest | None = None


@dataclass
class PromptRequest:
    """Immutable-at-admission request state for a queued prompt."""

    context: PromptContext
    connection: Connection
    session_id: str
    text: str
    cwd: str
    permission_level: PermissionLevel
    model: str | None
    mode: str | None
    output: OutputHandler
    done: asyncio.Future[None]
    sequence: int
    task: asyncio.Task[Any] | None = None
    started_prompt: bool = False
    cancel_requested: bool = False


@dataclass
class SessionRecord:
    """All daemon-owned state for one ACP session."""

    session_id: str
    cwd: str
    created_at: float
    last_used: float
    state: SessionState = "idle"
    queue: list[PromptRequest] = field(default_factory=list)
    session_response: Any | None = None
    current_model: str | None = None
    mode: str | None = None
    connected_client: Connection | None = None
    output: OutputHandler = field(default_factory=lambda: OutputHandler(mode=OutputMode.QUIET))
    permission_level: PermissionLevel = PermissionLevel.PROMPT
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    admission_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    running: bool = False
    reserved: bool = False
    reserved_context: PromptContext | None = None


class FifoLimiter:
    """A cancellation-safe FIFO limiter for prompts across all sessions."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._active = 0
        self._waiters: deque[asyncio.Future[None]] = deque()

    @property
    def is_saturated(self) -> bool:
        """Return whether a new prompt must wait for a global slot."""
        return self._active >= self._limit or bool(self._waiters)

    async def acquire(self) -> None:
        """Wait for the next global prompt slot in arrival order."""
        if self._active < self._limit and not self._waiters:
            self._active += 1
            return
        loop = asyncio.get_running_loop()
        waiter = loop.create_future()
        self._waiters.append(waiter)
        try:
            await waiter
        except asyncio.CancelledError:
            with contextlib.suppress(ValueError):
                self._waiters.remove(waiter)
            raise

    def release(self) -> None:
        """Return a slot to the oldest live waiter, if any."""
        self._active -= 1
        while self._waiters:
            waiter = self._waiters.popleft()
            if waiter.cancelled():
                continue
            self._active += 1
            waiter.set_result(None)
            break


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
        self.log_path = state_dir() / "log" / f"{target}.log"
        self._exit_stack: AsyncExitStack | None = None
        self._client_tasks: set[asyncio.Task[Any]] = set()
        self._request_tasks: set[asyncio.Task[Any]] = set()
        self._connection_contexts: dict[str, list[PromptContext]] = {}
        self._all_requests: dict[int, PromptRequest] = {}
        self._started_requests: set[int] = set()
        self._loading_sessions: dict[str, asyncio.Future[SessionRecord]] = {}
        self._process_watch_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._lifecycle_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._active_done = asyncio.Event()
        self._active_done.set()
        self._shutdown_lock = asyncio.Lock()
        self._shutdown_complete = False
        self._started = False
        self._stopping = False
        self._draining = False
        self._adapter_lost = False
        self._recycling = False
        self._started_at = 0.0
        self._idle_since: float | None = None
        self._limiter = FifoLimiter(_env_int("ACPC_DAEMON_MAX_CONCURRENT", _DEFAULT_CONCURRENT))
        self._ttl = _env_float("ACPC_DAEMON_TTL", _DEFAULT_TTL)
        self._max_age = _env_float("ACPC_DAEMON_MAX_AGE", _DEFAULT_MAX_AGE)
        self._lock_file: Any | None = None
        self._log_file: Any | None = None
        self._signals_installed = False
        self._request_sequence = 0

    async def start(self) -> bool:
        """Acquire the target lock, start the adapter, and initialize ACP."""
        if self._started:
            return True
        if not self._acquire_lock():
            return False
        try:
            self._open_log()
            self._write_lock_metadata()
            await self.transport.bind()
            agent = load_agent(self.target)
            command_parts = shlex.split(agent.run_command)
            command, args = command_parts[0], command_parts[1:]
            daemon_cwd = state_dir().resolve()
            daemon_cwd.mkdir(parents=True, exist_ok=True)
            stack = AsyncExitStack()
            await stack.__aenter__()
            self._exit_stack = stack
            connection, process = await stack.enter_async_context(
                _spawn_agent(self.client, command, *args, cwd=str(daemon_cwd))
            )
            self.connection = connection
            self.process = process
            self._started_at = time.monotonic()
            self._started = True
            self._install_signal_handlers()
            self._stderr_task = asyncio.create_task(self._capture_stderr(process))
            self._process_watch_task = asyncio.create_task(
                self._watch_process(process, connection),
                name="acpc.daemon.process-watch",
            )
            await connection.initialize(protocol_version=acp.PROTOCOL_VERSION)
            self._lifecycle_task = asyncio.create_task(self._lifecycle_monitor())
            self._log("daemon started")
            return True
        except BaseException:
            await self._cleanup_failed_start()
            raise

    async def run(self) -> None:
        """Accept clients until idle expiry, recycling, or shutdown."""
        if not await self.start():
            return
        try:
            while not self._stop_event.is_set():
                try:
                    connection = await self.transport.accept()
                except RuntimeError:
                    if self._stop_event.is_set():
                        break
                    raise
                self._register_connection(connection)
                task = asyncio.create_task(self._serve_connection(connection))
                self._client_tasks.add(task)
                task.add_done_callback(self._forget_client_task)
            await self._active_done.wait()
            while self._recycling and self._connection_contexts:
                await asyncio.sleep(0.01)
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Force teardown of the daemon and its adapter process tree."""
        async with self._shutdown_lock:
            if self._shutdown_complete:
                return
            self._stopping = True
            self._stop_event.set()
            with contextlib.suppress(Exception):
                await self.transport.stop_accepting()
            await self._cancel_request_tasks()
            cleanup_error = await self._close_client_tasks()
            await self._cancel_background_tasks()
            stack = self._exit_stack
            self._exit_stack = None
            if stack is not None:
                await stack.aclose()
            self.connection = None
            self.process = None
            self._started = False
            self._remove_signal_handlers()
            self._close_log()
            self._release_lock()
            if cleanup_error is not None:
                self._stopping = False
                raise cleanup_error
            self._shutdown_complete = True

    async def _serve_connection(self, connection: Connection) -> None:
        prompt_tasks: set[asyncio.Task[Any]] = set()
        try:
            while True:
                frame = await self.transport.receive(connection)
                frame_type = frame.get("type")
                if frame_type == "prompt":
                    context = PromptContext(connection=connection)
                    self._connection_contexts[connection.id].append(context)
                    task = asyncio.create_task(self._handle_prompt(context, frame))
                    prompt_tasks.add(task)
                    task.add_done_callback(prompt_tasks.discard)
                    continue
                if frame_type == "cancel":
                    await self._handle_cancel(connection, frame)
                    continue
                if frame_type == "status":
                    await self._send(connection, self._status_frame())
                    continue
                if frame_type == "shutdown":
                    await self._send(connection, {"type": "bye"})
                    await self._begin_shutdown()
                    break
                await self._send_error(connection, "unsupported daemon frame")
        except (ConnectionError, OSError, ValueError):
            pass
        finally:
            for context in tuple(self._connection_contexts.get(connection.id, ())):
                await self._abandon_context(context)
            if prompt_tasks:
                await asyncio.gather(*prompt_tasks, return_exceptions=True)
            with contextlib.suppress(ConnectionError, OSError, ValueError):
                await self.transport.close_connection(connection)
            self._connection_contexts.pop(connection.id, None)
            self._idle_since = None

    async def _handle_prompt(self, context: PromptContext, frame: dict[str, Any]) -> None:
        requested_id = frame.get("session_id")
        session_id = requested_id if isinstance(requested_id, str) else None
        try:
            permission = self._permission_level(frame)
            record, reused = await self._resolve_session(context, frame, permission)
            session_id = record.session_id
            request = self._make_request(context, record, frame, permission)
            context.request = request
            if context.abandoned:
                await self._release_reservation(record, context)
                return
            await self._send(
                context.connection,
                {"type": "session_started", "session_id": session_id, "reused": reused},
            )
            await self._admit_request(record, request)
            if request.done is not None:
                await asyncio.shield(request.done)
        except CapacityError as error:
            if not context.abandoned:
                await self._send(context.connection, {"type": "capacity", "rss_mb": error.rss_mb})
                await self._begin_shutdown(recycle=True)
        except RecyclingError:
            if not context.abandoned:
                await self._send(context.connection, {"type": "shutting_down"})
        except ValueError as error:
            if not context.abandoned:
                await self._send_error(
                    context.connection, str(error), session_id=session_id, exit_code=2
                )
        except (RequestError, OSError, RuntimeError) as error:
            if not context.abandoned:
                await self._send_error(context.connection, str(error), session_id=session_id)
        finally:
            contexts = self._connection_contexts.get(context.connection.id)
            if contexts is not None and context in contexts:
                contexts.remove(context)

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
            await self._ensure_can_create_session()
            if cwd is None:
                raise ValueError("cwd must be an absolute existing directory")
            response = await self._require_connection().new_session(cwd=cwd)
            record = self._new_record(response.session_id, cwd)
            record.session_response = response
            await self._reserve_record(record, context)
            return record, "new"
        while True:
            record = self.sessions.get(requested_id)
            if record is not None:
                self._validate_resume_cwd(record, cwd)
                await self._reserve_record(record, context)
                return record, "live"
            loading = self._loading_sessions.get(requested_id)
            if loading is not None:
                await asyncio.shield(loading)
                continue
            await self._ensure_can_create_session()
            record, reused = await self._load_session(context, requested_id, cwd, permission)
            await self._reserve_record(record, context)
            return record, reused

    def _new_record(self, session_id: str, cwd: str) -> SessionRecord:
        now = time.time()
        record = SessionRecord(
            session_id=session_id,
            cwd=cwd,
            created_at=now,
            last_used=time.monotonic(),
        )
        self.sessions[session_id] = record
        return record

    async def _load_session(
        self,
        context: PromptContext,
        session_id: str,
        cwd: str | None,
        permission: PermissionLevel,
    ) -> tuple[SessionRecord, Literal["loaded"]]:
        load_cwd = self._load_cwd_for_session(session_id, cwd)
        loop = asyncio.get_running_loop()
        loading = loop.create_future()
        self._loading_sessions[session_id] = loading
        record = SessionRecord(
            session_id=session_id,
            cwd=load_cwd,
            created_at=time.time(),
            last_used=time.monotonic(),
            permission_level=permission,
        )
        self._attach_replay_session(record, context.connection, permission)
        try:
            with self.client.replaying_history(session_id):
                record.session_response = await self._require_connection().load_session(
                    cwd=load_cwd,
                    session_id=session_id,
                )
            self.client.detach_session(session_id)
            self.sessions[session_id] = record
            loading.set_result(record)
            return record, "loaded"
        except BaseException as error:
            self.client.unregister_session(session_id)
            if isinstance(error, asyncio.CancelledError):
                loading.cancel()
            else:
                loading.set_exception(error)
                loading.exception()
            raise
        finally:
            self._loading_sessions.pop(session_id, None)

    def _attach_replay_session(
        self,
        record: SessionRecord,
        connection: Connection,
        permission: PermissionLevel,
    ) -> None:
        record.connected_client = connection
        self.client.register_session(
            record.session_id,
            output=record.output,
            permission_level=permission,
            is_tty=False,
            update_sink=self._make_replay_sink(connection),
        )

    def _make_replay_sink(self, connection: Connection) -> SessionUpdateSink:
        async def send_update(frame: dict[str, Any]) -> None:
            await self._send(connection, frame)

        return send_update

    def _make_request(
        self,
        context: PromptContext,
        record: SessionRecord,
        frame: dict[str, Any],
        permission: PermissionLevel,
    ) -> PromptRequest:
        text = frame.get("text")
        if not isinstance(text, str):
            raise ValueError("prompt text must be a string")
        model = frame.get("model")
        if model is not None and (not isinstance(model, str) or not model):
            raise ValueError("model must be a non-empty string or null")
        mode = frame.get("mode")
        if mode is not None and (not isinstance(mode, str) or not mode):
            raise ValueError("mode must be a non-empty string or null")
        return PromptRequest(
            context=context,
            connection=context.connection,
            session_id=record.session_id,
            text=text,
            cwd=record.cwd,
            permission_level=permission,
            model=model,
            mode=mode,
            output=OutputHandler(mode=OutputMode.QUIET),
            done=asyncio.get_running_loop().create_future(),
            sequence=self._next_request_sequence(),
        )

    async def _reserve_record(self, record: SessionRecord, context: PromptContext) -> None:
        async with record.admission_lock:
            if not record.running and not record.queue:
                record.running = True
                record.reserved = True
                record.reserved_context = context

    async def _release_reservation(self, record: SessionRecord, context: PromptContext) -> None:
        async with record.admission_lock:
            if record.reserved and record.reserved_context is context:
                record.reserved = False
                record.reserved_context = None
                record.running = False
                record.state = "idle"

    async def _admit_request(self, record: SessionRecord, request: PromptRequest) -> None:
        async with record.admission_lock:
            if record.reserved:
                if record.reserved_context is not request.context:
                    record.queue.append(request)
                    record.state = "active"
                    self._track_request(request)
                    await self._send_queue_positions(record)
                    self._start_request_task(record, request)
                    return
                record.reserved = False
                record.reserved_context = None
                record.state = "active"
                self._track_request(request)
            elif record.running or record.queue:
                if len(record.queue) >= _MAX_QUEUE_DEPTH:
                    await self._send(
                        request.connection,
                        {
                            "type": "error",
                            "message": "session queue full (4 waiting)",
                            "exit_code": 1,
                        },
                    )
                    request.done.set_result(None)
                    return
                record.queue.append(request)
                record.state = "active"
                self._track_request(request)
                await self._send_queue_positions(record)
            else:
                record.running = True
                record.state = "active"
                self._track_request(request)
            self._start_request_task(record, request)
            if self._limiter.is_saturated and not record.queue:
                await self._send_global_queue_positions()

    def _start_request_task(self, record: SessionRecord, request: PromptRequest) -> None:
        task = asyncio.create_task(self._run_request(record, request))
        request.task = task
        self._request_tasks.add(task)
        task.add_done_callback(self._forget_request_task)

    async def _run_request(self, record: SessionRecord, request: PromptRequest) -> None:
        limiter_acquired = False
        async with record.lock:
            async with record.admission_lock:
                if request in record.queue:
                    record.queue.remove(request)
                    await self._send_queue_positions(record)
            if request.context.abandoned or self._stopping:
                return
            try:
                await self._limiter.acquire()
                limiter_acquired = True
                await self._send_global_queue_positions()
                request.started_prompt = True
                self._started_requests.add(id(request))
                self._active_done.clear()
                self._attach_request(record, request)
                result = await self._execute_prompt(record, request)
                await self._drain_adapter_notifications()
                await self._finish_prompt(record, request, result.stop_reason)
            except asyncio.CancelledError:
                raise
            except ValueError as error:
                await self._send_request_error(request, str(error), exit_code=2)
            except (RequestError, OSError, RuntimeError) as error:
                if self._adapter_lost:
                    return
                if isinstance(error, ConnectionError):
                    self._adapter_lost = True
                    await self._send_request_error(request, "adapter connection lost")
                    await self._begin_shutdown()
                    return
                if _is_auth_error(error):
                    await self._begin_shutdown(recycle=True)
                await self._send_request_error(request, str(error))
            finally:
                self._detach_request(record, request)
                if request.started_prompt:
                    request.started_prompt = False
                    self._started_requests.discard(id(request))
                if limiter_acquired:
                    self._limiter.release()
                async with record.admission_lock:
                    if not record.queue:
                        record.running = False
                        record.state = "idle"
                        record.last_used = time.monotonic()
                if not self._started_requests:
                    self._active_done.set()
                self._all_requests.pop(id(request), None)
                request.done.set_result(None)

    async def _execute_prompt(self, record: SessionRecord, request: PromptRequest) -> Any:
        if request.model is not None and request.model != record.current_model:
            accepted = await _try_set_model(
                self._require_connection(),
                record.session_id,
                request.model,
                record.session_response,
                self._log,
            )
            if accepted:
                record.current_model = request.model
        if request.mode is not None and request.mode != record.mode:
            try:
                await self._require_connection().set_session_mode(
                    mode_id=request.mode,
                    session_id=record.session_id,
                )
                record.mode = request.mode
            except RequestError as error:
                self._log(f"warning: failed to set mode '{request.mode}': {error}")
        return await self._require_connection().prompt(
            record.session_id,
            [acp.text_block(request.text)],
        )

    async def _finish_prompt(
        self, record: SessionRecord, request: PromptRequest, stop_reason: str
    ) -> None:
        if request.context.abandoned:
            return
        if stop_reason == "cancelled" and not request.cancel_requested:
            await self._send_request_error(request, "prompt superseded on this session")
            return
        frame: dict[str, Any] = {
            "type": "prompt_done",
            "session_id": record.session_id,
            "stop_reason": stop_reason,
        }
        if stop_reason == "cancelled":
            frame["cancel_source"] = "client"
        await self._send(request.connection, frame)

    async def _send_request_error(
        self,
        request: PromptRequest,
        message: str,
        exit_code: int = 1,
    ) -> None:
        if request.context.abandoned:
            return
        await self._send(
            request.connection,
            {
                "type": "error",
                "session_id": request.session_id,
                "message": message,
                "exit_code": exit_code,
            },
        )

    def _attach_request(self, record: SessionRecord, request: PromptRequest) -> None:
        record.connected_client = request.connection
        record.permission_level = request.permission_level
        record.output = request.output
        self.client.register_session(
            record.session_id,
            output=request.output,
            permission_level=request.permission_level,
            is_tty=False,
            update_sink=self._make_update_sink(request),
        )

    def _detach_request(self, record: SessionRecord, request: PromptRequest) -> None:
        if record.connected_client is request.connection:
            self.client.detach_session(record.session_id)
            record.connected_client = None

    def _make_update_sink(self, request: PromptRequest) -> SessionUpdateSink:
        async def send_update(frame: dict[str, Any]) -> None:
            if not request.context.abandoned:
                await self._send(request.connection, frame)

        return send_update

    async def _send_queue_positions(self, record: SessionRecord) -> None:
        for position, request in enumerate(record.queue, start=1):
            if not request.context.abandoned:
                await self._send(request.connection, {"type": "queued", "position": position})

    async def _send_global_queue_positions(self) -> None:
        if not self._limiter.is_saturated:
            return
        waiting = sorted(
            (
                request
                for request in self._all_requests.values()
                if not request.started_prompt and not request.context.abandoned
            ),
            key=lambda request: request.sequence,
        )
        for position, request in enumerate(waiting, start=1):
            await self._send(request.connection, {"type": "queued", "position": position})

    async def _handle_cancel(self, connection: Connection, frame: dict[str, Any]) -> None:
        requested_id = frame.get("session_id")
        if requested_id is not None and not isinstance(requested_id, str):
            await self._send_cancel_ack(connection, None, False)
            return
        request = self._find_cancel_request(connection, requested_id)
        if request is None:
            await self._send_cancel_ack(connection, requested_id, False)
            return
        request.cancel_requested = True
        with contextlib.suppress(RequestError, OSError, RuntimeError, asyncio.TimeoutError):
            await asyncio.wait_for(
                self._require_connection().cancel(session_id=request.session_id),
                timeout=_CANCEL_TIMEOUT,
            )
        await self._send_cancel_ack(connection, request.session_id, True)

    def _find_cancel_request(
        self, connection: Connection, session_id: str | None
    ) -> PromptRequest | None:
        if session_id is None:
            requests = (
                context.request for context in self._connection_contexts.get(connection.id, ())
            )
        else:
            requests = iter(self._all_requests.values())
        return next(
            (
                request
                for request in requests
                if request is not None
                and request.started_prompt
                and (session_id is None or request.session_id == session_id)
            ),
            None,
        )

    async def _send_cancel_ack(
        self, connection: Connection, session_id: str | None, accepted: bool
    ) -> None:
        await self._send(
            connection,
            {"type": "cancel_ack", "session_id": session_id, "accepted": accepted},
        )

    async def _abandon_context(self, context: PromptContext) -> None:
        if context.abandoned:
            return
        context.abandoned = True
        request = context.request
        if request is None:
            return
        record = self.sessions.get(request.session_id)
        if record is not None:
            async with record.admission_lock:
                with contextlib.suppress(ValueError):
                    record.queue.remove(request)
                await self._send_queue_positions(record)
        if not request.started_prompt:
            if request.task is not None:
                request.task.cancel()
            if record is not None:
                async with record.admission_lock:
                    if not record.queue and not request.started_prompt:
                        record.reserved = False
                        record.reserved_context = None
                        record.running = False
                        record.state = "idle"
            return
        request.cancel_requested = True
        with contextlib.suppress(RequestError, OSError, RuntimeError, asyncio.TimeoutError):
            await asyncio.wait_for(
                self._require_connection().cancel(session_id=request.session_id),
                timeout=_CANCEL_TIMEOUT,
            )
        if request.task is not None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(request.done), timeout=_CANCEL_TIMEOUT)

    async def _begin_shutdown(self, *, recycle: bool = False) -> None:
        if recycle:
            self._recycling = True
        if self._draining:
            return
        self._draining = True
        self._stop_event.set()
        with contextlib.suppress(Exception):
            await self.transport.stop_accepting()
        await self._reject_queued_requests()

    async def _reject_queued_requests(self) -> None:
        requests = tuple(self._all_requests.values())
        for request in requests:
            if request.started_prompt or request.context.abandoned:
                continue
            record = self.sessions.get(request.session_id)
            if record is not None:
                async with record.admission_lock:
                    with contextlib.suppress(ValueError):
                        record.queue.remove(request)
                    await self._send_queue_positions(record)
            await self._send(
                request.connection,
                {"type": "shutting_down"},
            )
            if request.task is not None:
                request.task.cancel()

    async def _lifecycle_monitor(self) -> None:
        while not self._stop_event.is_set():
            interval = min(0.1, max(self._ttl / 4, 0.01))
            await asyncio.sleep(interval)
            now = time.monotonic()
            if self._max_age >= 0 and now - self._started_at >= self._max_age:
                await self._begin_shutdown(recycle=True)
                continue
            if self._started_requests or self._connection_contexts:
                self._idle_since = None
                continue
            self._idle_since = self._idle_since or now
            if now - self._idle_since >= self._ttl:
                await self._begin_shutdown()

    async def _capture_stderr(self, process: asyncio.subprocess.Process) -> None:
        stream = process.stderr
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                return
            self._log(line.decode("utf-8", errors="replace").rstrip("\n"))

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
        self._adapter_lost = True
        for request in tuple(self._all_requests.values()):
            if request.started_prompt and not request.context.abandoned:
                await self._send_request_error(request, "adapter connection lost")
        with contextlib.suppress(Exception):
            await connection.close()
        await self._begin_shutdown()

    async def _cleanup_failed_start(self) -> None:
        await self._cancel_background_tasks()
        stack = self._exit_stack
        self._exit_stack = None
        if stack is not None:
            with contextlib.suppress(BaseException):
                await stack.aclose()
        with contextlib.suppress(BaseException):
            await self.transport.cleanup()
        self.connection = None
        self.process = None
        self._close_log()
        self._release_lock()

    async def _cancel_background_tasks(self) -> None:
        current = asyncio.current_task()
        tasks = [
            task
            for task in (self._process_watch_task, self._stderr_task, self._lifecycle_task)
            if task is not None and task is not current
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._process_watch_task = None
        self._stderr_task = None
        self._lifecycle_task = None

    async def _cancel_request_tasks(self) -> None:
        tasks = tuple(self._request_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._request_tasks.clear()

    async def _close_client_tasks(self) -> BaseException | None:
        cleanup_error: BaseException | None = None
        try:
            await self.transport.cleanup()
        except BaseException as error:
            cleanup_error = error
        tasks = tuple(self._client_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._client_tasks.clear()
        return cleanup_error

    def _register_connection(self, connection: Connection) -> None:
        self._connection_contexts[connection.id] = []

    def _forget_client_task(self, task: asyncio.Task[Any]) -> None:
        self._client_tasks.discard(task)
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.exception()

    def _forget_request_task(self, task: asyncio.Task[Any]) -> None:
        self._request_tasks.discard(task)
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.exception()

    def _track_request(self, request: PromptRequest) -> None:
        self._all_requests[id(request)] = request

    def _next_request_sequence(self) -> int:
        self._request_sequence += 1
        return self._request_sequence

    def _status_frame(self) -> dict[str, Any]:
        sessions = [
            {
                "id": record.session_id,
                "cwd": record.cwd,
                "state": record.state,
                "started": record.created_at,
            }
            for record in self.sessions.values()
        ]
        return {
            "type": "status",
            "pid": os.getpid(),
            "uptime_s": int(max(0.0, time.monotonic() - self._started_at)),
            "log": str(self.log_path),
            "sessions": sessions,
        }

    def _request_cwd(self, frame: dict[str, Any]) -> str | None:
        cwd = frame.get("cwd")
        if cwd is None:
            return None
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

    def _validate_resume_cwd(self, record: SessionRecord, requested_cwd: str | None) -> None:
        try:
            recorded_cwd = self._canonical_cwd(record.cwd)
        except ValueError as error:
            self._evict_session(record.session_id)
            raise ValueError(f"session cwd no longer exists: {record.cwd}") from error
        record.cwd = recorded_cwd
        if requested_cwd is not None and recorded_cwd != requested_cwd:
            raise ValueError(f"session cwd is {recorded_cwd}, --cwd says {requested_cwd}")

    def _load_cwd_for_session(self, session_id: str, requested_cwd: str | None) -> str:
        recorded_cwd = self._session_cwd_metadata(session_id)
        if requested_cwd is None:
            if recorded_cwd is None:
                raise ValueError(
                    f"session cwd is unknown for {session_id}; provide --cwd to load it"
                )
            requested_cwd = recorded_cwd
        elif recorded_cwd is not None:
            try:
                recorded_cwd = self._canonical_cwd(recorded_cwd)
            except ValueError as error:
                self._evict_session(session_id)
                raise ValueError(f"session cwd no longer exists: {recorded_cwd}") from error
            if recorded_cwd != requested_cwd:
                raise ValueError(f"session cwd is {recorded_cwd}, --cwd says {requested_cwd}")
        try:
            return self._canonical_cwd(requested_cwd)
        except ValueError as error:
            if recorded_cwd is not None:
                self._evict_session(session_id)
                raise ValueError(f"session cwd no longer exists: {recorded_cwd}") from error
            raise

    def _session_cwd_metadata(self, session_id: str) -> str | None:
        from acpc.sessions import load_session_cwd

        return load_session_cwd(self.target, session_id)

    def _evict_session(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)
        self.client.unregister_session(session_id)
        from acpc.sessions import evict_session_metadata

        evict_session_metadata(self.target, session_id)

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
        with contextlib.suppress(ConnectionError, OSError, ValueError):
            await self.transport.send(connection, frame)

    def _require_connection(self) -> ClientSideConnection:
        if self.connection is None:
            raise RuntimeError("daemon adapter is not initialized")
        return self.connection

    async def _drain_adapter_notifications(self) -> None:
        await asyncio.sleep(0)
        await _drain_notifications(self._require_connection())

    async def _ensure_can_create_session(self) -> None:
        if self._recycling or self._draining:
            raise RecyclingError("daemon is shutting down")
        process = self.process
        if process is None or process.pid is None:
            raise RuntimeError("daemon adapter is not initialized")
        rss_mb = sample_process_tree_rss_mb(process.pid)
        if rss_mb > rss_ceiling_mb():
            self._log(f"capacity: rss={rss_mb}MB")
            self._recycling = True
            raise CapacityError(rss_mb)

    def _acquire_lock(self) -> bool:
        path = lock_path_for_target(self.target)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_file.close()
            return False
        path.chmod(0o600)
        self._lock_file = lock_file
        return True

    def _write_lock_metadata(self) -> None:
        if self._lock_file is None:
            return
        metadata = {
            "pid": os.getpid(),
            "socket": str(socket_path_for_target(self.target)),
            "acpc_version": __version__,
            "target": self.target,
            "start_time": time.time(),
            "process_start_time": process_start_time(),
            "cmdline": process_cmdline(),
        }
        self._lock_file.seek(0)
        self._lock_file.truncate()
        json.dump(metadata, self._lock_file, separators=(",", ":"))
        self._lock_file.flush()

    def _release_lock(self) -> None:
        lock_file = self._lock_file
        self._lock_file = None
        if lock_file is None:
            return
        path = Path(lock_file.name)
        with contextlib.suppress(OSError):
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()
        with contextlib.suppress(FileNotFoundError, OSError):
            path.unlink()

    def _open_log(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_file = self.log_path.open("w", encoding="utf-8", buffering=1)

    def _log(self, message: str) -> None:
        if self._log_file is not None:
            self._log_file.write(f"{time.time():.3f} {message}\n")
            self._log_file.flush()

    def _close_log(self) -> None:
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None

    def _install_signal_handlers(self) -> None:
        if sys.platform == "win32":
            return
        loop = asyncio.get_running_loop()

        def request_shutdown() -> None:
            loop.create_task(self._begin_shutdown())

        for signum in (signal.SIGTERM,):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(signum, request_shutdown)
                self._signals_installed = True

    def _remove_signal_handlers(self) -> None:
        if not self._signals_installed or sys.platform == "win32":
            return
        loop = asyncio.get_running_loop()
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.remove_signal_handler(signal.SIGTERM)
        self._signals_installed = False


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _is_auth_error(error: BaseException) -> bool:
    message = str(error).lower()
    return any(marker in message for marker in _AUTH_MARKERS)


def read_mem_available_mb(path: Path | None = None) -> int | None:
    """Read MemAvailable from Linux procfs, returning whole MiB."""
    path = _MEMINFO_PATH if path is None else path
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (FileNotFoundError, OSError, ValueError, IndexError):
        return None
    return None


def rss_ceiling_mb() -> int:
    """Resolve the configured RSS ceiling, computed default, and floor."""
    configured = os.environ.get("ACPC_DAEMON_RSS_MAX")
    if configured:
        try:
            return max(0, int(configured))
        except ValueError:
            pass
    available = read_mem_available_mb()
    if available is None:
        return _RSS_FLOOR_MB
    return max(_RSS_FLOOR_MB, available // 4)


def sample_process_tree_rss_mb(pid: int) -> int:
    """Sample the adapter process group RSS on Linux or macOS."""
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["ps", "-o", "rss=", "-g", str(os.getpgid(pid))],
                check=True,
                capture_output=True,
                text=True,
            )
            return sum(int(value) for value in result.stdout.split()) // 1024
        except (OSError, ValueError, ProcessLookupError):
            return 0
    try:
        process_group = os.getpgid(pid)
    except (OSError, ProcessLookupError):
        return 0
    total_kb = 0
    proc = Path("/proc")
    try:
        entries = tuple(proc.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            stat_text = (entry / "stat").read_text(encoding="utf-8")
            after_comm = stat_text.rsplit(")", 1)[1].split()
            if int(after_comm[2]) != process_group:
                continue
            for line in (entry / "status").read_text(encoding="utf-8").splitlines():
                if line.startswith("VmRSS:"):
                    total_kb += int(line.split()[1])
                    break
        except (FileNotFoundError, OSError, ValueError, IndexError):
            continue
    return total_kb // 1024


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
