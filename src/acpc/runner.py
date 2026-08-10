"""One turn, end to end: route, run, handle signals, finalize.

SPEC.md `run` and *Output contract*. The runner owns the lifecycle between a
resolved call and a finished session:

- **Routing.** Ask `daemon_client` for a warm adapter; if the daemon is not
  available, spawn the adapter as a direct child and say so — the fallback is
  visible, folded into the single `--` stderr summary line, never silent.
- **Signals.** SIGINT is a human's Ctrl-C and cancels the session (ACP
  `session/cancel`, bounded wait for the ack) → exit 130. SIGTERM is a harness
  killing the tool call; on the daemon path it detaches, but a direct child
  cannot outlive its parent, so there it cancels too → exit 143.
- **Timeout.** `--timeout` cancels the session the same way, but the state is
  `timeout` and the exit code 124.
- **Finalization.** Whatever the outcome, `answer.md` and `meta.json` are
  written before the process exits: a partial answer is still an answer.
"""

import asyncio
import contextlib
import os
import shutil
import signal
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from acp import PROTOCOL_VERSION, RequestError, text_block

from acpc import (
    cache,
    daemon_client,
    environment,
    paths,
    sessions,
    targets,
    transcript,
    vocab,
)
from acpc.client import AcpcClient
from acpc.permissions import PermissionLevel
from acpc.registry import CallResolution, ModeSpec, RegistryError, ResolvedEntry
from acpc.spawn import spawn_adapter

# SPEC.md `stop`: graceful cancel with a bounded wait for the ack (10s) — if
# the callee does not wind down in time, the connection is torn down anyway.
CANCEL_ACK_TIMEOUT = 10.0

# Failure events are part of the transcript and may be copied into a caller's
# context. Eight KiB is large enough for the useful tail of normal adapter
# diagnostics while keeping one unusually large stderr line bounded.
ADAPTER_LOG_TAIL_BYTES = 8 * 1024

# How much of that tail is spliced into the one-line failure message.
MESSAGE_TAIL_CHARS = 300

# ACP stop reasons that mean the turn failed rather than completed.
_FAILURE_STOP_REASONS = frozenset({"refusal", "max_tokens", "max_turn_requests"})

# ...of those, the ones where the adapter hit a ceiling rather than broke.
_ADAPTER_LIMIT_STOP_REASONS = frozenset({"max_tokens", "max_turn_requests"})


class RunnerError(Exception):
    """A turn could not be started; the message is one actionable line."""


class TurnEndedByAcpc(RunnerError):
    """acpc itself ended a running turn — a daemon stop, not an adapter fault."""


def describe_error(error: BaseException) -> str:
    """Render a failure with everything the adapter actually said.

    A JSON-RPC error's fixed message ("Internal error") often hides the
    vendor's real diagnosis, which rides in the optional `data` member.
    """
    if not isinstance(error, RequestError):
        return str(error)
    message = str(error) or "adapter error"
    data = error.data
    detail: str | None = None
    if isinstance(data, Mapping):
        raw = data.get("details") or data.get("message") or (data if data else None)
        detail = str(raw) if raw is not None else None
    elif data is not None:
        detail = str(data)
    suffix = f": {detail}" if detail else ""
    return f"{message}{suffix} (JSON-RPC {error.code})"


LOAD_SESSION_CAPABILITY_ERROR = (
    "continue requires an adapter that can restore a session (ACP session/resume or session/load)"
)


def require_load_session_capability(capabilities: Any) -> None:
    """Raise the user-facing error when an adapter cannot resume a session."""
    if not has_resume_session_capability(capabilities) and not getattr(
        capabilities, "load_session", False
    ):
        raise RunnerError(LOAD_SESSION_CAPABILITY_ERROR)


def has_resume_session_capability(capabilities: Any) -> bool:
    """Return whether initialize advertised ACP ``session/resume``."""
    session_capabilities = getattr(capabilities, "session_capabilities", None)
    return getattr(session_capabilities, "resume", None) is not None


async def restore_adapter_session(
    conn: Any, capabilities: Any, adapter_session_id: str, cwd: str
) -> None:
    """Restore an adapter session, preferring ``session/resume`` when offered."""
    require_load_session_capability(capabilities)
    request = {
        "session_id": adapter_session_id,
        "cwd": cwd,
        "mcp_servers": [],
    }
    if has_resume_session_capability(capabilities):
        await conn.resume_session(**request)
    else:
        await conn.load_session(**request)


@dataclass(frozen=True, slots=True)
class TurnRequest:
    """Everything one turn needs that is not already in `meta.json`."""

    resolution: CallResolution
    prompt: str
    cwd: str | None = None
    timeout: float | None = None
    permission_prompt: Callable[[str, str], bool] | None = None
    resume_adapter_session: str | None = None


