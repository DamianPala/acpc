"""Tests for acpc.client module."""

from __future__ import annotations

import asyncio
import contextlib
import json

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


def _make_agent_message_chunk(text: str, message_id: str | None = None) -> AgentMessageChunk:
    return AgentMessageChunk(
        session_update="agent_message_chunk",
        content=TextContentBlock(type="text", text=text),
        message_id=message_id,
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

    def test_json_event_preserves_populated_message_id(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        client = AcpcClient(
            OutputHandler(mode=OutputMode.JSON),
            PermissionLevel.ALL,
            is_tty=False,
        )

        asyncio.run(client.session_update("sess-1", _make_agent_message_chunk("hello", "msg-1")))

        event = json.loads(capsys.readouterr().out)
        assert event["messageId"] == "msg-1"

    def test_json_event_omits_absent_message_id(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        client = AcpcClient(
            OutputHandler(mode=OutputMode.JSON),
            PermissionLevel.ALL,
            is_tty=False,
        )

        asyncio.run(client.session_update("sess-1", _make_agent_message_chunk("hello")))

        event = json.loads(capsys.readouterr().out)
        assert "messageId" not in event

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


class TestHistoryReplay:
    """session/load replays the whole transcript; none of it belongs on stdout."""

    def test_replayed_messages_are_suppressed(
        self,
        output: OutputHandler,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        client = AcpcClient(output, PermissionLevel.ALL, is_tty=False)

        with client.replaying_history():
            asyncio.run(client.session_update("sess-1", _make_agent_message_chunk("old turn")))

        captured = capsys.readouterr()
        assert captured.out == ""

    def test_replayed_tool_calls_are_suppressed(
        self,
        output: OutputHandler,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        client = AcpcClient(output, PermissionLevel.ALL, is_tty=False)

        with client.replaying_history():
            asyncio.run(client.session_update("sess-1", _make_tool_call_start("Read old.py")))

        captured = capsys.readouterr()
        assert "Read old.py" not in captured.err

    def test_live_output_resumes_after_replay(
        self,
        output: OutputHandler,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        client = AcpcClient(output, PermissionLevel.ALL, is_tty=False)

        with client.replaying_history():
            asyncio.run(client.session_update("sess-1", _make_agent_message_chunk("old turn")))
        asyncio.run(client.session_update("sess-1", _make_agent_message_chunk("new answer")))

        captured = capsys.readouterr()
        assert captured.out == "new answer"

    def test_flag_is_cleared_when_load_fails(
        self,
        output: OutputHandler,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A failed session/load must not leave the client permanently muted."""
        client = AcpcClient(output, PermissionLevel.ALL, is_tty=False)

        with contextlib.suppress(RuntimeError), client.replaying_history():
            raise RuntimeError("load_session failed")

        assert client.replaying is False
        asyncio.run(client.session_update("sess-1", _make_agent_message_chunk("live")))
        assert capsys.readouterr().out == "live"

    def test_session_id_still_recorded_during_replay(
        self,
        output: OutputHandler,
    ) -> None:
        client = AcpcClient(output, PermissionLevel.ALL, is_tty=False)

        with client.replaying_history():
            asyncio.run(client.session_update("sess-42", _make_agent_message_chunk("old")))

        assert client.session_id == "sess-42"

    def test_replay_state_and_updates_are_scoped_to_each_session(self) -> None:
        events: dict[str, list[str]] = {"A": [], "B": []}
        client = AcpcClient(OutputHandler(mode=OutputMode.QUIET), PermissionLevel.ALL, is_tty=False)

        async def collect_a(frame: dict[str, object]) -> None:
            events["A"].append(frame["update"]["content"]["text"])  # type: ignore[index]

        async def collect_b(frame: dict[str, object]) -> None:
            events["B"].append(frame["update"]["content"]["text"])  # type: ignore[index]

        client.register_session("A", update_sink=collect_a)
        client.register_session("B", update_sink=collect_b)

        async def scenario() -> None:
            with client.replaying_history("A"):
                await client.session_update("A", _make_agent_message_chunk("old A"))
                await client.session_update("B", _make_agent_message_chunk("live B"))
            await client.session_update("A", _make_agent_message_chunk("live A"))

        asyncio.run(scenario())

        assert events == {"A": ["live A"], "B": ["live B"]}


# ---------------------------------------------------------------------------
# Permission policy
# ---------------------------------------------------------------------------


class TestPermissions:
    @pytest.mark.parametrize(
        ("permission_level", "outcome"),
        [(PermissionLevel.ALL, "allow"), (PermissionLevel.NONE, "deny")],
    )
    def test_policy_decision_is_forwarded_to_session_sink(
        self,
        output: OutputHandler,
        permission_level: PermissionLevel,
        outcome: str,
    ) -> None:
        frames: list[dict[str, object]] = []

        async def collect(frame: dict[str, object]) -> None:
            frames.append(frame)

        client = AcpcClient(output, PermissionLevel.NONE, is_tty=False, strict_sessions=True)
        client.register_session(
            "sess-1",
            permission_level=permission_level,
            update_sink=collect,
        )

        asyncio.run(
            client.request_permission(
                _make_permission_options(),
                "sess-1",
                _make_tool_call_update(title="write sentinel.txt"),
            )
        )

        assert frames == [
            {
                "type": "permission",
                "session_id": "sess-1",
                "kind": "edit",
                "title": "write sentinel.txt",
                "outcome": outcome,
            }
        ]

    def test_permissions_are_scoped_to_registered_session(self, output: OutputHandler) -> None:
        client = AcpcClient(output, PermissionLevel.ALL, is_tty=False, strict_sessions=True)
        client.register_session("none", permission_level=PermissionLevel.NONE)
        client.register_session("default", permission_level=PermissionLevel.ALL)
        options = _make_permission_options()
        tc = _make_tool_call_update(kind="edit", title="Edit file")

        denied = asyncio.run(client.request_permission(options, "none", tc))
        allowed = asyncio.run(client.request_permission(options, "default", tc))

        assert denied.outcome.outcome == "cancelled"
        assert allowed.outcome.outcome == "selected"

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


class TestMultiplexedFileOperations:
    def test_unknown_session_is_rejected_for_read(self, output: OutputHandler) -> None:
        client = AcpcClient(output, PermissionLevel.ALL, is_tty=False, strict_sessions=True)

        with pytest.raises(ValueError, match="unknown session id"):
            asyncio.run(client.read_text_file("missing.txt", "unknown"))

    def test_unknown_session_is_rejected_for_write(self, output: OutputHandler) -> None:
        client = AcpcClient(output, PermissionLevel.ALL, is_tty=False, strict_sessions=True)

        with pytest.raises(ValueError, match="unknown session id"):
            asyncio.run(client.write_text_file("text", "missing.txt", "unknown"))
