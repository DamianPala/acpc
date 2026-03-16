"""ACP Client subclass for acpc.

Dispatches session_update events to OutputHandler and handles permission
policy based on tool_call.kind.
"""

from __future__ import annotations

import sys
from enum import Enum
from pathlib import Path
from typing import Any

from acp.interfaces import Agent
from acp.schema import (
    AgentMessageChunk,
    AllowedOutcome,
    CreateTerminalResponse,
    DeniedOutcome,
    EnvVariable,
    KillTerminalCommandResponse,
    PermissionOption,
    ReadTextFileResponse,
    ReleaseTerminalResponse,
    RequestPermissionResponse,
    TerminalOutputResponse,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
    WaitForTerminalExitResponse,
    WriteTextFileResponse,
)

from acpc.output import OutputHandler, OutputMode, stderr_error, stderr_permission

# ---------------------------------------------------------------------------
# Permission levels
# ---------------------------------------------------------------------------

READ_KINDS: frozenset[str] = frozenset({"read", "search", "think", "fetch", "switch_mode", "other"})
WRITE_KINDS: frozenset[str] = frozenset({"edit", "execute"})
DELETE_KINDS: frozenset[str] = frozenset({"delete", "move"})


class PermissionLevel(Enum):
    ALL = "all"
    WRITE = "write"
    READ = "read"
    NONE = "none"
    PROMPT = "prompt"


def _classify_kind(kind: str | None) -> str:
    """Return 'read', 'write', or 'delete' for a ToolKind value."""
    if kind is None or kind in READ_KINDS:
        return "read"
    if kind in WRITE_KINDS:
        return "write"
    if kind in DELETE_KINDS:
        return "delete"
    return "read"


def _should_allow(level: PermissionLevel, category: str) -> bool | None:
    """Return True (allow), False (deny), or None (ask the user)."""
    if level is PermissionLevel.ALL:
        return True
    if level is PermissionLevel.NONE:
        return False
    if level is PermissionLevel.READ:
        return category == "read"
    if level is PermissionLevel.WRITE:
        return category != "delete"
    # PROMPT
    if category == "read":
        return True
    return None


def _find_option(
    options: list[PermissionOption],
    allow: bool,
) -> str:
    """Find the option_id for an allow_once or reject_once option."""
    target_kind = "allow_once" if allow else "reject_once"
    for opt in options:
        if opt.kind == target_kind:
            return opt.option_id
    # Fallback: try allow_always / reject_always
    fallback = "allow_always" if allow else "reject_always"
    for opt in options:
        if opt.kind == fallback:
            return opt.option_id
    # Last resort: first option
    return options[0].option_id


# ---------------------------------------------------------------------------
# AcpcClient
# ---------------------------------------------------------------------------