@dataclass(slots=True)
class TurnOutcome:
    """The result of one turn, ready for the output layer."""

    state: str
    stop_reason: str | None
    answer: str
    tokens: int = 0
    cost: float | None = None
    denied: dict[str, int] = field(default_factory=dict)
    denial_details: dict[str, dict[str, Any]] = field(default_factory=dict)
    adapter_session_id: str | None = None
    advertised: dict[str, Any] = field(default_factory=dict)
    route_note: str | None = None
    # The adapter's own failure, when the turn died in `session/prompt`. Carried
    # on the outcome rather than raised so the prose streamed before it survives.
    error: BaseException | None = None
    # Set when the daemon owns the session and has already written it out;
    # finalizing again here would overwrite the daemon's own result.
    finalized_elsewhere: bool = False
    queued: bool = False

    @property
    def exit_code(self) -> int:
        return exit_code_for(self.state, self.stop_reason)


def exit_code_for(state: str, stop_reason: str | None = None) -> int:
    """Map a finished session's state to SPEC's fixed exit codes."""
    if state == "done":
        return vocab.EXIT_OK
    if state == "timeout":
        return vocab.EXIT_TIMEOUT
    if state == "cancelled":
        return vocab.EXIT_CANCELLED
    # Detached (daemon path) and terminated (direct path) are both "SIGTERM
    # ended this client"; they differ only in whether the session survives it.
    if state in {"detached", "terminated"}:
        return vocab.EXIT_SIGTERM
    if stop_reason == "permission_denied":
        return vocab.EXIT_USAGE
    # failed and orphaned both mean the agent did not deliver an answer.
    return vocab.EXIT_AGENT_ERROR


def _state_for_stop_reason(stop_reason: str | None) -> str:
    if stop_reason == "end_turn":
        return "done"
    if stop_reason == "cancelled":
        return "cancelled"
    if stop_reason in _FAILURE_STOP_REASONS:
        return "failed"
    return "failed"


def adapter_command(resolution: CallResolution) -> tuple[str, tuple[str, ...]]:
    """Split the entry's command, and refuse early when it is not installed."""
    entry = resolution.entry
    try:
        args = entry.command_args
    except RegistryError as error:
        raise RunnerError(str(error)) from None
    if shutil.which(args[0]) is None:
        raise RunnerError(
            f"{entry.entry}: '{args[0]}' is not installed — run 'acpc install {entry.base_adapter}'"
        )
    return args[0], args[1:]


def call_target(resolution: CallResolution) -> str:
    """Compute the daemon target key for this call (never stores secrets)."""
    if resolution.permissions is None:
        raise RunnerError(
            f"cannot compute daemon target for {resolution.entry.entry}: "
            "permission policy is unresolved"
        )
    declared = dict(resolution.declared_env)
    passthrough = {
        name: value
        for name, value in environment.environment_overrides({}, resolution.env_passthrough).items()
    }
    return targets.target_for_call(
        resolution.entry.entry,
        home=resolution.home,
        declared_env=declared,
        passthrough_values=passthrough,
        permissions=resolution.permissions,
    )


class _CancelSignal:
    """Records why a turn is being wound down, and what that means on exit."""

    def __init__(self) -> None:
        self.state: str | None = None
        self.stop_reason: str | None = None
        self.requested = asyncio.Event()
        self.cancellation_dispatched = asyncio.Event()

    def request(self, state: str, *, stop_reason: str | None = None) -> None:
        if self.state is None:
            self.state = state
            self.stop_reason = stop_reason
        self.requested.set()

    def end_turn(self) -> None:
        """End a turn through the same cancellation path as external stops."""
        self.request("failed", stop_reason="permission_denied")


async def _drive_turn(
    session_id: str,
    request: TurnRequest,
    events: transcript.Transcript,
    cancel: _CancelSignal,
) -> TurnOutcome:
    """Run one turn against a directly spawned adapter."""
    resolution = request.resolution
    command, args = adapter_command(resolution)
    env = resolution.adapter_environment
    level = PermissionLevel(resolution.permissions or "read")
    client = AcpcClient(
        events,
        level,
        modes=resolution.entry.modes,
        end_turn=cancel.end_turn,
        cancellation_dispatched=cancel.cancellation_dispatched,
        permission_prompt=request.permission_prompt,
    )
    turn_error: BaseException | None = None

    try:
        async with spawn_adapter(
            client,
            command,
            *args,
            env=env,
            cwd=request.cwd,
            drain_stderr=True,
        ) as (conn, _process):
            initialize = await conn.initialize(protocol_version=PROTOCOL_VERSION)

            if request.resume_adapter_session is not None:
                adapter_session_id = request.resume_adapter_session
                capabilities = getattr(initialize, "agent_capabilities", None)
                async with client.replaying():
                    await restore_adapter_session(
                        conn, capabilities, adapter_session_id, request.cwd or os.getcwd()
                    )
            else:
                session = await conn.new_session(cwd=request.cwd or os.getcwd(), mcp_servers=[])
                adapter_session_id = session.session_id
                client.capture_advertised(session)

            # After the restore, never before it: codex-acp#343 resets model and
            # effort during session/load, so applying them first would be lost.
            await apply_call_options(conn, adapter_session_id, request)

            prompt_task = asyncio.create_task(
                conn.prompt(
                    session_id=adapter_session_id,
                    prompt=[text_block(request.prompt)],
                )
            )
            try:
                stop_reason = await _await_prompt(
                    conn, adapter_session_id, prompt_task, request, cancel
                )
            except Exception as caught:  # noqa: BLE001
                # The adapter failed the turn itself. Whatever prose it streamed
                # first is still the answer SPEC promises for a failed session,
                # so the cause travels on the outcome instead of unwinding here.
                turn_error = caught
                stop_reason = "error"
    finally:
        client.flush()

    if cancel.stop_reason is not None:
        stop_reason = cancel.stop_reason
    state = cancel.state if cancel.state is not None else _state_for_stop_reason(stop_reason)
    return TurnOutcome(
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
    )


