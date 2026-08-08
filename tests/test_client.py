"""Behavioral tests for the transcript-backed ACP client."""

import asyncio
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from acp import PROTOCOL_VERSION, text_block
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
)

from acpc.client import AcpcClient
from acpc.permissions import PermissionLevel
from acpc.registry import ModeSpec
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
    assert len(errors) == 5
    assert all(event["message"].startswith("permission denied:") for event in errors)
    assert "switch_mode:yolo" in client.answer


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


def test_switch_mode_undeclared_target_requires_all_but_allows_at_all(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "acpc-state"))
    modes = {"plan": ModeSpec(grants="read", delegates=True)}
    options = [
        PermissionOption(option_id="allow", name="Allow", kind="allow_once"),
        PermissionOption(option_id="deny", name="Deny", kind="reject_once"),
    ]

    async def request(level: PermissionLevel, root: Path) -> tuple[Any, list[dict[str, Any]]]:
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
        return response, transcript.read().events

    refused, refused_events = asyncio.run(request(PermissionLevel.READ, tmp_path / "read"))
    allowed, allowed_events = asyncio.run(request(PermissionLevel.ALL, tmp_path / "all"))

    assert isinstance(refused.outcome, DeniedOutcome)
    assert refused_events[0]["decision"] == "deny"
    assert "switch_mode yolo" in refused_events[1]["message"]
    assert "--permissions all" in refused_events[1]["message"]
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
    assert "--permissions all" in events[1]["message"]


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
