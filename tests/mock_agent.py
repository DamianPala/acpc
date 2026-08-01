"""Mock ACP agent for integration tests.

Standalone script that speaks ACP protocol over stdio.
Behavior controlled by prompt text:

- Any text: echoes it back as agent_message_chunk
- "tool:TITLE": simulates a tool call with given title (kind=read)
- "tool-edit:TITLE": simulates a tool call requiring edit permission
- "slow:N": waits N seconds before responding (for timeout tests)
- "error": returns stop_reason=refusal
- "multi:TEXT": echoes text, supports load_session for multi-turn
- "large:N": returns N kilobytes of text (for buffer tests)
"""

import asyncio
import sys
from typing import Any
from uuid import uuid4

from acp import (
    Agent,
    InitializeResponse,
    NewSessionResponse,
    PromptResponse,
    run_agent,
    text_block,
    update_agent_message,
)
from acp.helpers import start_tool_call, update_tool_call
from acp.interfaces import Client
from acp.schema import (
    AcpMcpServer,
    AgentCapabilities,
    AudioContentBlock,
    AuthenticateResponse,
    CloseSessionResponse,
    EmbeddedResourceContentBlock,
    ForkSessionResponse,
    HttpMcpServer,
    ImageContentBlock,
    Implementation,
    ListSessionsResponse,
    LoadSessionResponse,
    McpServerStdio,
    ResourceContentBlock,
    ResumeSessionResponse,
    SessionCapabilities,
    SessionCloseCapabilities,
    SessionConfigOptionSelect,
    SessionConfigSelectOption,
    SessionForkCapabilities,
    SessionListCapabilities,
    SessionResumeCapabilities,
    SetSessionConfigOptionResponse,
    SetSessionModeResponse,
    SseMcpServer,
    TextContentBlock,
)