# What most adapters call their effort session config option; entries override
# it via `effort_config_id` (claude speaks `effort`).
_DEFAULT_EFFORT_CONFIG_ID = "reasoning_effort"


async def apply_call_options(conn: Any, adapter_session_id: str, request: TurnRequest) -> None:
    """Apply resolved mode/model/effort to the adapter session before prompting."""
    resolution = request.resolution
    await _configure(
        conn.set_session_mode(session_id=adapter_session_id, mode_id=resolution.mode),
        f"mode {resolution.mode!r}",
    )
    if resolution.model is not None:
        await _configure(
            conn.set_config_option(
                config_id="model",
                session_id=adapter_session_id,
                value=resolution.model,
            ),
            f"model {resolution.model!r}",
        )
    if resolution.effort is not None:
        effort_config_id = resolution.entry.effort_config_id or _DEFAULT_EFFORT_CONFIG_ID
        await _configure(
            conn.set_config_option(
                config_id=effort_config_id,
                session_id=adapter_session_id,
                value=resolution.effort,
            ),
            f"effort {resolution.effort!r} (config option {effort_config_id!r})",
            unknown_option_hint=(
                f"model {resolution.model!r} may not take an effort setting — "
                "drop --effort, or drop effort from the preset in the entry TOML"
            ),
        )


class AdapterRejection(RuntimeError):
    """An adapter refused a session option; the message carries the detail.

    Deliberately not a RunnerError: it fails the turn like any other adapter
    error, so the message lands in answer.md and the transcript."""


async def _configure(call: Any, what: str, *, unknown_option_hint: str | None = None) -> None:
    """Name the rejected option: the bare JSON-RPC reply doesn't say which.

    An adapter that does not offer an option at all answers with a generic
    internal error, so the vendor's own wording is the only signal that the
    option is missing rather than the value wrong. When it says so, the hint
    names what the caller can actually change.
    """
    try:
        await call
    except RequestError as error:
        detail = describe_error(error)
        message = f"the adapter rejected {what}: {detail}"
        if unknown_option_hint is not None and "unknown config option" in detail.lower():
            message += f" — {unknown_option_hint}"
        raise AdapterRejection(message) from None


async def _await_prompt(
    conn: Any,
    adapter_session_id: str,
    prompt_task: "asyncio.Task[Any]",
    request: TurnRequest,
    cancel: _CancelSignal,
) -> str | None:
    """Wait for the turn, honoring `--timeout` and any signal-driven cancel.

    Cancellation always goes through ACP `session/cancel` with a bounded wait
    for the adapter to wind down, so the transcript and the partial answer are
    on disk before the connection is torn down.
    """
    waiter = asyncio.ensure_future(cancel.requested.wait())
    try:
        done, _pending = await asyncio.wait(
            {prompt_task, waiter},
            timeout=request.timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if prompt_task in done:
            return _stop_reason_of(prompt_task)

        # Either the timeout expired or a signal asked us to wind down.
        if not cancel.requested.is_set():
            cancel.request("timeout")
        cancel.cancellation_dispatched.set()
        with contextlib.suppress(Exception):
            await conn.cancel(session_id=adapter_session_id)
        with contextlib.suppress(TimeoutError, asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(prompt_task), timeout=CANCEL_ACK_TIMEOUT)
        if prompt_task.done():
            return _stop_reason_of(prompt_task)
        prompt_task.cancel()
        return "cancelled"
    finally:
        waiter.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await waiter


def _stop_reason_of(prompt_task: "asyncio.Task[Any]") -> str | None:
    """Read a finished prompt task's stop reason, preserving adapter errors."""
    error = prompt_task.exception() if not prompt_task.cancelled() else None
    if prompt_task.cancelled():
        return "cancelled"
    if error is not None:
        raise error
    return getattr(prompt_task.result(), "stop_reason", None)


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    cancel: _CancelSignal,
    *,
    daemon_routed: bool,
) -> None:
    """Route SIGINT/SIGTERM into the turn's cancel path.

    SIGTERM detaches only when the daemon owns the session; a direct child
    dies with its parent, so there it cancels (SPEC.md *Output contract*).
    """
    if sys.platform == "win32":
        return
    with contextlib.suppress(NotImplementedError, RuntimeError):
        loop.add_signal_handler(signal.SIGINT, lambda: cancel.request("cancelled"))
    with contextlib.suppress(NotImplementedError, RuntimeError):
        loop.add_signal_handler(
            signal.SIGTERM,
            lambda: cancel.request("detached" if daemon_routed else "terminated"),
        )


