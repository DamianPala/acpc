"""Tests for acpc.client module."""

from __future__ import annotations

import asyncio

import pytest

from acp.schema import (
    AgentMessageChunk,
    PermissionOption,
    TextContentBlock,
    ToolCallStart,
    ToolCallUpdate,
    ToolKind,
)

from acpc.client import AcpcClient, PermissionLevel
from acpc.output import OutputHandler, OutputMode


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def output() -> OutputHandler:
    return OutputHandler(mode=OutputMode.TEXT)


def _make_agent_message_chunk(text: str) -> AgentMessageChunk:
    return AgentMessageChunk(
        session_update="agent_message_chunk",
        content=TextContentBlock(type="text", text=text),
    )


def _make_tool_call_start(
    title: str,
    kind: ToolKind = "read",
    tool_call_id: str = "tc-1",
) -> ToolCallStart:
    return ToolCallStart(
        session_update="tool_call",
        title=title,
        kind=kind,
        tool_call_id=tool_call_id,
    )


def _make_permission_options() -> list[PermissionOption]:
    return [
        PermissionOption(
            option_id="allow-once",
            name="Allow once",
            kind="allow_once",
        ),
        PermissionOption(
            option_id="reject-once",
            name="Reject once",
            kind="reject_once",
        ),
    ]


def _make_tool_call_update(
    kind: ToolKind = "edit",
    title: str = "Write file",
) -> ToolCallUpdate:
    return ToolCallUpdate(
        tool_call_id="tc-1",
        kind=kind,
        title=title,
    )


# ---------------------------------------------------------------------------
# session_update dispatch
# ---------------------------------------------------------------------------


class TestSessionUpdate:
    def test_dispatches_agent_message_chunk(
        self,
        output: OutputHandler,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        client = AcpcClient(output, PermissionLevel.ALL, is_tty=False)
        update = _make_agent_message_chunk("hello")

        asyncio.run(client.session_update("sess-1", update))

        captured = capsys.readouterr()
        assert captured.out == "hello"

    def test_dispatches_tool_call(
        self,
        output: OutputHandler,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        client = AcpcClient(output, PermissionLevel.ALL, is_tty=False)
        update = _make_tool_call_start("Read file.py", kind="read")

        asyncio.run(client.session_update("sess-1", update))

        captured = capsys.readouterr()
        assert "tool:" in captured.err
        assert "Read file.py" in captured.err


# ---------------------------------------------------------------------------
# Permission policy
# ---------------------------------------------------------------------------


class TestPermissions:
    def test_all_allows_everything(self, output: OutputHandler) -> None:
        client = AcpcClient(output, PermissionLevel.ALL, is_tty=False)
        options = _make_permission_options()
        tc = _make_tool_call_update(kind="delete", title="Remove file")

        resp = asyncio.run(client.request_permission(options, "sess-1", tc))

        assert resp.outcome.outcome == "selected"

    def test_read_denies_edit(self, output: OutputHandler) -> None:
        client = AcpcClient(output, PermissionLevel.READ, is_tty=False)
        options = _make_permission_options()
        tc = _make_tool_call_update(kind="edit", title="Edit file")

        resp = asyncio.run(client.request_permission(options, "sess-1", tc))

        assert resp.outcome.outcome == "cancelled"

    def test_write_allows_edit(self, output: OutputHandler) -> None:
        client = AcpcClient(output, PermissionLevel.WRITE, is_tty=False)
        options = _make_permission_options()
        tc = _make_tool_call_update(kind="edit", title="Edit file")

        resp = asyncio.run(client.request_permission(options, "sess-1", tc))

        assert resp.outcome.outcome == "selected"

    def test_write_denies_delete(self, output: OutputHandler) -> None:
        client = AcpcClient(output, PermissionLevel.WRITE, is_tty=False)
        options = _make_permission_options()
        tc = _make_tool_call_update(kind="delete", title="Delete file")

        resp = asyncio.run(client.request_permission(options, "sess-1", tc))

        assert resp.outcome.outcome == "cancelled"

    def test_none_denies_everything(self, output: OutputHandler) -> None:
        client = AcpcClient(output, PermissionLevel.NONE, is_tty=False)
        options = _make_permission_options()
        tc = _make_tool_call_update(kind="read", title="Read file")

        resp = asyncio.run(client.request_permission(options, "sess-1", tc))

        assert resp.outcome.outcome == "cancelled"

    def test_prompt_non_tty_denies_write(
        self,
        output: OutputHandler,
    ) -> None:
        client = AcpcClient(output, PermissionLevel.PROMPT, is_tty=False)
        options = _make_permission_options()
        tc = _make_tool_call_update(kind="edit", title="Edit file")

        resp = asyncio.run(client.request_permission(options, "sess-1", tc))

        assert resp.outcome.outcome == "cancelled"

    def test_prompt_allows_read_without_tty(
        self,
        output: OutputHandler,
    ) -> None:
        client = AcpcClient(output, PermissionLevel.PROMPT, is_tty=False)
        options = _make_permission_options()
        tc = _make_tool_call_update(kind="read", title="Read file")

        resp = asyncio.run(client.request_permission(options, "sess-1", tc))

        assert resp.outcome.outcome == "selected"
