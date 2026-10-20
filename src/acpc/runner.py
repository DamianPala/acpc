"""One turn, end to end: route, run, handle signals, finalize.

SPEC.md `run` and *Output contract*. The runner owns the lifecycle between a
resolved call and a finished session:

- **Routing.** Ask `daemon_client` for a warm adapter; if the daemon is not
  available, spawn the adapter as a direct child and say so — the fallback is
  visible, folded into the single `--` stderr summary line, never silent.
- **Signals.** SIGINT is a human's Ctrl-C and cancels the session (ACP
  `session/cancel`, bounded wait for the ack) → exit 130. SIGTERM is a harness
  killing the tool call; on the daemon path it detaches, while an ordinary
  direct child cancels too → exit 143. A direct wait-deadline worker is an
  accepted, detached owner just for the `--timeout` wait-only path.
- **Timeout.** `--timeout` bounds the client's wait without canceling work;
  the daemon keeps the session running and the client exits 124.
- **Cancel-after.** `--cancel-after` cancels the session, which is a failed
  operation for a waiting caller and exits 1.
- **Finalization.** Whatever the outcome, `answer.md` and `meta.json` are
  written before the process exits: a partial answer is still an answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from acpc import (
    cache,
    daemon_client,
    environment,
    errors,
    limits,
    paths,
    sessions,
    targets,
    transcript,
    vocab,
)
from acpc.permissions import PermissionLevel
from acpc.proc import process_group_kwargs
from acpc.registry import (
    SYSTEM_PROMPT_VIA_VALUES,
    TOOLS_VIA_VALUES,
    AgentRegistry,
    CallResolution,
    ModeSpec,
    RegistryError,
    ResolvedEntry,
    SystemPrompt,
)

if TYPE_CHECKING:
    from acpc.client import AcpcClient


def __getattr__(name: str) -> Any:
    if name != "spawn_adapter":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from acpc.spawn import spawn_adapter  # import floor

    return spawn_adapter


# SPEC.md `cancel`: graceful cancellation with a bounded wait for the ack (10s) — if
# the callee does not wind down in time, the connection is torn down anyway.
CANCEL_ACK_TIMEOUT = 10.0

# Failure events are part of the transcript and may be copied into a caller's
# context. Eight KiB is large enough for the useful tail of normal adapter
# diagnostics while keeping one unusually large stderr line bounded.
ADAPTER_LOG_TAIL_BYTES = 8 * 1024

# Adapter stderr is terminal-oriented, but transcript consumers are not.
_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b\n]*(?:\x07|\x1b\\))")

# Bounds both the tail spliced into the one-line failure message and the
# persisted failure observation in meta.json.
MESSAGE_TAIL_CHARS = 300

# A direct timeout worker owns the accepted turn after the observing client
# exits.  The request is private session state and is removed after the worker
# reads it; it is not part of the advertised session paths.
_DIRECT_REQUEST_NAME = ".direct-request"
_DIRECT_WORKER_ENV = "ACPC_DIRECT_WORKER"

# ACP stop reasons that mean the turn failed rather than completed.
_FAILURE_STOP_REASONS = frozenset({"refusal", "max_tokens", "max_turn_requests"})

# ...of those, the ones where the adapter hit a ceiling rather than broke.
_ADAPTER_LIMIT_STOP_REASONS = frozenset({"max_tokens", "max_turn_requests"})


def run_with_sigint_deferred[T](
    coro_factory: Callable[[], Coroutine[Any, Any, T]],
    *,
    installs_sigint_handler: bool = False,
) -> T:
    """Run an async operation with SIGINT blocked until its loop can take it.

    The mask comes back before the operation's first step, so a process the
    operation spawns in that step does not inherit a blocked SIGINT. An operation
    that installs its own SIGINT handler in its first step keeps the mask until
    that step ends, so a SIGINT cannot land before the handler exists.
    """
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
    try:

        async def run() -> T:
            loop = asyncio.get_running_loop()
            if not installs_sigint_handler:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
            task = loop.create_task(coro_factory())
            if installs_sigint_handler:
                loop.call_soon(signal.pthread_sigmask, signal.SIG_SETMASK, previous_mask)
            return await task

        return asyncio.run(run())
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


class RunnerError(Exception):
    """A turn could not be started; the message is one actionable line.

    `kind` is set when the refusal arrived already classified — a daemon that
    refused the turn knows why, and the client must report that rather than
    re-derive it from the message text it was handed.
    """

    def __init__(self, message: str, *, kind: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind


class AdapterUnavailable(RunnerError):
    """The adapter this call needs cannot be reached at all.

    Distinct from the rest because nothing about the call is wrong and the
    adapter never answered: the caller has to install or start something, not
    fix an argument or read a refusal.
    """


class ResumePreparationError(RunnerError):
    """A cold resume failed before acpc opened the next turn on disk."""

    def __init__(self, message: str, *, kind: str | None = None) -> None:
        super().__init__(message, kind=kind)


class ResumeVerificationError(ResumePreparationError):
    """The adapter's identity or replay did not match the stored session."""


class ResumeRotationError(RunnerError):
    """The verified continuation could not be opened on the session store."""

    def __init__(
        self, message: str, *, turn_token: int | None = None, kind: str | None = None
    ) -> None:
        super().__init__(message, kind=kind)
        self.turn_token = turn_token


class ResumeSetupError(RunnerError):
    """A claimed continuation failed before its prompt could be dispatched."""

    def __init__(self, message: str, *, turn_token: int) -> None:
        super().__init__(message)
        self.turn_token = turn_token


class TurnEndedByAcpc(RunnerError):
    """acpc itself ended a running turn — a daemon stop, not an adapter fault."""


def describe_error(error: BaseException) -> str:
    """Render a failure with everything the adapter actually said.

    A JSON-RPC error's fixed message ("Internal error") often hides the
    vendor's real diagnosis, which rides in the optional `data` member.
    """
    from acp import RequestError  # import floor

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


def has_close_session_capability(capabilities: Any) -> bool:
    """Return whether initialize advertised ACP ``session/close``."""
    session_capabilities = getattr(capabilities, "session_capabilities", None)
    return getattr(session_capabilities, "close", None) is not None


def has_list_session_capability(capabilities: Any) -> bool:
    """Return whether initialize advertised ACP ``session/list``."""
    session_capabilities = getattr(capabilities, "session_capabilities", None)
    return getattr(session_capabilities, "list", None) is not None


async def verify_listed_adapter_session(
    conn: Any, capabilities: Any, adapter_session_id: str, cwd: str
) -> bool:
    """Verify the adapter id and cwd, walking every advertised list page."""
    if not has_list_session_capability(capabilities):
        return False

    cursor: str | None = None
    while True:
        response = await conn.list_sessions(cursor=cursor)
        for info in getattr(response, "sessions", []):
            if getattr(info, "session_id", None) != adapter_session_id:
                continue
            listed_cwd = getattr(info, "cwd", None)
            if listed_cwd != cwd:
                raise ResumeVerificationError(
                    f"resume verification failed for adapter session {adapter_session_id!r}: "
                    f"cwd is {listed_cwd!r}, expected {cwd!r}"
                )
            return True

        next_cursor = getattr(response, "next_cursor", None)
        if not next_cursor:
            raise ResumeVerificationError(
                f"resume verification failed: adapter session id {adapter_session_id!r} "
                "was not found by session/list"
            )
        cursor = next_cursor


def verify_replayed_prompts(
    adapter_session_id: str, expected: Sequence[tuple[Path, str]], replayed: Sequence[str]
) -> None:
    """Require stored prompts to occur in order in the adapter's user replay."""
    replay_index = 0
    for prompt_path, prompt in expected:
        try:
            found_at = replayed.index(prompt, replay_index)
        except ValueError:
            reason = "arrived out of order" if prompt in replayed else "was not found"
            raise ResumeVerificationError(
                f"resume verification failed for adapter session {adapter_session_id!r}: "
                f"stored prompt {prompt_path} {reason} in the replay"
            ) from None
        replay_index = found_at + 1


def _resume_status(
    *, list_checked: bool, replay_checked: bool, delivery_record_incomplete: bool = False
) -> str:
    """Render the persisted resume confidence for summaries and JSON."""
    if delivery_record_incomplete:
        return "unverified — delivery record incomplete"
    if list_checked or replay_checked:
        return "verified"
    unavailable = []
    if not list_checked:
        unavailable.append("session/list unavailable")
    if not replay_checked:
        unavailable.append("conversation replay unavailable")
    return "unverified — " + "; ".join(unavailable)


def _stored_prompt_items(session_id: str) -> list[tuple[Path, str]]:
    """Read only prompts whose outgoing request crossed the ACP boundary."""
    meta = sessions.read_meta(session_id)
    records = meta.extra.get("delivered_prompts")
    if not isinstance(records, list):
        return []
    delivered_turns = {
        record.get("turn")
        for record in records
        if isinstance(record, Mapping) and isinstance(record.get("turn"), int)
    }
    items: list[tuple[Path, str]] = []
    for turn in range(1, meta.turns + 1):
        if turn not in delivered_turns:
            continue
        prompt_path = (
            sessions.prompt_path(session_id)
            if turn == meta.turns
            else sessions.turn_path(session_id, "prompt", turn)
        )
        try:
            prompt = prompt_path.read_text(encoding="utf-8")
        except OSError as error:
            raise ResumePreparationError(
                f"cannot verify resume for session {session_id}: "
                f"stored prompt {prompt_path} is unreadable ({error})"
            ) from None
        items.append((prompt_path, prompt))
    return items


def adapter_session_meta(session_id: str, resolution: CallResolution) -> dict[str, Any]:
    """The `_meta` keys a session's system prompt, tool list and sterility travel in.

    Empty when the session has none of them, so a plain session stays
    byte-identical on the wire. The prompt text is read from the session's
    `system.md` here, at the call that sends it, so no payload or snapshot
    carries it. Only the `acp_meta` routes are served by this function; the
    adapter facts decide. One-shot disables vendor transcript persistence;
    setting_sources independently controls which vendor settings are loaded.
    """
    meta: dict[str, Any] = {}
    if resolution.system_prompt is not None and resolution.entry.system_prompt_via == "acp_meta":
        path = sessions.system_path(session_id)
        try:
            meta["systemPrompt"] = path.read_text(encoding="utf-8")
        except OSError as error:
            raise RunnerError(
                f"session {session_id}: its system prompt {path} is unreadable ({error}); "
                "acpc never falls back to the vendor's own prompt"
            ) from None
    if resolution.entry.tools_via == "acp_meta":
        options: dict[str, Any] = {}
        if resolution.tools is not None:
            options["tools"] = list(resolution.tools)
        if resolution.one_shot:
            options["persistSession"] = False
        if resolution.setting_sources is not None:
            options["settingSources"] = list(resolution.setting_sources)
            if not resolution.setting_sources:
                options["strictMcpConfig"] = True
        if options:
            meta["claudeCode"] = {"options": options}
    return meta