def daemon_payload(request: TurnRequest) -> dict[str, Any]:
    """The serializable shape of a turn, for the daemon to rebuild.

    Only the call's own inputs travel: the daemon reads the same `ACPC_HOME`,
    so it re-resolves the entry itself rather than trusting a resolution that
    crossed a socket.
    """
    resolution = request.resolution
    return {
        "entry": resolution.entry.entry,
        "model": resolution.model,
        "effort": resolution.effort,
        "mode": resolution.mode,
        "grants": resolution.mode_spec.grants if resolution.mode_spec else None,
        "delegates": resolution.mode_spec.delegates if resolution.mode_spec else None,
        "modes": mode_catalog_payload(resolution.entry.modes),
        "permissions": resolution.permissions,
        "home": resolution.home,
        "cwd": request.cwd,
        "timeout": request.timeout,
        "prompt": request.prompt,
        "resume_adapter_session": request.resume_adapter_session,
    }


def routes_direct(request: TurnRequest) -> str | None:
    """Why this call cannot use the daemon, or None when it can.

    An `ask` policy needs a terminal to ask on and the daemon has none, so
    such a call stays a direct child even when a daemon is available.
    """
    if request.permission_prompt is not None or request.resolution.permissions == "ask":
        return "--permissions ask needs this terminal"
    return None


async def _route(request: TurnRequest) -> tuple[Any | None, str | None]:
    """Pick the daemon or the direct path, and say so when it is the latter."""
    forced = routes_direct(request)
    if forced is not None:
        return None, f"direct child ({forced})"
    routed = await daemon_client.ensure_daemon(call_target(request.resolution))
    if isinstance(routed, daemon_client.DaemonUnavailable):
        return None, f"direct child ({routed.reason})"
    return routed, None


async def _execute(session_id: str, request: TurnRequest) -> TurnOutcome:
    cancel = _CancelSignal()
    daemon, route_note = await _route(request)
    _install_signal_handlers(asyncio.get_running_loop(), cancel, daemon_routed=daemon is not None)

    if daemon is not None:
        target = call_target(request.resolution)
        return await _execute_via_daemon(session_id, request, daemon, cancel, target)

    events = transcript.Transcript(sessions.transcript_path(session_id))
    sessions.mark_running(session_id, pid=_host_pid())
    events.append("state", **{"from": "starting", "to": "running"})
    outcome = await _drive_turn(session_id, request, events, cancel)
    outcome.route_note = route_note
    return outcome


async def _execute_via_daemon(
    session_id: str,
    request: TurnRequest,
    daemon: Any,
    cancel: _CancelSignal,
    target: str,
) -> TurnOutcome:
    """Hand the turn to the daemon and mirror its result.

    The daemon owns the session from here: it writes the transcript and
    finalizes `meta.json`, so this side must not finalize again. A SIGTERM
    detaches — the client stops watching and the turn carries on.
    """
    try:
        started = await daemon.start_turn(session_id, daemon_payload(request))
        if not started.get("ok"):
            raise RunnerError(str(started.get("error", "the daemon refused the turn")))
        queued = bool(started.get("queued"))

        waiting = asyncio.ensure_future(daemon.await_turn(session_id))
        signalled = asyncio.ensure_future(cancel.requested.wait())
        done, _pending = await asyncio.wait(
            {waiting, signalled}, return_when=asyncio.FIRST_COMPLETED
        )

        if waiting not in done:
            if cancel.state == "detached":
                waiting.cancel()
                signalled.cancel()
                return TurnOutcome(
                    state="detached",
                    stop_reason=None,
                    answer="",
                    finalized_elsewhere=True,
                    queued=queued,
                )
            await daemon_client.cancel_turn(target, session_id)
        signalled.cancel()
        reply = await waiting
        outcome = reply.get("outcome") or {}
        if error := outcome.get("error"):
            raise RunnerError(f"{error} [adapter log: {_daemon_log_note(target)}]")
        return TurnOutcome(
            state=str(outcome.get("state", "failed")),
            stop_reason=outcome.get("stop_reason"),
            answer=_answer_on_disk(session_id),
            finalized_elsewhere=True,
            queued=queued,
        )
    finally:
        with contextlib.suppress(Exception):
            await daemon.close()


def _daemon_log_note(target: str) -> str:
    """Point a failed turn at the adapter's stderr, saying so when it's empty.

    An empty log at failure time is itself a finding: the error came over the
    protocol and there is nothing more to read there.
    """
    from acpc import daemon as daemon_module

    log_file = daemon_module.log_path_for_target(target)
    try:
        empty = log_file.stat().st_size == 0
    except OSError:
        empty = True
    return f"{log_file} (empty)" if empty else str(log_file)


def _answer_on_disk(session_id: str) -> str:
    """Read back what the daemon wrote; the client never saw the stream."""
    try:
        return sessions.answer_path(session_id).read_text(encoding="utf-8")
    except OSError:
        return ""


async def dispatch_background(session_id: str, request: TurnRequest) -> str | None:
    """Start a turn the caller will not wait for; None on success.

    `--bg` needs an owner that outlives this process, which is exactly what the
    daemon is. Without one there is nobody to hand the session to, so this
    reports why instead of silently running a child that dies on exit.
    """
    forced = routes_direct(request)
    if forced is not None:
        return f"--bg needs the daemon, and {forced}"
    routed = await daemon_client.ensure_daemon(call_target(request.resolution))
    if isinstance(routed, daemon_client.DaemonUnavailable):
        return f"--bg needs the daemon: {routed.reason}"
    try:
        started = await routed.start_turn(session_id, daemon_payload(request))
        if not started.get("ok"):
            return str(started.get("error", "the daemon refused the turn"))
    finally:
        with contextlib.suppress(Exception):
            await routed.close()
    return None


