"""ACP client callbacks for transcript-backed acpc sessions."""

import asyncio
import inspect
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from acp import RequestError
from acp.schema import (
    AgentMessageChunk,
    AgentThoughtChunk,
    AllowedOutcome,
    AvailableCommandsUpdate,
    CreateTerminalResponse,
    CurrentModeUpdate,
    DeniedOutcome,
    KillTerminalResponse,
    NewSessionResponse,
    PermissionOption,
    ReadTextFileResponse,
    ReleaseTerminalResponse,
    RequestPermissionResponse,
    SessionConfigOptionBoolean,
    SessionConfigOptionSelect,
    TerminalOutputResponse,
    ToolCallProgress,
    ToolCallStart,
    UsageUpdate,
    WaitForTerminalExitResponse,
    WriteTextFileResponse,
)

from acpc.permissions import (
    CLIENT_METHOD_CATEGORIES,
    ModeSelectionError,
    PermissionLevel,
    classify_kind,
    find_option,
    select_mode,
    should_allow,
)
from acpc.registry import ModeSpec
from acpc.transcript import Transcript

_Clock = Callable[[], float]
_PermissionPrompt = Callable[[str, str], bool | Awaitable[bool]]
_EndTurn = Callable[[], None]
# 400 is application-defined because policy refusal is neither invalid params nor internal error.
CLIENT_PERMISSION_ERROR_CODE = 400

# Real adapters stream word-sized message chunks; one transcript event per
# chunk turns `log` into a per-fragment view and the cursor into a fragment
# counter. Consecutive same-type chunks therefore coalesce into one event, cut
# by whatever comes first: a different event type (the causal narrative keeps
# its interleaving), a pause in the stream, a bounded age so a long
# uninterrupted message still surfaces while running, or a size bound.
_CHUNK_GAP_SECONDS = 1.0
_CHUNK_MAX_AGE_SECONDS = 2.0
_CHUNK_MAX_CHARS = 4096
# A broken cancellation watcher must not hold an ACP permission response forever.
_CANCELLATION_DISPATCH_TIMEOUT = 1.0


@dataclass(slots=True)
class _ToolCall:
    title: str | None
    kind: str | None
    raw_input: Any
    started_at: float
    status: str | None = None
    finished: bool = False


@dataclass(slots=True)
class _PendingChunks:
    event_type: str
    parts: list[str]
    chars: int
    started_at: float
    last_at: float


