"""Behavioral tests for the transcript-backed ACP client."""

import asyncio
import gc
import sys
import weakref
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from acp import PROTOCOL_VERSION, RequestError, text_block
from acp.client import ClientSideConnection
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

from acpc.client import (
    MAX_RETAINED_CLOSED_REPLAY_GENERATIONS,
    REPLAY_GENERATION_KEY,
    AcpcClient,
    ReplayTracker,
)
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


class _ObserverOnlyRawConnection:
    """Raw test double that exposes ordered frames but cannot tag callbacks."""

    def __init__(self) -> None:
        self.observers: list[Any] = []

    def add_observer(self, observer: Any) -> None:
        self.observers.append(observer)

    def emit(self, message: Mapping[str, Any]) -> None:
        event = SimpleNamespace(direction=SimpleNamespace(value="incoming"), message=dict(message))
        for observer in self.observers:
            observer(event)

    def emit_outgoing(self, message: Mapping[str, Any]) -> None:
        event = SimpleNamespace(direction=SimpleNamespace(value="outgoing"), message=dict(message))
        for observer in self.observers:
            observer(event)


class _TaggingRawConnection(_ObserverOnlyRawConnection):
    """Raw test double with the receive-to-dispatch seam available."""

    async def _process_message(self, message: object) -> None:
        del message

    async def receive(self, message: Mapping[str, Any]) -> None:
        self.emit(message)
        await self._process_message(message)


@asynccontextmanager
async def _real_dispatch_connection(client: Any) -> AsyncIterator[Any]:
    """Give an in-process ACP client a real Connection dispatcher."""
    accepted: asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = (
        asyncio.get_running_loop().create_future()
    )

    async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if not accepted.done():
            accepted.set_result((reader, writer))
        else:
            writer.close()

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    address = server.sockets[0].getsockname()
    client_reader, client_writer = await asyncio.open_connection(address[0], address[1])
    _server_reader, server_writer = await accepted
    connection = ClientSideConnection(client, client_writer, client_reader, listening=False)
    try:
        yield connection
    finally:
        await connection.close()
        server_writer.close()
        await server_writer.wait_closed()
        server.close()
        await server.wait_closed()


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