def _host_pid() -> int:
    import os

    return os.getpid()


def execute_turn(session_id: str, request: TurnRequest) -> TurnOutcome:
    """Run one turn synchronously and leave the session finalized on disk."""
    try:
        outcome = asyncio.run(_execute(session_id, request))
    except RunnerError as error:
        _finalize(
            session_id,
            TurnOutcome(state="failed", stop_reason="error", answer=""),
            error=error,
        )
        raise
    except Exception as error:  # noqa: BLE001
        # Deliberately broad: an adapter crash, a protocol error and a spawn
        # failure all have to leave a finished session on disk. Anything that
        # escaped here would leave `meta.json` saying `running` forever, which
        # is the one state SPEC says no reader may ever be shown.
        outcome = TurnOutcome(state="failed", stop_reason="error", answer="")
        _finalize(session_id, outcome, error=error)
        return outcome
    if not outcome.finalized_elsewhere:
        _finalize(session_id, outcome, error=outcome.error)
    return outcome


def _finalize(
    session_id: str,
    outcome: TurnOutcome,
    *,
    error: BaseException | None = None,
    adapter_log_from: int | None = None,
) -> None:
    """Write the answer and close out `meta.json`, whatever happened.

    SPEC.md *State on disk*: `answer.md` is written whatever the final state —
    for a failed or cancelled turn it holds the partial answer.
    """
    try:
        current = sessions.read_meta(session_id)
        # A daemon has already finalized a failed turn when its client receives
        # the failure reply. Do not overwrite its answer or append a duplicate
        # event from the client-side error path.
        if current.is_finished and outcome.state == "failed":
            return
    except sessions.SessionError:
        current = None

    # A policy denial is not an adapter failure: acpc refused it, the summary
    # already names the policy that would admit it, and a second "inspect the
    # daemon log" would contradict that remedy.
    diagnosable = outcome.state == "failed" and outcome.stop_reason != "permission_denied"
    failure = (
        _failure_details(session_id, outcome, error=error, adapter_log_from=adapter_log_from)
        if diagnosable
        else None
    )
    answer = outcome.answer
    if failure is not None and not answer:
        answer = f"{failure.message}\n"
        outcome.answer = answer
    sessions.write_answer(session_id, answer)

    events_path = sessions.transcript_path(session_id)
    with contextlib.suppress(Exception):
        events = transcript.Transcript(events_path)
        if failure is not None:
            fields: dict[str, Any] = {
                "message": failure.message,
                "observation": failure.observation,
                "next_step": failure.next_step,
            }
            if failure.adapter_log is not None:
                fields["adapter_log"] = failure.adapter_log
            if failure.adapter_log_tail is not None:
                fields["adapter_log_tail"] = failure.adapter_log_tail
            events.append("error", **fields)
        from_state = current.state if current is not None else "running"
        events.append("state", **{"from": from_state, "to": outcome.state})

    state = outcome.state
    if state == "terminated":
        state = "cancelled"
    if state == "detached":
        state = "running"

    with contextlib.suppress(sessions.SessionError):
        sessions.transition(
            session_id,
            state,
            exit_code=outcome.exit_code,
            stop_reason=outcome.stop_reason,
            tokens=outcome.tokens,
            cost=outcome.cost,
            denied=outcome.denied,
            denial_details=outcome.denial_details,
            adapter_session_id=outcome.adapter_session_id,
        )

    if outcome.advertised:
        with contextlib.suppress(Exception):
            cache.refresh_advertised(_agent_of(session_id), outcome.advertised)


@dataclass(frozen=True, slots=True)
class _FailureDetails:
    """The observable diagnosis recorded for a failed turn."""

    message: str
    observation: str
    next_step: str
    adapter_log: str | None = None
    adapter_log_tail: str | None = None


def _failure_details(
    session_id: str,
    outcome: TurnOutcome,
    *,
    error: BaseException | None,
    adapter_log_from: int | None,
) -> _FailureDetails:
    """Build one bounded, actionable explanation for a failed session.

    `adapter_log_from` is the offset this turn's output starts at in the
    per-target daemon log, and is None when the turn did not run under a
    daemon — a directly spawned adapter forwards its stderr to acpc's own
    stderr and writes to no log at all, so there is nothing to point at.
    """
    try:
        meta = sessions.read_meta(session_id)
    except sessions.SessionError:
        meta = None

    error_text = describe_error(error).strip() if error is not None else ""
    authentication_refused = _is_authentication_failure(error)
    observation = _failure_observation(
        error_text,
        outcome.stop_reason,
        authentication_refused=authentication_refused,
        ended_by_acpc=isinstance(error, TurnEndedByAcpc),
    )
    adapter_log = _adapter_log_path(meta) if adapter_log_from is not None else None
    adapter_log_tail = _read_adapter_log_tail(adapter_log, adapter_log_from or 0)
    if authentication_refused and meta is not None:
        next_step = f"run '{meta.base_adapter} login'"
    elif outcome.stop_reason in _ADAPTER_LIMIT_STOP_REASONS:
        next_step = "split the task into smaller turns, or raise the adapter's own limit"
    elif adapter_log is not None:
        next_step = f"inspect the daemon log at {adapter_log}"
    else:
        next_step = "re-run the turn: a directly spawned adapter's stderr comes back on stderr"

    parts = [observation]
    if adapter_log_tail:
        # Bounded far tighter than the stored field: this string is spliced into
        # answer.md and into `wait`'s single-line summary, where 8 KiB on one
        # line would bury the summary. `log` renders the full field.
        parts.append(f"adapter log tail: {_single_line(adapter_log_tail)[:MESSAGE_TAIL_CHARS]}")
    parts.append(f"next step: {next_step}")
    return _FailureDetails(
        message="; ".join(parts),
        observation=observation,
        next_step=next_step,
        adapter_log=str(adapter_log) if adapter_log is not None else None,
        adapter_log_tail=adapter_log_tail,
    )


