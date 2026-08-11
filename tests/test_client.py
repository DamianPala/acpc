"""Behavioral tests for the transcript-backed ACP client."""

import asyncio
import sys
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from acp import PROTOCOL_VERSION, RequestError, text_block
from acp.schema import (
    AgentMessageChunk,
    AgentThoughtChunk,
    AllowedOutcome,
    AvailableCommand,
    AvailableCommandsUpdate,
    DeniedOutcome,
    NewSessionResponse,
    PermissionOption,
    SessionMode,
    SessionModeState,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
    UsageUpdate,
    UserMessageChunk,
)

from acpc.client import AcpcClient
from acpc.permissions import PermissionLevel
from acpc.registry import ModeSpec
from acpc.runner import verify_replayed_prompts
from acpc.spawn import spawn_adapter
from acpc.transcript import Transcript

MOCK_AGENT_SCRIPT = str(Path(__file__).with_name("mock_agent.py"))
MOCK_MODES = {
    "default": ModeSpec(grants="read", delegates=True),
    "acceptEdits": ModeSpec(grants="none", delegates=False),
    "plan": ModeSpec(grants="execute", delegates=True),
    "yolo": ModeSpec(grants="all", delegates=False),
}


def _make_client(
    tmp_path: Path,
    level: PermissionLevel,
    modes: Mapping[str, ModeSpec] | None = None,
) -> tuple[AcpcClient, Transcript]:
    state_root = tmp_path / "acpc-state"
    transcript = Transcript(
        state_root / "sessions" / "abcd" / "transcript.ndjson",
        clock=lambda: 100.0,
    )
    return (
        AcpcClient(
            transcript,
            level,
            modes=MOCK_MODES if modes is None else modes,
            clock=lambda: 100.0,
        ),
        transcript,
    )


def _spawn(client: AcpcClient, tmp_path: Path):
    return spawn_adapter(
        client,
        sys.executable,
        MOCK_AGENT_SCRIPT,
        env={
            "ACPC_HOME": str(tmp_path / "acpc-state"),
            "HOME": str(tmp_path),
            "PATH": "/usr/bin:/bin",
        },
        cwd=str(tmp_path),
    )


async def _drain_updates(predicate: Any) -> None:
    for _ in range(1000):
        if predicate():
            return
        await asyncio.sleep(0)


def test_answer_is_only_agent_messages_and_transcript_keeps_stream_order(
    monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "acpc-state"))
    client, transcript = _make_client(tmp_path, PermissionLevel.READ)

    async def scenario() -> None:
        async with _spawn(client, tmp_path) as (conn, _process):
            await conn.initialize(protocol_version=PROTOCOL_VERSION)
            session = await conn.new_session(cwd=str(tmp_path))
            client.capture_advertised(session)
            await client.session_update(
                session.session_id,
                AgentThoughtChunk(
                    content=text_block("private thought"),
                    session_update="agent_thought_chunk",
                ),
            )
            await conn.prompt(
                session_id=session.session_id, prompt=[text_block("burst:first|second")]
            )
            await _drain_updates(lambda: client.answer == "firstsecond")
            client.flush()

    asyncio.run(scenario())

    events = transcript.read().events
    assert client.answer == "firstsecond"
    # The two burst chunks are one message, so they land as one event.
    assert [event["type"] for event in events] == ["thought", "msg"]
    assert events[0]["text"] == "private thought"
    assert events[1]["text"] == "firstsecond"
    assert "private thought" not in client.answer


