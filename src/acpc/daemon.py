"""The per-target daemon: one warm adapter, many sessions.

SPEC.md `daemon`. The daemon is a performance cache and nothing more — it
holds an adapter process open so the next turn on that target starts warm, and
it owns turns whose client has gone away (`--bg`, and a client that took a
SIGTERM and detached).

Three properties drive the design:

- **One adapter process per target.** ACP adapters host many sessions on one
  connection, so keeping a single process per target is what "warm" actually
  means. Session updates are demultiplexed back to per-session clients by the
  ACP session id.
- **Warm beats cold, and the difference is observable.** A turn on a session
  this daemon still holds is prompted directly. `session/load` is the *cold*
  path, for a session whose adapter is gone — after that the adapter has no
  memory of earlier turns beyond what it persisted itself.
- **A daemon never orphans a session.** Whatever ends the daemon — `daemon
  stop`, TTL expiry, a version-skew restart — every session it owns is moved to
  a finished state with the reason recorded first.

The daemon is started by `daemon_client`, never by a user; there is no `start`
verb. It runs `python -m acpc.daemon <target>` with stdout and stderr pointed
at `daemon/<target>.log`, which is where adapter stderr lands too.
"""

import asyncio
import contextlib
import functools
import os
import shlex
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from acp import PROTOCOL_VERSION, RequestError, text_block

from acpc import __version__, config, errors, ipc, paths, runner, sessions, transcript, vocab
from acpc.client import (
    REPLAY_GENERATION_KEY,
    VALIDATED_SESSION_ID_KEY,
    AcpcClient,
    ReplayTracker,
)
from acpc.permissions import PermissionLevel
from acpc.registry import AgentRegistry, ModeSpec
from acpc.spawn import spawn_adapter

# How often the idle sweep runs. Short enough that a test can set a tiny TTL
# and still see expiry, long enough to cost nothing over a 30-minute default.
IDLE_CHECK_INTERVAL = 0.5

# How long one `_session/steering` request may take before its outcome is
# unknown: the adapter's acknowledgement is a single round trip, and past this
# bound the instruction may or may not have landed. A test can lower it.
STEER_REQUEST_TIMEOUT = 10.0

# The adapter must answer `initialize` before the daemon accepts a turn. A test
# can lower this bound; a hung adapter must never hold a session in `starting`.
HOST_START_TIMEOUT = 60.0

# Frames name their operation under this key.
OP = "op"

# JSON-RPC's "no such method": an adapter that never implemented the steering
# extension answers an unknown `_session/steering` with exactly this code.
_METHOD_NOT_FOUND = -32601


@dataclass(frozen=True, slots=True)
class _NoSteeringOutcome:
    """A steering reply that reports a failure instead of an adapter outcome."""

    payload: dict[str, Any]


async def _steering_request(raw: Any, adapter_session_id: str, text: str) -> Any:
    """Send one `_session/steering` request down the warm adapter connection.

    A module function rather than a method because it holds nothing but the
    wire shape: the caller owns the connection's lifetime, and this is the one
    place the extension's request is written down.
    """
    return await raw.send_request(
        "_session/steering",
        {
            "sessionId": adapter_session_id,
            "prompt": [{"type": "text", "text": text}],
            # SPEC.md `steer`: acpc asks the adapter never to start a turn of
            # its own, so an instruction that arrives with nothing in flight
            # is refused rather than silently becoming a new turn.
            "_meta": {"steering": {"idleBehavior": "promptRequired"}},
        },
    )


class DaemonError(Exception):
    """The daemon could not serve a request; the message is one line."""


class SpawnArgvMismatch(DaemonError):
    """The live entry now requires a different adapter process argv."""


class AdapterHandshakeTimeout(DaemonError):
    """The adapter did not answer the ACP initialize handshake in time."""


def log_path_for_target(target: str) -> Path:
    """Where this target's daemon and adapter stderr go (SPEC.md `daemon`)."""
    return paths.daemon_dir() / f"{target}.log"


def steering_supported(initialize: Any) -> bool:
    """Read the adapter's steering capability off its `initialize` answer.

    SPEC.md `steer` reads support from top-level `_meta.steering.supported`,
    not from `agentCapabilities`: the extension is not part of the ACP schema,
    so the flag that carries it is the reserved metadata channel. The value is
    absent for an adapter that does not implement the extension at all, and
    only an explicit `true` claims it.
    """
    meta = getattr(initialize, "field_meta", None)
    if not isinstance(meta, Mapping):
        return False
    steering = meta.get("steering")
    if not isinstance(steering, Mapping):
        return False
    return steering.get("supported") is True


def _refusal_kind(error: BaseException) -> str | None:
    """Classify a refused turn, so the client reports what the daemon saw.

    The reply crosses a socket as text, and a message is not a contract: the
    caller matches on the kind, which is why the side that knows sets it.
    """
    if isinstance(error, runner.ResumeRotationError):
        return errors.CONFLICT if error.turn_token is None else errors.CORRUPT_STATE
    if isinstance(error, sessions.SessionStateError):
        return errors.CONFLICT
    if isinstance(error, sessions.CorruptSessionError):
        return errors.CORRUPT_STATE
    if isinstance(error, sessions.SessionNotFound):
        return errors.NOT_FOUND
    if isinstance(error, AdapterHandshakeTimeout):
        return errors.AGENT_ERROR
    return None


@dataclass(slots=True)
class _Turn:
    """One in-flight turn owned by this daemon."""

    session_id: str
    task: "asyncio.Task[Any] | None"
    cancel: runner._CancelSignal
    phase: str = "preparing"
    claim_established: bool = False
    backup: "_PreparationBackup | None" = None
    preparation_cancelable: bool = False
    preparation_done: "asyncio.Future[dict[str, Any]] | None" = None
    turn_token: int | None = None
    waiters: list["asyncio.Future[dict[str, Any]]"] = field(default_factory=list)
    result: dict[str, Any] | None = None


@dataclass(slots=True)
class _PreparationBackup:
    """In-memory bytes needed to undo a failed deferred-turn claim."""

    files: dict[Path, str]