async def _restore_adapter_session(
    conn: Any,
    capabilities: Any,
    adapter_session_id: str,
    cwd: str,
    adapter_meta: Mapping[str, Any] | None = None,
) -> None:
    """Restore an adapter session, preferring ``session/resume`` when offered."""
    require_load_session_capability(capabilities)
    request = {
        "session_id": adapter_session_id,
        "cwd": cwd,
        "mcp_servers": [],
        **(adapter_meta or {}),
    }
    if has_resume_session_capability(capabilities):
        await conn.resume_session(**request)
    else:
        await conn.load_session(**request)


async def restore_adapter_session(
    conn: Any, capabilities: Any, adapter_session_id: str, cwd: str
) -> bool:
    """Verify the listed identity, then restore the adapter session."""
    list_checked = await verify_listed_adapter_session(conn, capabilities, adapter_session_id, cwd)
    await _restore_adapter_session(conn, capabilities, adapter_session_id, cwd)
    return list_checked


async def verify_adapter_resume(
    conn: Any,
    client: AcpcClient,
    capabilities: Any,
    adapter_session_id: str,
    cwd: str,
    session_id: str,
    *,
    abandon_event: asyncio.Event | None = None,
    adapter_meta: Mapping[str, Any] | None = None,
) -> str:
    """Restore and independently run the adapter identity and replay checks.

    `adapter_meta` is the session's stored system prompt and tool list; the
    restore sends them again so a resumed session runs under the first turn's.
    """
    try:
        meta = sessions.read_meta(session_id)
    except sessions.SessionNotFound:
        # The focused verifier tests can provide their stored prompt list
        # without constructing a session directory.
        delivery_record_incomplete = False
    else:
        delivery_record_incomplete = meta.extra.get(sessions.DELIVERY_RECORD_INCOMPLETE) is True
    expected_prompts = _stored_prompt_items(session_id)
    list_checked = False
    list_error: ResumeVerificationError | None = None
    if has_list_session_capability(capabilities):
        try:
            list_checked = await verify_listed_adapter_session(
                conn, capabilities, adapter_session_id, cwd
            )
        except ResumeVerificationError as error:
            list_error = error
    if abandon_event is not None and abandon_event.is_set():
        return _resume_status(list_checked=list_checked, replay_checked=False)

    restore_started = False
    restore_to_settle: asyncio.Task[None] | None = None
    async with client.replaying(adapter_session_id, conn) as sink:
        restore_task = asyncio.create_task(
            _restore_adapter_session(conn, capabilities, adapter_session_id, cwd, adapter_meta),
            name=f"acpc.restore.{adapter_session_id}",
        )
        restore_started = True
        if abandon_event is None:
            await restore_task
        else:
            abandoned = asyncio.create_task(
                abandon_event.wait(), name=f"acpc.restore-abandon.{adapter_session_id}"
            )
            try:
                done, _pending = await asyncio.wait(
                    {restore_task, abandoned}, return_when=asyncio.FIRST_COMPLETED
                )
                if restore_task not in done:
                    # Leave the replay scope before waiting for the adapter's
                    # uncancellable restore request to settle. Late frames are
                    # then retained by ReplayTracker and cannot reach a later
                    # live binding.
                    restore_to_settle = restore_task
                else:
                    await restore_task
            finally:
                abandoned.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await abandoned
    if restore_to_settle is not None:
        await restore_to_settle
    if not restore_started:
        return _resume_status(list_checked=list_checked, replay_checked=False)
    replay_checked = sink.replay_available and bool(expected_prompts) and bool(sink.user_messages)
    if replay_checked:
        verify_replayed_prompts(adapter_session_id, expected_prompts, sink.user_messages)
    if list_error is not None:
        raise list_error
    return _resume_status(
        list_checked=list_checked,
        replay_checked=replay_checked,
        delivery_record_incomplete=delivery_record_incomplete,
    )


_PROMPT_MARKER_ATTEMPTS = 3


class PromptDelivery:
    """Track a sent prompt until its durable delivery marker is persisted."""

    def __init__(
        self,
        session_id: str,
        adapter_session_id: str,
        prompt: str,
        on_delivered: Callable[[], None] | None = None,
    ) -> None:
        self.session_id = session_id
        self.adapter_session_id = adapter_session_id
        self.prompt = prompt
        self._on_delivered = on_delivered
        self.available = False
        self.seen = False
        self.persisted = False
        self.delivery_record_incomplete = False
        self.error: Exception | None = None

    def observe(self, event: Any) -> None:
        if getattr(getattr(event, "direction", None), "value", None) != "outgoing":
            return
        message = getattr(event, "message", {})
        params = message.get("params", {}) if isinstance(message, Mapping) else {}
        if (
            not isinstance(message, Mapping)
            or message.get("method") != "session/prompt"
            or not isinstance(params, Mapping)
            or params.get("sessionId") != self.adapter_session_id
        ):
            return
        first_observation = not self.seen
        self.seen = True
        if first_observation and self._on_delivered is not None:
            self._on_delivered()
        try:
            sessions.mark_prompt_delivered(self.session_id, self.prompt)
        except (OSError, sessions.SessionError) as error:
            self.error = error
        else:
            self.persisted = True

    async def ensure_persisted(self, *, prompt_completed: bool) -> None:
        """Retry marker persistence before the turn can be finalized."""
        if self.persisted:
            return
        if not self.available:
            if prompt_completed:
                self.delivery_record_incomplete = True
            return
        if not self.seen:
            if prompt_completed:
                self.delivery_record_incomplete = True
            return
        for _attempt in range(_PROMPT_MARKER_ATTEMPTS):
            try:
                sessions.mark_prompt_delivered(self.session_id, self.prompt)
            except (OSError, sessions.SessionError) as error:
                self.error = error
                continue
            self.persisted = True
            self.error = None
            return
        self.delivery_record_incomplete = True


def register_prompt_delivery(
    conn: Any,
    session_id: str,
    adapter_session_id: str,
    prompt: str,
    *,
    on_delivered: Callable[[], None] | None = None,
) -> PromptDelivery:
    """Record a prompt after ACP has accepted its outgoing wire frame."""
    delivery = PromptDelivery(session_id, adapter_session_id, prompt, on_delivered)
    raw_connection = getattr(conn, "_conn", None)
    add_observer = getattr(raw_connection, "add_observer", None)
    if not callable(add_observer):
        return delivery
    delivery.available = True
    add_observer(delivery.observe)
    return delivery


@dataclass(frozen=True, slots=True)
class TurnRequest:
    """Everything one turn needs that is not already in `meta.json`."""

    resolution: CallResolution
    prompt: str
    cwd: str | None = None
    # ``wait_timeout`` belongs to the client observing the turn.  It must not
    # be sent to the adapter or cancel work that the daemon has accepted.
    wait_timeout: float | None = None
    # ``cancel_after`` is the deadline that intentionally cancels this turn.
    cancel_after: float | None = None
    permission_prompt: Callable[[str, str], bool] | None = None
    resume_adapter_session: str | None = None
    defer_rotation: bool = False
    rotation_resolution: Mapping[str, Any] | None = None
    resume_prepared: bool = False
    turn_token: int | None = None
    limit_wait_max: float | None = None


@dataclass(slots=True)
class TurnOutcome:
    """The result of one turn, ready for the output layer."""

    state: str
    stop_reason: str | None
    answer: str
    context: vocab.ContextOccupancy | None = None
    usage: dict[str, Any] | None = None
    served_model: str | None = None
    usage_finalized: bool = False
    denied: dict[str, int] = field(default_factory=dict)
    denial_details: dict[str, dict[str, Any]] = field(default_factory=dict)
    adapter_session_id: str | None = None
    advertised: dict[str, Any] = field(default_factory=dict)
    rate_windows: dict[str, Any] | None = None
    route_note: str | None = None
    # The adapter's own failure, when the turn died in `session/prompt`. Carried
    # on the outcome rather than raised so the prose streamed before it survives.
    error: BaseException | None = None
    # The finalizer persists this under the same lock as the answer and state.
    delivery_record_incomplete: bool = False
    # Set when the daemon owns the session and has already written it out;
    # finalizing again here would overwrite the daemon's own result.
    finalized_elsewhere: bool = False
    queued: bool = False
    turn_token: int | None = None
    # The daemon continues the accepted turn after this client stops waiting.
    wait_timed_out: bool = False
    # Set when a usage limit touched this turn; carried onto `meta.limit`.
    limit: dict[str, Any] | None = None
    # True when this process's own Ctrl-C ended the turn. A session that reads
    # `cancelled` says nothing about who cancelled it, and the two answers need
    # different failure kinds: the command was stopped, or it watched an
    # operation end badly. Only the side that received the signal knows which.
    interrupted: bool = False

    def __post_init__(self) -> None:
        self.state = vocab.normalize_session_state(self.state)

    @property
    def exit_code(self) -> int:
        return exit_code_for(self.state, self.stop_reason)


def exit_code_for(state: str, stop_reason: str | None = None) -> int:
    """Map a finished session's state to SPEC's fixed exit codes."""
    state = vocab.normalize_session_state(state)
    if state == "succeeded":
        return vocab.EXIT_OK
    if state == "canceled":
        return vocab.EXIT_AGENT_ERROR if stop_reason == "cancel_after" else vocab.EXIT_CANCELLED
    # Detached (daemon path) and terminated (direct path) are both "SIGTERM
    # ended this client"; they differ only in whether the session survives it.
    if state in {"detached", "terminated"}:
        return vocab.EXIT_SIGTERM
    if stop_reason == "permission_denied":
        return vocab.EXIT_USAGE
    # failed and unknown both mean the agent did not deliver an answer.
    return vocab.EXIT_AGENT_ERROR


def _state_for_stop_reason(stop_reason: str | None) -> str:
    if stop_reason == "end_turn":
        return "succeeded"
    if stop_reason in ("cancelled", "canceled"):
        return "canceled"
    if stop_reason in _FAILURE_STOP_REASONS:
        return "failed"
    return "failed"