def test_replay_collection_uses_ordered_frames_before_delayed_callbacks(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A restore response must not outrun a delayed session/update callback."""
    client, transcript = _make_client(tmp_path, PermissionLevel.READ)
    raw = _TaggingRawConnection()
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

    async def scenario() -> tuple[list[str], str]:
        callback_started = asyncio.Event()
        release_callback = asyncio.Event()
        generation: dict[str, Any] = {}

        async def delayed_callback() -> None:
            callback_started.set()
            await release_callback.wait()
            await client.session_update(
                "adapter-session",
                UserMessageChunk(
                    session_update="user_message_chunk",
                    content=text_block("stored prompt"),
                    message_id="replay-1",
                ),
                **{REPLAY_GENERATION_KEY: generation["value"]},
            )

        async with client.replaying("adapter-session") as sink:
            await raw.receive(frame)
            generation["value"] = frame["params"]["_meta"][REPLAY_GENERATION_KEY]
            callback = asyncio.create_task(delayed_callback())
            await callback_started.wait()
        release_callback.set()
        await callback
        return sink.user_messages, caplog.text

    messages, logs = asyncio.run(scenario())
    assert messages == ["stored prompt"]
    assert "replay suppression:" not in logs
    assert transcript.read().events == []


def test_tagged_replay_after_disconnect_is_suppressed_by_real_dispatcher(
    tmp_path: Path,
) -> None:
    """A queued ACP notification still carries suppression after transport death."""
    _base_client, transcript = _make_client(tmp_path, PermissionLevel.READ)

    class CallbackClient(AcpcClient):
        def __init__(self) -> None:
            super().__init__(transcript, PermissionLevel.READ)
            self.callback_seen = asyncio.Event()

        async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
            self.callback_seen.set()
            await super().session_update(session_id, update, **kwargs)

    client = CallbackClient()
    frame = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "adapter-session",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "QUEUED_STALE"},
            },
        },
    }

    async def scenario() -> None:
        async with _real_dispatch_connection(client) as connection:
            raw = connection._conn
            async with client.replaying("adapter-session", raw):
                await raw._process_message(frame)
                raw._disconnect()
            await _drain_updates(client.callback_seen.is_set)
            assert client.callback_seen.is_set()

    asyncio.run(scenario())
    assert client.answer == ""
    assert transcript.read().events == []


@pytest.mark.parametrize(
    "raw_update",
    [
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "same"},
            "messageId": None,
        },
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "same", "annotations": None},
            "messageId": "same-id",
        },
    ],
    ids=["explicit-message-id-null", "content-annotation-null-vs-unset"],
)
def test_tagged_replay_historical_frame_variations_keep_identical_live_chunk(
    raw_update: Mapping[str, Any], tmp_path: Path
) -> None:
    """Frame identity, not serialized content, keeps replay out of the answer."""
    client, transcript = _make_client(tmp_path, PermissionLevel.READ)
    raw = _TaggingRawConnection()
    client.on_connect(SimpleNamespace(_conn=raw))
    frame = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "adapter-session",
            "update": dict(raw_update),
        },
    }

    async def scenario() -> None:
        async with client.replaying("adapter-session", raw):
            await raw.receive(frame)
            generation = frame["params"]["_meta"]["acpc_replay_generation"]
        replay_update = AgentMessageChunk(
            content=text_block("same"),
            message_id=raw_update.get("messageId"),
            session_update="agent_message_chunk",
        )
        await client.session_update(
            "adapter-session", replay_update, **{REPLAY_GENERATION_KEY: generation}
        )
        await client.session_update("adapter-session", replay_update)
        client.flush()

    asyncio.run(scenario())

    assert client.answer == "same"
    assert [event["text"] for event in transcript.read().events] == ["same"]


def test_tagless_replay_reports_unavailable_phase_attribution(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client, _transcript = _make_client(tmp_path, PermissionLevel.READ)
    raw = _ObserverOnlyRawConnection()
    client.on_connect(SimpleNamespace(_conn=raw))
    frame = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "adapter-session",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "orphaned"},
            },
        },
    }

    async def scenario() -> None:
        async with client.replaying("adapter-session", raw):
            raw.emit(frame)

    with caplog.at_level("WARNING", logger="acpc.client"):
        asyncio.run(scenario())

    assert "replay suppression unavailable" in caplog.text


def test_failed_restore_opens_a_fresh_generation(tmp_path: Path) -> None:
    client, _transcript = _make_client(tmp_path, PermissionLevel.READ)
    raw = _TaggingRawConnection()
    client.on_connect(SimpleNamespace(_conn=raw))
    tracker = ReplayTracker.for_connection(raw)
    assert tracker is not None

    async def scenario() -> tuple[int, int]:
        first: int | None = None
        with pytest.raises(RuntimeError, match="restore failed"):
            async with client.replaying("adapter-session", raw):
                first = tracker.active_generation_id("adapter-session")
                assert first == 1
                raise RuntimeError("restore failed")
        async with client.replaying("adapter-session", raw):
            second = tracker.active_generation_id("adapter-session")
            assert second == 2
        assert first is not None
        assert second is not None
        return first, second

    assert asyncio.run(scenario()) == (1, 2)
    assert tracker.active_generation_id("adapter-session") is None


def test_cancelled_restore_suppresses_stale_frame_in_the_next_attempt(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client, _transcript = _make_client(tmp_path, PermissionLevel.READ)
    raw = _TaggingRawConnection()
    client.on_connect(SimpleNamespace(_conn=raw))
    tracker = ReplayTracker.for_connection(raw)
    assert tracker is not None
    stale_frame = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "adapter-session",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "stale"},
            },
        },
    }

    async def scenario() -> None:
        old_tag: dict[str, Any] | None = None
        with pytest.raises(asyncio.CancelledError):
            async with client.replaying("adapter-session", raw):
                await raw.receive(stale_frame)
                old_tag = stale_frame["params"]["_meta"][REPLAY_GENERATION_KEY]
                raise asyncio.CancelledError
        async with client.replaying("adapter-session", raw):
            assert tracker.active_generation_id("adapter-session") == 2
        assert old_tag is not None
        await client.session_update(
            "adapter-session",
            AgentMessageChunk(content=text_block("stale"), session_update="agent_message_chunk"),
            **{REPLAY_GENERATION_KEY: old_tag},
        )
        await client.session_update(
            "adapter-session",
            AgentMessageChunk(content=text_block("live"), session_update="agent_message_chunk"),
        )
        client.flush()

    with caplog.at_level("WARNING", logger="acpc.client"):
        asyncio.run(scenario())

    assert client.answer == "live"
    assert [event["text"] for event in _transcript.read().events] == ["live"]
    assert "unknown generation" not in caplog.text
    assert "replay suppression:" not in caplog.text


def test_connection_drop_keeps_open_generation_for_queued_callbacks(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client, _transcript = _make_client(tmp_path, PermissionLevel.READ)

    class DroppingRawConnection(_TaggingRawConnection):
        def _disconnect(self) -> None:
            return

    raw = DroppingRawConnection()
    client.on_connect(SimpleNamespace(_conn=raw))
    tracker = ReplayTracker.for_connection(raw)
    assert tracker is not None
    frame = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "adapter-session",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "stale"},
            },
        },
    }

    async def scenario() -> tuple[dict[str, Any], str]:
        async with client.replaying("adapter-session", raw):
            await raw.receive(frame)
            old_tag = frame["params"]["_meta"][REPLAY_GENERATION_KEY]
            raw._disconnect()
            assert tracker.active_generation_id("adapter-session") == 1
            assert tracker._generations
        update = AgentMessageChunk(content=text_block("live"), session_update="agent_message_chunk")
        await client.session_update("adapter-session", update, **{REPLAY_GENERATION_KEY: old_tag})
        return old_tag, caplog.text

    with caplog.at_level("WARNING", logger="acpc.client"):
        old_tag, logs = asyncio.run(scenario())

    assert old_tag["generation"] == 1
    assert "was still open when the connection dropped" not in logs
    assert "unknown generation" not in logs
    assert client.answer == ""
    assert _transcript.read().events == []


def test_nonempty_closed_generations_release_when_tagged_callbacks_are_seen(tmp_path: Path) -> None:
    client, _transcript = _make_client(tmp_path, PermissionLevel.READ)
    raw = _TaggingRawConnection()
    client.on_connect(SimpleNamespace(_conn=raw))
    tracker = ReplayTracker.for_connection(raw)
    assert tracker is not None

    async def scenario(test_client: AcpcClient, test_raw: _TaggingRawConnection) -> None:
        for index in range(10_000):
            frame = {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "adapter-session",
                    "update": {
                        "sessionUpdate": "user_message_chunk",
                        "content": {"type": "text", "text": "stored"},
                        "messageId": f"replay-{index}",
                    },
                },
            }
            async with test_client.replaying("adapter-session", test_raw):
                await test_raw.receive(frame)
                tag = frame["params"]["_meta"][REPLAY_GENERATION_KEY]
            await test_client.session_update(
                "adapter-session",
                UserMessageChunk(
                    content=text_block("stored"),
                    message_id=f"replay-{index}",
                    session_update="user_message_chunk",
                ),
                **{REPLAY_GENERATION_KEY: tag},
            )

    asyncio.run(scenario(client, raw))
    assert tracker._generations == {}

    raw_ref = weakref.ref(raw)
    del client, raw
    gc.collect()
    assert raw_ref() is None
    assert tracker._generations == {}


def test_close_invalidates_only_after_the_wrapped_close_awaits(tmp_path: Path) -> None:
    client, _transcript = _make_client(tmp_path, PermissionLevel.READ)

    class ClosingRawConnection(_TaggingRawConnection):
        def __init__(self) -> None:
            super().__init__()
            self.generations_during_close: int | None = None

        async def close(self) -> None:
            tracker = ReplayTracker.for_connection(self)
            assert tracker is not None
            self.generations_during_close = len(tracker._generations)

    raw = ClosingRawConnection()
    client.on_connect(SimpleNamespace(_conn=raw))
    tracker = ReplayTracker.for_connection(raw)
    assert tracker is not None
    frame = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "adapter-session",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "unaccounted"},
            },
        },
    }

    async def scenario() -> None:
        async with client.replaying("adapter-session", raw):
            await raw.receive(frame)
        assert tracker._generations
        await raw.close()

    asyncio.run(scenario())
    assert raw.generations_during_close == 1
    assert tracker._generations == {}


def test_cancelled_close_waiter_retries_shared_shutdown_before_invalidation(tmp_path: Path) -> None:
    """A cancelled waiter cannot turn ACP's early _closed flag into proof of shutdown."""
    _base_client, transcript = _make_client(tmp_path, PermissionLevel.READ)

    class DelayedClient(AcpcClient):
        def __init__(self) -> None:
            super().__init__(transcript, PermissionLevel.READ)
            self.callback_started = asyncio.Event()
            self.callback_finished = asyncio.Event()
            self.release_callback = asyncio.Event()

        async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
            if kwargs.get(REPLAY_GENERATION_KEY) is not None:
                self.callback_started.set()
                await self.release_callback.wait()
            await super().session_update(session_id, update, **kwargs)
            self.callback_finished.set()

    client = DelayedClient()
    frame = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "adapter-session",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "STALE_AFTER_RETRY"},
            },
        },
    }

    async def scenario() -> None:
        async with _real_dispatch_connection(client) as connection:
            raw = connection._conn
            tracker = ReplayTracker.for_connection(raw)
            assert tracker is not None
            async with client.replaying("adapter-session", raw):
                await raw._process_message(frame)
            await client.callback_started.wait()

            stop_started = asyncio.Event()
            release_stop = asyncio.Event()
            original_stop = raw._dispatcher.stop

            async def delayed_stop() -> None:
                stop_started.set()
                await release_stop.wait()
                await original_stop()

            raw._dispatcher.stop = delayed_stop
            first = asyncio.create_task(connection.close())
            await stop_started.wait()
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first

            assert tracker._generations
            second = asyncio.create_task(connection.close())
            for _ in range(10):
                await asyncio.sleep(0)
            assert not second.done()

            client.release_callback.set()
            await client.callback_finished.wait()
            release_stop.set()
            await second
            assert tracker._generations == {}

    asyncio.run(scenario())
    assert client.answer == ""
    assert transcript.read().events == []