def _failure_observation(
    error_text: str,
    stop_reason: str | None,
    *,
    authentication_refused: bool,
    ended_by_acpc: bool = False,
) -> str:
    if ended_by_acpc:
        # Never "the adapter failed": acpc ended this turn, and saying otherwise
        # sends the reader hunting for a vendor problem that does not exist.
        return f"acpc ended the turn: {error_text}"
    if authentication_refused:
        return f"authentication was refused by the adapter: {error_text}"
    if error_text:
        lowered = error_text.lower()
        if any(marker in lowered for marker in ("connection closed", "broken pipe", "eof")):
            return f"the adapter connection was torn down: {error_text}"
        return f"acpc observed an adapter failure: {error_text}"
    if stop_reason in _ADAPTER_LIMIT_STOP_REASONS:
        return f"the adapter stopped at its own limit (stop reason: {stop_reason})"
    if stop_reason:
        return f"acpc observed a failed adapter turn (stop reason: {stop_reason})"
    return "acpc observed the adapter exit without a result"


def _is_authentication_failure(error: BaseException | None) -> bool:
    """Recognize the narrow ACP auth refusal shape used by vendor adapters."""
    if not isinstance(error, RequestError) or error.code != -32000:
        return False
    data = error.data
    if isinstance(data, Mapping):
        for key in ("code", "type", "kind", "reason"):
            marker = data.get(key)
            if isinstance(marker, str) and marker.lower() in {
                "auth_required",
                "authentication_required",
                "not_authenticated",
            }:
                return True
    text = describe_error(error).lower()
    return "authentication required" in text or "not authenticated" in text


def adapter_log_offset(target: str) -> int:
    """Current size of a target's log, to mark where a turn's output begins."""
    from acpc import daemon as daemon_module

    try:
        return daemon_module.log_path_for_target(target).stat().st_size
    except OSError:
        return 0


def _adapter_log_path(meta: sessions.SessionMeta | None) -> Path | None:
    if meta is None or meta.target is None:
        return None
    from acpc import daemon as daemon_module

    return daemon_module.log_path_for_target(meta.target)


def _read_adapter_log_tail(path: Path | None, start: int) -> str | None:
    """Read this turn's own bytes from ``start``, bounded; never fail finalization.

    Everything before ``start`` belongs to earlier turns on the same target —
    quoting it would attribute a stranger's stderr to this failure.
    """
    if path is None:
        return None
    try:
        with path.open("rb") as log_file:
            log_file.seek(0, os.SEEK_END)
            size = log_file.tell()
            if size <= start:
                return None
            log_file.seek(max(start, size - ADAPTER_LOG_TAIL_BYTES))
            raw = log_file.read(ADAPTER_LOG_TAIL_BYTES)
    except (OSError, ValueError):
        return None
    if not raw:
        return None
    decoded = raw.decode("utf-8", errors="ignore").strip()
    return decoded or None


def _single_line(text: str) -> str:
    return " ".join(text.split())


def _agent_of(session_id: str) -> str:
    with contextlib.suppress(sessions.SessionError):
        return sessions.read_meta(session_id).base_adapter
    return ""


def auto_prune(retention_seconds: float) -> None:
    """Opportunistic retention sweep; never fails a run (SPEC.md `prune`)."""
    if retention_seconds <= 0:
        # A zero retention would silently delete every finished session on
        # the next run; deleting everything takes an explicit `--older-than`.
        return
    with contextlib.suppress(Exception):
        sessions.prune_sessions(older_than=retention_seconds)


def resolution_payload(resolution: CallResolution, *, cwd: str | None) -> dict[str, Any]:
    """The `--dry-run` view: every resolved value and where it came from."""
    entry = resolution.entry
    provenance: Mapping[str, Any] = resolution.provenance
    fields: dict[str, Any] = {
        "model": resolution.model,
        "effort": resolution.effort,
        "mode": resolution.mode,
        "permissions": resolution.permissions,
        "home": resolution.home,
    }
    resolved: dict[str, dict[str, Any]] = {
        name: {
            "value": value,
            "source": _source_label(provenance.get(name)),
        }
        for name, value in fields.items()
    }
    if resolution.permissions_clamp is not None:
        requested, ceiling = resolution.permissions_clamp
        permissions = resolved["permissions"]
        permissions["source"] = (
            f"{permissions['source']} (clamped from {requested} by inherited ceiling {ceiling})"
        )
        permissions["clamp"] = {
            "requested": requested,
            "ceiling": ceiling,
            "effective": resolution.permissions,
        }
    if resolution.mode_spec is not None:
        resolved["mode"]["grants"] = resolution.mode_spec.grants
        resolved["mode"]["delegates"] = resolution.mode_spec.delegates
        resolved["mode"]["escalates"] = resolution.mode_spec.escalates
    return {
        "entry": entry.entry,
        "base_adapter": entry.base_adapter,
        "command": entry.command,
        "cwd": cwd,
        "env": dict(resolution.declared_env),
        "env_passthrough": list(resolution.env_passthrough),
        "resolved": resolved,
    }