def adapter_command(resolution: CallResolution) -> tuple[str, tuple[str, ...]]:
    """Split the call's spawn argv, and refuse early when it is not installed.

    Uses ``CallResolution.command`` so effort_via=cli can inject flags for this
    call without rewriting the entry's base command string.
    """
    entry = resolution.entry
    try:
        args = resolution.command
    except RegistryError as error:
        raise RunnerError(str(error)) from None
    if shutil.which(args[0]) is None:
        raise AdapterUnavailable(entry.missing_binary_error())
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
    spawn_identity: dict[str, str] = {}
    if resolution.entry.effort_via == "cli" and resolution.effort is not None:
        # Effort is process-level on the CLI path — distinct efforts need
        # distinct daemons (they cannot be changed after spawn).
        spawn_identity["effort"] = resolution.effort
        flag = resolution.entry.effort_cli_flag or "--reasoning-effort"
        spawn_identity["effort_cli_flag"] = flag
    if (
        resolution.entry.system_prompt_via == "codex_config"
        and resolution.system_prompt is not None
    ):
        spawn_identity["system_prompt_file"] = resolution.system_prompt.path
    return targets.target_for_call(
        resolution.entry.entry,
        home=resolution.home,
        declared_env=declared,
        passthrough_values=passthrough,
        permissions=resolution.permissions,
        spawn_identity=spawn_identity or None,
    )


def daemon_target(resolution: CallResolution) -> str:
    """Compute a continue target with the live entry's apply-path identity."""
    try:
        live_entry = AgentRegistry().resolve(resolution.entry.entry)
    except RegistryError:
        # A deleted or temporarily invalid entry must not make continue fail
        # before the daemon gets its existing, actionable rebuild error.
        return call_target(resolution)
    return call_target(replace(resolution, entry=live_entry))


def daemon_target_from_session(meta: sessions.SessionMeta) -> str:
    """Compute a continue target from stored values plus live entry vias."""
    return daemon_target(resolution_from_session(meta))


class _CancelSignal:
    """Record a turn stop, resolving signal meaning once routing is known."""

    def __init__(self) -> None:
        self.state: str | None = None
        self.stop_reason: str | None = None
        self.received_signal: int | None = None
        self._daemon_routed: bool | None = None
        self.requested = asyncio.Event()
        self.cancellation_dispatched = asyncio.Event()

    def request(self, state: str, *, stop_reason: str | None = None) -> None:
        if self.state is None:
            self.state = state
            self.stop_reason = stop_reason
        self.requested.set()

    def request_signal(self, signal_number: int) -> None:
        """Latch a signal without guessing what SIGTERM means yet."""
        if self.received_signal is None:
            self.received_signal = signal_number
            if self.state is None and self._daemon_routed is not None:
                self.state = self._signal_state()
        self.requested.set()

    def resolve_route(self, *, daemon_routed: bool) -> None:
        """Apply the route-specific meaning to the first received signal."""
        self._daemon_routed = daemon_routed
        if self.state is None:
            self.state = self._signal_state()

    def _signal_state(self) -> str | None:
        if self.received_signal == signal.SIGINT:
            return "canceled"
        if self.received_signal == signal.SIGTERM and self._daemon_routed is not None:
            return "detached" if self._daemon_routed else "terminated"
        return None

    def end_turn(self) -> None:
        """End a turn through the same cancellation path as external stops."""
        self.request("failed", stop_reason="permission_denied")


# SPEC.md *Session states*: "A canceled turn records `stop_reason: canceled`
# whichever side canceled it, or `canceled during preparation` when the
# cancel landed before any prompt was sent" — acpc's own public spelling
# (slice 23 review debt: this used to be the ACP wire spelling `cancelled`).
PREPARATION_CANCELLED_REASON = "canceled during preparation"


def preparation_cancelled_answer(session_id: str) -> str:
    """Explain a cancellation for which no prompt crossed the ACP boundary."""
    return f"Session {session_id} was canceled during preparation; no prompt was sent.\n"


def _spawn_adapter(*args: Any, **kwargs: Any) -> Any:
    spawn = globals().get("spawn_adapter")
    if spawn is None:
        from acpc.spawn import spawn_adapter as spawn  # import floor

    return spawn(*args, **kwargs)


def _prompt_block(text: str) -> Any:
    from acp import text_block  # import floor

    return text_block(text)


def _is_request_error(error: BaseException | None) -> bool:
    if error is None:
        return False
    from acp import RequestError  # import floor

    return isinstance(error, RequestError)


def _direct_turn_client(
    session_id: str, request: TurnRequest, events: transcript.Transcript, cancel: _CancelSignal
) -> AcpcClient:
    from acpc.client import AcpcClient  # import floor

    resolution = request.resolution
    stored = sessions.read_meta(session_id)
    return AcpcClient(
        events,
        PermissionLevel(resolution.permissions or "read"),
        modes=resolution.entry.modes,
        effort_config_id=resolution.entry.effort_config_id,
        end_turn=cancel.end_turn,
        cancellation_dispatched=cancel.cancellation_dispatched,
        permission_prompt=request.permission_prompt,
        previous_context=stored.context,
        previous_usage=stored.usage,
        usage_profile=resolution.entry.usage_profile,
        billing=resolution.entry.billing,
        resolved_model=resolution.model,
        prompt=request.prompt,
        turn_number=stored.turns,
        session_id=session_id,
    )


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
    client = _direct_turn_client(session_id, request, events, cancel)
    turn_error: BaseException | None = None
    prompt_sent = False
    usage_value: dict[str, Any] | None = None
    usage_finalized = False
    delivery: PromptDelivery | None = None
    limit_record: dict[str, Any] | None = None
    adapter_session_id = request.resume_adapter_session
    stop_reason: str | None = "error"

    def mark_prompt_sent() -> None:
        nonlocal prompt_sent
        prompt_sent = True

    def finalize_usage() -> None:
        nonlocal usage_value, usage_finalized
        if not usage_finalized:
            usage_value = client.finish_usage()
            usage_finalized = True

    try:
        try:
            async with _spawn_adapter(
                client,
                command,
                *args,
                env=env,
                cwd=request.cwd,
                drain_stderr=True,
            ) as (conn, _process):
                adapter_session_id, request = await _prepare_direct_adapter_session(
                    conn, session_id, request, events, client
                )
                stop_reason, turn_error, delivery, limit_record = await _prompt_direct_turn(
                    conn,
                    request,
                    session_id=session_id,
                    adapter_session_id=adapter_session_id,
                    cancel=cancel,
                    client=client,
                    on_delivered=mark_prompt_sent,
                )
                if delivery is not None:
                    finalize_usage()
        finally:
            client.flush()
    except Exception as error:
        if not prompt_sent:
            raise
        turn_error = error
        stop_reason = "error"
        finalize_usage()

    if cancel.stop_reason is not None:
        stop_reason = cancel.stop_reason
    state = cancel.state if cancel.state is not None else _state_for_stop_reason(stop_reason)
    # SPEC.md *Session states*: the adapter's `cancelled` stop reason is
    # normalized to acpc's own `canceled` when the turn ends.
    stop_reason = vocab.normalize_stop_reason(stop_reason)
    return TurnOutcome(
        state=state,
        stop_reason=stop_reason,
        answer=client.answer,
        context=client.context,
        served_model=client.served_model,
        usage=usage_value,
        usage_finalized=usage_finalized,
        denied=client.denied,
        denial_details=client.denial_details,
        adapter_session_id=adapter_session_id,
        advertised=client.advertised,
        rate_windows=client.rate_windows,
        error=turn_error,
        delivery_record_incomplete=bool(delivery and delivery.delivery_record_incomplete),
        turn_token=request.turn_token,
        limit=limit_record,
    )


async def _prepare_direct_adapter_session(
    conn: Any,
    session_id: str,
    request: TurnRequest,
    events: transcript.Transcript,
    client: AcpcClient,
) -> tuple[str, TurnRequest]:
    """Initialize or restore the adapter session before applying call options."""
    from acp import PROTOCOL_VERSION  # import floor

    initialize = await conn.initialize(protocol_version=PROTOCOL_VERSION)
    client.capture_adapter(initialize)
    record_usage_identity(session_id, client.adapter_identity)
    if request.resume_adapter_session is None:
        session = await conn.new_session(
            cwd=request.cwd or os.getcwd(),
            mcp_servers=[],
            **adapter_session_meta(session_id, request.resolution),
        )
        adapter_session_id = session.session_id
        client.capture_advertised(session)
        sessions.update_meta(session_id, adapter_session_id=adapter_session_id)
        return adapter_session_id, request

    adapter_session_id = request.resume_adapter_session
    capabilities = getattr(initialize, "agent_capabilities", None)
    try:
        resume_status = await verify_adapter_resume(
            conn,
            client,
            capabilities,
            adapter_session_id,
            request.cwd or os.getcwd(),
            session_id,
            adapter_meta=adapter_session_meta(session_id, request.resolution),
        )
    except ResumePreparationError:
        raise
    except Exception as error:
        if request.defer_rotation:
            raise ResumePreparationError(str(error)) from None
        raise
    client.mark_cold_resume()
    request = _prepare_resumed_turn(
        session_id,
        request,
        events,
        client=client,
        resume_status=resume_status,
        steer_mode=vocab.STEER_CANCEL_THEN_START,
    )
    client.permission_level = PermissionLevel(request.resolution.permissions or "read")
    client.modes = request.resolution.entry.modes
    return adapter_session_id, request


async def _prompt_direct_turn(
    conn: Any,
    request: TurnRequest,
    *,
    session_id: str,
    adapter_session_id: str,
    cancel: _CancelSignal,
    client: AcpcClient,
    on_delivered: Callable[[], None],
) -> tuple[str | None, BaseException | None, PromptDelivery | None, dict[str, Any] | None]:
    """Apply options and send a prompt, preserving setup-failure handling."""
    prompt_started = False
    try:
        await apply_call_options(conn, adapter_session_id, request, observer=client)
        prompt_started = True
        return await run_prompt_with_limits(
            conn,
            session_id,
            adapter_session_id,
            request,
            cancel,
            client,
            on_delivered=on_delivered,
        )
    except BaseException as error:
        if prompt_started or (request.turn_token is None and not isinstance(error, Exception)):
            raise
        if request.turn_token is None:
            # No delivery: the caller finalizes no usage for a prompt never started.
            return "error", error, None, None
        _finalize_claimed_setup_failure(
            session_id,
            request.turn_token,
            error,
            adapter_session_id,
            advertised=client.advertised,
            rate_windows=client.rate_windows,
        )
        if isinstance(error, asyncio.CancelledError):
            raise
        raise ResumeSetupError(describe_error(error), turn_token=request.turn_token) from None