def test_replay_is_silent_and_collects_user_messages_without_flushing_pending_prose(
    tmp_path: Path,
) -> None:
    client, transcript = _make_client(tmp_path, PermissionLevel.READ)

    async def scenario() -> tuple[list[str], int]:
        await client.session_update(
            "adapter-session",
            UsageUpdate(session_update="usage_update", used=7, size=100),
        )
        await client.session_update(
            "adapter-session",
            AgentMessageChunk(content=text_block("before"), session_update="agent_message_chunk"),
        )
        events_before = transcript.read()
        async with client.replaying("adapter-session") as sink:
            await client.session_update(
                "adapter-session",
                AgentMessageChunk(
                    content=text_block("replayed answer"), session_update="agent_message_chunk"
                ),
            )
            await client.session_update(
                "adapter-session",
                UserMessageChunk(
                    content=text_block("first "),
                    message_id="user-1",
                    session_update="user_message_chunk",
                ),
            )
            await client.session_update(
                "adapter-session",
                UserMessageChunk(
                    content=text_block("message"),
                    message_id="user-1",
                    session_update="user_message_chunk",
                ),
            )
            await client.session_update(
                "adapter-session",
                UserMessageChunk(
                    content=text_block("second"),
                    message_id="user-2",
                    session_update="user_message_chunk",
                ),
            )
            await client.session_update(
                "adapter-session",
                UserMessageChunk(
                    content=text_block("anonymous "), session_update="user_message_chunk"
                ),
            )
            await client.session_update(
                "adapter-session",
                UserMessageChunk(content=text_block("run"), session_update="user_message_chunk"),
            )
            await client.session_update(
                "adapter-session",
                AgentMessageChunk(
                    content=text_block("separator"), session_update="agent_message_chunk"
                ),
            )
            await client.session_update(
                "adapter-session",
                UserMessageChunk(
                    content=text_block("new run"), session_update="user_message_chunk"
                ),
            )
            await client.session_update(
                "adapter-session",
                UsageUpdate(session_update="usage_update", used=999, size=1000),
            )
        return sink.user_messages, events_before.next_cursor

    messages, cursor_before = asyncio.run(scenario())
    assert messages == ["first message", "second", "anonymous run", "new run"]
    assert client.answer == "before"
    assert client.tokens == 7
    assert client.cost is None
    events_after = transcript.read()
    assert events_after.events == [
        {"type": "usage", "tokens": 7, "cost": None, "ts": 100.0, "i": 1}
    ]
    assert events_after.next_cursor == cursor_before
    client.flush()
    assert transcript.read().events[1]["text"] == "before"


def test_replay_collection_uses_ordered_frames_before_delayed_callbacks(tmp_path: Path) -> None:
    """A restore response must not outrun a delayed session/update callback."""
    client, transcript = _make_client(tmp_path, PermissionLevel.READ)

    class RawConnection:
        def __init__(self) -> None:
            self.observers: list[Any] = []

        def add_observer(self, observer: Any) -> None:
            self.observers.append(observer)

        def emit(self, message: Mapping[str, Any]) -> None:
            event = SimpleNamespace(
                direction=SimpleNamespace(value="incoming"), message=dict(message)
            )
            for observer in self.observers:
                observer(event)

    raw = RawConnection()
    client.on_connect(SimpleNamespace(_conn=raw))
    frame = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "adapter-session",
            "update": {
                "sessionUpdate": "user_message_chunk",
                "content": {"type": "text", "text": "stored prompt"},
                "messageId": "replay-1",
            },
        },
    }

    async def restore_response() -> None:
        """Model restore sending replay frames before its response arrives."""
        raw.emit(frame)
        await asyncio.sleep(0)

    async def scenario() -> list[str]:
        async with client.replaying("adapter-session") as sink:
            await restore_response()

            async def delayed_callback() -> None:
                await asyncio.sleep(0.05)
                await client.session_update(
                    "adapter-session",
                    UserMessageChunk(
                        session_update="user_message_chunk",
                        content=text_block("stored prompt"),
                        message_id="replay-1",
                    ),
                )

            callback = asyncio.create_task(delayed_callback())
        await callback
        return sink.user_messages

    assert asyncio.run(scenario()) == ["stored prompt"]
    assert transcript.read().events == []


