"""ACP client callbacks for transcript-backed acpc sessions."""

import inspect
import json
import time
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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

from acpc.permissions import PermissionLevel, classify_kind, find_option, should_allow
from acpc.transcript import Transcript

_Clock = Callable[[], float]
_PermissionPrompt = Callable[[str, str], bool | Awaitable[bool]]


@dataclass(slots=True)
class _ToolCall:
    title: str | None
    kind: str | None
    raw_input: Any
    started_at: float
    status: str | None = None
    finished: bool = False


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
        bypass_modes: Collection[str] = (),
        permission_prompt: _PermissionPrompt | None = None,
        clock: _Clock | None = None,
    ) -> None:
        self.transcript = transcript
        self.permission_level = permission_level
        self.bypass_modes = frozenset(bypass_modes)
        self.permission_prompt = permission_prompt
        self._clock = time.monotonic if clock is None else clock
        self._answer_parts: list[str] = []
        self._tool_calls: dict[str, _ToolCall] = {}
        self._tokens = 0
        self._cost: float | None = None
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

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        """Record one ACP session update in the public transcript format."""
        del session_id, kwargs
        update_type = getattr(update, "session_update", None)

        if update_type == "agent_message_chunk" and isinstance(update, AgentMessageChunk):
            text = getattr(update.content, "text", None)
            if isinstance(text, str):
                self._answer_parts.append(text)
                self.transcript.append("msg", text=text)
            return

        if update_type == "agent_thought_chunk" and isinstance(update, AgentThoughtChunk):
            text = getattr(update.content, "text", None)
            if isinstance(text, str):
                self.transcript.append("thought", text=text)
            return

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
        kind = getattr(tool_call, "kind", None) or "unknown"
        title = getattr(tool_call, "title", None) or ""
        raw_input = getattr(tool_call, "raw_input", None)
        target = raw_input.get("target") if isinstance(raw_input, Mapping) else None
        bypass_switch = kind == "switch_mode" and target in self.bypass_modes
        category = classify_kind(kind, bypass_mode_switch=bypass_switch)
        decision = should_allow(self.permission_level, category)
        if decision is None:
            decision = await self._ask_permission(kind, title)

        option_id = find_option(options, decision, self.permission_level)
        allowed = bool(decision and option_id is not None)
        self.transcript.append("permission", kind=kind, decision="allow" if allowed else "deny")
        if not allowed:
            reason = f"permission denied: {kind}"
            if decision and option_id is None:
                reason += " (no matching allow option)"
            self.transcript.append("error", message=reason)
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        assert option_id is not None
        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id=option_id)
        )

    async def read_text_file(
        self,
        path: str,
        session_id: str,
        line: int | None = None,
        limit: int | None = None,
        **kwargs: Any,
    ) -> ReadTextFileResponse:
        """Return text requested through ACP's filesystem callback."""
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
        del command, session_id, args, env, cwd, output_byte_limit, kwargs
        raise NotImplementedError("Terminal methods are not supported by acpc")

    async def terminal_output(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> TerminalOutputResponse:
        del session_id, terminal_id, kwargs
        raise NotImplementedError("Terminal methods are not supported by acpc")

    async def release_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> ReleaseTerminalResponse:
        del session_id, terminal_id, kwargs
        raise NotImplementedError("Terminal methods are not supported by acpc")

    async def wait_for_terminal_exit(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> WaitForTerminalExitResponse:
        del session_id, terminal_id, kwargs
        raise NotImplementedError("Terminal methods are not supported by acpc")

    async def kill_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> KillTerminalResponse:
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