def _prepare_resumed_turn(
    session_id: str,
    request: TurnRequest,
    events: transcript.Transcript,
    *,
    client: AcpcClient | None = None,
    resume_status: str | None = None,
    pid: int | None = None,
    steer_mode: str | None = None,
) -> TurnRequest:
    """Atomically claim a verified continuation before it can prompt."""
    if not request.defer_rotation:
        return request

    def resolution_from_meta(meta: sessions.SessionMeta) -> Mapping[str, Any]:
        if request.rotation_resolution is None:
            return meta.resolution
        updated = deepcopy(meta.resolution)
        candidate = request.rotation_resolution
        for key in ("resolved", "adapter"):
            if key in candidate:
                updated[key] = deepcopy(candidate[key])
        if "permissions_source" in candidate:
            updated["permissions_source"] = candidate["permissions_source"]
        else:
            updated.pop("permissions_source", None)
        return updated

    try:
        rotated = sessions.rotate_turn(
            session_id,
            resolution_from_meta=resolution_from_meta,
            target_from_meta=daemon_target_from_session,
            prompt=request.prompt,
            resume_status=resume_status,
            pid=pid if pid is not None else _host_pid(),
            steer_mode=steer_mode,
        )
    except (RunnerError, sessions.SessionError) as error:
        raise ResumeRotationError(str(error)) from None
    try:
        resolution = resolution_from_session(rotated)
        cwd = rotated.resolution.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            raise RunnerError(f"session {session_id} has an invalid stored working directory")
        if cwd is None:
            cwd = request.cwd
    except RunnerError as error:
        _finalize_claimed_setup_failure(session_id, rotated.turns, error)
        raise ResumeRotationError(str(error), turn_token=rotated.turns) from None
    try:
        if client is None:
            events.append("state", **{"from": "starting", "to": "running"})
        else:
            client.turn_number = rotated.turns
            client.record_external_event("state", **{"from": "starting", "to": "running"})
        return replace(
            request,
            resolution=resolution,
            cwd=cwd,
            defer_rotation=False,
            rotation_resolution=None,
            resume_prepared=True,
            turn_token=rotated.turns,
        )
    except BaseException as error:
        _finalize_claimed_setup_failure(session_id, rotated.turns, error)
        if isinstance(error, asyncio.CancelledError):
            raise
        raise ResumeSetupError(describe_error(error), turn_token=rotated.turns) from None


def _finalize_claimed_setup_failure(
    session_id: str,
    turn_token: int,
    error: BaseException,
    adapter_session_id: str | None = None,
    *,
    advertised: dict[str, Any] | None = None,
    rate_windows: dict[str, Any] | None = None,
) -> None:
    """Close a claimed turn when setup failed before an owner was installed."""
    _finalize(
        session_id,
        TurnOutcome(
            state="failed",
            stop_reason="error",
            answer="",
            adapter_session_id=adapter_session_id,
            error=error,
            advertised=advertised or {},
            rate_windows=rate_windows,
            turn_token=turn_token,
        ),
        error=error,
        expected_turn=turn_token,
    )


# What most adapters call their effort session config option; entries override
# it via `effort_config_id` (claude speaks `effort`).
_DEFAULT_EFFORT_CONFIG_ID = "reasoning_effort"


async def apply_call_options(
    conn: Any,
    adapter_session_id: str,
    request: TurnRequest,
    *,
    observer: AcpcClient | Callable[[list[Any], str | None], None] | None = None,
) -> None:
    """Apply resolved mode/model/effort to the adapter session before prompting."""
    resolution = request.resolution
    await _configure(
        conn.set_session_mode(session_id=adapter_session_id, mode_id=resolution.mode),
        f"mode {resolution.mode!r}",
    )
    if resolution.model is not None:
        if resolution.entry.model_via == "set_model":
            await _configure(
                _set_session_model(conn, adapter_session_id, resolution.model),
                f"model {resolution.model!r} (session/set_model)",
            )
        else:
            response = await _configure(
                conn.set_config_option(
                    config_id="model",
                    session_id=adapter_session_id,
                    value=resolution.model,
                ),
                f"model {resolution.model!r}",
            )
            observe_config_options(observer, response, resolution.model)
    # effort_via=cli is applied on the spawn argv (CallResolution.command).
    if resolution.effort is not None and resolution.entry.effort_via != "cli":
        effort_config_id = resolution.entry.effort_config_id or _DEFAULT_EFFORT_CONFIG_ID
        response = await _configure(
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
        observe_config_options(observer, response, resolution.model)


def observe_config_options(
    observer: AcpcClient | Callable[[list[Any], str | None], None] | None,
    response: Any,
    model_key: str | None,
) -> None:
    """Forward options only when the response actually carries them."""
    from acpc.client import AcpcClient  # import floor

    options = getattr(response, "config_options", None)
    if options is None or observer is None:
        return
    if isinstance(observer, AcpcClient):
        observer.capture_config_options(options, model_key=model_key)
    else:
        observer(options, model_key)


async def _set_session_model(conn: Any, session_id: str, model_id: str) -> Any:
    """Set the session model via ACP ``session/set_model`` (not config options).

    Some agents (Grok Build) advertise models on session/new but reject
    ``session/set_config_option``. The typed SDK may omit a set_model helper;
    fall back to the raw JSON-RPC connection.
    """
    set_model = getattr(conn, "set_model", None)
    if callable(set_model):
        typed = cast("Callable[..., Awaitable[Any]]", set_model)
        return await typed(session_id=session_id, model_id=model_id)
    raw = getattr(conn, "_conn", None)
    if raw is None or not hasattr(raw, "send_request"):
        raise AdapterRejection(
            "the adapter requested model_via=set_model but the connection "
            "has no session/set_model path"
        )
    return await raw.send_request(
        "session/set_model",
        {"sessionId": session_id, "modelId": model_id},
    )


class AdapterRejection(RuntimeError):
    """An adapter refused a session option; the message carries the detail.

    Deliberately not a RunnerError: it fails the turn like any other adapter
    error, so the message lands in answer.md and the transcript."""


async def _configure(call: Any, what: str, *, unknown_option_hint: str | None = None) -> Any:
    """Name the rejected option: the bare JSON-RPC reply doesn't say which.

    An adapter that does not offer an option at all answers with a generic
    internal error, so the vendor's own wording is the only signal that the
    option is missing rather than the value wrong. When it says so, the hint
    names what the caller can actually change.
    """
    from acp import RequestError  # import floor

    try:
        return await call
    except RequestError as error:
        detail = describe_error(error)
        message = f"the adapter rejected {what}: {detail}"
        if unknown_option_hint is not None and "unknown config option" in detail.lower():
            message += f" — {unknown_option_hint}"
        raise AdapterRejection(message) from error


async def _await_prompt(
    conn: Any,
    adapter_session_id: str,
    prompt_task: asyncio.Task[Any],
    request: TurnRequest,
    cancel: _CancelSignal,
    *,
    usage_client: Any | None = None,
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
            timeout=request.cancel_after,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if prompt_task in done:
            return _stop_reason_of(prompt_task, usage_client=usage_client)

        # Either the cancellation deadline expired or a signal asked us to
        # wind down.  A client-side wait deadline is handled by the daemon
        # route, because direct children cannot outlive this process.
        if not cancel.requested.is_set():
            cancel.request("canceled", stop_reason="cancel_after")
        cancel.cancellation_dispatched.set()
        with contextlib.suppress(Exception):
            await conn.cancel(session_id=adapter_session_id)
        with contextlib.suppress(TimeoutError, asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(prompt_task), timeout=CANCEL_ACK_TIMEOUT)
        if prompt_task.done():
            return _stop_reason_of(prompt_task, usage_client=usage_client)
        prompt_task.cancel()
        return "canceled"
    finally:
        waiter.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await waiter


def _stop_reason_of(
    prompt_task: asyncio.Task[Any],
    *,
    usage_client: Any | None = None,
) -> str | None:
    """Read a finished prompt task's stop reason, preserving adapter errors."""
    error = prompt_task.exception() if not prompt_task.cancelled() else None
    if prompt_task.cancelled():
        return "canceled"
    if error is not None:
        raise error
    result = prompt_task.result()
    if usage_client is not None:
        record = getattr(usage_client, "record_prompt_usage", None)
        if callable(record):
            record(result)
    return getattr(result, "stop_reason", None)


# SPEC.md `run`: sent instead of the original prompt when a limit interrupted
# it after the turn had already recorded progress, so the adapter continues
# the same task rather than restarting it. Shared with `continue` without a
# message (slice 19), which names the actual cause instead of always blaming
# a usage limit (slice 23 review debt).
_CONTINUATION_TEXTS = {
    "rate_limit": (
        "acpc: the previous request in this turn was interrupted by a usage limit that has now "
        "reset. Continue the task from where you stopped. Do not repeat work that is already "
        "done; if the task was already complete, reply with the final answer."
    ),
    "canceled": (
        "acpc: the previous turn of this session was canceled before it finished. Continue the "
        "task from where it stopped. Do not repeat work that is already done; if the task was "
        "already complete, reply with the final answer."
    ),
    "failed": (
        "acpc: the previous turn of this session ended with an error before it finished. "
        "Continue the task from where it stopped. Do not repeat work that is already done; if "
        "the task was already complete, reply with the final answer."
    ),
    "unknown": (
        "acpc: the previous turn of this session was interrupted and its outcome was not "
        "observed. Continue the task from where it stopped. Do not repeat work that is already "
        "done; if the task was already complete, reply with the final answer."
    ),
}


def continuation_instruction(cause: str) -> str:
    """SPEC.md `continue`: the fixed instruction naming how the previous turn ended.

    `cause` is one of `rate_limit`, `canceled`, `failed` or `unknown` — the
    limit resumption always passes `rate_limit`; `continue` without a message
    passes the session's own last terminal state.
    """
    return _CONTINUATION_TEXTS[cause]


# How long acpc waits past a limit's own reported reset before resuming: a
# fixed cushion for clock skew against the vendor's clock.
LIMIT_RESUME_FIXED_DELAY_SECONDS = 5.0


def _limit_wait_max_seconds(request: TurnRequest) -> float:
    from acpc.config import load_config, parse_duration

    if request.limit_wait_max is not None:
        return request.limit_wait_max
    try:
        entry = AgentRegistry().resolve(request.resolution.entry.entry)
    except RegistryError:
        entry = None
    if entry is not None and entry.limit_wait_max is not None:
        return parse_duration(entry.limit_wait_max, allow_zero=True)
    return load_config().limit_wait_max_seconds


def _limit_record(observation: Any, *, auto_continue: bool) -> dict[str, Any]:
    resume_at = observation.resume_at
    return {
        "reason": observation.reason,
        "resume_at": sessions.format_timestamp(resume_at.timestamp()) if resume_at else None,
        "auto_continue": auto_continue,
        "source": observation.source,
    }


def _limit_action(observation: Any, *, limit_waited_seconds: float, limit_wait_max: float) -> str:
    """Decide whether to wait through a recognized limit or fail the turn now.

    `limit_wait_max == 0` (SPEC.md *State on disk*: `limit_wait_max = "0s"`)
    always fails, even when the reported reset is already due — a zero cap
    means no wait was ever authorized, not a wait of length zero.
    """
    if observation.resume_at is None or limit_wait_max <= 0:
        return "fail"
    wait_needed = (observation.resume_at - datetime.now(UTC)).total_seconds()
    if wait_needed > limit_wait_max - limit_waited_seconds:
        return "fail"
    return "wait"


async def _sleep_through_limit(seconds: float, cancel: _CancelSignal) -> bool:
    """Sleep, interruptible by `cancel`. Returns True when canceled first."""
    sleeper = asyncio.ensure_future(asyncio.sleep(max(0.0, seconds)))
    waiter = asyncio.ensure_future(cancel.requested.wait())
    try:
        done, _pending = await asyncio.wait({sleeper, waiter}, return_when=asyncio.FIRST_COMPLETED)
        return waiter in done
    finally:
        for task in (sleeper, waiter):
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


async def run_prompt_with_limits(
    conn: Any,
    session_id: str,
    adapter_session_id: str,
    request: TurnRequest,
    cancel: _CancelSignal,
    client: AcpcClient,
    *,
    on_delivered: Callable[[], None] | None = None,
) -> tuple[str | None, BaseException | None, PromptDelivery, dict[str, Any] | None]:
    """Send the prompt, waiting through recognized usage limits within `limit_wait_max`.

    Replaces one bare ``_await_prompt`` call with a loop that recognizes a
    vendor usage limit (SPEC.md `run`), and either fails the turn with
    ``stop_reason: rate_limit`` or moves the session to ``waiting``, sleeps
    past the reported reset, and resends on the same adapter session — the
    original prompt if this turn recorded no progress yet, acpc's fixed
    continuation instruction otherwise. Returns the same
    ``(stop_reason, turn_error, delivery)`` shape the two callers previously
    built inline, plus the final ``limit`` record for ``meta.json`` (``None``
    when no limit ever touched this turn).
    """
    limit_wait_max = _limit_wait_max_seconds(request)
    limit_waited_seconds = 0.0
    limit_record: dict[str, Any] | None = None
    prompt_text = request.prompt
    delivered_on_first_send = on_delivered

    while True:
        # A rejection's rate-limit metadata describes that one send; carrying
        # it into the next send's classification would misread a genuine
        # success as a repeat of the limit that already resolved.
        activity_before = client.begin_prompt_send()
        client.clear_rate_limit_info()
        delivery = register_prompt_delivery(
            conn, session_id, adapter_session_id, prompt_text, on_delivered=delivered_on_first_send
        )
        # Only the very first send in a turn reaches the daemon's "running"
        # phase callback; a resend after a limit is still the same turn.
        delivered_on_first_send = None
        prompt_task = asyncio.create_task(
            conn.prompt(session_id=adapter_session_id, prompt=[_prompt_block(prompt_text)])
        )
        turn_error: BaseException | None = None
        try:
            stop_reason = await _await_prompt(
                conn, adapter_session_id, prompt_task, request, cancel, usage_client=client
            )
        except Exception as caught:  # noqa: BLE001
            turn_error = caught
            stop_reason = "error"
        try:
            await delivery.ensure_persisted(prompt_completed=turn_error is None)
        except Exception as caught:  # noqa: BLE001
            turn_error = caught if turn_error is None else turn_error
            stop_reason = "error"

        # SPEC.md `run`: a limit is recognized on a failed `session/prompt`; a
        # prompt the adapter completed is never reclassified from metadata.
        if cancel.requested.is_set() or not _is_request_error(turn_error):
            return stop_reason, turn_error, delivery, limit_record

        observation = limits.classify_limit(turn_error, client.rate_limit_info, datetime.now(UTC))
        if observation is None:
            return stop_reason, turn_error, delivery, limit_record

        client.record_limit_rejection(activity_before)

        action = _limit_action(
            observation,
            limit_waited_seconds=limit_waited_seconds,
            limit_wait_max=limit_wait_max,
        )
        if action == "fail":
            limit_record = _limit_record(observation, auto_continue=False)
            client.record_external_event(
                "limit",
                reason=observation.reason,
                resume_at=limit_record["resume_at"],
                action="fail",
                source=observation.source,
                detail=observation.detail,
            )
            return "rate_limit", turn_error, delivery, limit_record

        limit_record = _limit_record(observation, auto_continue=True)
        client.record_external_event(
            "limit",
            reason=observation.reason,
            resume_at=limit_record["resume_at"],
            action="wait",
            source=observation.source,
            detail=observation.detail,
        )
        client.record_external_event("state", **{"from": "running", "to": "waiting"})
        sessions.transition(session_id, "waiting", limit=limit_record)
        now = datetime.now(UTC)
        assert observation.resume_at is not None  # action == "wait" guarantees this
        delay = (
            max(0.0, (observation.resume_at - now).total_seconds())
            + LIMIT_RESUME_FIXED_DELAY_SECONDS
        )
        started = time.monotonic()
        canceled = await _sleep_through_limit(delay, cancel)
        limit_waited_seconds += time.monotonic() - started
        if canceled:
            # SPEC.md `cancel`: a session in `waiting` is canceled without
            # contacting the adapter — the scheduled resumption is dropped.
            limit_record = {**limit_record, "auto_continue": False}
            sessions.update_meta(session_id, limit=limit_record)
            return "canceled", turn_error, delivery, limit_record

        client.record_external_event("state", **{"from": "waiting", "to": "running"})
        sessions.transition(session_id, "running", limit=limit_record)
        prompt_text = (
            request.prompt
            if not client.has_recorded_progress
            else continuation_instruction("rate_limit")
        )


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    cancel: _CancelSignal,
) -> None:
    """Route SIGINT/SIGTERM into the turn's cancel path.

    SIGTERM detaches only when the daemon owns the session; an ordinary direct
    child dies with its parent, so there it cancels (SPEC.md *Output contract*).
    """
    if sys.platform == "win32":
        return
    with contextlib.suppress(NotImplementedError, RuntimeError):
        loop.add_signal_handler(signal.SIGINT, lambda: cancel.request_signal(signal.SIGINT))
    with contextlib.suppress(NotImplementedError, RuntimeError):
        loop.add_signal_handler(signal.SIGTERM, lambda: cancel.request_signal(signal.SIGTERM))


def daemon_payload(request: TurnRequest) -> dict[str, Any]:
    """The serializable shape of a turn, for the daemon to rebuild.

    Only the call's own inputs travel: the daemon reads the same `ACPC_HOME`,
    so it re-resolves the entry itself rather than trusting a resolution that
    crossed a socket.
    """
    resolution = request.resolution
    try:
        cwd = str(Path(request.cwd).expanduser().resolve()) if request.cwd else os.getcwd()
    except OSError as error:
        raise RunnerError(
            f"cannot resolve the daemon dispatch working directory: {error}"
        ) from None
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
        "cwd": cwd,
        "cancel_after": request.cancel_after,
        "limit_wait_max": request.limit_wait_max,
        "prompt": request.prompt,
        "resume_adapter_session": request.resume_adapter_session,
        "defer_rotation": request.defer_rotation,
        "rotation_resolution": dict(request.rotation_resolution)
        if request.rotation_resolution is not None
        else None,
        "resume_prepared": request.resume_prepared,
        "turn_token": request.turn_token,
        # The prompt's source and size, never its text: the daemon reads the
        # text from the session's `system.md`.
        "system_prompt": (
            system_prompt_payload(resolution.system_prompt)
            if resolution.system_prompt is not None
            else None
        ),
        "tools": list(resolution.tools) if resolution.tools is not None else None,
        "system_prompt_via": resolution.entry.system_prompt_via,
        "tools_via": resolution.entry.tools_via,
        "one_shot": resolution.one_shot,
        "setting_sources": (
            list(resolution.setting_sources) if resolution.setting_sources is not None else None
        ),
    }


