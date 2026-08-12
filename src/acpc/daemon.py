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
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from acp import PROTOCOL_VERSION, text_block

from acpc import __version__, config, ipc, paths, runner, sessions, transcript, vocab
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

# Frames name their operation under this key.
OP = "op"


class DaemonError(Exception):
    """The daemon could not serve a request; the message is one line."""


def log_path_for_target(target: str) -> Path:
    """Where this target's daemon and adapter stderr go (SPEC.md `daemon`)."""
    return paths.daemon_dir() / f"{target}.log"


@dataclass(slots=True)
class _Turn:
    """One in-flight turn owned by this daemon."""

    session_id: str
    task: "asyncio.Task[runner.TurnOutcome]"
    cancel: runner._CancelSignal
    waiters: list["asyncio.Future[dict[str, Any]]"] = field(default_factory=list)
    result: dict[str, Any] | None = None


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
        self._starting = asyncio.Lock()
        # acpc session id -> the adapter session id it is bound to, for as long
        # as this adapter process lives. Presence here *is* "warm".
        self.adapter_sessions: dict[str, str] = {}

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
            return self._conn
        async with self._starting:
            if self._conn is not None:
                if not self._adapter_died():
                    return self._conn
                # The adapter process died under this daemon. Drop the dead
                # connection so the target heals with a fresh adapter instead
                # of failing every turn until someone runs `daemon stop`.
                # Warm sessions go with it: the memory they relied on lived in
                # the dead process, so their next turn honestly resumes cold.
                await self.close()
            return await self._start(resolution)

    async def reset_if_dead(self) -> None:
        """Drop the connection to an adapter whose process is gone."""
        async with self._starting:
            if self._conn is not None and self._adapter_died():
                await self.close()

    async def _start(self, resolution: Any) -> Any:
        command, args = runner.adapter_command(resolution)
        stack = contextlib.AsyncExitStack()
        conn, process = await stack.enter_async_context(
            spawn_adapter(
                self.mux,
                command,
                *args,
                env=resolution.adapter_environment,
                drain_stderr=True,
            )
        )
        initialize = await conn.initialize(protocol_version=PROTOCOL_VERSION)
        self._stack = stack
        self._conn = conn
        self._process = process
        self._command = (command, args)
        self.agent_capabilities = getattr(initialize, "agent_capabilities", None)
        return conn

    async def close(self) -> None:
        if self._stack is not None:
            with contextlib.suppress(Exception):
                await self._stack.aclose()
        self._stack = None
        self._conn = None
        self._process = None
        self.agent_capabilities = None
        self.adapter_sessions.clear()


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
        if operation == "cancel":
            return self._cancel(frame)
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
            "uptime": time.time() - self.started_at,
            "log": str(log_path_for_target(self.target)),
            "sessions": sorted(self.turns),
            "max_concurrent": self.max_concurrent,
        }

    def _cancel(self, frame: dict[str, Any]) -> dict[str, Any]:
        session_id = frame.get("session_id", "")
        turn = self.turns.get(session_id)
        if turn is None:
            return {"ok": False, "error": f"session {session_id} is not running here"}
        turn.cancel.request("cancelled")
        return {"ok": True}

    async def _start(self, frame: dict[str, Any]) -> dict[str, Any]:
        session_id = frame.get("session_id", "")
        if session_id in self.turns:
            return {"ok": False, "error": f"session {session_id} already has a turn in flight"}
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

        claimed_turn: int | None = None
        try:
            async with contextlib.AsyncExitStack() as stack:
                if request.defer_rotation:
                    await stack.enter_async_context(sessions.session_reservation(session_id))
                    request = await self._prepare_turn(session_id, request)
                claimed_turn = request.turn_token
                queued = self._slots.locked()
                cancel = runner._CancelSignal()
                turn_coro = self._run_turn(session_id, request, cancel)
                try:
                    task = asyncio.create_task(
                        turn_coro,
                        name=f"acpc.turn.{session_id}",
                    )
                except BaseException:
                    turn_coro.close()
                    raise
                turn = _Turn(session_id=session_id, task=task, cancel=cancel)
                self.turns[session_id] = turn
                self._last_busy = time.monotonic()
                return {"ok": True, "queued": queued, "max_concurrent": self.max_concurrent}
        except runner.ResumeRotationError as error:
            if error.turn_token is not None:
                runner._finalize_claimed_setup_failure(session_id, error.turn_token, error)
            return {
                "ok": False,
                "error": runner.describe_error(error),
                "preserve_session": True,
            }
        except asyncio.CancelledError as error:
            if claimed_turn is not None:
                runner._finalize_claimed_setup_failure(session_id, claimed_turn, error)
            raise
        except BaseException as error:  # noqa: BLE001
            if claimed_turn is not None:
                runner._finalize_claimed_setup_failure(session_id, claimed_turn, error)
            return {
                "ok": False,
                "error": runner.describe_error(error),
                "preserve_session": True,
            }

    async def _await(self, frame: dict[str, Any]) -> dict[str, Any]:
        session_id = frame.get("session_id", "")
        turn = self.turns.get(session_id)
        if turn is None:
            return {"ok": True, "outcome": self._outcome_from_disk(session_id)}
        if turn.result is not None:
            return {"ok": True, "outcome": turn.result}
        waiter: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        turn.waiters.append(waiter)
        return {"ok": True, "outcome": await waiter}

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
            timeout=payload.get("timeout"),
            resume_adapter_session=payload.get("resume_adapter_session"),
            defer_rotation=bool(payload.get("defer_rotation", False)),
            rotation_resolution=payload.get("rotation_resolution"),
            resume_prepared=bool(payload.get("resume_prepared", False)),
            turn_token=payload.get("turn_token"),
        )

    async def _prepare_turn(
        self, session_id: str, request: runner.TurnRequest
    ) -> runner.TurnRequest:
        """Verify and claim a continuation before it joins the prompt queue."""
        events = transcript.Transcript(sessions.transcript_path(session_id))
        conn = await self.host.ensure(request.resolution)
        warm = self.host.adapter_sessions.get(session_id)
        if warm is not None:
            return runner._prepare_resumed_turn(session_id, request, events, pid=os.getpid())

        adapter_session_id = request.resume_adapter_session
        if adapter_session_id is None:
            raise runner.ResumePreparationError("continued turn has no adapter session id")
        client = AcpcClient(
            events,
            PermissionLevel(request.resolution.permissions or "read"),
            modes=request.resolution.entry.modes,
        )
        self.host.mux.bind(adapter_session_id, client)
        try:
            try:
                resume_status = await runner.verify_adapter_resume(
                    conn,
                    client,
                    self.host.agent_capabilities,
                    adapter_session_id,
                    request.cwd or os.getcwd(),
                    session_id,
                )
            except runner.ResumePreparationError:
                raise
            except Exception as error:  # noqa: BLE001
                raise runner.ResumePreparationError(str(error)) from None
            return runner._prepare_resumed_turn(
                session_id,
                request,
                events,
                resume_status=resume_status,
                pid=os.getpid(),
            )
        finally:
            self.host.mux.release(adapter_session_id)

    async def _run_turn(
        self,
        session_id: str,
        request: runner.TurnRequest,
        cancel: runner._CancelSignal,
    ) -> runner.TurnOutcome:
        """Run one turn on the warm adapter and finalize it on disk."""
        async with self._slots:
            events = transcript.Transcript(sessions.transcript_path(session_id))
            outcome = runner.TurnOutcome(state="failed", stop_reason="error", answer="")
            error: BaseException | None = None
            # Where this turn's own output starts in the shared per-target log:
            # the file is append-only across every session the target ever ran,
            # so without this mark a failure would quote a stranger's stderr.
            log_from = runner.adapter_log_offset(self.target)
            try:
                if not request.defer_rotation and not request.resume_prepared:
                    sessions.mark_running(session_id, pid=os.getpid())
                    events.append("state", **{"from": "starting", "to": "running"})
                outcome = await self._drive(session_id, request, events, cancel)
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
                outcome = runner.TurnOutcome(
                    state=cancel.state or "failed",
                    stop_reason=cancel.stop_reason or "error",
                    answer="",
                    turn_token=request.turn_token,
                )
                runner._finalize(
                    session_id,
                    outcome,
                    error=error,
                    adapter_log_from=log_from,
                    expected_turn=outcome.turn_token,
                )
                # If the crash was the adapter dying, heal the target now
                # rather than on the next turn's ensure().
                with contextlib.suppress(Exception):
                    await self.host.reset_if_dead()
                if isinstance(caught, asyncio.CancelledError):
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
        cancel: runner._CancelSignal,
    ) -> runner.TurnOutcome:
        """Prompt the adapter, reusing this session's warm ACP session if it has one."""
        conn = await self.host.ensure(request.resolution)
        level = PermissionLevel(request.resolution.permissions or "read")
        client = AcpcClient(
            events,
            level,
            modes=request.resolution.entry.modes,
            end_turn=cancel.end_turn,
            cancellation_dispatched=cancel.cancellation_dispatched,
        )

        turn_error: BaseException | None = None
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

        self.host.adapter_sessions[session_id] = adapter_session_id
        self.host.mux.bind(adapter_session_id, client)
        try:
            try:
                await runner.apply_call_options(conn, adapter_session_id, request)
                delivery = runner.register_prompt_delivery(
                    conn, session_id, adapter_session_id, request.prompt
                )
                prompt_task = asyncio.create_task(
                    conn.prompt(session_id=adapter_session_id, prompt=[text_block(request.prompt)])
                )
            except BaseException as error:
                if request.turn_token is None:
                    raise
                raise runner.ResumeSetupError(
                    runner.describe_error(error), turn_token=request.turn_token
                ) from None
            try:
                stop_reason = await runner._await_prompt(
                    conn, adapter_session_id, prompt_task, request, cancel
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

    def _finish(
        self, session_id: str, outcome: runner.TurnOutcome, *, error: BaseException | None = None
    ) -> None:
        turn = self.turns.pop(session_id, None)
        self._last_busy = time.monotonic()
        if turn is None:
            return
        state = outcome.state
        if state == "terminated":
            state = "cancelled"
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