def mode_catalog_payload(modes: Mapping[str, ModeSpec]) -> dict[str, dict[str, Any]]:
    """Serialize the measured mode catalog for a session or daemon request."""
    return {
        name: {"grants": spec.grants, "delegates": spec.delegates} for name, spec in modes.items()
    }


def mode_catalog_from_payload(raw: object, *, context: str) -> dict[str, ModeSpec]:
    """Validate and rebuild a serialized measured mode catalog."""
    if not isinstance(raw, Mapping):
        raise RunnerError(f"{context} has invalid stored mode catalog")
    modes: dict[str, ModeSpec] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not name or not isinstance(value, Mapping):
            raise RunnerError(f"{context} has invalid stored mode catalog")
        grants = value.get("grants")
        delegates = value.get("delegates")
        if grants not in vocab.PERMISSION_VALUES[:-1] or not isinstance(delegates, bool):
            raise RunnerError(f"{context} has invalid stored mode catalog")
        modes[name] = ModeSpec(grants=grants, delegates=delegates)
    return modes


def session_resolution(
    resolution: CallResolution,
    *,
    cwd: str | None,
    permissions_source: str | None = None,
) -> dict[str, Any]:
    """The persisted session shape, distinct from the printed dry-run view."""
    payload = resolution_payload(resolution, cwd=cwd)
    if permissions_source is not None:
        payload["permissions_source"] = permissions_source
    adapter: dict[str, Any] = {
        "home_env": resolution.entry.home_env,
        "effort_config_id": resolution.entry.effort_config_id,
        "modes": mode_catalog_payload(resolution.entry.modes),
    }
    if resolution.mode is not None and resolution.mode_spec is not None:
        adapter.update(
            mode=resolution.mode,
            grants=resolution.mode_spec.grants,
            delegates=resolution.mode_spec.delegates,
        )
    payload["adapter"] = adapter
    return payload


def _stored_value(payload: Mapping[str, Any], field: str) -> Any:
    resolved = payload.get("resolved")
    if not isinstance(resolved, Mapping):
        return None
    value = resolved.get(field)
    if not isinstance(value, Mapping):
        return None
    return value.get("value")


def resolution_from_session(meta: sessions.SessionMeta) -> CallResolution:
    """Rebuild stored call facts and the mode catalog snapshot."""
    payload = meta.resolution
    adapter = payload.get("adapter")
    if not isinstance(adapter, Mapping):
        raise RunnerError(
            f"session {meta.session_id} has no complete stored adapter resolution; "
            "it cannot be continued"
        )

    command = payload.get("command")
    if not isinstance(command, str) or not command:
        raise RunnerError(f"session {meta.session_id} has no stored adapter command")

    def stored_strings(name: str, source: Mapping[str, Any]) -> tuple[str, ...]:
        value = source.get(name, ())
        if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
            raise RunnerError(f"session {meta.session_id} has invalid stored {name}")
        return tuple(value)

    declared_env = payload.get("env", {})
    if not isinstance(declared_env, Mapping) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in declared_env.items()
    ):
        raise RunnerError(f"session {meta.session_id} has invalid stored adapter environment")
    home_env = adapter.get("home_env")
    if home_env is not None and not isinstance(home_env, str):
        raise RunnerError(f"session {meta.session_id} has invalid stored home environment name")
    effort_config_id = adapter.get("effort_config_id")
    if effort_config_id is not None and not isinstance(effort_config_id, str):
        raise RunnerError(f"session {meta.session_id} has invalid stored effort config id")
    env_passthrough = stored_strings("env_passthrough", payload)

    stored_mode = adapter.get("mode", _stored_value(payload, "mode"))
    if stored_mode is not None and not isinstance(stored_mode, str):
        raise RunnerError(f"session {meta.session_id} has invalid stored mode")
    grants = adapter.get("grants")
    delegates = adapter.get("delegates")
    mode_spec: ModeSpec | None = None
    if stored_mode is not None and grants is not None and delegates is not None:
        if not isinstance(grants, str) or grants not in vocab.PERMISSION_VALUES[:-1]:
            raise RunnerError(f"session {meta.session_id} has invalid stored mode grants")
        if not isinstance(delegates, bool):
            raise RunnerError(f"session {meta.session_id} has invalid stored mode delegation")
        mode_spec = ModeSpec(grants=grants, delegates=delegates)

    raw_modes = adapter.get("modes")
    if raw_modes is None:
        modes = {stored_mode: mode_spec} if stored_mode is not None and mode_spec else {}
    else:
        modes = mode_catalog_from_payload(raw_modes, context=f"session {meta.session_id}")
        if stored_mode is not None and mode_spec is not None:
            modes[stored_mode] = mode_spec

    entry = ResolvedEntry(
        entry=meta.entry,
        base_adapter=meta.base_adapter,
        name=meta.entry,
        author=None,
        command=command,
        install_command=None,
        home=None,
        home_env=home_env,
        efforts=(),
        effort_config_id=effort_config_id,
        env_passthrough=env_passthrough,
        description=None,
        model=None,
        effort=None,
        mode=None,
        permissions=None,
        env=dict(declared_env),
        modes=modes,
        presets={},
        extends=None,
        provenance={},
    )
    return CallResolution(
        entry=entry,
        model=_stored_value(payload, "model"),
        effort=_stored_value(payload, "effort"),
        mode=stored_mode,
        mode_spec=mode_spec,
        permissions=vocab.normalize_permission(_stored_value(payload, "permissions")) or "read",
        home=_stored_value(payload, "home"),
        declared_env=dict(declared_env),
        env_passthrough=env_passthrough,
        provenance={},
    )


