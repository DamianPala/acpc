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
import shutil
import signal
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from acp import PROTOCOL_VERSION, text_block

from acpc import cache, daemon_client, environment, sessions, targets, transcript, vocab
from acpc.client import AcpcClient
from acpc.permissions import PermissionLevel
from acpc.registry import CallResolution, RegistryError
from acpc.spawn import spawn_adapter

# SPEC.md `stop`: graceful cancel with a bounded wait for the ack (10s) — if
# the callee does not wind down in time, the connection is torn down anyway.
CANCEL_ACK_TIMEOUT = 10.0

# ACP stop reasons that mean the turn failed rather than completed.
_FAILURE_STOP_REASONS = frozenset({"refusal", "max_tokens", "max_turn_requests"})


class RunnerError(Exception):
    """A turn could not be started; the message is one actionable line."""


@dataclass(frozen=True, slots=True)
class TurnRequest:
    """Everything one turn needs that is not already in `meta.json`."""

    resolution: CallResolution
    prompt: str
    cwd: str | None = None
    mode: str | None = None
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
    adapter_session_id: str | None = None
    advertised: dict[str, Any] = field(default_factory=dict)
    route_note: str | None = None

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
    # failed and orphaned both mean the agent did not deliver an answer.
    del stop_reason
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
    )


class _CancelSignal:
    """Records why a turn is being wound down, and what that means on exit."""

    def __init__(self) -> None:
        self.state: str | None = None
        self.requested = asyncio.Event()

    def request(self, state: str) -> None:
        if self.state is None:
            self.state = state
        self.requested.set()


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
        bypass_modes=resolution.entry.bypass_modes,
        permission_prompt=request.permission_prompt,
    )

    async with spawn_adapter(
        client,
        command,
        *args,
        env=env,
        cwd=request.cwd,
        drain_stderr=True,
    ) as (conn, _process):
        await conn.initialize(protocol_version=PROTOCOL_VERSION)

        if request.resume_adapter_session is not None:
            adapter_session_id = request.resume_adapter_session
            await conn.load_session(
                session_id=adapter_session_id,
                cwd=request.cwd or ".",
                mcp_servers=[],
            )
        else:
            session = await conn.new_session(cwd=request.cwd or ".", mcp_servers=[])
            adapter_session_id = session.session_id
            client.capture_advertised(session)

        await _apply_call_options(conn, adapter_session_id, request)

        prompt_task = asyncio.create_task(
            conn.prompt(
                session_id=adapter_session_id,
                prompt=[text_block(request.prompt)],
            )
        )
        stop_reason = await _await_prompt(conn, adapter_session_id, prompt_task, request, cancel)

    state = cancel.state if cancel.state is not None else _state_for_stop_reason(stop_reason)
    return TurnOutcome(
        state=state,
        stop_reason=stop_reason,
        answer=client.answer,
        tokens=client.tokens,
        cost=client.cost,
        adapter_session_id=adapter_session_id,
        advertised=client.advertised,
    )


async def _apply_call_options(conn: Any, adapter_session_id: str, request: TurnRequest) -> None:
    """Apply --mode/--model/--effort to the adapter session before prompting."""
    resolution = request.resolution
    if request.mode is not None:
        await conn.set_session_mode(session_id=adapter_session_id, mode_id=request.mode)
    if resolution.model is not None:
        await conn.set_config_option(
            config_id="model",
            session_id=adapter_session_id,
            value=resolution.model,
        )
    if resolution.effort is not None:
        await conn.set_config_option(
            config_id="reasoning_effort",
            session_id=adapter_session_id,
            value=resolution.effort,
        )


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
    """Read a finished prompt task's stop reason, mapping a crash to a failure."""
    error = prompt_task.exception() if not prompt_task.cancelled() else None
    if prompt_task.cancelled():
        return "cancelled"
    if error is not None:
        return "error"
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