class AcpcClient:
    """Implement the ACP client callbacks used by a single session turn.

    The client keeps no terminal UI state. Agent content is appended to the
    transcript and to ``answer`` in the order received; permission prompting,
    when allowed by the caller, is supplied as a callback rather than read
    from stdin.
    """

    def __init__(
        self,
        transcript: Transcript,
        permission_level: PermissionLevel,
        *,
        modes: Mapping[str, ModeSpec] | None = None,
        end_turn: _EndTurn | None = None,
        cancellation_dispatched: asyncio.Event | None = None,
        permission_prompt: _PermissionPrompt | None = None,
        clock: _Clock | None = None,
    ) -> None:
        self.transcript = transcript
        self.permission_level = permission_level
        self.modes = {} if modes is None else modes
        self.end_turn = end_turn
        self.cancellation_dispatched = cancellation_dispatched
        self.permission_prompt = permission_prompt
        self._clock = time.monotonic if clock is None else clock
        self._pending: _PendingChunks | None = None
        self._answer_parts: list[str] = []
        self._answer_boundary_pending = False
        self._tool_calls: dict[str, _ToolCall] = {}
        self._tokens = 0
        self._cost: float | None = None
        self._denied: dict[str, int] = {}
        self._advertised: dict[str, Any] = {
            "modes": [],
            "models": [],
            "commands": [],
        }

    @property
    def answer(self) -> str:
        """Return the agent-message content received so far."""
        return "".join(self._answer_parts)

    @property
    def tokens(self) -> int:
        """Return the cumulative token count reported by the adapter."""
        return self._tokens

    @property
    def cost(self) -> float | None:
        """Return the cumulative cost reported by the adapter."""
        return self._cost

    @property
    def denied(self) -> dict[str, int]:
        """Return this turn's permission-denial counts by category."""
        return dict(self._denied)

    @property
    def advertised(self) -> dict[str, Any]:
        """Return the latest adapter modes, models, and commands advertisement."""
        return {
            "modes": [dict(item) for item in self._advertised["modes"]],
            "models": list(self._advertised["models"]),
            "commands": [dict(item) for item in self._advertised["commands"]],
        }

    def capture_advertised(self, session: NewSessionResponse) -> None:
        """Capture modes and models announced by ``session/new``."""
        modes = session.modes.available_modes if session.modes is not None else []
        self._advertised["modes"] = [
            mode.model_dump(mode="json", by_alias=True, exclude_none=True) for mode in modes
        ]
        self._advertised["models"] = self._models_from_options(session.config_options or [])

    def on_connect(self, conn: Any) -> None:
        """Satisfy the ACP connection hook; no client-side setup is needed."""
        del conn

    def flush(self) -> None:
        """Write buffered agent prose to the transcript as one event.

        Called before any non-chunk event lands and at the end of a turn, so
        message text always precedes the event that interrupted it.
        """
        pending = self._pending
        if pending is None:
            return
        self._pending = None
        self.transcript.append(pending.event_type, text="".join(pending.parts))

    def _buffer_chunk(self, event_type: str, text: str) -> None:
        now = self._clock()
        pending = self._pending
        if pending is not None and (
            pending.event_type != event_type or now - pending.last_at >= _CHUNK_GAP_SECONDS
        ):
            self.flush()
            pending = None
        if pending is None:
            pending = _PendingChunks(
                event_type=event_type, parts=[], chars=0, started_at=now, last_at=now
            )
            self._pending = pending
        pending.parts.append(text)
        pending.chars += len(text)
        pending.last_at = now
        if now - pending.started_at >= _CHUNK_MAX_AGE_SECONDS or pending.chars >= _CHUNK_MAX_CHARS:
            self.flush()

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        """Record one ACP session update in the public transcript format."""
        del session_id, kwargs
        update_type = getattr(update, "session_update", None)

        if update_type == "agent_message_chunk" and isinstance(update, AgentMessageChunk):
            text = getattr(update.content, "text", None)
            if isinstance(text, str):
                if (
                    self._answer_boundary_pending
                    and self.answer
                    and not self.answer.endswith("\n\n")
                ):
                    self._answer_parts.append("\n\n")
                self._answer_parts.append(text)
                self._answer_boundary_pending = False
                self._buffer_chunk("msg", text)
            return

        self._answer_boundary_pending = True

        if update_type == "agent_thought_chunk" and isinstance(update, AgentThoughtChunk):
            text = getattr(update.content, "text", None)
            if isinstance(text, str):
                self._buffer_chunk("thought", text)
            return

        # Every non-chunk update cuts the buffered prose, even one that writes
        # nothing itself (a tool start): the boundary is where the narrative
        # forked, not where the interrupting event was finally recorded.
        self.flush()

        if update_type == "tool_call" and isinstance(update, ToolCallStart):
            self._start_tool(update)
            return

        if update_type == "tool_call_update" and isinstance(update, ToolCallProgress):
            self._update_tool(update)
            return

        if update_type == "usage_update" and isinstance(update, UsageUpdate):
            self._record_usage(update)
            return

        if update_type == "available_commands_update" and isinstance(
            update, AvailableCommandsUpdate
        ):
            self._advertised["commands"] = [
                command.model_dump(mode="json", by_alias=True, exclude_none=True)
                for command in update.available_commands
            ]
            return

        if update_type == "config_option_update":
            self._advertised["models"] = self._models_from_options(
                getattr(update, "config_options", [])
            )
            return

        if update_type == "current_mode_update" and isinstance(update, CurrentModeUpdate):
            return

    async def request_permission(
        self,
        session_id: str,
        tool_call: Any,
        options: list[PermissionOption],
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        """Answer an ACP permission request and record the policy decision."""
        del session_id, kwargs
        self.flush()
        kind = getattr(tool_call, "kind", None) or "unknown"
        title = getattr(tool_call, "title", None) or ""
        category = classify_kind(kind)
        switch_reason: str | None = None
        if kind == "switch_mode":
            target = self._switch_mode_target(tool_call)
            decision, required = self._switch_mode_decision(target)
            if not decision:
                display_target = target if target is not None else "<unknown>"
                switch_reason = self._switch_mode_reason(display_target, required)
        else:
            decision = should_allow(self.permission_level, category)
            if decision is None:
                decision = await self._ask_permission(kind, title)

        option_id = find_option(options, decision, self.permission_level)
        allowed = bool(decision and option_id is not None)
        reason: str | None = None
        if not allowed:
            reason = switch_reason or f"permission denied: {kind}"
            if switch_reason is None and decision and option_id is None:
                reason += " (no matching allow option)"
        self._record_permission(kind, category, allowed, reason)
        if not allowed:
            if switch_reason is not None and self.end_turn is not None:
                self.end_turn()
                await self._await_cancellation_dispatch()
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        assert option_id is not None
        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id=option_id)
        )

    async def _await_cancellation_dispatch(self) -> None:
        """Wait until the runner starts ACP cancellation, with a bounded fallback."""
        if self.cancellation_dispatched is None:
            return
        try:
            await asyncio.wait_for(
                self.cancellation_dispatched.wait(), timeout=_CANCELLATION_DISPATCH_TIMEOUT
            )
        except TimeoutError:
            return

    def _switch_mode_decision(self, target: str | None) -> tuple[bool, str]:
        """Resolve a runtime mode switch through the stored policy ceiling."""
        if target is None:
            return False, PermissionLevel.ALL.value
        try:
            _mode, spec = select_mode(self.modes, self.permission_level, explicit_mode=target)
        except ModeSelectionError:
            spec = self.modes.get(target)
            return False, spec.grants if spec is not None else PermissionLevel.ALL.value
        return True, spec.grants

    @staticmethod
    def _switch_mode_reason(target: str, required: str) -> str:
        """Describe why a runtime mode switch cannot be admitted."""
        return f"permission denied: switch_mode {target} (requires --permissions {required})"

    @staticmethod
    def _switch_mode_target(tool_call: Any) -> str | None:
        """Read the requested mode from ACP's generic tool-call input."""
        raw_input = getattr(tool_call, "raw_input", None)
        if isinstance(raw_input, Mapping):
            for key in ("target", "mode"):
                target = raw_input.get(key)
                if isinstance(target, str) and target:
                    return target
        return None

    async def _authorize_client_method(self, kind: str, title: str) -> None:
        """Apply the policy to an ACP callback outside ``request_permission``."""
        self.flush()
        category = CLIENT_METHOD_CATEGORIES[kind]
        decision = should_allow(self.permission_level, category)
        if decision is None:
            decision = await self._ask_permission(kind, title)
        allowed = bool(decision)
        reason = None if allowed else f"permission denied: {kind}"
        self._record_permission(kind, category, allowed, reason)
        if not allowed:
            assert reason is not None
            raise RequestError(CLIENT_PERMISSION_ERROR_CODE, reason, {"category": category})

    def _record_permission(
        self, kind: str, category: str, allowed: bool, reason: str | None
    ) -> None:
        """Append a permission decision and update the denial tally."""
        self.transcript.append("permission", kind=kind, decision="allow" if allowed else "deny")
        if not allowed:
            self._denied[category] = self._denied.get(category, 0) + 1
            self.transcript.append("error", message=reason or f"permission denied: {kind}")

    def _authorize_existing_terminal(self, method: str, terminal_id: str) -> None:
        """Keep the terminal callbacks unsupported until terminal ownership exists."""
        self.flush()
        del method, terminal_id
        raise NotImplementedError("Terminal methods are not supported by acpc")

    async def read_text_file(
        self,
        path: str,
        session_id: str,
        line: int | None = None,
        limit: int | None = None,
        **kwargs: Any,
    ) -> ReadTextFileResponse:
        """Return text requested through ACP's filesystem callback."""
        await self._authorize_client_method("fs/read_text_file", f"Read {path}")
        del session_id, kwargs
        text = Path(path).read_text(encoding="utf-8")
        if line is not None:
            lines = text.splitlines(keepends=True)
            start = max(0, line - 1)
            text = "".join(lines[start : start + limit if limit is not None else None])
        elif limit is not None:
            text = text[:limit]
        return ReadTextFileResponse(content=text)

    async def write_text_file(
        self,
        path: str,
        content: str,
        session_id: str,
        **kwargs: Any,
    ) -> WriteTextFileResponse:
        """Write text requested through ACP's filesystem callback."""
        await self._authorize_client_method("fs/write_text_file", f"Write {path}")
        del session_id, kwargs
        file_path = Path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return WriteTextFileResponse()

    async def create_terminal(
        self,
        command: str,
        session_id: str,
        args: list[str] | None = None,
        env: list[Any] | None = None,
        cwd: str | None = None,
        output_byte_limit: int | None = None,
        **kwargs: Any,
    ) -> CreateTerminalResponse:
        await self._authorize_client_method("terminal/create", f"Create terminal: {command}")
        del command, session_id, args, env, cwd, output_byte_limit, kwargs
        raise NotImplementedError("Terminal methods are not supported by acpc")

    async def terminal_output(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> TerminalOutputResponse:
        self._authorize_existing_terminal("terminal/output", terminal_id)
        del session_id, terminal_id, kwargs
        raise NotImplementedError("Terminal methods are not supported by acpc")

    async def release_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> ReleaseTerminalResponse:
        self._authorize_existing_terminal("terminal/release", terminal_id)
        del session_id, terminal_id, kwargs
        raise NotImplementedError("Terminal methods are not supported by acpc")

    async def wait_for_terminal_exit(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> WaitForTerminalExitResponse:
        self._authorize_existing_terminal("terminal/wait_for_exit", terminal_id)
        del session_id, terminal_id, kwargs
        raise NotImplementedError("Terminal methods are not supported by acpc")

    async def kill_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> KillTerminalResponse:
        self._authorize_existing_terminal("terminal/kill", terminal_id)
        del session_id, terminal_id, kwargs
        raise NotImplementedError("Terminal methods are not supported by acpc")

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        del method, params
        return {}

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        del method, params

    def _start_tool(self, update: ToolCallStart) -> None:
        now = self._clock()
        current = self._tool_calls.get(update.tool_call_id)
        if current is None or current.status in {"completed", "failed"}:
            current = _ToolCall(
                title=update.title,
                kind=update.kind,
                raw_input=update.raw_input,
                started_at=now,
            )
            self._tool_calls[update.tool_call_id] = current
        else:
            self._merge_tool(current, update)
        current.status = update.status
        if current.status in {"completed", "failed"}:
            self._finish_tool(update.tool_call_id, current.status)

    def _update_tool(self, update: ToolCallProgress) -> None:
        current = self._tool_calls.get(update.tool_call_id)
        if current is None:
            current = _ToolCall(
                title=update.title,
                kind=update.kind,
                raw_input=update.raw_input,
                started_at=self._clock(),
            )
            self._tool_calls[update.tool_call_id] = current
        if current.finished:
            return
        self._merge_tool(current, update)
        if update.status is not None:
            current.status = update.status
        if current.status in {"completed", "failed"}:
            self._finish_tool(update.tool_call_id, current.status)

    def _merge_tool(self, current: _ToolCall, update: Any) -> None:
        title = getattr(update, "title", None)
        kind = getattr(update, "kind", None)
        raw_input = getattr(update, "raw_input", None)
        if title is not None:
            current.title = title
        if kind is not None:
            current.kind = kind
        if raw_input is not None:
            current.raw_input = raw_input

    def _finish_tool(self, tool_call_id: str, status: str) -> None:
        current = self._tool_calls[tool_call_id]
        if current.finished:
            return
        elapsed_ms = max(0.0, self._clock() - current.started_at) * 1000
        name, args_summary = self._tool_description(current)
        self.transcript.append(
            "tool",
            name=name,
            args_summary=args_summary,
            status=status,
            duration_ms=int(elapsed_ms),
        )
        current.status = status
        current.finished = True

    def _record_usage(self, update: UsageUpdate) -> None:
        self._tokens = max(self._tokens, update.used)
        if update.cost is not None:
            amount = update.cost.amount
            self._cost = amount if self._cost is None else max(self._cost, amount)
        self.transcript.append("usage", tokens=self._tokens, cost=self._cost)

    async def _ask_permission(self, kind: str, title: str) -> bool:
        if self.permission_prompt is None:
            return False
        answer = self.permission_prompt(kind, title)
        if inspect.isawaitable(answer):
            return bool(await answer)
        return bool(answer)

    @staticmethod
    def _tool_description(tool: _ToolCall) -> tuple[str, str]:
        title = (tool.title or "").strip()
        if title:
            name, separator, remainder = title.partition(" ")
            if separator:
                return name, remainder.strip()
            return name, AcpcClient._raw_input_summary(tool.raw_input)
        return tool.kind or "tool", AcpcClient._raw_input_summary(tool.raw_input)

    @staticmethod
    def _raw_input_summary(raw_input: Any) -> str:
        if raw_input is None:
            return ""
        if isinstance(raw_input, str):
            return raw_input
        try:
            return json.dumps(raw_input, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return str(raw_input)

    @staticmethod
    def _models_from_options(options: list[Any]) -> list[str]:
        models: list[str] = []
        for option in options:
            if not isinstance(option, (SessionConfigOptionSelect, SessionConfigOptionBoolean)):
                continue
            if option.id != "model" or not isinstance(option, SessionConfigOptionSelect):
                continue
            for choice in option.options:
                value = getattr(choice, "value", None)
                if isinstance(value, str):
                    models.append(value)
                else:
                    for nested in getattr(choice, "options", []):
                        nested_value = getattr(nested, "value", None)
                        if isinstance(nested_value, str):
                            models.append(nested_value)
        return models