class _MultiplexClient:
    """Route one adapter connection's callbacks to per-session clients.

    A warm adapter serves several acpc sessions at once, but every ACP callback
    carries the session it belongs to, so the demultiplex is exact. ACP's
    router can overlay peer ``_meta`` on that argument, so updates use the
    validated top-level session captured before routing.
    Anything arriving for a session this daemon does not know is dropped
    rather than misfiled onto another session's transcript.
    """

    def __init__(self) -> None:
        self._clients: dict[str, AcpcClient] = {}
        self._replay_tracker: ReplayTracker | None = None

    def bind(self, adapter_session_id: str, client: AcpcClient) -> None:
        generation_id = (
            self._replay_tracker.active_generation_id(adapter_session_id)
            if self._replay_tracker is not None
            else None
        )
        current = self._clients.get(adapter_session_id)
        if generation_id is not None and current is not None and current is not client:
            raise DaemonError(
                f"adapter session {adapter_session_id} cannot bind a second acpc session "
                f"while replay generation {generation_id} is open"
            )
        if generation_id is not None and current is None:
            raise DaemonError(
                f"adapter session {adapter_session_id} cannot bind while replay generation "
                f"{generation_id} is open"
            )
        self._clients[adapter_session_id] = client

    def release(self, adapter_session_id: str) -> None:
        self._clients.pop(adapter_session_id, None)

    def on_connect(self, conn: Any) -> None:
        self._replay_tracker = ReplayTracker.for_connection(getattr(conn, "_conn", None))

    def _for(self, session_id: str) -> AcpcClient | None:
        return self._clients.get(session_id)

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        validated_session_id = kwargs.pop(VALIDATED_SESSION_ID_KEY, None)
        routed_session_id = (
            validated_session_id if isinstance(validated_session_id, str) else session_id
        )
        generation_id = kwargs.get(REPLAY_GENERATION_KEY)
        if generation_id is not None and self._replay_tracker is not None:
            tagged_session_id = self._replay_tracker.session_for_tag(generation_id)
            if tagged_session_id is not None:
                # ACP merges peer _meta over validated handler arguments. The
                # tracker captured the real top-level sessionId before that
                # merge, so a replay tag remains a safe fallback for delayed
                # callbacks that were handed to this method directly.
                routed_session_id = tagged_session_id
            status = self._replay_tracker.consume_tag(generation_id)
            if status in {"active", "closed"}:
                return
            kwargs = dict(kwargs)
            kwargs.pop(REPLAY_GENERATION_KEY, None)
        client = self._for(routed_session_id)
        if client is not None:
            await client.session_update(routed_session_id, update, **kwargs)

    async def request_permission(
        self, session_id: str, tool_call: Any, options: list[Any], **kwargs: Any
    ) -> Any:
        client = self._for(session_id)
        if client is None:
            raise DaemonError(f"permission request for unknown session {session_id}")
        return await client.request_permission(session_id, tool_call, options, **kwargs)

    async def read_text_file(self, path: str, session_id: str, **kwargs: Any) -> Any:
        client = self._require(session_id)
        return await client.read_text_file(path, session_id, **kwargs)

    async def write_text_file(self, path: str, content: str, session_id: str, **kwargs: Any) -> Any:
        client = self._require(session_id)
        return await client.write_text_file(path, content, session_id, **kwargs)

    async def create_terminal(self, command: str, session_id: str, **kwargs: Any) -> Any:
        return await self._require(session_id).create_terminal(command, session_id, **kwargs)

    async def terminal_output(self, session_id: str, terminal_id: str, **kwargs: Any) -> Any:
        return await self._require(session_id).terminal_output(session_id, terminal_id, **kwargs)

    async def release_terminal(self, session_id: str, terminal_id: str, **kwargs: Any) -> Any:
        return await self._require(session_id).release_terminal(session_id, terminal_id, **kwargs)

    async def wait_for_terminal_exit(self, session_id: str, terminal_id: str, **kwargs: Any) -> Any:
        return await self._require(session_id).wait_for_terminal_exit(
            session_id, terminal_id, **kwargs
        )

    async def kill_terminal(self, session_id: str, terminal_id: str, **kwargs: Any) -> Any:
        return await self._require(session_id).kill_terminal(session_id, terminal_id, **kwargs)

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        del method, params
        return {}

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        del method, params

    def _require(self, session_id: str) -> AcpcClient:
        client = self._for(session_id)
        if client is None:
            raise DaemonError(f"callback for unknown session {session_id}")
        return client


class AdapterHost:
    """The warm adapter process for one target."""

    def __init__(self, target: str) -> None:
        self.target = target
        self.mux = _MultiplexClient()
        self._stack: contextlib.AsyncExitStack | None = None
        self._conn: Any = None
        self._process: Any = None
        self._command: tuple[str, tuple[str, ...]] | None = None
        self.agent_capabilities: Any = None
        # SPEC.md `steer`: whether this adapter takes `_session/steering`.
        # Retained per warm process, exactly like the capabilities above.
        self.steering_supported = False
        self._starting = asyncio.Lock()
        # acpc session id -> the adapter session id it is bound to, for as long
        # as this adapter process lives. Presence here *is* "warm".
        self.adapter_sessions: dict[str, str] = {}
        self._restore_tasks: dict[str, asyncio.Task[Any]] = {}

    @property
    def started(self) -> bool:
        return self._conn is not None

    def _adapter_died(self) -> bool:
        return self._process is not None and self._process.returncode is not None

    async def ensure(self, resolution: Any) -> Any:
        """Start the adapter once; later turns reuse the same process.

        Locked because the first two turns on a cold target arrive together:
        without it both would see no connection, both would spawn, and the
        loser's process would be orphaned with a session already bound to it.
        """
        if self._conn is not None and not self._adapter_died():
            self._check_command(resolution)
            return self._conn
        async with self._starting:
            if self._conn is not None:
                if not self._adapter_died():
                    self._check_command(resolution)
                    return self._conn
                # The adapter process died under this daemon. Drop the dead
                # connection so the target heals with a fresh adapter instead
                # of failing every turn until someone runs `daemon stop`.
                # Warm sessions go with it: the memory they relied on lived in
                # the dead process, so their next turn honestly resumes cold.
                await self.close()
            return await self._start(resolution)

    def _check_command(self, resolution: Any) -> None:
        """Refuse a turn whose live entry requires a different spawn argv."""
        running = self._command
        if running is None:
            return
        required = runner.adapter_command(resolution)
        if running == required:
            return
        running_argv = shlex.join((running[0], *running[1]))
        required_argv = shlex.join((required[0], *required[1]))
        raise SpawnArgvMismatch(
            f"daemon adapter argv mismatch: running {running_argv}; "
            f"required {required_argv}; run 'acpc daemon stop {self.target}' and retry"
        )

    async def reset_if_dead(self) -> None:
        """Drop the connection to an adapter whose process is gone."""
        async with self._starting:
            if self._conn is not None and self._adapter_died():
                await self.close()

    async def _start(self, resolution: Any) -> Any:
        command, args = runner.adapter_command(resolution)
        stack = contextlib.AsyncExitStack()
        try:
            conn, process = await stack.enter_async_context(
                spawn_adapter(
                    self.mux,
                    command,
                    *args,
                    env=resolution.adapter_environment,
                    drain_stderr=True,
                )
            )
            try:
                initialize = await asyncio.wait_for(
                    conn.initialize(protocol_version=PROTOCOL_VERSION),
                    timeout=HOST_START_TIMEOUT,
                )
            except TimeoutError as error:
                raise AdapterHandshakeTimeout(
                    f"adapter handshake did not complete within {HOST_START_TIMEOUT:g}s"
                ) from error
        except BaseException:
            # A cancelled preparation can interrupt initialize after the
            # adapter process and its transport have been entered, before the
            # host has published its stack as the warm connection.
            with contextlib.suppress(Exception):
                await stack.aclose()
            raise
        self._stack = stack
        self._conn = conn
        self._process = process
        self._command = (command, args)
        self.agent_capabilities = getattr(initialize, "agent_capabilities", None)
        self.steering_supported = steering_supported(initialize)
        return conn

    async def close(self) -> None:
        restore_tasks = tuple(self._restore_tasks.values())
        self._restore_tasks.clear()
        for task in restore_tasks:
            task.cancel()
        for task in restore_tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._stack is not None:
            with contextlib.suppress(Exception):
                await self._stack.aclose()
        self._stack = None
        self._conn = None
        self._process = None
        self._command = None
        self.agent_capabilities = None
        self.steering_supported = False
        self.adapter_sessions.clear()

    def start_restore(self, adapter_session_id: str, coroutine: Any) -> asyncio.Task[Any]:
        """Track one uncancellable adapter restore until its response settles."""
        previous = self._restore_tasks.get(adapter_session_id)
        if previous is not None and not previous.done():
            raise DaemonError(f"adapter session {adapter_session_id} is still restoring")
        task = asyncio.create_task(coroutine, name=f"acpc.restore-flight.{adapter_session_id}")
        self._restore_tasks[adapter_session_id] = task

        def settled(completed: asyncio.Task[Any]) -> None:
            if self._restore_tasks.get(adapter_session_id) is completed:
                self._restore_tasks.pop(adapter_session_id, None)
            if completed.cancelled():
                return
            with contextlib.suppress(BaseException):
                completed.exception()

        task.add_done_callback(settled)
        return task

    async def wait_for_restore(self, adapter_session_id: str) -> None:
        """Wait for a cancelled restore before another one can use its session."""
        task = self._restore_tasks.get(adapter_session_id)
        if task is None:
            return
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.cancelled():
                raise
        except Exception:  # noqa: BLE001, S110
            # The old restore's result belongs to the cancelled turn. A later
            # continuation only needs its remote work to have settled.
            pass