def test_simultaneous_replays_scope_frames_to_their_adapter_sessions(tmp_path: Path) -> None:
    """A shared raw connection cannot feed one cold resume another's history."""
    client_a, _ = _make_client(tmp_path / "a", PermissionLevel.READ)
    client_b, _ = _make_client(tmp_path / "b", PermissionLevel.READ)

    class SharedRawConnection:
        def __init__(self) -> None:
            self.observers: list[Any] = []

        def add_observer(self, observer: Any) -> None:
            self.observers.append(observer)

        def emit(self, session_id: str, text: str) -> None:
            event = SimpleNamespace(
                direction=SimpleNamespace(value="incoming"),
                message={
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": session_id,
                        "update": {
                            "sessionUpdate": "user_message_chunk",
                            "content": {"type": "text", "text": text},
                            "messageId": f"replay-{session_id}",
                        },
                    },
                },
            )
            for observer in self.observers:
                observer(event)

    raw = SharedRawConnection()
    client_a.on_connect(SimpleNamespace(_conn=raw))
    client_b.on_connect(SimpleNamespace(_conn=raw))

    async def scenario() -> tuple[list[str], list[str]]:
        a_ready = asyncio.Event()
        b_ready = asyncio.Event()
        start = asyncio.Event()
        foreign_sent = asyncio.Event()
        b_finished = asyncio.Event()

        async def resume_a() -> None:
            a_ready.set()
            await start.wait()
            await b_ready.wait()
            raw.emit("adapter-b", "wrong")
            foreign_sent.set()
            await b_finished.wait()
            raw.emit("adapter-a", "alpha")

        async def resume_b() -> None:
            b_ready.set()
            await start.wait()
            await foreign_sent.wait()
            b_finished.set()

        async with (
            client_a.replaying("adapter-a", raw) as sink_a,
            client_b.replaying("adapter-b", raw) as sink_b,
        ):
            task_a = asyncio.create_task(resume_a())
            task_b = asyncio.create_task(resume_b())
            await asyncio.gather(a_ready.wait(), b_ready.wait())
            start.set()
            await asyncio.gather(task_a, task_b)
            return sink_a.user_messages, sink_b.user_messages

    a_messages, b_messages = asyncio.run(scenario())
    assert a_messages == ["alpha"]
    assert b_messages == ["wrong"]
    verify_replayed_prompts("adapter-a", [(Path("a-prompt"), "alpha")], a_messages)
    verify_replayed_prompts("adapter-b", [(Path("b-prompt"), "wrong")], b_messages)


def test_answer_keeps_interleaved_narration_and_excludes_tool_events(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "acpc-state"))
    client, transcript = _make_client(tmp_path, PermissionLevel.READ)

    async def scenario() -> None:
        await client.session_update(
            "adapter-session",
            AgentMessageChunk(content=text_block("before "), session_update="agent_message_chunk"),
        )
        await client.session_update(
            "adapter-session",
            ToolCallStart(
                tool_call_id="tool-1",
                title="Read notes.md",
                kind="read",
                status="in_progress",
                session_update="tool_call",
            ),
        )
        await client.session_update(
            "adapter-session",
            AgentMessageChunk(content=text_block("between "), session_update="agent_message_chunk"),
        )
        await client.session_update(
            "adapter-session",
            ToolCallProgress(
                tool_call_id="tool-1",
                status="completed",
                raw_output={"output": "hidden tool output"},
                session_update="tool_call_update",
            ),
        )
        await client.session_update(
            "adapter-session",
            AgentThoughtChunk(
                content=text_block("another private thought"),
                session_update="agent_thought_chunk",
            ),
        )
        await client.session_update(
            "adapter-session",
            AgentMessageChunk(content=text_block("after"), session_update="agent_message_chunk"),
        )
        client.flush()

    asyncio.run(scenario())

    events = transcript.read().events
    assert client.answer == "before \n\nbetween \n\nafter"
    assert [event["type"] for event in events] == ["msg", "msg", "tool", "thought", "msg"]
    tool = events[2]
    assert tool == {
        "type": "tool",
        "name": "Read",
        "args_summary": "notes.md",
        "status": "completed",
        "duration_ms": 0,
        "ts": 100.0,
        "i": 3,
    }
    assert "hidden tool output" not in client.answer
    assert "another private thought" not in client.answer


def test_answer_separates_messages_at_a_tool_boundary(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "acpc-state"))
    client, _transcript = _make_client(tmp_path, PermissionLevel.READ)

    async def scenario() -> None:
        await client.session_update(
            "adapter-session",
            AgentMessageChunk(content=text_block("preamble"), session_update="agent_message_chunk"),
        )
        await client.session_update(
            "adapter-session",
            ToolCallStart(
                tool_call_id="tool-1",
                title="Read notes.md",
                kind="read",
                status="in_progress",
                session_update="tool_call",
            ),
        )
        await client.session_update(
            "adapter-session",
            ToolCallProgress(
                tool_call_id="tool-1",
                status="completed",
                session_update="tool_call_update",
            ),
        )
        await client.session_update(
            "adapter-session",
            AgentMessageChunk(content=text_block("answer"), session_update="agent_message_chunk"),
        )

    asyncio.run(scenario())

    assert client.answer == "preamble\n\nanswer"