class MockAgent(Agent):
    _conn: Client

    def __init__(self) -> None:
        self._sessions: dict[str, list[str]] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._models: dict[str, str] = {}
        self._modes: dict[str, str] = {}
        self._model_calls: dict[str, int] = {}
        self._mode_calls: dict[str, int] = {}
        self._barrier = asyncio.Event()
        self._barrier_waiters = 0
        self._initialized = False

    def on_connect(self, conn: Client) -> None:
        self._conn = conn

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: Any = None,
        client_info: Any = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        self._initialized = True
        return InitializeResponse(
            protocol_version=protocol_version,
            agent_capabilities=AgentCapabilities(
                load_session=True,
                session_capabilities=SessionCapabilities(
                    close=SessionCloseCapabilities(),
                    fork=SessionForkCapabilities(),
                    list=SessionListCapabilities(),
                    resume=SessionResumeCapabilities(),
                ),
            ),
            agent_info=Implementation(name="mock-agent", title="Mock Agent", version="0.1.0"),
        )

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[HttpMcpServer | SseMcpServer | AcpMcpServer | McpServerStdio]
        | None = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        if not self._initialized:
            raise RuntimeError("initialize must run before session/new")
        session_id = uuid4().hex[:12]
        self._sessions[session_id] = []
        self._cancel_events[session_id] = asyncio.Event()
        model_option = SessionConfigOptionSelect(
            type="select",
            id="model",
            name="Model",
            category="model",
            current_value="default",
            options=[
                SessionConfigSelectOption(name="Default", value="default"),
                SessionConfigSelectOption(name="Model A", value="model-a"),
                SessionConfigSelectOption(name="Model B", value="model-b"),
            ],
        )
        return NewSessionResponse(session_id=session_id, config_options=[model_option])

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[HttpMcpServer | SseMcpServer | AcpMcpServer | McpServerStdio]
        | None = None,
        additional_directories: list[str] | None = None,
        **kwargs: Any,
    ) -> LoadSessionResponse | None:
        if not self._initialized:
            raise RuntimeError("initialize must run before session/load")
        if session_id == "load-fail":
            raise RuntimeError("load failed")
        if session_id not in self._sessions:
            self._sessions[session_id] = ["history"] if session_id.startswith("load-") else []
            self._cancel_events[session_id] = asyncio.Event()
        elif not session_id.startswith("load-"):
            self._sessions[session_id].append("reloaded")
        if session_id.startswith("load-"):
            await self._send_text(session_id, "history")
            if session_id == "load-slow":
                await asyncio.sleep(0.2)
        return LoadSessionResponse()

    async def set_session_mode(
        self, session_id: str, mode_id: str, **kwargs: Any
    ) -> SetSessionModeResponse | None:
        self._modes[session_id] = mode_id
        self._mode_calls[session_id] = self._mode_calls.get(session_id, 0) + 1
        return SetSessionModeResponse()

    async def prompt(
        self,
        session_id: str,
        prompt: list[
            TextContentBlock
            | ImageContentBlock
            | AudioContentBlock
            | ResourceContentBlock
            | EmbeddedResourceContentBlock
        ],
        **kwargs: Any,
    ) -> PromptResponse:
        prompt_text = ""
        for block in prompt:
            if hasattr(block, "text"):
                prompt_text += block.text

        self._sessions.setdefault(session_id, []).append(prompt_text)
        cancel_event = self._cancel_events.setdefault(session_id, asyncio.Event())
        cancel_event.clear()

        if prompt_text.startswith("error"):
            return PromptResponse(stop_reason="refusal")

        if prompt_text.startswith("slow:"):
            delay = int(prompt_text.split(":")[1])
            try:
                await asyncio.wait_for(cancel_event.wait(), timeout=delay)
                return PromptResponse(stop_reason="cancelled")
            except asyncio.TimeoutError:
                pass
            await self._send_text(session_id, f"waited {delay}s")
            return PromptResponse(stop_reason="end_turn")

        if prompt_text.startswith("chunkslow:"):
            delay = int(prompt_text.split(":")[1])
            await self._send_text(session_id, "started")
            try:
                await asyncio.wait_for(cancel_event.wait(), timeout=delay)
                return PromptResponse(stop_reason="cancelled")
            except asyncio.TimeoutError:
                pass
            await self._send_text(session_id, "finished")
            return PromptResponse(stop_reason="end_turn")

        if prompt_text.startswith("supersede:"):
            return PromptResponse(stop_reason="cancelled")

        if prompt_text.startswith("stderr:"):
            print(prompt_text.split(":", 1)[1], file=sys.stderr, flush=True)
            await self._send_text(session_id, prompt_text.split(":", 1)[1])
            return PromptResponse(stop_reason="end_turn")

        if prompt_text == "settings":
            settings = (
                f"{self._models.get(session_id, '-')}/"
                f"{self._modes.get(session_id, '-')}/"
                f"{self._model_calls.get(session_id, 0)}/"
                f"{self._mode_calls.get(session_id, 0)}"
            )
            await self._send_text(session_id, settings)
            return PromptResponse(stop_reason="end_turn")

        if prompt_text.startswith("burst:"):
            for chunk in prompt_text.split(":", 1)[1].split("|"):
                await self._send_text(session_id, chunk)
                await asyncio.sleep(0)
            return PromptResponse(stop_reason="end_turn")

        if prompt_text.startswith("barrier:"):
            self._barrier_waiters += 1
            if self._barrier_waiters >= 2:
                self._barrier.set()
            await self._barrier.wait()
            await self._send_text(session_id, prompt_text.split(":", 1)[1])
            return PromptResponse(stop_reason="end_turn")

        if prompt_text.startswith("tool:"):
            title = prompt_text.split(":", 1)[1]
            tool_id = uuid4().hex[:8]
            tc = start_tool_call(tool_call_id=tool_id, title=title, kind="read")
            await self._conn.session_update(session_id=session_id, update=tc)
            done = update_tool_call(tool_call_id=tool_id, status="completed")
            await self._conn.session_update(session_id=session_id, update=done)
            await self._send_text(session_id, f"tool {title} done")
            return PromptResponse(stop_reason="end_turn")

        if prompt_text.startswith("tool-edit:"):
            title = prompt_text.split(":", 1)[1]
            tool_id = uuid4().hex[:8]
            tc = start_tool_call(tool_call_id=tool_id, title=title, kind="edit")
            await self._conn.session_update(session_id=session_id, update=tc)
            done = update_tool_call(tool_call_id=tool_id, status="completed")
            await self._conn.session_update(session_id=session_id, update=done)
            await self._send_text(session_id, f"edit {title} done")
            return PromptResponse(stop_reason="end_turn")

        if prompt_text.startswith("large:"):
            kb = int(prompt_text.split(":")[1])
            payload = "X" * (kb * 1024)
            await self._send_text(session_id, payload)
            return PromptResponse(stop_reason="end_turn")

        if prompt_text.startswith("multi:"):
            text = prompt_text.split(":", 1)[1]
            history = self._sessions.get(session_id, [])
            response = f"turn {len(history)}: {text}"
            await self._send_text(session_id, response)
            return PromptResponse(stop_reason="end_turn")

        # Default: echo
        await self._send_text(session_id, prompt_text)
        return PromptResponse(stop_reason="end_turn")

    async def _send_text(self, session_id: str, text: str) -> None:
        chunk = update_agent_message(text_block(text))
        await self._conn.session_update(session_id=session_id, update=chunk)

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        self._cancel_events.setdefault(session_id, asyncio.Event()).set()

    async def close_session(self, session_id: str, **kwargs: Any) -> CloseSessionResponse:
        if session_id not in self._sessions:
            raise ValueError(f"unknown session id: {session_id}")
        self._sessions.pop(session_id)
        self._cancel_events.pop(session_id, None)
        self._models.pop(session_id, None)
        self._modes.pop(session_id, None)
        self._model_calls.pop(session_id, None)
        self._mode_calls.pop(session_id, None)
        return CloseSessionResponse()

    async def list_sessions(
        self, cwd: str | None = None, cursor: str | None = None, **kwargs: Any
    ) -> ListSessionsResponse:
        return ListSessionsResponse(sessions=[])

    async def set_config_option(
        self, config_id: str, session_id: str, value: str | bool, **kwargs: Any
    ) -> SetSessionConfigOptionResponse | None:
        if config_id == "model" and isinstance(value, str):
            self._models[session_id] = value
            self._model_calls[session_id] = self._model_calls.get(session_id, 0) + 1
        return SetSessionConfigOptionResponse(config_options=[])

    async def authenticate(self, method_id: str, **kwargs: Any) -> AuthenticateResponse | None:
        return AuthenticateResponse()

    async def fork_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[HttpMcpServer | SseMcpServer | AcpMcpServer | McpServerStdio]
        | None = None,
        **kwargs: Any,
    ) -> ForkSessionResponse:
        return ForkSessionResponse(session_id=uuid4().hex[:12])

    async def resume_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[HttpMcpServer | SseMcpServer | AcpMcpServer | McpServerStdio]
        | None = None,
        **kwargs: Any,
    ) -> ResumeSessionResponse:
        return ResumeSessionResponse()

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        pass


async def main() -> None:
    await run_agent(MockAgent())


if __name__ == "__main__":
    asyncio.run(main())