def routes_direct(request: TurnRequest) -> str | None:
    """Why this call cannot use the daemon, or None when it can.

    An `ask` policy needs a terminal to ask on and the daemon has none, so
    such a call stays a direct child even when a daemon is available.
    """
    if os.environ.get(_DIRECT_WORKER_ENV) == "1":
        return "the detached direct worker owns this session"
    if request.permission_prompt is not None or request.resolution.permissions == "ask":
        return "--permissions ask needs this terminal"
    return None


async def _route(request: TurnRequest) -> tuple[Any | None, str | None]:
    """Pick the daemon or the direct path, and say so when it is the latter."""
    forced = routes_direct(request)
    if forced is not None:
        return None, f"direct child ({forced})"
    routed = await daemon_client.ensure_daemon(daemon_target(request.resolution))
    if isinstance(routed, daemon_client.DaemonUnavailable):
        return None, f"direct child ({routed.reason})"
    return routed, None


async def _execute(session_id: str, request: TurnRequest) -> TurnOutcome:
    """Run the turn and record whether this process's Ctrl-C ended it."""
    cancel = _CancelSignal()
    outcome = await _execute_routed(session_id, request, cancel)
    outcome.interrupted = cancel.received_signal == signal.SIGINT
    return outcome


async def _execute_routed(
    session_id: str, request: TurnRequest, cancel: _CancelSignal
) -> TurnOutcome:
    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, cancel)

    routing = asyncio.create_task(_route(request), name="acpc.route")
    signalled = asyncio.create_task(cancel.requested.wait(), name="acpc.route-cancel")
    try:
        done, _pending = await asyncio.wait(
            {routing, signalled}, return_when=asyncio.FIRST_COMPLETED
        )
        if routing not in done:
            routing.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await routing
            return await _cancel_before_route_acceptance(session_id, request, cancel)
        daemon, route_note = routing.result()
        cancel.resolve_route(daemon_routed=daemon is not None)
    finally:
        signalled.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await signalled

    if daemon is not None:
        target = daemon_target(request.resolution)
        return await _execute_via_daemon(session_id, request, daemon, cancel, target)

    if request.wait_timeout is not None:
        return await _execute_direct_with_wait_timeout(
            session_id,
            request,
            route_note=route_note,
        )

    if request.defer_rotation:
        try:
            async with sessions.session_reservation(session_id):
                return await _execute_direct(session_id, request, cancel, route_note=route_note)
        except sessions.SessionStateError as error:
            # Same classification as the daemon's: a held session is a conflict
            # the caller can retry, not an adapter failure.
            raise ResumePreparationError(str(error), kind=errors.CONFLICT) from None

    return await _execute_direct(session_id, request, cancel, route_note=route_note)


