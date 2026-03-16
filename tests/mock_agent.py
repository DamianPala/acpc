"""Mock ACP agent for integration tests.

Standalone script that speaks ACP protocol over stdio.
Behavior controlled by prompt text:

- Any text: echoes it back as agent_message_chunk
- "tool:TITLE": simulates a tool call with given title (kind=read)
- "tool-edit:TITLE": simulates a tool call requiring edit permission
- "slow:N": waits N seconds before responding (for timeout tests)
- "error": returns stop_reason=refusal
- "multi:TEXT": echoes text, supports load_session for multi-turn
"""

import asyncio
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
    AgentCapabilities,
    AudioContentBlock,
    AuthenticateResponse,
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
    SetSessionConfigOptionResponse,
    SetSessionModeResponse,
    SetSessionModelResponse,
    SseMcpServer,
    TextContentBlock,
)


class MockAgent(Agent):
    _conn: Client

    def __init__(self) -> None:
        self._sessions: dict[str, list[str]] = {}

    def on_connect(self, conn: Client) -> None:
        self._conn = conn

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: Any = None,
        client_info: Any = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        return InitializeResponse(
            protocol_version=protocol_version,
            agent_capabilities=AgentCapabilities(load_session=True),
            agent_info=Implementation(name="mock-agent", title="Mock Agent", version="0.1.0"),
        )

    async def new_session(
        self,
        cwd: str,
        mcp_servers: list[HttpMcpServer | SseMcpServer | McpServerStdio] | None = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        session_id = uuid4().hex[:12]
        self._sessions[session_id] = []
        return NewSessionResponse(session_id=session_id)

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[HttpMcpServer | SseMcpServer | McpServerStdio] | None = None,
        **kwargs: Any,
    ) -> LoadSessionResponse | None:
        if session_id not in self._sessions:
            self._sessions[session_id] = []
        return LoadSessionResponse()

    async def set_session_model(
        self, model_id: str, session_id: str, **kwargs: Any
    ) -> SetSessionModelResponse | None:
        return SetSessionModelResponse()

    async def set_session_mode(
        self, mode_id: str, session_id: str, **kwargs: Any
    ) -> SetSessionModeResponse | None:
        return SetSessionModeResponse()

    async def prompt(
        self,
        prompt: list[
            TextContentBlock
            | ImageContentBlock
            | AudioContentBlock
            | ResourceContentBlock
            | EmbeddedResourceContentBlock
        ],
        session_id: str,
        **kwargs: Any,
    ) -> PromptResponse:
        prompt_text = ""
        for block in prompt:
            if hasattr(block, "text"):
                prompt_text += block.text

        self._sessions.setdefault(session_id, []).append(prompt_text)

        if prompt_text.startswith("error"):
            return PromptResponse(stop_reason="refusal")

        if prompt_text.startswith("slow:"):
            delay = int(prompt_text.split(":")[1])
            await asyncio.sleep(delay)
            await self._send_text(session_id, f"waited {delay}s")
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
        pass

    async def list_sessions(
        self, cursor: str | None = None, cwd: str | None = None, **kwargs: Any
    ) -> ListSessionsResponse:
        return ListSessionsResponse(sessions=[])

    async def set_config_option(
        self, config_id: str, session_id: str, value: str, **kwargs: Any
    ) -> SetSessionConfigOptionResponse | None:
        return SetSessionConfigOptionResponse(config_options=[])

    async def authenticate(self, method_id: str, **kwargs: Any) -> AuthenticateResponse | None:
        return AuthenticateResponse()

    async def fork_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[HttpMcpServer | SseMcpServer | McpServerStdio] | None = None,
        **kwargs: Any,
    ) -> ForkSessionResponse:
        return ForkSessionResponse(session_id=uuid4().hex[:12])

    async def resume_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[HttpMcpServer | SseMcpServer | McpServerStdio] | None = None,
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