def continue_request(
    meta: sessions.SessionMeta,
    prompt: str,
    *,
    timeout: float | None = None,
    permissions: str | None = None,
    permission_prompt: Callable[[str, str], bool] | None = None,
    resolution: CallResolution | None = None,
) -> TurnRequest:
    """Build a follow-up turn from the session's persisted resolution."""
    if meta.adapter_session_id is None:
        raise RunnerError(
            f"session {meta.session_id} has no stored adapter session id; it cannot be continued"
        )
    payload = meta.resolution
    cwd = payload.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise RunnerError(f"session {meta.session_id} has an invalid stored working directory")
    resolution = resolution or resolution_from_session(meta)
    if permissions is not None:
        resolution = replace(resolution, permissions=vocab.normalize_permission(permissions))
    return TurnRequest(
        resolution=resolution,
        prompt=prompt,
        cwd=cwd,
        timeout=timeout,
        permission_prompt=permission_prompt,
        resume_adapter_session=meta.adapter_session_id,
    )


def _source_label(source: Any) -> str:
    """Render a `FieldSource` the way `--dry-run` and `agents <name>` show it."""
    if source is None:
        return "unset"
    kind = getattr(source, "kind", "unset")
    path = getattr(source, "path", None)
    labels = {
        "entry": "entry",
        "adapter-default": "adapter default",
        "call": "call flag",
        "default": "default",
        "unset": "unset",
        "selected": "selected",
    }
    label = labels.get(kind, kind)
    if path is not None and kind == "entry":
        return f"{label} ({path})"
    return label


# Polling interval for `wait` when the session has no daemon to ask.
WAIT_POLL_INTERVAL = 0.1


async def _await_session(session_id: str, target: str | None, timeout: float | None) -> str | None:
    """Block until the session finishes; None means the wait timed out.

    Asks the owning daemon when there is one, because that returns the moment
    the turn ends. A session with no daemon (direct path, or a daemon that has
    since gone) is watched through `meta.json` instead.
    """
    deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout
    daemon = None
    if target is not None:
        routed = await daemon_client.connect(target)
        daemon = routed

    try:
        if daemon is not None:
            waiting = asyncio.ensure_future(daemon.await_turn(session_id))
            try:
                reply = await asyncio.wait_for(asyncio.shield(waiting), timeout=timeout)
            except TimeoutError:
                waiting.cancel()
                return None
            outcome = reply.get("outcome") or {}
            return str(outcome.get("state", "failed"))

        while True:
            meta = sessions.load(session_id)
            if meta.is_finished:
                return meta.state
            if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                return None
            await asyncio.sleep(WAIT_POLL_INTERVAL)
    finally:
        if daemon is not None:
            with contextlib.suppress(Exception):
                await daemon.close()


def wait_for_session(session_id: str, *, timeout: float | None = None) -> str | None:
    """Block until a session finishes, returning its state or None on timeout.

    SPEC.md `wait`: the timeout stops *waiting* only — unlike `run --timeout`,
    the session is left running.
    """
    meta = sessions.load(session_id)
    if meta.is_finished:
        return meta.state
    return asyncio.run(_await_session(session_id, meta.target, timeout))


def daemon_targets_for(agent: str) -> list[str]:
    """Every known daemon target under an agent or variant name.

    SPEC.md `daemon`: the `[target]` argument is an agent or variant name and
    addresses every concrete target beneath it.
    """
    prefix = f"{agent}~"
    return [
        target for target in all_daemon_targets() if target == agent or target.startswith(prefix)
    ]


def all_daemon_targets() -> list[str]:
    """Every target this state root has ever started a daemon for.

    Read from the lock files, not the sockets: a long `ACPC_HOME` pushes the
    socket path past the Unix limit and `ipc` falls back to a hashed name, so a
    socket's filename is not reliably its target. Lock names never are hashed.
    A target whose daemon has since exited simply fails to connect.
    """
    directory = paths.daemon_dir()
    if not directory.exists():
        return []
    return sorted(entry.name.removesuffix(".lock") for entry in directory.glob("*.lock"))