class AcpcClient:
    """ACP Client implementation that dispatches events to OutputHandler."""

    def __init__(
        self,
        output: OutputHandler,
        permission_level: PermissionLevel,
        is_tty: bool,
    ) -> None:
        self.output = output
        self.permission_level = permission_level
        self.is_tty = is_tty
        self.session_id: str | None = None

    # -- connection callback ------------------------------------------------

    def on_connect(self, conn: Agent) -> None:  # noqa: ARG002
        pass

    # -- session_update -----------------------------------------------------

    async def session_update(
        self,
        session_id: str,
        update: Any,
        **kwargs: Any,  # noqa: ARG002
    ) -> None:
        """Dispatch session_update to the output handler."""
        self.session_id = session_id
        discriminator: str = getattr(update, "session_update", "")

        if discriminator == "agent_message_chunk":
            chunk: AgentMessageChunk = update
            if hasattr(chunk.content, "text"):
                self.output.on_agent_message_chunk(chunk.content.text)

        if discriminator == "tool_call":
            tc_start: ToolCallStart = update
            self.output.on_tool_call(tc_start.title, kind=tc_start.kind)

        if discriminator == "tool_call_update":
            tc_progress: ToolCallProgress = update
            if tc_progress.title:
                self.output.on_tool_call(
                    tc_progress.title,
                    kind=tc_progress.kind,
                )

        # JSON mode gets every event
        if self.output.mode is OutputMode.JSON:
            self.output.on_event(update.model_dump(mode="json", by_alias=True))

    # -- permissions --------------------------------------------------------

    async def request_permission(
        self,
        options: list[PermissionOption],
        session_id: str,  # noqa: ARG002
        tool_call: ToolCallUpdate,
        **kwargs: Any,  # noqa: ARG002
    ) -> RequestPermissionResponse:
        """Apply permission policy based on tool_call.kind."""
        kind_str: str | None = tool_call.kind
        category = _classify_kind(kind_str)
        decision = _should_allow(self.permission_level, category)

        if decision is None:
            decision = self._prompt_user(tool_call)

        outcome_label = "allow" if decision else "deny"
        title = tool_call.title if hasattr(tool_call, "title") and tool_call.title else ""
        stderr_permission(kind_str or "unknown", title, outcome_label)

        option_id = _find_option(options, decision)

        if decision:
            return RequestPermissionResponse(
                outcome=AllowedOutcome(
                    outcome="selected",
                    option_id=option_id,
                ),
            )
        return RequestPermissionResponse(
            outcome=DeniedOutcome(outcome="cancelled"),
        )

    def _prompt_user(self, tool_call: ToolCallUpdate) -> bool:
        """Ask the user on stderr/stdin. Returns False if not a TTY."""
        if not self.is_tty:
            stderr_error("permission prompt requires a TTY (use --permissions all/write/read/none)")
            return False

        title = tool_call.title if hasattr(tool_call, "title") and tool_call.title else ""
        kind_str = tool_call.kind or "unknown"
        print(
            f"[acpc] approve {kind_str}: {title}? [y/N] ",
            file=sys.stderr,
            end="",
            flush=True,
        )
        try:
            answer = input().strip().lower()
        except EOFError:
            return False
        return answer in ("y", "yes")

    # -- file operations ----------------------------------------------------

    async def read_text_file(
        self,
        path: str,
        session_id: str,  # noqa: ARG002
        limit: int | None = None,  # noqa: ARG002
        line: int | None = None,  # noqa: ARG002
        **kwargs: Any,  # noqa: ARG002
    ) -> ReadTextFileResponse:
        """Read file from disk and return content."""
        content = Path(path).read_text(encoding="utf-8")
        return ReadTextFileResponse(content=content)

    async def write_text_file(
        self,
        content: str,
        path: str,
        session_id: str,  # noqa: ARG002
        **kwargs: Any,  # noqa: ARG002
    ) -> WriteTextFileResponse:
        """Write content to file on disk."""
        file_path = Path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return WriteTextFileResponse()

    # -- terminal (not supported in v0.1) -----------------------------------

    async def create_terminal(
        self,
        command: str,  # noqa: ARG002
        session_id: str,  # noqa: ARG002
        args: list[str] | None = None,  # noqa: ARG002
        cwd: str | None = None,  # noqa: ARG002
        env: list[EnvVariable] | None = None,  # noqa: ARG002
        output_byte_limit: int | None = None,  # noqa: ARG002
        **kwargs: Any,  # noqa: ARG002
    ) -> CreateTerminalResponse:
        raise NotImplementedError("Terminal not supported in acpc v0.1")

    async def kill_terminal(
        self,
        session_id: str,  # noqa: ARG002
        terminal_id: str,  # noqa: ARG002
        **kwargs: Any,  # noqa: ARG002
    ) -> KillTerminalCommandResponse:
        raise NotImplementedError("Terminal not supported in acpc v0.1")

    async def release_terminal(
        self,
        session_id: str,  # noqa: ARG002
        terminal_id: str,  # noqa: ARG002
        **kwargs: Any,  # noqa: ARG002
    ) -> ReleaseTerminalResponse:
        raise NotImplementedError("Terminal not supported in acpc v0.1")

    async def terminal_output(
        self,
        session_id: str,  # noqa: ARG002
        terminal_id: str,  # noqa: ARG002
        **kwargs: Any,  # noqa: ARG002
    ) -> TerminalOutputResponse:
        raise NotImplementedError("Terminal not supported in acpc v0.1")

    async def wait_for_terminal_exit(
        self,
        session_id: str,  # noqa: ARG002
        terminal_id: str,  # noqa: ARG002
        **kwargs: Any,  # noqa: ARG002
    ) -> WaitForTerminalExitResponse:
        raise NotImplementedError("Terminal not supported in acpc v0.1")

    # -- extension methods --------------------------------------------------

    async def ext_method(
        self,
        method: str,  # noqa: ARG002
        params: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        return {}

    async def ext_notification(
        self,
        method: str,  # noqa: ARG002
        params: dict[str, Any],  # noqa: ARG002
    ) -> None:
        pass
