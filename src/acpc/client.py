"""ACP Client subclass for acpc.

Dispatches session_update events to OutputHandler and handles permission
policy based on tool_call.kind.
"""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
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


SessionUpdateSink = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class ClientSession:
    """Per-session state used while one ACP connection is multiplexed."""

    output: OutputHandler
    permission_level: PermissionLevel
    is_tty: bool
    replaying: bool = False
    update_sink: SessionUpdateSink | None = None


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
    """ACP client that keeps direct and multiplexed session state separate."""

    def __init__(
        self,
        output: OutputHandler,
        permission_level: PermissionLevel,
        is_tty: bool,
        *,
        strict_sessions: bool = False,
    ) -> None:
        self._default_session = ClientSession(
            output=output,
            permission_level=permission_level,
            is_tty=is_tty,
        )
        self._sessions: dict[str, ClientSession] = {}
        self._strict_sessions = strict_sessions
        self.session_id: str | None = None

    @property
    def output(self) -> OutputHandler:
        """Return the direct-path output handler for backwards compatibility."""
        return self._default_session.output

    @property
    def permission_level(self) -> PermissionLevel:
        """Return the direct-path permission policy for backwards compatibility."""
        return self._default_session.permission_level

    @property
    def is_tty(self) -> bool:
        """Return whether direct-path permission prompts may use the terminal."""
        return self._default_session.is_tty

    @property
    def replaying(self) -> bool:
        """Return the direct-path replay flag for backwards compatibility."""
        return self._default_session.replaying

    def register_session(
        self,
        session_id: str,
        *,
        output: OutputHandler | None = None,
        permission_level: PermissionLevel | None = None,
        is_tty: bool | None = None,
        update_sink: SessionUpdateSink | None = None,
    ) -> ClientSession:
        """Attach per-session output, permissions, terminal state, and sink."""
        session = self._sessions.get(session_id)
        if session is None:
            session = ClientSession(
                output=output or self._default_session.output,
                permission_level=permission_level or self._default_session.permission_level,
                is_tty=self._default_session.is_tty if is_tty is None else is_tty,
            )
            self._sessions[session_id] = session
        elif output is not None:
            session.output = output
        if permission_level is not None:
            session.permission_level = permission_level
        if is_tty is not None:
            session.is_tty = is_tty
        session.update_sink = update_sink
        return session

    def unregister_session(self, session_id: str) -> None:
        """Remove a session mapping after its transport is no longer usable."""
        self._sessions.pop(session_id, None)

    @property
    def session_ids(self) -> tuple[str, ...]:
        """Return registered session ids for daemon bookkeeping cleanup."""
        return tuple(self._sessions)

    def detach_session(self, session_id: str) -> None:
        """Stop forwarding notifications while retaining session policy and output."""
        session = self._sessions.get(session_id)
        if session is not None:
            session.update_sink = None

    def _session_for(self, session_id: str) -> ClientSession:
        """Return registered state, optionally rejecting unknown multiplexed ids."""
        session = self._sessions.get(session_id)
        if session is None and self._strict_sessions:
            raise ValueError(f"unknown session id: {session_id}")
        return session or self._default_session

    # -- connection callback ------------------------------------------------

    def on_connect(self, conn: Agent) -> None:  # noqa: ARG002
        pass

    # -- history replay -----------------------------------------------------

    @contextmanager
    def replaying_history(self, session_id: str | None = None) -> Iterator[None]:
        """Suppress event output while the agent replays past conversation.

        ACP requires an agent to emit session/update notifications for the
        entire prior conversation before it responds to session/load. Those
        events are indistinguishable from live ones, so without this guard
        every resume reprints the whole transcript ahead of the new answer.
        """
        session = self._default_session if session_id is None else self._session_for(session_id)
        was_replaying = session.replaying
        session.replaying = True
        try:
            yield
        finally:
            session.replaying = was_replaying

    # -- session_update -----------------------------------------------------

    async def session_update(
        self,
        session_id: str,
        update: Any,
        **kwargs: Any,  # noqa: ARG002
    ) -> None:
        """Dispatch session_update to the output handler."""
        session = self._session_for(session_id)
        self.session_id = session_id
        if session.replaying:
            return
        discriminator: str = getattr(update, "session_update", "")
        event = update.model_dump(mode="json", by_alias=True)

        if session.update_sink is not None:
            try:
                await session.update_sink(
                    {
                        "type": "session_update",
                        "session_id": session_id,
                        "update": event,
                    }
                )
            except (ConnectionError, OSError):
                session.update_sink = None

        if discriminator == "agent_message_chunk":
            chunk: AgentMessageChunk = update
            if hasattr(chunk.content, "text"):
                session.output.on_agent_message_chunk(chunk.content.text)

        if discriminator == "tool_call":
            tc_start: ToolCallStart = update
            session.output.on_tool_call(tc_start.title, kind=tc_start.kind)

        if discriminator == "tool_call_update":
            tc_progress: ToolCallProgress = update
            if tc_progress.title:
                session.output.on_tool_call(
                    tc_progress.title,
                    kind=tc_progress.kind,
                )

        # JSON mode gets every event
        if session.output.mode is OutputMode.JSON:
            session.output.on_event(event)

    # -- permissions --------------------------------------------------------

    async def request_permission(
        self,
        options: list[PermissionOption],
        session_id: str,  # noqa: ARG002
        tool_call: ToolCallUpdate,
        **kwargs: Any,  # noqa: ARG002
    ) -> RequestPermissionResponse:
        """Apply permission policy based on tool_call.kind."""
        session = self._session_for(session_id)
        kind_str: str | None = tool_call.kind
        category = _classify_kind(kind_str)
        decision = _should_allow(session.permission_level, category)

        title = getattr(tool_call, "title", None) or ""

        if decision is None:
            decision = self._prompt_user(kind_str or "unknown", title, is_tty=session.is_tty)

        outcome_label = "allow" if decision else "deny"
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

    def _prompt_user(self, kind_str: str, title: str, *, is_tty: bool) -> bool:
        """Ask the user on stderr/stdin. Returns False if not a TTY."""
        if not is_tty:
            stderr_error("permission prompt requires a TTY (use --permissions all/write/read/none)")
            return False

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
        _ = self._session_for(session_id)
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
        _ = self._session_for(session_id)
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