def _endpoint_identity(path: Path | None) -> tuple[int, int] | None:
    """The socket's (device, inode), or None when nothing is at that path."""
    if path is None:
        return None
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_dev, info.st_ino)


class Daemon:
    """Serves one target until it is stopped or goes idle."""

    def __init__(self, target: str, *, ttl: float | None = None, max_concurrent: int | None = None):
        settings = config.load_config()
        self.target = target
        self.ttl = settings.daemon_ttl_seconds if ttl is None else ttl
        self.max_concurrent = (
            settings.daemon_max_concurrent if max_concurrent is None else max_concurrent
        )
        self.started_at = time.time()
        self.host = AdapterHost(target)
        self.turns: dict[str, _Turn] = {}
        self._slots = asyncio.Semaphore(self.max_concurrent)
        self._transport = ipc.UnixSocketTransport(target)
        self._shutdown = asyncio.Event()
        self._last_busy = time.monotonic()
        self._stop_reason: str | None = None
        self._clients: set[asyncio.Task[None]] = set()
        self._endpoint: tuple[int, int] | None = None

    # -- lifecycle ---------------------------------------------------------

    async def serve(self) -> None:
        """Bind, accept clients, and expire when idle for the whole TTL."""
        await self._transport.bind()
        self._endpoint = _endpoint_identity(self._transport.path)
        idle = asyncio.create_task(self._expire_when_idle(), name="acpc.daemon.idle")
        accepting = asyncio.create_task(self._accept_forever(), name="acpc.daemon.accept")
        idle.add_done_callback(functools.partial(self._lifecycle_task_ended, "idle"))
        accepting.add_done_callback(functools.partial(self._lifecycle_task_ended, "accept"))
        try:
            await self._shutdown.wait()
        finally:
            reason = self._stop_reason or "the daemon stopped"
            print(f"acpc daemon {self.target}: {reason}", file=sys.stderr, flush=True)
            accepting.cancel()
            idle.cancel()
            for task in (accepting, idle):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            await self._shut_down_sessions(reason)
            await self.host.close()
            with contextlib.suppress(Exception):
                await self._transport.cleanup()

    async def _accept_forever(self) -> None:
        while True:
            try:
                connection = await self._transport.accept()
            except asyncio.CancelledError:
                raise
            except (ConnectionError, OSError):
                return
            # Held so the task is not garbage collected mid-conversation.
            task = asyncio.create_task(self._serve_client(connection), name="acpc.daemon.client")
            self._clients.add(task)
            task.add_done_callback(self._clients.discard)

    def _lifecycle_task_ended(self, name: str, task: asyncio.Task[None]) -> None:
        """Retire the daemon when a loop that should never finish, finishes.

        Neither loop returns while the daemon is serving, and nothing awaits
        them until shutdown, so on its own a death in here is silent and
        leaves a daemon that nothing can ever retire: unreachable, still
        holding an adapter, and immune to `daemon stop`. Whatever the cause,
        the outcome is now a recorded shutdown rather than an immortal
        process.
        """
        if self._shutdown.is_set() or task.cancelled():
            return
        error = task.exception()
        detail = f": {error!r}" if error is not None else ""
        self._stop_reason = f"the daemon's {name} loop ended unexpectedly{detail}"
        self._shutdown.set()

    async def _expire_when_idle(self) -> None:
        """Exit after a whole TTL with nothing to keep the adapter warm.

        Idle means no turn is in flight and no session this daemon owns is
        still active. One check covers both: a turn stays in `self.turns`
        from dispatch until it finishes, whether or not a client is still
        watching it, so a detached session — work nobody is waiting on, whose
        adapter must not be killed under it — keeps the daemon busy for as
        long as it runs.
        """
        while True:
            try:
                await asyncio.sleep(IDLE_CHECK_INTERVAL)
                if self._endpoint_gone():
                    # Nothing can reach us any more, so staying warm serves no
                    # one. This is also what retires the daemons an acceptance run
                    # leaves behind when it removes its throwaway state root.
                    self._stop_reason = "the daemon's socket was removed"
                    self._shutdown.set()
                    return
                if self._busy():
                    self._last_busy = time.monotonic()
                    continue
                if time.monotonic() - self._last_busy >= self.ttl:
                    self._stop_reason = "the daemon expired after its idle TTL"
                    self._shutdown.set()
                    return
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001, S112
                # An incidental check failure must not kill this task: the next
                # sweep can still observe a removed endpoint or an expired TTL.
                continue

    def _busy(self) -> bool:
        return bool(self.turns)

    def _endpoint_gone(self) -> bool:
        """True once this daemon no longer owns the socket clients dial.

        Identity, not existence: binding unlinks whatever is already at the
        path, so a daemon that started later takes the endpoint over and this
        one would otherwise keep running unreachable — alive, unstoppable, and
        holding an adapter nobody can use.
        """
        return _endpoint_identity(self._transport.path) != self._endpoint

    async def _shut_down_sessions(self, reason: str) -> None:
        """Finish every session this daemon owns; never leave one running.

        SPEC.md `daemon`: `daemon stop` with active sessions transitions them
        to `failed` with the reason recorded in meta — never orphans.
        """
        for turn in list(self.turns.values()):
            turn.cancel.request("failed", stop_reason=reason)
            if turn.preparation_done is not None and not turn.preparation_done.done():
                turn.preparation_done.set_result({"ok": False, "error": reason})
            if turn.task is not None:
                turn.task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await turn.task
            with contextlib.suppress(sessions.SessionError):
                meta = sessions.read_meta(turn.session_id)
                if meta.is_active:
                    outcome = runner.TurnOutcome(
                        state="failed",
                        stop_reason=reason,
                        answer="",
                    )
                    runner._finalize(
                        turn.session_id,
                        outcome,
                        error=runner.TurnEndedByAcpc(reason),
                    )
            self._resolve_waiters(turn, {"state": "failed", "exit_code": vocab.EXIT_AGENT_ERROR})
        self.turns.clear()

    # -- protocol ----------------------------------------------------------

    async def _serve_client(self, connection: ipc.Connection) -> None:
        try:
            while True:
                try:
                    frame = await self._transport.receive(connection)
                except (ipc.InvalidFrameError, ipc.FrameTooLargeError) as error:
                    await self._reply(connection, {"ok": False, "error": str(error)})
                    continue
                except ConnectionError:
                    return
                reply = await self._dispatch(frame)
                if reply is None:
                    return
                await self._reply(connection, reply)
        finally:
            with contextlib.suppress(Exception):
                await self._transport.close_connection(connection)

    async def _reply(self, connection: ipc.Connection, frame: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            await self._transport.send(connection, frame)

    async def _dispatch(self, frame: dict[str, Any]) -> dict[str, Any] | None:
        operation = frame.get(OP)
        if operation == "hello":
            return self._hello(frame)
        if operation == "start":
            return await self._start(frame)
        if operation == "await":
            return await self._await(frame)
        if operation == "await_preparation":
            return await self._await_preparation(frame)
        if operation == "cancel":
            return self._cancel(frame)
        if operation == "steer":
            return await self._steer(frame)
        if operation == "status":
            return self._status()
        if operation == "stop":
            self._stop_reason = "the daemon was stopped"
            self._shutdown.set()
            return {"ok": True}
        return {"ok": False, "error": f"unknown daemon operation {operation!r}"}

    def _hello(self, frame: dict[str, Any]) -> dict[str, Any]:
        """Greet a client, and stand down if it is a different acpc build.

        SPEC.md `daemon`: version skew self-heals. The daemon is the one that
        gives way, because the client is the newer intent.
        """
        client_version = frame.get("version")
        if client_version != __version__:
            self._stop_reason = "the daemon restarted for a new acpc version"
            self._shutdown.set()
            return {"ok": False, "restart": True, "version": __version__}
        return {
            "ok": True,
            "version": __version__,
            "pid": os.getpid(),
            "started_at": self.started_at,
            "target": self.target,
        }

    def _status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "version": __version__,
            "target": self.target,
            "pid": os.getpid(),
            "uptime_seconds": time.time() - self.started_at,
            "log": str(log_path_for_target(self.target)),
            "sessions": sorted(self.turns),
            "preparing": sorted(
                session_id for session_id, turn in self.turns.items() if turn.phase == "preparing"
            ),
            "restoring": sorted(self.host._restore_tasks),
            "max_concurrent": self.max_concurrent,
        }

    def _cancel(self, frame: dict[str, Any]) -> dict[str, Any]:
        """Cancel the turn this daemon holds for one session.

        SPEC.md `cancel`: a call selects the turn active when it starts, so a
        request naming an older ``turn_token`` than the one this daemon has
        established must not reach into whatever turn is running now — it
        reports `stale` and lets the caller reselect. A frame with no token,
        or one that arrives before this turn's own token is established, is
        never "a different token" and is honored as before.
        """
        session_id = frame.get("session_id", "")
        turn = self.turns.get(session_id)
        if turn is None:
            return {"ok": False, "error": f"session {session_id} is not running here"}
        requested_token = frame.get("turn_token")
        if isinstance(requested_token, bool) or not isinstance(requested_token, int):
            requested_token = None
        if (
            requested_token is not None
            and turn.turn_token is not None
            and turn.turn_token != requested_token
        ):
            return {
                "ok": False,
                "kind": errors.CONFLICT,
                "stale": True,
                "turn_token": turn.turn_token,
            }
        turn.cancel.request("canceled")
        if (
            turn.phase == "preparing"
            and turn.task is not None
            and (not turn.claim_established or turn.preparation_cancelable)
        ):
            # During cold start this is the request handler itself; after
            # acceptance it is the daemon-owned preparation/turn task.
            turn.task.cancel()
        return {"ok": True, "turn_token": turn.turn_token}

    async def _steer(self, frame: dict[str, Any]) -> dict[str, Any]:
        """Deliver an in-place correction to a turn this daemon is running.

        SPEC.md `steer`: the instruction goes to the adapter's
        `_session/steering` extension over the connection that already owns
        the turn. Nothing here rotates, re-prompts or finalizes — the turn
        keeps its number and its files, and only the adapter's answer says
        whether the instruction landed. A failed delivery is never retried
        and never falls back to cancel-then-start: repeating the instruction
        is the caller's decision.
        """
        session_id = frame.get("session_id", "")
        text = frame.get("text", "")
        turn = self.turns.get(session_id)
        if turn is None or turn.phase != "running":
            return {
                "ok": False,
                "kind": errors.CONFLICT,
                "error": f"session {session_id} has no turn in flight",
            }
        adapter_session_id = self.host.adapter_sessions.get(session_id)
        if adapter_session_id is None or not self.host.steering_supported:
            return {
                "ok": False,
                "kind": errors.NOT_SUPPORTED,
                "error": "the adapter does not support in-place steering",
            }
        raw = getattr(self.host._conn, "_conn", None)
        if raw is None or not hasattr(raw, "send_request"):
            # The adapter process is gone, so nothing crossed and nothing can.
            return {"ok": False, "kind": errors.OUTCOME_UNKNOWN}
        reply = await self._steering_reply(raw, adapter_session_id, text)
        if isinstance(reply, _NoSteeringOutcome):
            return reply.payload
        return await self._apply_steering(session_id, adapter_session_id, text, turn, reply)

    async def _steering_reply(self, raw: Any, adapter_session_id: str, text: str) -> Any:
        """Send the request and describe everything that is not an outcome."""
        try:
            return await asyncio.wait_for(
                _steering_request(raw, adapter_session_id, text),
                timeout=STEER_REQUEST_TIMEOUT,
            )
        except TimeoutError:
            # The request crossed; the answer is the one thing missing.
            return _NoSteeringOutcome({"ok": False, "kind": errors.OUTCOME_UNKNOWN, "sent": True})
        except RequestError as error:
            if error.code == _METHOD_NOT_FOUND:
                # The adapter declared the extension and then denied it — a
                # capability lie, not a delivery acpc can report on.
                return _NoSteeringOutcome({"ok": False, "kind": errors.NOT_SUPPORTED})
            return _NoSteeringOutcome({"ok": False, "kind": errors.OUTCOME_UNKNOWN, "sent": True})
        except Exception:  # noqa: BLE001
            # A transport failure after the frame went out is exactly the
            # case SPEC calls unknown: the adapter may have taken it.
            return _NoSteeringOutcome({"ok": False, "kind": errors.OUTCOME_UNKNOWN, "sent": True})

    async def _apply_steering(
        self,
        session_id: str,
        adapter_session_id: str,
        text: str,
        turn: _Turn,
        reply: Any,
    ) -> dict[str, Any]:
        """Turn the adapter's own answer into this daemon's reply."""
        outcome = reply.get("outcome") if isinstance(reply, Mapping) else None
        if outcome == "injected":
            self._record_steer(session_id, text, "injected")
            return {"ok": True, "outcome": "injected", "turn_token": turn.turn_token}
        if outcome == "promptRequired":
            self._record_steer(session_id, text, "promptRequired")
            return {"ok": False, "kind": errors.CONFLICT, "outcome": "promptRequired"}
        if outcome == "startedNewTurn":
            # The adapter started a turn of its own. acpc owns neither it nor
            # its ending, so it is cancelled and the result stays unknown.
            self._record_steer(session_id, text, "startedNewTurn")
            await self._cancel_adapter_turn(adapter_session_id)
            return {"ok": False, "kind": errors.OUTCOME_UNKNOWN, "outcome": "startedNewTurn"}
        # An answer acpc does not recognize is not a delivery it can describe,
        # so it is reported the way an unanswered request is.
        self._record_steer(session_id, text, outcome if isinstance(outcome, str) else "unknown")
        return {"ok": False, "kind": errors.OUTCOME_UNKNOWN, "sent": True}

    async def _cancel_adapter_turn(self, adapter_session_id: str) -> None:
        """Cancel a turn the adapter started on its own after a steering request."""
        raw = getattr(self.host._conn, "_conn", None)
        if raw is None or not hasattr(raw, "send_notification"):
            return
        with contextlib.suppress(Exception):
            await raw.send_notification("session/cancel", {"sessionId": adapter_session_id})

    def _record_steer(self, session_id: str, text: str, outcome: str) -> None:
        """Append the correction to the session transcript.

        The steer itself has already happened, so a transcript that cannot be
        written must not turn into a failed reply: the caller would be told the
        instruction did not land when it did.
        """
        with contextlib.suppress(OSError, sessions.SessionError, transcript.TranscriptError):
            transcript.Transcript(sessions.transcript_path(session_id)).append(
                "steer", mode=vocab.STEER_IN_PLACE, text=text, outcome=outcome
            )

    async def _start(self, frame: dict[str, Any]) -> dict[str, Any]:
        session_id = frame.get("session_id", "")
        if session_id in self.turns:
            return {
                "ok": False,
                "error": f"session {session_id} already has a turn in flight",
                "kind": errors.CONFLICT,
            }
        try:
            request = self._rebuild_request(frame.get("payload") or {})
        except Exception as error:  # noqa: BLE001
            # Every way of failing to build a turn is the same reply. This was
            # a list of expected types until a new raise was added to
            # `_rebuild_request` and escaped it: an uncaught one here leaves the
            # connection with no reply at all, so the caller sees a socket error
            # instead of the reason. Nothing else is scoped in this try.
            return {
                "ok": False,
                "error": str(error),
                "preserve_session": bool((frame.get("payload") or {}).get("defer_rotation")),
            }

        queued = self._slots.locked()
        cancel = runner._CancelSignal()
        preparation = request.defer_rotation
        turn = _Turn(
            session_id=session_id,
            task=None,
            cancel=cancel,
            preparation_cancelable=request.defer_rotation,
            preparation_done=(
                asyncio.get_running_loop().create_future() if request.defer_rotation else None
            ),
        )
        self.turns[session_id] = turn
        turn.task = asyncio.current_task()
        self._last_busy = time.monotonic()

        try:
            if request.defer_rotation:
                async with sessions.session_reservation(session_id):
                    turn.backup = self._snapshot_session(session_id)
                    events = transcript.Transcript(sessions.transcript_path(session_id))
                    request = runner._prepare_resumed_turn(
                        session_id, request, events, pid=os.getpid()
                    )
            if not self.host.started or self.host._adapter_died():
                await self.host.ensure(request.resolution)
            self._record_steer_mode(session_id)
            if not preparation:
                sessions.mark_running(session_id, pid=os.getpid())
                transcript.Transcript(sessions.transcript_path(session_id)).append(
                    "state", **{"from": "starting", "to": "running"}
                )
            turn.claim_established = True
            turn.turn_token = request.turn_token
            if turn.turn_token is None:
                turn.turn_token = sessions.read_meta(session_id).turns
        except runner.ResumeRotationError as error:
            if error.turn_token is None:
                self._rollback_preparation(session_id, turn)
            self.turns.pop(session_id, None)
            return {
                "ok": False,
                "error": runner.describe_error(error),
                "kind": _refusal_kind(error),
                "preserve_session": preparation,
            }
        except BaseException as error:  # noqa: BLE001
            canceled = isinstance(error, asyncio.CancelledError) and turn.cancel.state == "canceled"
            stop_reason = "error"
            if isinstance(error, asyncio.CancelledError) and turn.cancel.state == "failed":
                # The daemon is shutting down under this handshake: the session
                # records that reason, the way a running turn does.
                stop_reason = turn.cancel.stop_reason or "the daemon was stopped"
                error = runner.TurnEndedByAcpc(stop_reason)
            finalized = False
            if preparation:
                if canceled and turn.backup is not None:
                    expected_turn = sessions.read_meta(session_id).turns
                    outcome = runner.TurnOutcome(
                        state="canceled",
                        stop_reason=runner.PREPARATION_CANCELLED_REASON,
                        answer=runner.preparation_cancelled_answer(session_id),
                        turn_token=expected_turn,
                    )
                    runner._finalize(session_id, outcome, expected_turn=expected_turn)
                    self._finish(session_id, outcome)
                    finalized = True
                else:
                    self._rollback_preparation(session_id, turn)
            elif canceled:
                outcome = runner.TurnOutcome(state="canceled", stop_reason="canceled", answer="")
                runner._finalize(session_id, outcome)
                self._finish(session_id, outcome)
                finalized = True
            else:
                with contextlib.suppress(Exception):
                    sessions.update_meta(session_id, steer_mode=vocab.STEER_CANCEL_THEN_START)
                with contextlib.suppress(Exception):
                    runner._finalize(
                        session_id,
                        runner.TurnOutcome(state="failed", stop_reason=stop_reason, answer=""),
                        error=error,
                    )
            if not finalized:
                self.turns.pop(session_id, None)
            return {
                "ok": False,
                "error": runner.describe_error(error),
                "kind": _refusal_kind(error),
                "preserve_session": preparation,
            }

        turn.task = None
        turn_coro = self._run_accepted_turn(session_id, request, turn)
        try:
            task = asyncio.create_task(turn_coro, name=f"acpc.turn.{session_id}")
        except BaseException as error:  # noqa: BLE001
            turn_coro.close()
            if turn.preparation_cancelable:
                self._rollback_preparation(session_id, turn)
            else:
                runner._finalize(
                    session_id,
                    runner.TurnOutcome(state="failed", stop_reason="error", answer=""),
                    error=error,
                )
            self.turns.pop(session_id, None)
            return {
                "ok": False,
                "error": runner.describe_error(error),
                "preserve_session": turn.preparation_cancelable,
            }
        turn.task = task
        return {"ok": True, "queued": queued, "max_concurrent": self.max_concurrent}

    async def _run_accepted_turn(
        self, session_id: str, request: runner.TurnRequest, turn: _Turn
    ) -> runner.TurnOutcome:
        """Prepare and run a turn after its accepted disk claim."""
        current_request = request
        try:
            if turn.preparation_cancelable:
                current_request = await self._prepare_turn(session_id, request, turn)
                if turn.preparation_done is not None and not turn.preparation_done.done():
                    turn.preparation_done.set_result({"ok": True})
                turn.backup = None
            return await self._run_turn(session_id, current_request, turn)
        except SpawnArgvMismatch as error:
            if turn.preparation_cancelable and turn.phase == "preparing":
                turn.backup = None
                if turn.preparation_done is not None and not turn.preparation_done.done():
                    turn.preparation_done.set_result({"ok": False, "error": str(error)})
                expected_turn = sessions.read_meta(session_id).turns
                outcome = runner.TurnOutcome(
                    state="failed",
                    stop_reason="error",
                    answer="",
                    turn_token=expected_turn,
                )
                runner._finalize(
                    session_id,
                    outcome,
                    error=error,
                    expected_turn=expected_turn,
                )
                self._finish(session_id, outcome, error=error)
                return outcome
            raise
        except asyncio.CancelledError:
            if (
                turn.cancel.state == "canceled"
                and turn.phase == "preparing"
                and turn.preparation_cancelable
            ):
                # No prompt task exists yet. The claimed turn is real, but its
                # prompt did not cross ACP, so cancellation has no diagnosis and
                # no partial answer to preserve.
                try:
                    expected_turn = sessions.read_meta(session_id).turns
                except sessions.SessionError:
                    expected_turn = current_request.turn_token
                outcome = runner.TurnOutcome(
                    state="canceled",
                    stop_reason=runner.PREPARATION_CANCELLED_REASON,
                    answer=runner.preparation_cancelled_answer(session_id),
                    turn_token=expected_turn,
                )
                if turn.preparation_done is not None and not turn.preparation_done.done():
                    turn.preparation_done.set_result(
                        {
                            "ok": False,
                            "cancelled": True,
                            "error": runner.PREPARATION_CANCELLED_REASON,
                        }
                    )
                runner._finalize(
                    session_id,
                    outcome,
                    expected_turn=expected_turn,
                )
                self._finish(session_id, outcome)
                return outcome
            raise
        except runner.ResumePreparationError as error:
            self._rollback_preparation(session_id, turn)
            if turn.preparation_done is not None and not turn.preparation_done.done():
                turn.preparation_done.set_result(
                    {"ok": False, "error": runner.describe_error(error)}
                )
            outcome = runner.TurnOutcome(state="failed", stop_reason="error", answer="")
            self._finish(session_id, outcome, error=error)
            return outcome
        except BaseException as error:
            if turn.preparation_cancelable and turn.phase == "preparing":
                self._rollback_preparation(session_id, turn)
                if turn.preparation_done is not None and not turn.preparation_done.done():
                    turn.preparation_done.set_result(
                        {"ok": False, "error": runner.describe_error(error)}
                    )
                outcome = runner.TurnOutcome(state="failed", stop_reason="error", answer="")
                self._finish(session_id, outcome, error=error)
                return outcome
            raise

    async def _await(self, frame: dict[str, Any]) -> dict[str, Any]:
        """Block until the session's turn ends, or say so at once if it already has.

        ``turn`` (optional) pins the call to one turn (M1g): a caller that
        started observing turn N before it lost the race to a rotation gets
        `stale` immediately rather than waiting on a turn that is not the one
        it asked about, and reads that turn's own parked outcome instead. A
        frame with no `turn` is served as it always was — the same leniency
        `_cancel` gives an untokened request. One that arrives before this
        daemon's own turn has a token yet (`_start` assigns it only once
        backend initialization clears, up to the full `--timeout` on a cold
        start) is checked against the on-disk turn count instead, the same
        source `_start` itself falls back to once the token is available.
        """
        session_id = frame.get("session_id", "")
        requested_turn = frame.get("turn")
        if isinstance(requested_turn, bool) or not isinstance(requested_turn, int):
            requested_turn = None
        in_flight = self.turns.get(session_id)
        if in_flight is not None:
            if requested_turn is not None:
                current_token = (
                    in_flight.turn_token
                    if in_flight.turn_token is not None
                    else self._current_turns(session_id)
                )
                if current_token is not None and current_token != requested_turn:
                    return {"ok": True, "stale": True, "turn": current_token}
            if in_flight.result is not None:
                return {"ok": True, "outcome": in_flight.result}
            waiter: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
            in_flight.waiters.append(waiter)
            return {"ok": True, "outcome": await waiter}
        if requested_turn is not None:
            current_turn = self._current_turns(session_id)
            if current_turn is not None and current_turn != requested_turn:
                return {"ok": True, "stale": True, "turn": current_turn}
        return {"ok": True, "outcome": self._outcome_from_disk(session_id)}

    @staticmethod
    def _current_turns(session_id: str) -> int | None:
        try:
            return sessions.read_meta(session_id).turns
        except sessions.SessionError:
            return None

    async def _await_preparation(self, frame: dict[str, Any]) -> dict[str, Any]:
        """Wait only for deferred resume preparation, not for the prompt."""
        session_id = frame.get("session_id", "")
        turn = self.turns.get(session_id)
        if turn is None or turn.preparation_done is None:
            return {"ok": True}
        if turn.preparation_done.done():
            return turn.preparation_done.result()
        return await turn.preparation_done

    @staticmethod
    def _outcome_from_disk(session_id: str) -> dict[str, Any]:
        """Answer for a session this daemon is not running (finished, or never here)."""
        try:
            meta = sessions.read_meta(session_id)
        except sessions.SessionError as error:
            return {"state": "failed", "exit_code": vocab.EXIT_AGENT_ERROR, "error": str(error)}
        return {
            "state": meta.state,
            "exit_code": (
                meta.exit_code
                if meta.exit_code is not None
                else runner.exit_code_for(meta.state, meta.stop_reason)
            ),
            "stop_reason": meta.stop_reason,
        }

    # -- running a turn ----------------------------------------------------

    def _rebuild_request(self, payload: dict[str, Any]) -> runner.TurnRequest:
        """Rebuild the turn from the small serializable shape the client sent.

        The entry is looked up here rather than shipped whole — the daemon
        reads the same `ACPC_HOME` as its client, so the adapter contract
        (command, env and provider settings) is smaller on the wire as a name. The
        *resolved* fields are then taken from the payload verbatim, never
        re-derived from the entry: the client already resolved them and
        `meta.json` stored them, and SPEC's "editing an entry never changes a
        session mid-conversation" holds only if this side agrees. Passing them
        back as overrides would not, because an override of `None` reads as
        "not set on this call" and hands the decision back to the entry file —
        which for `mode`, the one field whose resolved value is legitimately
        `None`, silently adopts whatever the entry says at *this* moment.

        The lookup is still trusted for everything the payload does not carry,
        and that list is wider than the resolved values: the command, the env
        and passthrough behind `adapter_environment`, `home_env`,
        `effort_config_id`, and the effort validation that runs on the entry's
        own value. So an entry edit can still redirect a live session's provider
        mid-conversation, and an entry that grows an unsupported effort makes
        `continue` fail here rather than run on its stored one. Both are the
        cost of shipping the entry as a name; the selected mode facts are pinned
        in the payload.

        Provenance is deliberately left as the lookup produced it: nothing on
        this path reads it, `session_resolution` runs client-side at dispatch.
        """
        mode = payload.get("mode")
        grants = payload.get("grants")
        delegates = payload.get("delegates")
        mode_spec = None
        policy_level = None
        if mode is not None:
            if not isinstance(mode, str):
                raise DaemonError("dispatched mode has invalid mode facts: mode must be a string")
            if grants is None or delegates is None:
                raise DaemonError(
                    f"mode {mode} has missing mode facts: dispatch must include grants and delegates"
                )
            if (
                not isinstance(grants, str)
                or grants not in vocab.PERMISSION_VALUES[:-1]
                or not isinstance(delegates, bool)
            ):
                raise DaemonError(
                    f"mode {mode} has invalid mode facts: grants must be a scale value and "
                    "delegates must be boolean"
                )
            mode_spec = ModeSpec(grants=grants, delegates=delegates)
            policy = payload.get("permissions")
            if not isinstance(policy, str):
                raise DaemonError(
                    f"mode {mode} has no valid permission policy in the dispatch payload"
                )
            policy = vocab.normalize_permission(policy)
            try:
                policy_level = PermissionLevel(policy)
            except ValueError:
                raise DaemonError(f"mode {mode} has invalid permission policy {policy!r}") from None
        resolved_call = AgentRegistry().resolve_call(payload["entry"])
        if "modes" in payload:
            try:
                modes = runner.mode_catalog_from_payload(
                    payload["modes"], context="daemon dispatch"
                )
            except runner.RunnerError as error:
                raise DaemonError(str(error)) from None
            resolved_call = replace(resolved_call, entry=replace(resolved_call.entry, modes=modes))
        resolution = replace(
            resolved_call,
            model=payload.get("model"),
            effort=payload.get("effort"),
            mode=payload.get("mode"),
            permissions=payload.get("permissions"),
            home=payload.get("home"),
            mode_spec=mode_spec,
        )
        # Backstop, not the gate: the CLI selected this mode already. If its
        # stored facts exceed the policy, fail rather than let the daemon
        # disagree with the caller's resolution.
        if mode_spec is not None and policy_level is not None:
            ceiling = PermissionLevel.READ if policy_level is PermissionLevel.ASK else policy_level
            if PermissionLevel(mode_spec.grants).rank > ceiling.rank:
                required = mode_spec.grants
                policy = policy_level.value
                raise DaemonError(
                    f"mode {resolution.mode} grants {required}, exceeding policy {policy}; "
                    f"the lowest policy that admits it is {required}"
                )
        return runner.TurnRequest(
            resolution=resolution,
            prompt=payload.get("prompt", ""),
            cwd=payload.get("cwd"),
            cancel_after=payload.get("cancel_after"),
            resume_adapter_session=payload.get("resume_adapter_session"),
            defer_rotation=bool(payload.get("defer_rotation", False)),
            rotation_resolution=payload.get("rotation_resolution"),
            resume_prepared=bool(payload.get("resume_prepared", False)),
            turn_token=payload.get("turn_token"),
        )

    async def _prepare_turn(
        self, session_id: str, request: runner.TurnRequest, turn: _Turn | None = None
    ) -> runner.TurnRequest:
        """Claim, verify and prepare a continuation before its prompt."""
        if turn is None:
            turn = self.turns[session_id]
        events = transcript.Transcript(sessions.transcript_path(session_id))
        adapter_session_id = request.resume_adapter_session
        if adapter_session_id is None:
            raise runner.ResumePreparationError("continued turn has no adapter session id")
        await self.host.wait_for_restore(adapter_session_id)
        conn = await self.host.ensure(request.resolution)
        warm = self.host.adapter_sessions.get(session_id)
        if warm is not None:
            return request
        client = AcpcClient(
            events,
            PermissionLevel(request.resolution.permissions or "read"),
            modes=request.resolution.entry.modes,
        )
        self.host.mux.bind(adapter_session_id, client)
        abandon_event = asyncio.Event()
        restore_task: asyncio.Task[Any] | None = None
        try:
            try:
                restore_task = self.host.start_restore(
                    adapter_session_id,
                    runner.verify_adapter_resume(
                        conn,
                        client,
                        self.host.agent_capabilities,
                        adapter_session_id,
                        request.cwd or os.getcwd(),
                        session_id,
                        abandon_event=abandon_event,
                    ),
                )
                resume_status = await asyncio.shield(restore_task)
            except runner.ResumePreparationError:
                raise
            except Exception as error:  # noqa: BLE001
                raise runner.ResumePreparationError(str(error)) from None
            meta = sessions.read_meta(session_id)
            extra = dict(meta.extra)
            extra["resume"] = resume_status
            with sessions.session_lock(session_id):
                current = sessions.read_meta(session_id)
                current.extra = extra
                sessions.write_meta(current)
            return request
        except asyncio.CancelledError:
            # ACP has no cancellation for session/load or session/resume. The
            # local turn is cancelled now, while the tracked verification task
            # closes its replay scope and waits for the remote restore response.
            abandon_event.set()
            raise
        finally:
            self.host.mux.release(adapter_session_id)

    @staticmethod
    def _snapshot_session(session_id: str) -> _PreparationBackup:
        """Keep a failed verification able to restore the prior finished turn."""
        files: dict[Path, str] = {}
        directory = sessions.session_dir(session_id)
        for path in directory.iterdir():
            if path.name == sessions.LOCK_NAME or not path.is_file():
                continue
            files[path] = path.read_text(encoding="utf-8")
        return _PreparationBackup(files)

    @staticmethod
    def _rollback_preparation(session_id: str, turn: _Turn) -> None:
        """Undo a failed preparation claim while its daemon is still alive."""
        backup = turn.backup
        if backup is None:
            return
        directory = sessions.session_dir(session_id)
        with sessions.session_lock(session_id):
            for path in directory.iterdir():
                if path.name == sessions.LOCK_NAME or not path.is_file():
                    continue
                if path not in backup.files:
                    path.unlink()
            for path, content in backup.files.items():
                from acpc import paths

                paths.atomic_write(path, content)
        turn.backup = None

    async def _run_turn(
        self,
        session_id: str,
        request: runner.TurnRequest,
        turn: _Turn,
    ) -> runner.TurnOutcome:
        """Run one turn on the warm adapter and finalize it on disk."""
        cancel = turn.cancel
        async with self._slots:
            events = transcript.Transcript(sessions.transcript_path(session_id))
            outcome = runner.TurnOutcome(state="failed", stop_reason="error", answer="")
            error: BaseException | None = None
            # Where this turn's own output starts in the shared per-target log:
            # the file is append-only across every session the target ever ran,
            # so without this mark a failure would quote a stranger's stderr.
            log_from = runner.adapter_log_offset(self.target)
            try:
                if not turn.claim_established:
                    sessions.mark_running(session_id, pid=os.getpid())
                    events.append("state", **{"from": "starting", "to": "running"})
                outcome = await self._drive(session_id, request, events, turn)
            except runner.ResumePreparationError as caught:
                # A deferred continuation has not rotated the session yet, so
                # verification failure must leave its finished state untouched.
                error = caught
                outcome = runner.TurnOutcome(state="failed", stop_reason="error", answer="")
                if not request.defer_rotation:
                    runner._finalize(session_id, outcome, error=error, adapter_log_from=log_from)
            except BaseException as caught:
                # Same reasoning as the direct path: a crash must still leave a
                # finished session, never a meta.json stuck on `running`.
                ended_by_acpc = isinstance(caught, asyncio.CancelledError) and (
                    cancel.stop_reason is not None
                )
                error = (
                    runner.TurnEndedByAcpc(cancel.stop_reason or "the daemon ended the turn")
                    if ended_by_acpc
                    else caught
                )
                preparing_cancel = (
                    isinstance(caught, asyncio.CancelledError)
                    and turn.cancel.state == "canceled"
                    and turn.phase == "preparing"
                    and turn.preparation_cancelable
                )
                if preparing_cancel:
                    error = None
                outcome = runner.TurnOutcome(
                    state=("canceled" if preparing_cancel else turn.cancel.state or "failed"),
                    stop_reason=(
                        runner.PREPARATION_CANCELLED_REASON
                        if preparing_cancel
                        else turn.cancel.stop_reason or "error"
                    ),
                    answer=(
                        runner.preparation_cancelled_answer(session_id) if preparing_cancel else ""
                    ),
                    turn_token=request.turn_token,
                )
                runner._finalize(
                    session_id,
                    outcome,
                    error=None if preparing_cancel else error,
                    adapter_log_from=log_from,
                    expected_turn=outcome.turn_token,
                )
                # If the crash was the adapter dying, heal the target now
                # rather than on the next turn's ensure().
                with contextlib.suppress(Exception):
                    await self.host.reset_if_dead()
                if isinstance(caught, asyncio.CancelledError) and not preparing_cancel:
                    raise
            else:
                runner._finalize(
                    session_id,
                    outcome,
                    error=outcome.error,
                    adapter_log_from=log_from,
                    expected_turn=outcome.turn_token,
                )
            finally:
                # Only a turn that could not be *started* travels back as an
                # error: the client raises on it. A turn that ran and failed is
                # already finalized on disk, and the client mirrors that result
                # so the caller still gets the answer, the summary and --json.
                self._finish(session_id, outcome, error=error)
            return outcome

    async def _drive(
        self,
        session_id: str,
        request: runner.TurnRequest,
        events: transcript.Transcript,
        turn: _Turn,
    ) -> runner.TurnOutcome:
        """Prompt the adapter, reusing this session's warm ACP session if it has one."""
        cancel = turn.cancel
        conn = await self.host.ensure(request.resolution)
        level = PermissionLevel(request.resolution.permissions or "read")
        stored = sessions.read_meta(session_id)
        client = AcpcClient(
            events,
            level,
            modes=request.resolution.entry.modes,
            end_turn=cancel.end_turn,
            cancellation_dispatched=cancel.cancellation_dispatched,
            previous_tokens=stored.tokens,
            previous_cost=stored.cost,
        )

        turn_error: BaseException | None = None
        prompt_task: asyncio.Task[Any] | None = None
        warm = self.host.adapter_sessions.get(session_id)
        if warm is not None:
            # The adapter still holds this session, so its own history is
            # intact: prompt it directly. Re-loading here would make the
            # adapter treat the turn as a resume and lose that continuity.
            adapter_session_id = warm
        elif request.resume_prepared and request.resume_adapter_session is not None:
            adapter_session_id = request.resume_adapter_session
        elif request.resume_adapter_session is not None:
            adapter_session_id = request.resume_adapter_session
            self.host.mux.bind(adapter_session_id, client)
            try:
                try:
                    await runner.verify_adapter_resume(
                        conn,
                        client,
                        self.host.agent_capabilities,
                        adapter_session_id,
                        request.cwd or os.getcwd(),
                        session_id,
                    )
                except runner.ResumePreparationError:
                    raise
                except Exception as error:
                    if request.defer_rotation:
                        raise runner.ResumePreparationError(str(error)) from None
                    raise
                request = runner._prepare_resumed_turn(session_id, request, events, pid=os.getpid())
            finally:
                self.host.mux.release(adapter_session_id)
        else:
            session = await conn.new_session(cwd=request.cwd or os.getcwd(), mcp_servers=[])
            adapter_session_id = session.session_id
            client.capture_advertised(session)

        self.host.mux.bind(adapter_session_id, client)
        try:
            try:
                await runner.apply_call_options(conn, adapter_session_id, request)
                delivery = runner.register_prompt_delivery(
                    conn,
                    session_id,
                    adapter_session_id,
                    request.prompt,
                    on_delivered=lambda: self._prompt_delivered(
                        session_id, adapter_session_id, turn
                    ),
                )
                if cancel.requested.is_set() and request.resume_prepared:
                    return runner.TurnOutcome(
                        state="canceled",
                        stop_reason=runner.PREPARATION_CANCELLED_REASON,
                        answer=runner.preparation_cancelled_answer(session_id),
                        adapter_session_id=adapter_session_id,
                        turn_token=request.turn_token,
                    )
                prompt_task = asyncio.create_task(
                    conn.prompt(session_id=adapter_session_id, prompt=[text_block(request.prompt)])
                )
            except BaseException as error:
                if request.turn_token is None:
                    raise
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise runner.ResumeSetupError(
                    runner.describe_error(error), turn_token=request.turn_token
                ) from None
            try:
                stop_reason = await runner._await_prompt(
                    conn,
                    adapter_session_id,
                    prompt_task,
                    request,
                    cancel,
                    usage_client=client,
                )
            except Exception as caught:  # noqa: BLE001
                # Same bargain as the direct path: keep the streamed prose as the
                # failed session's answer and carry the cause on the outcome.
                turn_error = caught
                stop_reason = "error"
            try:
                await delivery.ensure_persisted(prompt_completed=turn_error is None)
            except Exception as caught:  # noqa: BLE001
                turn_error = caught if turn_error is None else turn_error
                stop_reason = "error"
        finally:
            if prompt_task is not None and not prompt_task.done():
                prompt_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await prompt_task
            self.host.mux.release(adapter_session_id)
            client.flush()

        state = (
            cancel.state if cancel.state is not None else runner._state_for_stop_reason(stop_reason)
        )
        if cancel.stop_reason is not None:
            stop_reason = cancel.stop_reason
        return runner.TurnOutcome(
            state=state,
            stop_reason=stop_reason,
            answer=client.answer,
            tokens=client.tokens,
            cost=client.cost,
            denied=client.denied,
            denial_details=client.denial_details,
            adapter_session_id=adapter_session_id,
            advertised=client.advertised,
            error=turn_error,
            delivery_record_incomplete=delivery.delivery_record_incomplete,
            turn_token=request.turn_token,
        )

    def _prompt_delivered(self, session_id: str, adapter_session_id: str, turn: _Turn) -> None:
        """Enter the running phase only after the outgoing prompt was observed."""
        turn.phase = "running"
        self.host.adapter_sessions[session_id] = adapter_session_id
        # SPEC.md `steer`: support is read from `initialize` and recorded on the
        # session. The start receipt records it earlier; this callback keeps the
        # record correct for any path that reaches prompt delivery later.
        with contextlib.suppress(OSError, sessions.SessionError):
            self._record_steer_mode(session_id)

    def _record_steer_mode(self, session_id: str) -> None:
        mode = (
            vocab.STEER_IN_PLACE if self.host.steering_supported else vocab.STEER_CANCEL_THEN_START
        )
        sessions.update_meta(session_id, steer_mode=mode)

    def _finish(
        self, session_id: str, outcome: runner.TurnOutcome, *, error: BaseException | None = None
    ) -> None:
        turn = self.turns.pop(session_id, None)
        self._last_busy = time.monotonic()
        if turn is None:
            return
        state = outcome.state
        if state == "terminated":
            state = "canceled"
        payload = {
            "state": state,
            "exit_code": outcome.exit_code,
            "stop_reason": outcome.stop_reason,
        }
        if error is not None:
            payload["error"] = runner.describe_error(error)
        turn.result = payload
        self._resolve_waiters(turn, payload)

    @staticmethod
    def _resolve_waiters(turn: _Turn, payload: dict[str, Any]) -> None:
        for waiter in turn.waiters:
            if not waiter.done():
                waiter.set_result(payload)
        turn.waiters.clear()


async def _serve_target(target: str) -> None:
    daemon = Daemon(target)
    await daemon.serve()


def main(argv: list[str] | None = None) -> int:
    """Entry point for `python -m acpc.daemon <target>`."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m acpc.daemon <target>", file=sys.stderr)
        return vocab.EXIT_USAGE
    try:
        asyncio.run(_serve_target(args[0]))
    except KeyboardInterrupt:
        return vocab.EXIT_CANCELLED
    return vocab.EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