async def _cancel_before_route_acceptance(
    session_id: str, request: TurnRequest, cancel: _CancelSignal
) -> TurnOutcome:
    """Record a pre-route cancellation without overwriting an earlier turn."""
    cancel.resolve_route(daemon_routed=False)
    if request.defer_rotation:
        try:
            async with sessions.session_reservation(session_id):
                events = transcript.Transcript(sessions.transcript_path(session_id))
                request = _prepare_resumed_turn(
                    session_id,
                    request,
                    events,
                    pid=_host_pid(),
                    steer_mode=vocab.STEER_CANCEL_THEN_START,
                )
        except (ResumeRotationError, sessions.SessionError) as error:
            raise ResumePreparationError(str(error)) from None

    outcome = TurnOutcome(
        state=cancel.state or "canceled",
        stop_reason=PREPARATION_CANCELLED_REASON,
        answer=preparation_cancelled_answer(session_id),
        turn_token=request.turn_token,
    )
    _finalize(session_id, outcome, expected_turn=request.turn_token)
    return outcome


async def _execute_direct(
    session_id: str,
    request: TurnRequest,
    cancel: _CancelSignal,
    *,
    route_note: str | None,
) -> TurnOutcome:
    """Run the direct path after routing and, for continue, reservation."""
    events = transcript.Transcript(sessions.transcript_path(session_id))
    if not request.defer_rotation:
        sessions.mark_running(
            session_id,
            pid=_host_pid(),
            steer_mode=vocab.STEER_CANCEL_THEN_START,
        )
        events.append("state", **{"from": "starting", "to": "running"})
    outcome = await _drive_turn(session_id, request, events, cancel)
    outcome.route_note = route_note
    return outcome


def _direct_request_path(session_id: str) -> Path:
    return sessions.session_dir(session_id) / _DIRECT_REQUEST_NAME


def _write_direct_request(session_id: str, request: TurnRequest) -> None:
    """Persist the direct worker's request before detaching it."""
    worker_request = replace(request, wait_timeout=None)
    paths.atomic_write(
        _direct_request_path(session_id),
        json.dumps(daemon_payload(worker_request), ensure_ascii=False),
    )


def start_direct_worker(session_id: str, request: TurnRequest) -> int:
    """Start a detached direct worker and return its process id.

    The worker gets a new process session or group and no terminal streams, so
    closing the observing client's terminal cannot signal the accepted turn.
    """
    _write_direct_request(session_id, request)
    argv = (sys.executable, "-m", "acpc.direct_worker", session_id)
    env = dict(os.environ)
    env[_DIRECT_WORKER_ENV] = "1"
    process = subprocess.Popen(
        argv,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **process_group_kwargs(),
    )
    return process.pid