def test_a_streamed_message_stays_joined_across_coalescer_cuts(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "acpc-state"))
    chunks = [f"{index:04d}" for index in range(5000)]
    client, transcript = _make_ticking_client(
        tmp_path, [index * 0.001 for index in range(len(chunks))]
    )

    async def scenario() -> None:
        for chunk in chunks:
            await client.session_update(
                "adapter-session",
                AgentMessageChunk(content=text_block(chunk), session_update="agent_message_chunk"),
            )
        client.flush()

    asyncio.run(scenario())

    assert client.answer == "".join(chunks)
    assert len(transcript.read().events) > 1


def test_a_thought_chunk_is_an_answer_boundary(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "acpc-state"))
    client, _transcript = _make_client(tmp_path, PermissionLevel.READ)

    async def scenario() -> None:
        await client.session_update(
            "adapter-session",
            AgentMessageChunk(content=text_block("before"), session_update="agent_message_chunk"),
        )
        await client.session_update(
            "adapter-session",
            AgentThoughtChunk(content=text_block("private"), session_update="agent_thought_chunk"),
        )
        await client.session_update(
            "adapter-session",
            AgentMessageChunk(content=text_block("after"), session_update="agent_message_chunk"),
        )

    asyncio.run(scenario())

    assert client.answer == "before\n\nafter"


def test_leading_message_has_no_separator_after_a_thought(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "acpc-state"))
    client, _transcript = _make_client(tmp_path, PermissionLevel.READ)

    async def scenario() -> None:
        await client.session_update(
            "adapter-session",
            AgentThoughtChunk(content=text_block("private"), session_update="agent_thought_chunk"),
        )
        await client.session_update(
            "adapter-session",
            AgentMessageChunk(content=text_block("answer"), session_update="agent_message_chunk"),
        )

    asyncio.run(scenario())

    assert client.answer == "answer"
    assert not client.answer.startswith("\n\n")


def test_existing_blank_line_is_not_doubled_at_a_boundary(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "acpc-state"))
    client, _transcript = _make_client(tmp_path, PermissionLevel.READ)

    async def scenario() -> None:
        await client.session_update(
            "adapter-session",
            AgentMessageChunk(
                content=text_block("first\n\n"), session_update="agent_message_chunk"
            ),
        )
        await client.session_update(
            "adapter-session",
            ToolCallStart(
                tool_call_id="tool-1",
                title="Read notes.md",
                kind="read",
                status="in_progress",
                session_update="tool_call",
            ),
        )
        await client.session_update(
            "adapter-session",
            AgentMessageChunk(content=text_block("second"), session_update="agent_message_chunk"),
        )

    asyncio.run(scenario())

    assert client.answer == "first\n\nsecond"


def _make_ticking_client(tmp_path: Path, times: list[float]) -> tuple[AcpcClient, Transcript]:
    """A client whose clock pops the next value from ``times`` on every read."""
    state_root = tmp_path / "acpc-state"
    transcript = Transcript(
        state_root / "sessions" / "abcd" / "transcript.ndjson",
        clock=lambda: 100.0,
    )
    client = AcpcClient(
        transcript,
        PermissionLevel.READ,
        clock=lambda: times.pop(0),
    )
    return client, transcript


def _send_msg(client: AcpcClient, text: str) -> None:
    asyncio.run(
        client.session_update(
            "adapter-session",
            AgentMessageChunk(content=text_block(text), session_update="agent_message_chunk"),
        )
    )


def test_a_stream_pause_cuts_the_coalesced_message(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "acpc-state"))
    client, transcript = _make_ticking_client(tmp_path, [0.0, 0.2, 1.5, 1.6])

    _send_msg(client, "first ")
    _send_msg(client, "part")
    _send_msg(client, "second ")  # 1.3s after the last chunk: a pause
    _send_msg(client, "part")
    client.flush()

    events = transcript.read().events
    assert [event["text"] for event in events] == ["first part", "second part"]
    assert client.answer == "first partsecond part"


def test_a_long_uninterrupted_message_surfaces_while_running(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "acpc-state"))
    client, transcript = _make_ticking_client(tmp_path, [0.0, 0.9, 1.7, 2.5])

    for text in ("a", "b", "c", "d"):  # gaps below the pause cut-off
        _send_msg(client, text)

    # No flush call: the age bound alone must have written the event.
    events = transcript.read().events
    assert [event["text"] for event in events] == ["abcd"]