async def _execute(session_id: str, request: TurnRequest) -> TurnOutcome:
    events = transcript.Transcript(sessions.transcript_path(session_id))
    cancel = _CancelSignal()

    routed = await daemon_client.ensure_daemon(call_target(request.resolution))
    route_note: str | None = None
    if isinstance(routed, daemon_client.DaemonUnavailable):
        route_note = f"direct child ({routed.reason})"

    _install_signal_handlers(asyncio.get_running_loop(), cancel, daemon_routed=route_note is None)

    sessions.mark_running(session_id, pid=_host_pid())
    events.append("state", **{"from": "starting", "to": "running"})

    outcome = await _drive_turn(session_id, request, events, cancel)
    outcome.route_note = route_note
    return outcome


def _host_pid() -> int:
    import os

    return os.getpid()


def execute_turn(session_id: str, request: TurnRequest) -> TurnOutcome:
    """Run one turn synchronously and leave the session finalized on disk."""
    try:
        outcome = asyncio.run(_execute(session_id, request))
    except RunnerError:
        _finalize(session_id, TurnOutcome(state="failed", stop_reason="error", answer=""))
        raise
    except Exception as error:  # noqa: BLE001
        # Deliberately broad: an adapter crash, a protocol error and a spawn
        # failure all have to leave a finished session on disk. Anything that
        # escaped here would leave `meta.json` saying `running` forever, which
        # is the one state SPEC says no reader may ever be shown.
        outcome = TurnOutcome(state="failed", stop_reason="error", answer="")
        _finalize(session_id, outcome, error=error)
        return outcome
    _finalize(session_id, outcome)
    return outcome


def _finalize(
    session_id: str,
    outcome: TurnOutcome,
    *,
    error: BaseException | None = None,
) -> None:
    """Write the answer and close out `meta.json`, whatever happened.

    SPEC.md *State on disk*: `answer.md` is written whatever the final state —
    for a failed or cancelled turn it holds the partial answer.
    """
    answer = outcome.answer
    if error is not None and not answer:
        answer = f"{error}\n"
    sessions.write_answer(session_id, answer)

    events_path = sessions.transcript_path(session_id)
    with contextlib.suppress(Exception):
        events = transcript.Transcript(events_path)
        if error is not None:
            events.append("error", message=str(error))
        events.append("state", **{"from": "running", "to": outcome.state})

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
            adapter_session_id=outcome.adapter_session_id,
        )

    if outcome.advertised:
        with contextlib.suppress(Exception):
            cache.refresh_advertised(_agent_of(session_id), outcome.advertised)


def _agent_of(session_id: str) -> str:
    with contextlib.suppress(sessions.SessionError):
        return sessions.read_meta(session_id).base_adapter
    return ""


def auto_prune(retention_seconds: float) -> None:
    """Opportunistic retention sweep; never fails a run (SPEC.md `prune`)."""
    with contextlib.suppress(Exception):
        sessions.prune_sessions(older_than=retention_seconds)


def resolution_payload(resolution: CallResolution, *, cwd: str | None) -> dict[str, Any]:
    """The `--dry-run` view: every resolved value and where it came from."""
    entry = resolution.entry
    fields: dict[str, Any] = {
        "model": resolution.model,
        "effort": resolution.effort,
        "permissions": resolution.permissions,
        "home": resolution.home,
    }
    provenance: Mapping[str, Any] = resolution.provenance
    return {
        "entry": entry.entry,
        "base_adapter": entry.base_adapter,
        "command": entry.command,
        "cwd": cwd,
        "env": dict(resolution.declared_env),
        "env_passthrough": list(resolution.env_passthrough),
        "resolved": {
            name: {
                "value": value,
                "source": _source_label(provenance.get(name)),
            }
            for name, value in fields.items()
        },
    }


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
    }
    label = labels.get(kind, kind)
    if path is not None and kind == "entry":
        return f"{label} ({path})"
    return label