async def _execute_direct_with_wait_timeout(
    session_id: str,
    request: TurnRequest,
    *,
    route_note: str | None,
) -> TurnOutcome:
    """Observe a detached direct worker until it finishes or the wait expires."""
    initial_turns = sessions.read_meta(session_id).turns
    try:
        start_direct_worker(session_id, request)
    except OSError as error:
        raise RunnerError(f"could not start the direct timeout worker: {error}") from None

    wait_timeout = request.wait_timeout
    if wait_timeout is None:
        raise RunnerError("direct timeout worker started without a wait timeout")
    deadline = asyncio.get_running_loop().time() + wait_timeout
    while True:
        current = sessions.load(session_id)
        turn_started = not request.defer_rotation or current.turns > initial_turns
        if turn_started and current.is_finished:
            return TurnOutcome(
                state=current.state,
                stop_reason=current.stop_reason,
                answer=_answer_on_disk(session_id),
                context=current.context,
                finalized_elsewhere=True,
                route_note=route_note,
            )
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return TurnOutcome(
                state=current.state,
                stop_reason=current.stop_reason,
                answer="",
                finalized_elsewhere=True,
                wait_timed_out=True,
                route_note=route_note,
            )
        await asyncio.sleep(min(0.02, remaining))


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
    deadline = (
        None
        if request.wait_timeout is None
        else asyncio.get_running_loop().time() + request.wait_timeout
    )
    starting = asyncio.ensure_future(daemon.start_turn(session_id, daemon_payload(request)))
    signalled = asyncio.ensure_future(cancel.requested.wait())
    try:
        done, _pending = await asyncio.wait(
            {starting, signalled},
            timeout=_remaining_wait(deadline),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if starting not in done:
            if signalled in done and cancel.state == "detached":
                try:
                    await asyncio.wait_for(asyncio.shield(starting), timeout=CANCEL_ACK_TIMEOUT)
                except TimeoutError:
                    starting.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await starting
                return TurnOutcome(
                    state="detached",
                    stop_reason=None,
                    answer="",
                    finalized_elsewhere=True,
                )
            starting.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await starting
            if signalled in done and cancel.state != "detached":
                await _cancel_daemon_turn(target, session_id)
                return TurnOutcome(
                    state="canceled",
                    stop_reason=cancel.stop_reason or "canceled",
                    answer="",
                    finalized_elsewhere=True,
                )
            return TurnOutcome(
                state=cancel.state or "running",
                stop_reason=cancel.stop_reason,
                answer="",
                finalized_elsewhere=True,
                wait_timed_out=signalled not in done,
            )
        started = starting.result()
        if not started.get("ok"):
            reason = str(started.get("error", "the daemon refused the turn"))
            refused_as = started.get("kind")
            kind = str(refused_as) if isinstance(refused_as, str) else None
            if started.get("preserve_session"):
                raise ResumePreparationError(reason, kind=kind)
            raise RunnerError(reason, kind=kind)
        queued = bool(started.get("queued"))
        remaining = _remaining_wait(deadline)
        if remaining is not None and remaining <= 0:
            return TurnOutcome(
                state="starting",
                stop_reason=None,
                answer="",
                finalized_elsewhere=True,
                queued=queued,
                wait_timed_out=True,
            )

        waiting = asyncio.ensure_future(daemon.await_turn(session_id))
        done, _pending = await asyncio.wait(
            {waiting, signalled},
            timeout=remaining,
            return_when=asyncio.FIRST_COMPLETED,
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
            if not cancel.requested.is_set():
                waiting.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await waiting
                return TurnOutcome(
                    state="running",
                    stop_reason=None,
                    answer="",
                    finalized_elsewhere=True,
                    queued=queued,
                    wait_timed_out=True,
                )
            await _cancel_daemon_turn(target, session_id)
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
        signalled.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await signalled
        with contextlib.suppress(Exception):
            await daemon.close()


def _remaining_wait(deadline: float | None) -> float | None:
    """Return the remaining client wait, or ``None`` for an unbounded wait."""
    if deadline is None:
        return None
    return deadline - asyncio.get_running_loop().time()


async def _cancel_daemon_turn(target: str, session_id: str) -> sessions.SessionMeta | None:
    """Cancel a daemon turn without using the connection stuck in ``start``."""
    with contextlib.suppress(TimeoutError, Exception):
        await asyncio.wait_for(
            daemon_client.cancel_turn(target, session_id), timeout=CANCEL_ACK_TIMEOUT
        )
    deadline = asyncio.get_running_loop().time() + CANCEL_ACK_TIMEOUT
    while True:
        try:
            current = sessions.load(session_id)
        except sessions.SessionError:
            return None
        if not current.is_active or asyncio.get_running_loop().time() >= deadline:
            return current
        await asyncio.sleep(0.02)


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


async def dispatch_background(
    session_id: str, request: TurnRequest
) -> str | daemon_client.DaemonUnavailable | None:
    """Start a turn the caller will not wait for; None on success.

    `--background` needs an owner that outlives this process, which is exactly what the
    daemon is. Without one there is nobody to hand the session to, so this
    reports why instead of silently running a child that dies on exit — as a
    `DaemonUnavailable` (SPEC.md `daemon`: reported to the caller as
    `unavailable`), kept apart from a plain string once the daemon accepted
    the turn and then refused it, which is an ordinary agent failure.
    """
    forced = routes_direct(request)
    if forced is not None:
        return daemon_client.DaemonUnavailable(f"--background needs the daemon, and {forced}")
    routed = await daemon_client.ensure_daemon(daemon_target(request.resolution))
    if isinstance(routed, daemon_client.DaemonUnavailable):
        return daemon_client.DaemonUnavailable(f"--background needs the daemon: {routed.reason}")
    try:
        started = await routed.start_turn(session_id, daemon_payload(request))
        if not started.get("ok"):
            return str(started.get("error", "the daemon refused the turn"))
        if request.defer_rotation:
            prepared = await routed.await_preparation(session_id)
            if not prepared.get("ok"):
                return str(prepared.get("error", "the daemon refused the turn"))
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
        outcome = run_with_sigint_deferred(
            lambda: _execute(session_id, request),
            installs_sigint_handler=True,
        )
    except ResumePreparationError as error:
        # Verification happens before a deferred continuation rotates the
        # session, so preserve the finished session exactly as it was.
        if request.defer_rotation:
            raise
        _finalize(
            session_id,
            TurnOutcome(state="failed", stop_reason="error", answer=""),
            error=error,
        )
        raise
    except ResumeSetupError as error:
        _finalize_claimed_setup_failure(session_id, error.turn_token, error)
        raise
    except ResumeRotationError as error:
        # A competing continuation may have claimed this session first. It has
        # no turn token, so it must not finalize the winner's active turn.
        if error.turn_token is not None:
            _finalize(
                session_id,
                TurnOutcome(state="failed", stop_reason="error", answer=""),
                error=error,
                expected_turn=error.turn_token,
            )
        raise
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
        _finalize(session_id, outcome, error=outcome.error, expected_turn=outcome.turn_token)
    return outcome


def _direct_worker_request(session_id: str) -> TurnRequest:
    """Read a detached direct request from its owner-only session file."""
    request_path = _direct_request_path(session_id)
    try:
        payload = json.loads(request_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("direct worker request has an invalid shape")
        meta = sessions.read_meta(session_id)
        request = TurnRequest(
            resolution=resolution_from_session(meta),
            prompt=payload.get("prompt", ""),
            cwd=payload.get("cwd"),
            cancel_after=payload.get("cancel_after"),
            limit_wait_max=payload.get("limit_wait_max"),
            resume_adapter_session=payload.get("resume_adapter_session"),
            defer_rotation=bool(payload.get("defer_rotation", False)),
            rotation_resolution=payload.get("rotation_resolution"),
            resume_prepared=bool(payload.get("resume_prepared", False)),
            turn_token=payload.get("turn_token"),
        )
        return request
    except (OSError, ValueError, TypeError, KeyError, RunnerError, sessions.SessionError) as error:
        failure = RunnerError(f"cannot read the direct worker request: {error}")
        _finalize(
            session_id,
            TurnOutcome(state="failed", stop_reason="error", answer=""),
            error=failure,
        )
        raise failure from None
    finally:
        with contextlib.suppress(OSError):
            request_path.unlink()


def run_direct_worker(session_id: str, request: TurnRequest | None = None) -> None:
    """Run a detached direct request from its owner-only session file."""
    request = request or _direct_worker_request(session_id)
    execute_turn(session_id, request)


def _finalize(
    session_id: str,
    outcome: TurnOutcome,
    *,
    error: BaseException | None = None,
    adapter_log_from: int | None = None,
    expected_turn: int | None = None,
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
        if expected_turn is not None and current.turns != expected_turn:
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
    state = outcome.state
    if state == "terminated":
        state = "canceled"
    if state == "detached":
        state = "running"

    error_event = failure.error_event() if failure is not None else None

    try:
        finalized = sessions.finalize_turn(
            session_id,
            state,
            answer=answer,
            expected_turn=expected_turn,
            exit_code=outcome.exit_code,
            stop_reason=outcome.stop_reason,
            context=outcome.context,
            usage=(
                outcome.usage
                if outcome.usage_finalized
                else (current.usage if current is not None else None)
            ),
            denied=outcome.denied,
            denial_details=outcome.denial_details,
            error_event=error_event,
            failure=(
                _single_line(_strip_ansi(failure.observation))[:MESSAGE_TAIL_CHARS]
                if failure is not None
                else None
            ),
            failure_class=failure.failure_class if failure is not None else None,
            failure_next_step=failure.next_step if failure is not None else None,
            served_model=(
                outcome.served_model
                if outcome.served_model is not None
                else (current.served_model if current is not None else None)
            ),
            delivery_record_incomplete=outcome.delivery_record_incomplete,
            reduce=_reduces_at_end(current),
            adapter_session_id=(
                outcome.adapter_session_id
                if outcome.adapter_session_id is not None
                else (current.adapter_session_id if current is not None else None)
            ),
            # A turn ended from outside the prompt loop (daemon stop, a setup
            # failure) carries no record of its own; the one on disk stays.
            limit=(
                outcome.limit
                if outcome.limit is not None
                else (current.limit if current is not None else None)
            ),
        )
    except sessions.SessionError:
        return
    if finalized is None:
        return

    _after_finalize(finalized, outcome)


def _after_finalize(finalized: sessions.SessionMeta, outcome: TurnOutcome) -> None:
    """What follows a written answer and final `meta.json`; it may not fail finalization."""
    if outcome.advertised:
        with contextlib.suppress(Exception):
            cache.refresh_advertised(_agent_of(finalized.session_id), outcome.advertised)
    with contextlib.suppress(Exception):
        home = _stored_value(finalized.resolution, "home")
        cache.refresh_windows(
            finalized.base_adapter, home, outcome.rate_windows, session_id=finalized.session_id
        )


def _reduces_at_end(current: sessions.SessionMeta | None) -> bool:
    """Whether ending a turn of this session consumes it (a one-shot session).

    The session's own snapshot, not the live entry, decides.
    A snapshot that cannot be read leaves the session whole.
    """
    if current is None:
        return False
    try:
        return stored_one_shot(current)
    except RunnerError:
        return False


@dataclass(frozen=True, slots=True)
class _FailureDetails:
    """The observable diagnosis recorded for a failed turn."""

    message: str
    observation: str
    next_step: str | None
    failure_class: str
    adapter_log: str | None = None
    adapter_log_tail: str | None = None

    def error_event(self) -> dict[str, Any]:
        """The transcript `error` event's fields."""
        event: dict[str, Any] = {
            "message": self.message,
            "observation": self.observation,
            "next_step": self.next_step,
            "class": self.failure_class,
        }
        if self.adapter_log is not None:
            event["adapter_log"] = self.adapter_log
        if self.adapter_log_tail is not None:
            event["adapter_log_tail"] = self.adapter_log_tail
        return event


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
    failure_class = _failure_class(
        error,
        outcome.stop_reason,
        authentication_refused=authentication_refused,
        ended_by_acpc=isinstance(error, TurnEndedByAcpc),
    )
    if failure_class == "acpc":
        if isinstance(error, TurnEndedByAcpc):
            next_step = f"continue the session: acpc continue {session_id}"
        elif isinstance(error, AdapterUnavailable):
            next_step = str(error)
        else:
            next_step = None
    elif failure_class == "rate_limit":
        limit = outcome.limit or (meta.limit if meta is not None else None)
        resume_at = limit.get("resume_at") if limit else None
        next_step = (
            f"retry after {resume_at}, or raise the limit wait cap"
            if resume_at
            else "retry later; acpc cannot tell when the limit lifts"
        )
    elif authentication_refused and meta is not None:
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
    if next_step is not None:
        parts.append(f"next step: {next_step}")
    return _FailureDetails(
        message="; ".join(parts),
        observation=observation,
        next_step=next_step,
        failure_class=failure_class,
        adapter_log=str(adapter_log) if adapter_log is not None else None,
        adapter_log_tail=adapter_log_tail,
    )


def _failure_class(
    error: BaseException | None,
    stop_reason: str | None,
    *,
    authentication_refused: bool,
    ended_by_acpc: bool,
) -> str:
    """Classify observed failures without reading session state."""
    if ended_by_acpc:
        return "acpc"
    if authentication_refused:
        return "auth"
    if stop_reason == "rate_limit":
        return "rate_limit"
    if stop_reason in _ADAPTER_LIMIT_STOP_REASONS:
        return "adapter_limit"
    text = describe_error(error).lower() if error is not None else ""
    if (
        isinstance(error, ConnectionError)
        or getattr(error, "handshake_timeout", False)
        or any(marker in text for marker in ("connection closed", "broken pipe", "eof"))
    ):
        return "connection"
    if isinstance(error, RunnerError) or getattr(error, "acpc_refusal", False):
        return "acpc"
    return "adapter_error"


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
    from acp import RequestError  # import floor

    if isinstance(error, AdapterRejection):
        return _is_authentication_failure(error.__cause__)
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
    decoded = _strip_ansi(raw.decode("utf-8", errors="ignore")).strip()
    return decoded or None


def _strip_ansi(text: str) -> str:
    """Remove terminal control sequences without discarding surrounding text."""
    return _ANSI_ESCAPE_RE.sub("", text).replace("\x1b", "").replace("\x07", "")


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


def effort_levels_payload(resolution: CallResolution) -> dict[str, list[str]]:
    """The selectable effort values and the factors that bounded them."""
    values, bounded_by = resolution.entry.allowed_efforts(resolution.model)
    return {"values": list(values), "bounded_by": list(bounded_by)}


def resolution_payload(
    resolution: CallResolution, *, cwd: str | None, permissions_source: str | None = None
) -> dict[str, Any]:
    """The `--resolve` view: every resolved value and where it came from.

    `permissions_source` names an origin provenance cannot see, because the
    policy did not come from the entry, a flag or acpc's own default — it was
    typed at a prompt, or it will be, once this call is really dispatched.
    """
    entry = resolution.entry
    provenance: Mapping[str, Any] = resolution.provenance
    fields: dict[str, Any] = {
        "model": resolution.model,
        "effort": resolution.effort,
        "mode": resolution.mode,
        "permissions": resolution.permissions,
        "home": resolution.home,
        "data_residency": resolution.entry.data_residency,
    }
    resolved: dict[str, dict[str, Any]] = {
        name: {
            "value": value,
            "source": _source_label(provenance.get(name)),
        }
        for name, value in fields.items()
    }
    resolved["effort"]["levels"] = effort_levels_payload(resolution)
    if permissions_source is not None:
        resolved["permissions"]["source"] = permissions_source
    if resolution.permissions_clamp is not None:
        requested, ceiling = resolution.permissions_clamp
        permissions = resolved["permissions"]
        permissions["clamp"] = {
            "requested": requested,
            "ceiling": ceiling,
            "effective": resolution.permissions,
        }
    if resolution.mode_spec is not None:
        resolved["mode"]["grants"] = resolution.mode_spec.grants
        resolved["mode"]["delegates"] = resolution.mode_spec.delegates
        resolved["mode"]["escalates"] = resolution.mode_spec.escalates
        resolved["mode"]["selectable"] = resolution.mode_spec.selectable
    if resolution.system_prompt is not None:
        resolved["system_prompt"] = system_prompt_payload(resolution.system_prompt)
    if resolution.tools is not None:
        resolved["tools"] = {
            "value": list(resolution.tools),
            "source": _source_label(provenance.get("tools")),
        }
    if resolution.setting_sources is not None:
        resolved["setting_sources"] = {
            "value": list(resolution.setting_sources),
            "source": _source_label(provenance.get("setting_sources")),
        }
    resolved["one_shot"] = {
        "value": resolution.one_shot,
        "source": _source_label(provenance.get("one_shot")),
    }
    try:
        spawn_command = " ".join(resolution.command)
    except RegistryError:
        spawn_command = entry.command
    return {
        "entry": entry.entry,
        "base_adapter": entry.base_adapter,
        "system_prompt_via": entry.system_prompt_via,
        "tools_via": entry.tools_via,
        "command": spawn_command,
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


def system_prompt_payload(prompt: SystemPrompt) -> dict[str, Any]:
    """The one serialized shape of a system prompt: source and size, never its text."""
    return {"value": prompt.path, "source": prompt.source, "bytes": prompt.size}


def system_prompt_from_payload(raw: object, *, context: str) -> SystemPrompt:
    """Rebuild a `SystemPrompt` from `system_prompt_payload`; refuse any other shape."""
    path = raw.get("value") if isinstance(raw, Mapping) else None
    source = raw.get("source") if isinstance(raw, Mapping) else None
    size = raw.get("bytes") if isinstance(raw, Mapping) else None
    if (
        not isinstance(path, str)
        or source not in {"entry", "call"}
        or not isinstance(size, int)
        or isinstance(size, bool)
    ):
        raise RunnerError(f"{context} has an invalid stored system prompt")
    return SystemPrompt(path, source, size)


def tools_from_payload(raw: object, *, context: str) -> tuple[str, ...] | None:
    """Rebuild a tool list: None is "unset", an empty list is "no tools"."""
    if raw is None:
        return None
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise RunnerError(f"{context} has an invalid stored tool list")
    return tuple(raw)


def setting_sources_from_payload(raw: object, *, context: str) -> tuple[str, ...] | None:
    """Read the optional setting source list, including pre-key snapshots."""
    if raw is None:
        return None
    if (
        not isinstance(raw, list)
        or any(
            not isinstance(item, str) or item not in {"user", "project", "local"} for item in raw
        )
        or len(set(raw)) != len(raw)
    ):
        raise RunnerError(f"{context} has invalid stored setting_sources: {raw!r}")
    return tuple(raw)


def one_shot_from_payload(raw: object, *, context: str) -> bool:
    """Rebuild the stored `one_shot` fact; absent means `false`, anything but a boolean is refused."""
    if raw is None:
        return False
    if not isinstance(raw, bool):
        raise RunnerError(f"{context} has an invalid stored one_shot")
    return raw


def routes_from_payload(raw: Mapping[str, Any], *, context: str) -> tuple[str, str]:
    """The stored `system_prompt_via` and `tools_via`; an absent route is `none`."""
    prompt_via = raw.get("system_prompt_via", "none")
    if prompt_via not in SYSTEM_PROMPT_VIA_VALUES:
        raise RunnerError(f"{context} has an invalid stored system_prompt_via")
    tools_via = raw.get("tools_via", "none")
    if tools_via not in TOOLS_VIA_VALUES:
        raise RunnerError(f"{context} has an invalid stored tools_via")
    return prompt_via, tools_via


def session_resolution(
    resolution: CallResolution,
    *,
    cwd: str | None,
    permissions_source: str | None = None,
) -> dict[str, Any]:
    """The persisted session shape, distinct from the printed `--resolve` view.

    Stores the entry's **base** command (not CLI-injected spawn argv) so
    effort_via=cli can re-inject on continue without doubling flags.
    """
    payload = resolution_payload(resolution, cwd=cwd)
    payload["resolved"]["effort"].pop("levels", None)
    payload["command"] = resolution.entry.command
    if permissions_source is not None:
        payload["permissions_source"] = permissions_source
    adapter: dict[str, Any] = {
        "home_env": resolution.entry.home_env,
        "effort_config_id": resolution.entry.effort_config_id,
        "model_via": resolution.entry.model_via,
        "effort_via": resolution.entry.effort_via,
        "effort_cli_flag": resolution.entry.effort_cli_flag,
        "usage_profile": resolution.entry.usage_profile,
        "billing": resolution.entry.billing,
        "system_prompt_via": resolution.entry.system_prompt_via,
        "tools_via": resolution.entry.tools_via,
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


def record_usage_identity(session_id: str, identity: Mapping[str, Any]) -> None:
    """Persist initialize's adapter identity for later liveness-detected gaps."""
    name, version = identity.get("name"), identity.get("version")
    if not isinstance(name, str) and not isinstance(version, str):
        return
    meta = sessions.read_meta(session_id)
    resolution = dict(meta.resolution)
    adapter = dict(resolution.get("adapter", {}))
    if adapter.get("usage_profile", "none") == "none":
        return
    current = adapter.get("usage_identity")
    value = {
        "name": name if isinstance(name, str) else None,
        "version": version if isinstance(version, str) else None,
    }
    if current == value:
        return
    adapter["usage_identity"] = value
    resolution["adapter"] = adapter
    sessions.update_meta(session_id, resolution=resolution)


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
    model_via = adapter.get("model_via") or "config_option"
    if model_via not in {"config_option", "set_model"}:
        raise RunnerError(f"session {meta.session_id} has invalid stored model_via")
    effort_via = adapter.get("effort_via") or "config_option"
    if effort_via not in {"config_option", "cli"}:
        raise RunnerError(f"session {meta.session_id} has invalid stored effort_via")
    effort_cli_flag = adapter.get("effort_cli_flag")
    if effort_cli_flag is not None and not isinstance(effort_cli_flag, str):
        raise RunnerError(f"session {meta.session_id} has invalid stored effort_cli_flag")
    usage_profile = adapter.get("usage_profile", "none")
    if usage_profile not in {
        "claude_model_usage",
        "codex_usage_updates",
        "grok_meta_usage",
        "none",
    }:
        raise RunnerError(f"session {meta.session_id} has invalid stored usage_profile")
    billing = adapter.get("billing")
    if billing not in {None, "subscription", "api"}:
        raise RunnerError(f"session {meta.session_id} has invalid stored billing")
    context = f"session {meta.session_id}"
    system_prompt_via, tools_via = routes_from_payload(adapter, context=context)
    system_prompt, tools = _stored_prompt_and_tools(meta)
    one_shot = stored_one_shot(meta)
    setting_sources = _stored_setting_sources(meta)
    env_passthrough = stored_strings("env_passthrough", payload)
    stored_mode, mode_spec, modes = _stored_modes(meta)

    entry = ResolvedEntry(
        entry=meta.entry,
        base_adapter=meta.base_adapter,
        name=meta.entry,
        author=None,
        command=command,
        install_command=None,
        install_docs=None,
        home=None,
        home_env=home_env,
        effort_by_model={},
        effort_config_id=effort_config_id,
        model_via=model_via,
        effort_via=effort_via,
        effort_cli_flag=effort_cli_flag,
        usage_profile=usage_profile,
        billing=billing,
        limit_wait_max=None,
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
        system_prompt_file=None,
        tools=None,
        system_prompt_via=system_prompt_via,
        tools_via=tools_via,
        one_shot=one_shot,
        setting_sources=setting_sources,
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
        system_prompt=system_prompt,
        tools=tools,
        one_shot=one_shot,
        setting_sources=setting_sources,
    )


def _stored_modes(
    meta: sessions.SessionMeta,
) -> tuple[str | None, ModeSpec | None, dict[str, ModeSpec]]:
    """Read the stored mode, its facts and the measured catalog from the snapshot."""
    payload = meta.resolution
    adapter = payload.get("adapter", {})
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
    return stored_mode, mode_spec, modes


def _stored_prompt_and_tools(
    meta: sessions.SessionMeta,
) -> tuple[SystemPrompt | None, tuple[str, ...] | None]:
    """Read the stored system prompt source and tool list; absent means unset."""
    resolved = meta.resolution.get("resolved")
    if not isinstance(resolved, Mapping):
        return None, None
    context = f"session {meta.session_id}"
    raw_prompt = resolved.get("system_prompt")
    prompt = None if raw_prompt is None else system_prompt_from_payload(raw_prompt, context=context)
    raw_tools = resolved.get("tools")
    if raw_tools is None:
        return prompt, None
    # A malformed entry must not read as "unset": that would lift a judge's tool limit.
    if not isinstance(raw_tools, Mapping) or "value" not in raw_tools:
        raise RunnerError(f"{context} has an invalid stored tool list")
    return prompt, tools_from_payload(raw_tools["value"], context=context)


def _stored_setting_sources(meta: sessions.SessionMeta) -> tuple[str, ...] | None:
    resolved = meta.resolution.get("resolved")
    if not isinstance(resolved, Mapping) or "setting_sources" not in resolved:
        return None
    raw = resolved["setting_sources"]
    context = f"session {meta.session_id}"
    if not isinstance(raw, Mapping) or "value" not in raw or raw["value"] is None:
        raise RunnerError(f"{context} has invalid stored setting_sources: {raw!r}")
    return setting_sources_from_payload(raw["value"], context=context)


def stored_one_shot(meta: sessions.SessionMeta) -> bool:
    """Whether the session's own snapshot says it is consumed once.

    The snapshot, not the live entry, decides: an entry edited after the
    session started neither turns the reduction on nor off for it.
    """
    try:
        return sessions.stored_one_shot(meta)
    except sessions.SessionError as error:
        raise RunnerError(str(error)) from None


def continue_request(
    meta: sessions.SessionMeta,
    prompt: str,
    *,
    wait_timeout: float | None = None,
    cancel_after: float | None = None,
    limit_wait_max: float | None = None,
    permissions: str | None = None,
    permission_prompt: Callable[[str, str], bool] | None = None,
    resolution: CallResolution | None = None,
    defer_rotation: bool = False,
    rotation_resolution: Mapping[str, Any] | None = None,
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
        wait_timeout=wait_timeout,
        cancel_after=cancel_after,
        limit_wait_max=limit_wait_max,
        permission_prompt=permission_prompt,
        resume_adapter_session=meta.adapter_session_id,
        defer_rotation=defer_rotation,
        rotation_resolution=rotation_resolution,
        resume_prepared=False,
    )


def _source_label(source: Any) -> str:
    """Render a `FieldSource` the way `--resolve` and `agents get` show it."""
    if source is None:
        return "unset"
    kind = getattr(source, "kind", "unset")
    path = getattr(source, "path", None)
    labels = {
        "entry": "entry",
        "adapter-default": "adapter default",
        "vendor-default": "vendor default",
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


async def _await_session(
    session_id: str, target: str | None, timeout: float | None, turn: int
) -> str | None:
    """Block until turn `turn` ends; None means the wait timed out.

    Asks the owning daemon when there is one, because that returns the moment
    the turn ends. A session with no daemon (direct path, or a daemon that has
    since gone) is watched through `meta.json` instead. Either path can find
    the session already past `turn` when a `continue` or `steer` rotated it in
    the meantime (M1g): the daemon then answers `stale` and the fallback sees
    `meta.turns` ahead of `turn`, and both read the turn's own parked outcome
    (SPEC.md *State on disk*) rather than the session's current one.
    """
    deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout
    daemon = None
    waiting: asyncio.Task[Any] | None = None
    if target is not None:
        routed = await daemon_client.connect(target)
        daemon = routed

    try:
        if daemon is not None:
            waiting = asyncio.ensure_future(daemon.await_turn(session_id, turn=turn))
            try:
                reply = await asyncio.wait_for(asyncio.shield(waiting), timeout=timeout)
            except TimeoutError:
                waiting.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await waiting
                return None
            if reply.get("stale"):
                return sessions.read_turn_meta(session_id, turn).state
            outcome = reply.get("outcome") or {}
            return str(outcome.get("state", "failed"))

        while True:
            meta = sessions.load(session_id)
            if meta.turns > turn:
                return sessions.read_turn_meta(session_id, turn).state
            if meta.turns == turn and meta.is_finished:
                return meta.state
            if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                return None
            await asyncio.sleep(WAIT_POLL_INTERVAL)
    finally:
        if waiting is not None and not waiting.done():
            waiting.cancel()
        if waiting is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await waiting
        if daemon is not None:
            with contextlib.suppress(Exception):
                await daemon.close()


def wait_for_session(session_id: str, *, timeout: float | None = None) -> tuple[int, str | None]:
    """Block until the session's turn selected at call start ends.

    SPEC.md `wait`/M1g: the turn observed is the session's current one when
    this call starts, kept even if a later `continue` or `steer` opens a newer
    turn while waiting. Returns the selected turn and its terminal state, or
    `None` for the state when the timeout stopped waiting — the session is
    left running either way.
    """
    meta = sessions.load(session_id)
    turn = meta.turns
    if meta.is_finished:
        return turn, meta.state
    return turn, run_with_sigint_deferred(
        lambda: _await_session(session_id, meta.target, timeout, turn)
    )


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