def test_an_oversized_message_buffer_flushes_on_size(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "acpc-state"))
    client, transcript = _make_client(tmp_path, PermissionLevel.READ)

    _send_msg(client, "x" * 5000)

    events = transcript.read().events
    assert len(events) == 1
    assert events[0]["text"] == "x" * 5000


def test_ask_policy_without_callback_denies_without_reading_stdin(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "acpc-state"))
    client, transcript = _make_client(tmp_path, PermissionLevel.ASK)

    async def scenario() -> Any:
        return await client.request_permission(
            "adapter-session",
            ToolCallUpdate(tool_call_id="tool-2", kind="edit", title="Edit app.py"),
            [
                PermissionOption(option_id="allow", name="Allow", kind="allow_once"),
                PermissionOption(option_id="deny", name="Deny", kind="reject_once"),
            ],
        )

    response = asyncio.run(scenario())

    assert isinstance(response.outcome, DeniedOutcome)
    assert not isinstance(response.outcome, AllowedOutcome)
    events = transcript.read().events
    assert [event["type"] for event in events] == ["permission", "error"]
    assert events[0]["decision"] == "deny"
    assert events[0]["auto"] is False
    assert events[1]["message"] == "permission denied: edit"


def test_tool_usage_and_advertised_data_are_captured(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "acpc-state"))
    client, transcript = _make_client(tmp_path, PermissionLevel.READ)

    async def scenario() -> None:
        async with _spawn(client, tmp_path) as (conn, _process):
            await conn.initialize(protocol_version=PROTOCOL_VERSION)
            session = await conn.new_session(cwd=str(tmp_path))
            client.capture_advertised(session)
            await conn.prompt(session_id=session.session_id, prompt=[text_block("ordinary task")])
            await _drain_updates(lambda: client.tokens == 1200)

    asyncio.run(scenario())

    advertised = client.advertised
    assert [mode["id"] for mode in advertised["modes"]] == [
        "default",
        "acceptEdits",
        "plan",
        "yolo",
    ]
    assert advertised["models"] == ["mock-opus-5", "mock-sonnet-5", "mock-haiku-4-5"]

    events = transcript.read().events
    tools = [event for event in events if event["type"] == "tool"]
    assert [(event["name"], event["args_summary"], event["status"]) for event in tools] == [
        ("Read", "README.md", "completed"),
        ("Grep", 'pattern matching "ordinary task"', "completed"),
        ("Bash", "pytest -q", "completed"),
    ]
    assert all(event["duration_ms"] == 0 for event in tools)
    assert [event for event in events if event["type"] == "usage"] == [
        {"type": "usage", "tokens": 1200, "cost": None, "ts": 100.0, "i": 5}
    ]
    assert [event["i"] for event in events] == list(range(1, len(events) + 1))


def test_permission_denials_are_answered_and_recorded(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "acpc-state"))
    client, transcript = _make_client(tmp_path, PermissionLevel.READ)

    async def scenario() -> None:
        async with _spawn(client, tmp_path) as (conn, _process):
            await conn.initialize(protocol_version=PROTOCOL_VERSION)
            session = await conn.new_session(cwd=str(tmp_path))
            await conn.prompt(session_id=session.session_id, prompt=[text_block("perm scenario")])
            await _drain_updates(lambda: "Denied:" in client.answer)

    asyncio.run(scenario())

    events = transcript.read().events
    permissions = [event for event in events if event["type"] == "permission"]
    errors = [event for event in events if event["type"] == "error"]
    assert [event["decision"] for event in permissions] == [
        "allow",
        "deny",
        "deny",
        "deny",
        "deny",
        "deny",
    ]
    assert all(isinstance(event["auto"], bool) for event in permissions)
    assert len(errors) == 5
    assert all(event["message"].startswith("permission denied:") for event in errors)
    assert "switch_mode:yolo" in client.answer


@pytest.mark.parametrize(
    ("level", "should_write"),
    [
        (PermissionLevel.NONE, False),
        (PermissionLevel.READ, False),
        (PermissionLevel.EDIT, True),
    ],
)
def test_filesystem_callback_write_is_gated_by_policy(
    tmp_path: Path, monkeypatch: Any, level: PermissionLevel, should_write: bool
) -> None:
    """A callback-only filesystem write follows the client's permission policy."""
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "acpc-state"))
    client, _transcript = _make_client(tmp_path, level)
    path = tmp_path / "callback-write.txt"

    async def scenario() -> None:
        async with _spawn(client, tmp_path) as (conn, _process):
            await conn.initialize(protocol_version=PROTOCOL_VERSION)
            session = await conn.new_session(cwd=str(tmp_path))
            await conn.prompt(
                session_id=session.session_id, prompt=[text_block("fs-write:callback-write.txt")]
            )
            await _drain_updates(lambda: "fs write callback-write.txt" in client.answer)

    asyncio.run(scenario())

    assert path.exists() is should_write