def test_concurrent_close_calls_share_one_underlying_shutdown(tmp_path: Path) -> None:
    client, _transcript = _make_client(tmp_path, PermissionLevel.READ)

    class ClosingRawConnection(_TaggingRawConnection):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0
            self.close_started = asyncio.Event()
            self.release_close = asyncio.Event()

        async def close(self) -> None:
            self.close_calls += 1
            self.close_started.set()
            await self.release_close.wait()

    raw = ClosingRawConnection()
    client.on_connect(SimpleNamespace(_conn=raw))
    tracker = ReplayTracker.for_connection(raw)
    assert tracker is not None
    frame = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "adapter-session",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "stale"},
            },
        },
    }

    async def scenario() -> None:
        async with client.replaying("adapter-session", raw):
            await raw.receive(frame)
        first = asyncio.create_task(raw.close())
        await raw.close_started.wait()
        second = asyncio.create_task(raw.close())
        for _ in range(10):
            await asyncio.sleep(0)
        assert not second.done()
        assert raw.close_calls == 1
        raw.release_close.set()
        await asyncio.gather(first, second)

    asyncio.run(scenario())
    assert raw.close_calls == 1
    assert tracker._generations == {}


def test_failing_close_retains_replay_records(tmp_path: Path) -> None:
    client, _transcript = _make_client(tmp_path, PermissionLevel.READ)

    class FailingRawConnection(_TaggingRawConnection):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            raise RuntimeError("shutdown failed")

    raw = FailingRawConnection()
    client.on_connect(SimpleNamespace(_conn=raw))
    tracker = ReplayTracker.for_connection(raw)
    assert tracker is not None
    frame = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "adapter-session",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "retained"},
            },
        },
    }

    async def scenario() -> None:
        async with client.replaying("adapter-session", raw):
            await raw.receive(frame)
        with pytest.raises(RuntimeError, match="shutdown failed"):
            await raw.close()
        assert tracker._generations
        with pytest.raises(RuntimeError, match="shutdown failed"):
            await raw.close()
        assert tracker._generations

    asyncio.run(scenario())
    assert raw.close_calls == 1