@pytest.mark.parametrize(
    ("level", "should_read"),
    [
        (PermissionLevel.NONE, False),
        (PermissionLevel.READ, True),
        (PermissionLevel.EDIT, True),
        (PermissionLevel.EXECUTE, True),
        (PermissionLevel.ALL, True),
    ],
)
def test_filesystem_callback_read_is_gated_by_policy(
    tmp_path: Path, monkeypatch: Any, level: PermissionLevel, should_read: bool
) -> None:
    """A callback-only filesystem read follows the client's permission policy."""
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "acpc-state"))
    source = tmp_path / "callback-read.txt"
    source.write_text("callback content", encoding="utf-8")
    client, _transcript = _make_client(tmp_path, level)

    async def scenario() -> Any:
        if should_read:
            return await client.read_text_file(str(source), "adapter-session")
        with pytest.raises(RequestError):
            await client.read_text_file(str(source), "adapter-session")
        return None

    response = asyncio.run(scenario())

    if should_read:
        assert response.content == "callback content"


def test_client_callback_flushes_buffered_message_before_permission_event(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The transcript keeps agent prose before the callback decision it preceded."""
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "acpc-state"))
    source = tmp_path / "ordered-read.txt"
    source.write_text("callback content", encoding="utf-8")
    client, transcript = _make_client(tmp_path, PermissionLevel.READ)

    async def scenario() -> None:
        await client.session_update(
            "adapter-session",
            AgentMessageChunk(content=text_block("before"), session_update="agent_message_chunk"),
        )
        await client.read_text_file(str(source), "adapter-session")

    asyncio.run(scenario())

    events = transcript.read().events
    assert [(event["type"], event.get("text"), event.get("kind")) for event in events] == [
        ("msg", "before", None),
        ("permission", None, "fs/read_text_file"),
    ]


@pytest.mark.parametrize(
    ("level", "expected_error"),
    [
        (PermissionLevel.NONE, RequestError),
        (PermissionLevel.READ, RequestError),
        (PermissionLevel.EDIT, RequestError),
        (PermissionLevel.EXECUTE, NotImplementedError),
    ],
)
def test_terminal_creation_gate_precedes_unsupported_implementation(
    tmp_path: Path,
    monkeypatch: Any,
    level: PermissionLevel,
    expected_error: type[Exception],
) -> None:
    """Policy refusal is distinct from the terminal feature not being implemented."""
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "acpc-state"))
    client, _transcript = _make_client(tmp_path, level)

    async def scenario() -> None:
        with pytest.raises(expected_error):
            await client.create_terminal("echo hello", "adapter-session")

    asyncio.run(scenario())


def test_filesystem_refusal_is_an_acp_error_and_connection_survives(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A denied callback returns an ACP error, records it, and lets the turn finish."""
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "acpc-state"))
    client, transcript = _make_client(tmp_path, PermissionLevel.NONE)
    path = tmp_path / "refused.txt"

    async def scenario() -> None:
        async with _spawn(client, tmp_path) as (conn, _process):
            await conn.initialize(protocol_version=PROTOCOL_VERSION)
            session = await conn.new_session(cwd=str(tmp_path))
            await conn.prompt(
                session_id=session.session_id, prompt=[text_block("fs-write:refused.txt")]
            )
            await _drain_updates(lambda: "fs write refused.txt error:" in client.answer)
            client.flush()

    asyncio.run(scenario())

    assert not path.exists()
    assert "permission denied: fs/write_text_file" in client.answer
    assert client.denied == {"edit": 1}
    events = transcript.read().events
    assert {event["type"] for event in events} >= {"permission", "error", "msg"}
    permission = next(event for event in events if event["type"] == "permission")
    assert permission["type"] == "permission"
    assert permission["kind"] == "fs/write_text_file"
    assert permission["decision"] == "deny"
    assert permission["auto"] is True
    assert permission["ts"] == 100.0
    assert client.denial_details == {
        "edit": {
            "category": "edit",
            "minimum_policy": "edit",
            "remedy": "pass --permissions edit",
        }
    }


def test_ask_policy_prompts_for_client_edits_but_not_reads(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The callback path uses the same ask rule as request_permission."""
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "acpc-state"))
    source = tmp_path / "ask-read.txt"
    source.write_text("readable", encoding="utf-8")
    prompted: list[str] = []

    def prompt(kind: str, _title: str) -> bool:
        prompted.append(kind)
        return True

    client, _transcript = _make_client(tmp_path, PermissionLevel.ASK)
    client.permission_prompt = prompt

    async def scenario() -> None:
        response = await client.read_text_file(str(source), "adapter-session")
        assert response.content == "readable"
        await client.write_text_file(str(tmp_path / "ask-write.txt"), "written", "adapter-session")

    asyncio.run(scenario())

    assert prompted == ["fs/write_text_file"]


def test_switch_mode_reselection_denies_above_ceiling_and_allows_all(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "acpc-state"))

    async def run(level: PermissionLevel) -> tuple[str, list[dict[str, Any]]]:
        client, transcript = _make_client(tmp_path / level.value, level)
        async with _spawn(client, tmp_path / level.value) as (conn, _process):
            await conn.initialize(protocol_version=PROTOCOL_VERSION)
            session = await conn.new_session(cwd=str(tmp_path))
            await conn.prompt(session_id=session.session_id, prompt=[text_block("perm scenario")])
            await _drain_updates(lambda: "Denied:" in client.answer)
        return client.answer, transcript.read().events

    _read_answer, read_events = asyncio.run(run(PermissionLevel.READ))
    all_answer, all_events = asyncio.run(run(PermissionLevel.ALL))

    read_errors = [event["message"] for event in read_events if event["type"] == "error"]
    assert any("switch_mode yolo" in message for message in read_errors)
    assert any("--permissions all" in message for message in read_errors)
    assert "switch_mode:yolo" in all_answer.split("Allowed:", 1)[1].split("Denied:", 1)[0]
    assert "switch_mode:plan" in all_answer.split("Allowed:", 1)[1].split("Denied:", 1)[0]
    assert sum(event["type"] == "error" for event in read_events) == 5
    assert sum(event["type"] == "error" for event in all_events) == 0


def test_switch_mode_at_or_below_ceiling_allows_later_requests(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "acpc-state"))
    modes = {"plan": ModeSpec(grants="read", delegates=True)}
    client, transcript = _make_client(tmp_path, PermissionLevel.ASK, modes)
    options = [
        PermissionOption(option_id="allow", name="Allow", kind="allow_once"),
        PermissionOption(option_id="deny", name="Deny", kind="reject_once"),
    ]

    async def scenario() -> None:
        first = await client.request_permission(
            "adapter-session",
            ToolCallUpdate(
                tool_call_id="switch",
                kind="switch_mode",
                title="Switch mode",
                raw_input={"target": "plan"},
            ),
            options,
        )
        second = await client.request_permission(
            "adapter-session",
            ToolCallUpdate(tool_call_id="read", kind="read", title="Read"),
            options,
        )
        assert isinstance(first.outcome, AllowedOutcome)
        assert isinstance(second.outcome, AllowedOutcome)

    asyncio.run(scenario())

    assert [event["decision"] for event in transcript.read().events] == ["allow", "allow"]


def test_switch_mode_undeclared_target_uses_all_as_the_working_remedy(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "acpc-state"))
    modes = {"plan": ModeSpec(grants="read", delegates=True)}
    options = [
        PermissionOption(option_id="allow", name="Allow", kind="allow_once"),
        PermissionOption(option_id="deny", name="Deny", kind="reject_once"),
    ]

    async def request(
        level: PermissionLevel, root: Path
    ) -> tuple[Any, list[dict[str, Any]], dict[str, dict[str, Any]]]:
        client, transcript = _make_client(root, level, modes)
        response = await client.request_permission(
            "adapter-session",
            ToolCallUpdate(
                tool_call_id="switch",
                kind="switch_mode",
                title="Switch mode",
                raw_input={"target": "yolo"},
            ),
            options,
        )
        return response, transcript.read().events, client.denial_details

    refused, refused_events, details = asyncio.run(request(PermissionLevel.READ, tmp_path / "read"))
    allowed, allowed_events, _allowed_details = asyncio.run(
        request(PermissionLevel.ALL, tmp_path / "all")
    )

    assert isinstance(refused.outcome, DeniedOutcome)
    assert refused_events[0]["decision"] == "deny"
    assert "switch_mode yolo" in refused_events[1]["message"]
    assert "requires --permissions all" in refused_events[1]["message"]
    assert details == {
        "switch_mode:yolo": {
            "category": "switch_mode",
            "target": "yolo",
            "minimum_policy": "all",
            "remedy": "pass --permissions all",
        }
    }
    assert isinstance(allowed.outcome, AllowedOutcome)
    assert [event["decision"] for event in allowed_events] == ["allow"]


@pytest.mark.parametrize("raw_input", [None, "not a mapping", {"target": ""}])
def test_malformed_switch_mode_target_is_refused_before_lookup(
    tmp_path: Path, monkeypatch: Any, raw_input: Any
) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "acpc-state"))
    client, transcript = _make_client(
        tmp_path,
        PermissionLevel.ALL,
        {"<unknown>": ModeSpec(grants="none", delegates=True)},
    )

    async def scenario() -> Any:
        return await client.request_permission(
            "adapter-session",
            ToolCallUpdate(
                tool_call_id="switch",
                kind="switch_mode",
                title="Switch mode",
                raw_input=raw_input,
            ),
            [
                PermissionOption(option_id="allow", name="Allow", kind="allow_once"),
                PermissionOption(option_id="deny", name="Deny", kind="reject_once"),
            ],
        )

    response = asyncio.run(scenario())

    assert isinstance(response.outcome, DeniedOutcome)
    events = transcript.read().events
    assert events[0]["decision"] == "deny"
    assert "switch_mode <unknown>" in events[1]["message"]
    assert "requires --permissions all" in events[1]["message"]


@pytest.mark.parametrize(
    ("options", "minimum_policy", "remedy"),
    [
        (
            [PermissionOption(option_id="always", name="Always", kind="allow_always")],
            "all",
            "pass --permissions all",
        ),
        (
            [PermissionOption(option_id="deny", name="Deny", kind="reject_once")],
            None,
            "agent offered no allow option",
        ),
    ],
)
def test_allowed_policy_without_a_usable_allow_option_has_an_honest_remedy(
    tmp_path: Path,
    monkeypatch: Any,
    options: list[PermissionOption],
    minimum_policy: str | None,
    remedy: str,
) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "acpc-state"))
    client, transcript = _make_client(tmp_path, PermissionLevel.EXECUTE)

    async def scenario() -> Any:
        return await client.request_permission(
            "adapter-session",
            ToolCallUpdate(tool_call_id="execute", kind="execute", title="Run command"),
            options,
        )

    response = asyncio.run(scenario())

    assert isinstance(response.outcome, DeniedOutcome)
    assert client.denial_details == {
        "execute": {
            "category": "execute",
            "minimum_policy": minimum_policy,
            "remedy": remedy,
        }
    }
    assert transcript.read().events[0]["auto"] is True


def test_declared_unknown_spelling_is_a_real_mode_under_all(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "acpc-state"))
    client, transcript = _make_client(
        tmp_path,
        PermissionLevel.ALL,
        {"<unknown>": ModeSpec(grants="all", delegates=False)},
    )

    async def scenario() -> Any:
        return await client.request_permission(
            "adapter-session",
            ToolCallUpdate(
                tool_call_id="switch",
                kind="switch_mode",
                title="Switch mode",
                raw_input={"target": "<unknown>"},
            ),
            [PermissionOption(option_id="allow", name="Allow", kind="allow_once")],
        )

    response = asyncio.run(scenario())

    assert isinstance(response.outcome, AllowedOutcome)
    assert transcript.read().events[0]["decision"] == "allow"


def test_commands_update_replaces_the_advertised_command_list(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "acpc-state"))
    client, _transcript = _make_client(tmp_path, PermissionLevel.READ)
    session = NewSessionResponse(
        session_id="adapter-session",
        modes=SessionModeState(
            current_mode_id="default",
            available_modes=[SessionMode(id="default", name="Default")],
        ),
        config_options=[],
    )
    client.capture_advertised(session)

    async def scenario() -> None:
        await client.session_update(
            "adapter-session",
            AvailableCommandsUpdate(
                session_update="available_commands_update",
                available_commands=[AvailableCommand(name="review", description="Review")],
            ),
        )

    asyncio.run(scenario())
    assert client.advertised["commands"] == [{"name": "review", "description": "Review"}]