def test_a_close_failing_after_its_waiter_left_reports_itself(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A shielded shutdown that fails with nobody waiting must not fail silently."""
    client, _transcript = _make_client(tmp_path, PermissionLevel.READ)

    class LateFailingRawConnection(_TaggingRawConnection):
        def __init__(self) -> None:
            super().__init__()
            self.entered: asyncio.Event | None = None
            self.release: asyncio.Event | None = None

        async def close(self) -> None:
            assert self.entered is not None and self.release is not None
            self.entered.set()
            await self.release.wait()
            raise RuntimeError("late shutdown failed")

    raw = LateFailingRawConnection()
    client.on_connect(SimpleNamespace(_conn=raw))
    tracker = ReplayTracker.for_connection(raw)
    assert tracker is not None
    frame = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "adapter-session",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "retained"},
            },
        },
    }

    unstructured: list[Any] = []

    async def scenario() -> None:
        # asyncio reports the orphaned shielded future through the loop handler.
        # Capture it: it is the whole point of the fix that acpc says so itself
        # rather than leaving that report as the only trace, and letting it reach
        # the default handler would put an ERROR into every serial suite run.
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: unstructured.append(context)
        )
        raw.entered = asyncio.Event()
        raw.release = asyncio.Event()
        async with client.replaying("adapter-session", raw):
            await raw.receive(frame)

        waiter = asyncio.ensure_future(raw.close())
        await raw.entered.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        # The shared shutdown outlives its only waiter, then fails.
        shared = tracker._close_task
        assert shared is not None
        raw.release.set()
        with pytest.raises(RuntimeError, match="late shutdown failed"):
            await shared
        assert tracker._generations

    with caplog.at_level("WARNING", logger="acpc.client"):
        asyncio.run(scenario())

    assert any("closing the connection failed" in record.getMessage() for record in caplog.records)


def test_session_close_does_not_purge_an_unaccounted_replay_generation(tmp_path: Path) -> None:
    client, transcript = _make_client(tmp_path, PermissionLevel.READ)
    raw = _TaggingRawConnection()
    client.on_connect(SimpleNamespace(_conn=raw))
    tracker = ReplayTracker.for_connection(raw)
    assert tracker is not None
    replay_frame = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "adapter-session",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "stale"},
            },
        },
    }
    close_frame = {
        "jsonrpc": "2.0",
        "method": "session/close",
        "params": {"sessionId": "adapter-session"},
    }

    async def scenario() -> None:
        async with client.replaying("adapter-session", raw):
            await raw.receive(replay_frame)
            tag = replay_frame["params"]["_meta"][REPLAY_GENERATION_KEY]
            await raw.receive(close_frame)
        await client.session_update(
            "adapter-session",
            AgentMessageChunk(content=text_block("stale"), session_update="agent_message_chunk"),
            **{REPLAY_GENERATION_KEY: tag},
        )

    asyncio.run(scenario())
    assert client.answer == ""
    assert transcript.read().events == []
    assert tracker._generations == {}


def test_closed_generation_cap_evicts_oldest_unaccounted_frame_with_diagnostic(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    assert MAX_RETAINED_CLOSED_REPLAY_GENERATIONS == 64
    client, _transcript = _make_client(tmp_path, PermissionLevel.READ)
    raw = _TaggingRawConnection()
    client.on_connect(SimpleNamespace(_conn=raw))
    tracker = ReplayTracker.for_connection(raw)
    assert tracker is not None

    async def scenario() -> None:
        for index in range(MAX_RETAINED_CLOSED_REPLAY_GENERATIONS + 1):
            frame = {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "adapter-session",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": f"stale-{index}"},
                    },
                },
            }
            async with client.replaying("adapter-session", raw):
                await raw.receive(frame)

    with caplog.at_level("WARNING", logger="acpc.client"):
        asyncio.run(scenario())

    assert len(tracker._generations) == MAX_RETAINED_CLOSED_REPLAY_GENERATIONS
    assert 1 not in tracker._generations
    assert "evicted generation 1" in caplog.text

    caplog.clear()
    healthy_client, _healthy_transcript = _make_client(tmp_path / "healthy", PermissionLevel.READ)
    healthy_raw = _TaggingRawConnection()
    healthy_client.on_connect(SimpleNamespace(_conn=healthy_raw))
    healthy_frame = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "adapter-session",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "healthy"},
            },
        },
    }

    async def healthy() -> None:
        async with healthy_client.replaying("adapter-session", healthy_raw):
            await healthy_raw.receive(healthy_frame)
            tag = healthy_frame["params"]["_meta"][REPLAY_GENERATION_KEY]
        await healthy_client.session_update(
            "adapter-session",
            AgentMessageChunk(content=text_block("healthy"), session_update="agent_message_chunk"),
            **{REPLAY_GENERATION_KEY: tag},
        )

    asyncio.run(healthy())
    assert "evicted" not in caplog.text


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
