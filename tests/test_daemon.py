"""Behavioral tests for the per-target daemon.

These drive real daemon processes against the mock adapter: the properties
under test — a warm adapter keeping conversation history, a stop that finishes
sessions instead of orphaning them, an idle TTL — only exist across process
boundaries, so mocking them out would test nothing.
"""

import asyncio
import contextlib
import json
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from acp import text_block
from acp.client import ClientSideConnection
from acp.schema import AgentMessageChunk

from acpc import daemon, daemon_client, ipc, output, proc, runner, sessions, vocab
from acpc.client import REPLAY_GENERATION_KEY, AcpcClient
from acpc.permissions import PermissionLevel, select_mode
from acpc.registry import AgentRegistry
from acpc.transcript import Transcript

MOCK_AGENT_SCRIPT = str(Path(__file__).with_name("mock_agent.py"))

MOCK_ENTRY = f"""
name = "Mock Agent"
command = "{sys.executable} {MOCK_AGENT_SCRIPT}"
install_command = "true"
home = "~/.mock"
home_env = "MOCK_HOME"
mode = "default"

[modes]
default = {{ grants = "read", delegates = true }}
plan = {{ grants = "read", delegates = true }}
yolo = {{ grants = "all", delegates = false }}

[presets]
standard = {{ model = "mock-sonnet-5", effort = "high" }}
"""


@pytest.fixture
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "s"
    agents = root / "agents"
    agents.mkdir(parents=True)
    (agents / "mock.toml").write_text(MOCK_ENTRY, encoding="utf-8")
    monkeypatch.setenv("ACPC_HOME", str(root))
    return root


def configure(state_root: Path, text: str) -> None:
    (state_root / "config.toml").write_text(text, encoding="utf-8")


def resolve():
    resolution = AgentRegistry().resolve_call("mock", permissions="read")
    mode, spec = select_mode(resolution.entry.modes, "read", resolution.mode)
    return replace(resolution, mode=mode, mode_spec=spec)


def target() -> str:
    return runner.call_target(resolve())


def new_session(prompt: str) -> str:
    resolution = resolve()
    meta = sessions.create_session(
        entry="mock",
        base_adapter="mock",
        prompt=prompt,
        resolution=runner.resolution_payload(resolution, cwd=None),
        target=runner.call_target(resolution),
    )
    return meta.session_id


def run_turn(session_id: str, prompt: str, **kwargs) -> runner.TurnOutcome:
    return runner.execute_turn(
        session_id, runner.TurnRequest(resolution=resolve(), prompt=prompt, **kwargs)
    )


def next_turn(session_id: str, prompt: str) -> runner.TurnOutcome:
    """Do what `continue` will do: rotate, then run against the same session."""
    sessions.rotate_turn(session_id)
    sessions.write_prompt(session_id, prompt)
    resumed = sessions.read_meta(session_id).adapter_session_id
    return run_turn(session_id, prompt, resume_adapter_session=resumed)


class _ClosedBeforePromptConnection:
    """Proxy a real ACP connection that closes before its prompt is sent."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection
        self._conn = connection._conn

    async def prompt(self, **kwargs: Any) -> Any:
        await self._connection.close()
        return await self._connection.prompt(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def dispatch_payload(prompt: str = "x") -> dict:
    return runner.daemon_payload(runner.TurnRequest(resolution=resolve(), prompt=prompt))


def rebuild(payload: dict) -> runner.TurnRequest:
    return daemon.Daemon(target())._rebuild_request(payload)


@asynccontextmanager
async def real_client_connection(client: Any) -> AsyncIterator[ClientSideConnection]:
    """Use ACP's real dispatcher and router without starting an adapter."""
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


# --- rebuilding a dispatched turn -------------------------------------------


def test_a_rebuilt_turn_ignores_a_mode_fact_the_entry_gained_after_dispatch(
    state_root: Path,
) -> None:
    """SPEC: editing an entry never changes a session mid-conversation.

    The selected mode facts travel in the dispatch payload, so an entry cannot
    reclaim the mode by changing its TOML after dispatch.
    """
    payload = dispatch_payload()
    assert payload["mode"] == "default"

    (state_root / "agents" / "mock.toml").write_text(
        MOCK_ENTRY.replace('mode = "default"', 'mode = "plan"'), encoding="utf-8"
    )

    rebuilt = rebuild(payload)
    assert rebuilt.resolution.mode == "default"
    assert rebuilt.resolution.entry.modes["yolo"].grants == "all"


def test_a_mode_above_the_policy_reaching_the_daemon_fails_the_turn(state_root: Path) -> None:
    """Backstop only: arriving here at all means the CLI guard was evaded."""
    payload = dispatch_payload() | {
        "mode": "yolo",
        "grants": "all",
        "delegates": False,
        "permissions": "read",
    }

    with pytest.raises(daemon.DaemonError, match="grants all"):
        rebuild(payload)


def test_a_mode_without_measured_facts_is_rejected_by_the_daemon(state_root: Path) -> None:
    payload = dispatch_payload() | {"mode": "yolo", "permissions": "edit"}
    payload.pop("grants")
    payload.pop("delegates")

    with pytest.raises(daemon.DaemonError, match="missing mode facts"):
        rebuild(payload)


def test_a_mode_at_the_all_ceiling_still_rebuilds(state_root: Path) -> None:
    payload = dispatch_payload() | {
        "mode": "yolo",
        "grants": "all",
        "delegates": False,
        "permissions": "all",
    }

    assert rebuild(payload).resolution.mode == "yolo"


def test_a_turn_that_cannot_be_built_is_refused_in_a_reply_not_a_dropped_connection(
    state_root: Path,
) -> None:
    """A refusal the caller cannot read is worse than no refusal at all.

    Raising out of `_start` leaves the connection with no frame, so the client
    sees a socket error rather than the reason, and the session it already
    created stays `starting` until orphan detection finds it.
    """
    frame = {
        "session_id": new_session("x"),
        "payload": dispatch_payload() | {"mode": "yolo", "grants": "all", "delegates": False},
    }

    reply = asyncio.run(daemon.Daemon(target())._start(frame))

    assert reply["ok"] is False
    assert "grants all" in reply["error"]


def test_daemon_acceptance_has_a_running_disk_claim_before_the_turn_task_runs(
    state_root: Path,
) -> None:
    session_id = new_session("accepted turn")
    instance = daemon.Daemon(target())

    async def scenario() -> None:
        reply = await instance._start(
            {"session_id": session_id, "payload": dispatch_payload("accepted turn")}
        )
        assert reply["ok"] is True
        claimed = sessions.read_meta(session_id)
        assert claimed.state == "running"
        assert claimed.pid is not None
        await instance._shut_down_sessions("test cleanup")
        await instance.host.close()

    asyncio.run(scenario())


# --- routing ----------------------------------------------------------------


def test_a_turn_runs_on_the_daemon_and_finishes_the_session(
    state_root: Path, live_daemon: None
) -> None:
    session_id = new_session("a daemon turn")

    outcome = run_turn(session_id, "a daemon turn")

    assert outcome.state == "done"
    assert outcome.route_note is None
    assert sessions.read_meta(session_id).state == "done"


def test_cancel_before_daemon_acceptance_is_the_no_such_turn_case(
    state_root: Path, live_daemon: None
) -> None:
    session_id = new_session("already finished")
    run_turn(session_id, "already finished")

    async def cancel_finished() -> dict[str, Any]:
        connection = await daemon_client.connect(target())
        assert connection is not None
        try:
            return await connection.cancel(session_id)
        finally:
            await connection.close()

    reply = asyncio.run(cancel_finished())

    assert reply["ok"] is False
    assert "not running here" in reply["error"]
    assert sessions.read_meta(session_id).state == "done"


def test_daemon_replay_tag_survives_restore_client_release_and_live_rebind(
    state_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A late restore callback is dropped after the daemon binds the live client."""

    class RawConnection:
        def __init__(self) -> None:
            self.observers: list[object] = []

        def add_observer(self, observer: object) -> None:
            self.observers.append(observer)

        async def _process_message(self, message: object) -> None:
            del message

        async def receive(self, message: dict) -> None:
            event = SimpleNamespace(direction=SimpleNamespace(value="incoming"), message=message)
            for observer in self.observers:
                assert callable(observer)
                observer(event)
            await self._process_message(message)

    raw = RawConnection()
    mux = daemon._MultiplexClient()
    mux.on_connect(SimpleNamespace(_conn=raw))
    session_id = new_session("same")
    restore_transcript = Transcript(state_root / "restore.ndjson")
    live_transcript = Transcript(sessions.transcript_path(session_id))
    restore_client = AcpcClient(restore_transcript, PermissionLevel.READ)
    live_client = AcpcClient(live_transcript, PermissionLevel.READ)
    adapter_session_id = "adapter-session"
    frame = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": adapter_session_id,
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "same"},
                "messageId": "replay-message",
            },
        },
    }
    replay_update = AgentMessageChunk(
        content=text_block("same"),
        message_id="replay-message",
        session_update="agent_message_chunk",
    )

    async def scenario() -> None:
        mux.bind(adapter_session_id, restore_client)
        async with restore_client.replaying(adapter_session_id, raw):
            await raw.receive(frame)
        generation = frame["params"]["_meta"][REPLAY_GENERATION_KEY]

        mux.release(adapter_session_id)
        mux.bind(adapter_session_id, live_client)
        await mux.session_update(
            adapter_session_id,
            replay_update,
            **{REPLAY_GENERATION_KEY: generation},
        )
        await mux.session_update(adapter_session_id, replay_update)
        live_client.flush()

    with caplog.at_level("WARNING", logger="acpc.client"):
        asyncio.run(scenario())
    sessions.write_answer(session_id, live_client.answer)

    assert live_client.answer == "same"
    assert sessions.answer_path(session_id).read_text(encoding="utf-8") == "same"
    assert [event["text"] for event in live_transcript.read().events] == ["same"]
    assert restore_transcript.read().events == []
    assert not any("replay suppression:" in record.getMessage() for record in caplog.records)


def test_daemon_live_routing_uses_top_level_session_id_not_peer_metadata(
    state_root: Path,
) -> None:
    mux = daemon._MultiplexClient()
    client_a = AcpcClient(Transcript(state_root / "a.ndjson"), PermissionLevel.READ)
    client_b = AcpcClient(Transcript(state_root / "b.ndjson"), PermissionLevel.READ)
    frame = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "adapter-a",
            "_meta": {"session_id": "adapter-b"},
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "ROUTED_STALE"},
            },
        },
    }

    async def scenario() -> None:
        async with real_client_connection(mux) as connection:
            mux.bind("adapter-a", client_a)
            mux.bind("adapter-b", client_b)
            await connection._conn._process_message(frame)
            assert REPLAY_GENERATION_KEY not in frame["params"]["_meta"]
            for _ in range(1000):
                if client_a.answer == "ROUTED_STALE":
                    break
                await asyncio.sleep(0)
            client_a.flush()
            client_b.flush()

    asyncio.run(scenario())
    assert client_a.answer == "ROUTED_STALE"
    assert client_b.answer == ""
    assert [event["text"] for event in client_a.transcript.read().events] == ["ROUTED_STALE"]
    assert client_b.transcript.read().events == []


def test_duplicate_replay_callback_does_not_spend_another_frame_identity(
    state_root: Path,
) -> None:
    class RawConnection:
        def __init__(self) -> None:
            self.observers: list[object] = []

        def add_observer(self, observer: object) -> None:
            self.observers.append(observer)

        async def _process_message(self, message: object) -> None:
            del message

        async def receive(self, message: dict) -> None:
            event = SimpleNamespace(direction=SimpleNamespace(value="incoming"), message=message)
            for observer in self.observers:
                assert callable(observer)
                observer(event)
            await self._process_message(message)

    raw = RawConnection()
    mux = daemon._MultiplexClient()
    mux.on_connect(SimpleNamespace(_conn=raw))
    tracker = mux._replay_tracker
    assert tracker is not None
    restore_client = AcpcClient(Transcript(state_root / "restore.ndjson"), PermissionLevel.READ)
    live_client = AcpcClient(Transcript(state_root / "live.ndjson"), PermissionLevel.READ)
    frames = [
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "adapter-session",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "ONE"},
                },
            },
        },
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "adapter-session",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "TWO"},
                },
            },
        },
    ]

    async def scenario() -> None:
        mux.bind("adapter-session", restore_client)
        async with restore_client.replaying("adapter-session", raw):
            for frame in frames:
                await raw.receive(frame)
        first_tag = frames[0]["params"]["_meta"][REPLAY_GENERATION_KEY]
        second_tag = frames[1]["params"]["_meta"][REPLAY_GENERATION_KEY]
        mux.release("adapter-session")
        mux.bind("adapter-session", live_client)
        update = AgentMessageChunk(
            content=text_block("stale"), session_update="agent_message_chunk"
        )
        await mux.session_update("adapter-session", update, **{REPLAY_GENERATION_KEY: first_tag})
        await mux.session_update("adapter-session", update, **{REPLAY_GENERATION_KEY: first_tag})
        await mux.session_update("adapter-session", update, **{REPLAY_GENERATION_KEY: second_tag})
        await mux.session_update(
            "adapter-session",
            AgentMessageChunk(content=text_block("LIVE"), session_update="agent_message_chunk"),
        )
        live_client.flush()

    asyncio.run(scenario())
    assert live_client.answer == "LIVE"
    assert tracker._generations == {}


def test_failed_restore_replay_tag_does_not_reach_retry_turn(
    state_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class RawConnection:
        def __init__(self) -> None:
            self.observers: list[object] = []

        def add_observer(self, observer: object) -> None:
            self.observers.append(observer)

        async def _process_message(self, message: object) -> None:
            del message

        async def receive(self, message: dict) -> None:
            event = SimpleNamespace(direction=SimpleNamespace(value="incoming"), message=message)
            for observer in self.observers:
                assert callable(observer)
                observer(event)
            await self._process_message(message)

    raw = RawConnection()
    mux = daemon._MultiplexClient()
    mux.on_connect(SimpleNamespace(_conn=raw))
    session_id = new_session("retry")
    restore_transcript = Transcript(state_root / "restore-failed.ndjson")
    retry_transcript = Transcript(sessions.transcript_path(session_id))
    restore_client = AcpcClient(restore_transcript, PermissionLevel.READ)
    retry_client = AcpcClient(retry_transcript, PermissionLevel.READ)
    frame = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "adapter-session",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "failed-stale"},
            },
        },
    }

    async def scenario() -> None:
        mux.bind("adapter-session", restore_client)
        with pytest.raises(RuntimeError, match="restore failed"):
            async with restore_client.replaying("adapter-session", raw):
                await raw.receive(frame)
                raise RuntimeError("restore failed")
        mux.release("adapter-session")

        mux.bind("adapter-session", retry_client)
        tag = frame["params"]["_meta"][REPLAY_GENERATION_KEY]
        stale = AgentMessageChunk(
            content=text_block("failed-stale"), session_update="agent_message_chunk"
        )
        await mux.session_update("adapter-session", stale, **{REPLAY_GENERATION_KEY: tag})
        await mux.session_update(
            "adapter-session",
            AgentMessageChunk(
                content=text_block("retry-answer"), session_update="agent_message_chunk"
            ),
        )
        retry_client.flush()

    with caplog.at_level("WARNING", logger="acpc.client"):
        asyncio.run(scenario())

    assert retry_client.answer == "retry-answer"
    assert [event["text"] for event in retry_transcript.read().events] == ["retry-answer"]
    assert restore_transcript.read().events == []
    assert "unknown generation" not in caplog.text
    assert "replay suppression:" not in caplog.text


def test_replay_binding_is_exclusive_while_generation_is_open(state_root: Path) -> None:
    class RawConnection:
        def __init__(self) -> None:
            self.observers: list[object] = []

        def add_observer(self, observer: object) -> None:
            self.observers.append(observer)

        async def _process_message(self, message: object) -> None:
            del message

    raw = RawConnection()
    mux = daemon._MultiplexClient()
    mux.on_connect(SimpleNamespace(_conn=raw))
    adapter_session_id = "adapter-session"
    restore_client = AcpcClient(Transcript(state_root / "restore.ndjson"), PermissionLevel.READ)
    replacement_client = AcpcClient(
        Transcript(state_root / "replacement.ndjson"), PermissionLevel.READ
    )

    async def scenario() -> None:
        mux.bind(adapter_session_id, restore_client)
        async with restore_client.replaying(adapter_session_id, raw):
            mux.release(adapter_session_id)
            with pytest.raises(daemon.DaemonError, match="cannot bind"):
                mux.bind(adapter_session_id, replacement_client)

    asyncio.run(scenario())


def test_replay_binding_rejects_a_different_client_while_generation_is_open(
    state_root: Path,
) -> None:
    class RawConnection:
        def __init__(self) -> None:
            self.observers: list[object] = []

        def add_observer(self, observer: object) -> None:
            self.observers.append(observer)

        async def _process_message(self, message: object) -> None:
            del message

    raw = RawConnection()
    mux = daemon._MultiplexClient()
    mux.on_connect(SimpleNamespace(_conn=raw))
    adapter_session_id = "adapter-session"
    first_client = AcpcClient(Transcript(state_root / "first.ndjson"), PermissionLevel.READ)
    second_client = AcpcClient(Transcript(state_root / "second.ndjson"), PermissionLevel.READ)

    async def scenario() -> None:
        mux.bind(adapter_session_id, first_client)
        async with first_client.replaying(adapter_session_id, raw):
            with pytest.raises(daemon.DaemonError, match="second acpc session"):
                mux.bind(adapter_session_id, second_client)

    asyncio.run(scenario())


def test_replay_generation_scope_does_not_block_another_daemon_session(
    state_root: Path,
) -> None:
    class RawConnection:
        def __init__(self) -> None:
            self.observers: list[object] = []

        def add_observer(self, observer: object) -> None:
            self.observers.append(observer)

        async def _process_message(self, message: object) -> None:
            del message

    raw = RawConnection()
    mux = daemon._MultiplexClient()
    mux.on_connect(SimpleNamespace(_conn=raw))
    restore_client = AcpcClient(Transcript(state_root / "restore.ndjson"), PermissionLevel.READ)
    live_client = AcpcClient(Transcript(state_root / "live.ndjson"), PermissionLevel.READ)

    async def scenario() -> None:
        mux.bind("adapter-x", restore_client)
        async with restore_client.replaying("adapter-x", raw):
            mux.bind("adapter-y", live_client)
            await mux.session_update(
                "adapter-y",
                AgentMessageChunk(
                    content=text_block("session y"), session_update="agent_message_chunk"
                ),
            )
            live_client.flush()

    asyncio.run(scenario())
    assert live_client.answer == "session y"


def test_a_turn_is_not_started_if_daemon_task_registration_fails(
    state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = new_session("turn one")
    run_turn(session_id, "turn one")
    meta = sessions.read_meta(session_id)
    assert meta.adapter_session_id is not None
    request = runner.TurnRequest(
        resolution=resolve(),
        prompt="must not prompt",
        resume_adapter_session=meta.adapter_session_id,
        defer_rotation=True,
    )
    instance = daemon.Daemon(target())

    def fail_task_registration(*args: object, **kwargs: object) -> None:
        raise OSError("injected task registration failure")

    monkeypatch.setattr(daemon.asyncio, "create_task", fail_task_registration)

    reply = asyncio.run(
        instance._start(
            {
                "session_id": session_id,
                "payload": runner.daemon_payload(request),
            }
        )
    )

    assert reply["ok"] is False
    failed = sessions.read_meta(session_id)
    assert failed.state == "done"
    assert failed.turns == 1


def test_a_refused_switch_leaves_the_daemon_warm_for_continue(
    state_root: Path, live_daemon: None
) -> None:
    session_id = new_session("run the perm scenario")

    refused = run_turn(session_id, "run the perm scenario")

    assert refused.route_note is None
    assert refused.state == "failed"
    assert refused.stop_reason == "permission_denied"
    assert refused.exit_code == vocab.EXIT_USAGE
    meta = sessions.read_meta(session_id)
    assert meta.state == "failed"
    assert meta.stop_reason == "permission_denied"
    assert meta.exit_code == vocab.EXIT_USAGE
    assert meta.denial_details["switch_mode:yolo"] == {
        "category": "switch_mode",
        "target": "yolo",
        "minimum_policy": "all",
        "remedy": "pass --permissions all",
    }
    envelope = output.result_envelope(meta, sessions.answer_path(session_id).read_text())
    assert envelope["denied"][-1] == {
        "category": "switch_mode",
        "target": "yolo",
        "count": 1,
        "minimum_policy": "all",
        "remedy": "pass --permissions all",
    }
    mode = meta.resolution["resolved"]["mode"]
    assert mode["value"] == "default"
    assert mode["grants"] == "read"
    assert mode["delegates"] is True

    continued = next_turn(session_id, "multi:after the refused switch")

    assert continued.route_note is None
    assert continued.state == "done"
    assert sessions.read_meta(session_id).state == "done"
    assert "turn 2: after the refused switch" in sessions.answer_path(session_id).read_text()


def test_the_daemon_writes_the_answer_the_client_never_saw(
    state_root: Path, live_daemon: None
) -> None:
    session_id = new_session("who writes the answer")

    outcome = run_turn(session_id, "who writes the answer")

    on_disk = sessions.answer_path(session_id).read_text(encoding="utf-8")
    assert "who writes the answer" in on_disk
    assert outcome.answer == on_disk


def test_daemon_carries_incomplete_delivery_into_a_cold_resume(
    state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daemon must persist the delivery fact it learned on its own path."""
    original_mark = sessions.mark_prompt_delivered

    def fail_first_marker(session_id: str, prompt: str) -> sessions.SessionMeta:
        if prompt == "daemon marker outage":
            raise OSError("the daemon cannot persist this marker")
        return original_mark(session_id, prompt)

    monkeypatch.setattr(sessions, "mark_prompt_delivered", fail_first_marker)
    resolution = resolve()
    session_id = sessions.create_session(
        entry="mock",
        base_adapter="mock",
        prompt="daemon marker outage",
        resolution=runner.session_resolution(resolution, cwd=None),
        target=runner.call_target(resolution),
    ).session_id

    async def run_daemon_turns() -> tuple[runner.TurnOutcome, str, dict[str, Any]]:
        instance = daemon.Daemon(target())
        try:
            first = await instance._run_turn(
                session_id,
                runner.TurnRequest(resolution=resolution, prompt="daemon marker outage"),
                daemon._Turn(session_id=session_id, task=None, cancel=runner._CancelSignal()),
            )
            first_meta = sessions.read_meta(session_id)
            await instance.host.close()

            request = runner.continue_request(
                first_meta, "daemon cold follow-up", defer_rotation=True
            )
            # The claim and rotation belong to acceptance, so the continuation
            # goes through it rather than reproducing that sequence here.
            accepted = await instance._start(
                {"session_id": session_id, "payload": runner.daemon_payload(request)}
            )
            assert accepted["ok"] is True
            await instance._await_preparation({"session_id": session_id})
            resume_status = sessions.read_meta(session_id).extra["resume"]
            second = await instance._await({"session_id": session_id})
            return first, resume_status, second["outcome"]
        finally:
            await instance.host.close()

    first, resume_status, second = asyncio.run(run_daemon_turns())

    assert first.state == "done"
    assert first.delivery_record_incomplete is True
    assert sessions.read_meta(session_id).extra[sessions.DELIVERY_RECORD_INCOMPLETE] is True
    assert resume_status == "unverified — delivery record incomplete"
    assert second["state"] == "done"


def test_daemon_keeps_a_pre_send_failure_record_complete_on_cold_resume(
    state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed prompt that never crossed ACP must not poison later verification."""
    original_spawn = daemon.spawn_adapter

    @contextlib.asynccontextmanager
    async def spawn_closed_before_prompt(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        async with original_spawn(*args, **kwargs) as (connection, process):
            yield _ClosedBeforePromptConnection(connection), process

    monkeypatch.setattr(daemon, "spawn_adapter", spawn_closed_before_prompt)
    resolution = resolve()
    session_id = sessions.create_session(
        entry="mock",
        base_adapter="mock",
        prompt="daemon before-wire",
        resolution=runner.session_resolution(resolution, cwd=None),
        target=runner.call_target(resolution),
    ).session_id

    async def run_failed_turn() -> runner.TurnOutcome:
        instance = daemon.Daemon(target())
        try:
            return await instance._run_turn(
                session_id,
                runner.TurnRequest(resolution=resolution, prompt="daemon before-wire"),
                daemon._Turn(session_id=session_id, task=None, cancel=runner._CancelSignal()),
            )
        finally:
            await instance.host.close()

    first = asyncio.run(run_failed_turn())
    first_meta = sessions.read_meta(session_id)
    assert first.state == "failed"
    assert first_meta.extra.get(sessions.DELIVERY_RECORD_INCOMPLETE) is not True
    assert "delivered_prompts" not in first_meta.extra

    monkeypatch.setattr(daemon, "spawn_adapter", original_spawn)

    async def run_cold_resume() -> str:
        instance = daemon.Daemon(target())
        try:
            request = runner.continue_request(
                first_meta, "daemon later prompt", defer_rotation=True
            )
            accepted = await instance._start(
                {"session_id": session_id, "payload": runner.daemon_payload(request)}
            )
            assert accepted["ok"] is True
            await instance._await_preparation({"session_id": session_id})
            resume_status = sessions.read_meta(session_id).extra["resume"]
            outcome = await instance._await({"session_id": session_id})
            assert outcome["outcome"]["state"] == "done"
            return resume_status
        finally:
            await instance.host.close()

    assert asyncio.run(run_cold_resume()) == "verified"


def test_one_daemon_serves_several_sessions(state_root: Path, live_daemon: None) -> None:
    first = new_session("first session")
    second = new_session("second session")

    run_turn(first, "first session")
    run_turn(second, "second session")

    async def pids() -> int:
        connection = await daemon_client.connect(target())
        assert connection is not None
        try:
            return (await connection.status())["pid"]
        finally:
            await connection.close()

    assert asyncio.run(pids()) > 0
    assert sessions.read_meta(first).state == "done"
    assert sessions.read_meta(second).state == "done"


# --- warm versus cold, the contract `continue` depends on -------------------


def test_a_warm_second_turn_still_knows_the_first(state_root: Path, live_daemon: None) -> None:
    """The daemon holds the adapter, so its own history survives the turn."""
    session_id = new_session("turn one of the conversation")
    run_turn(session_id, "turn one of the conversation")

    next_turn(session_id, "turn two, please build on turn one")

    answer = sessions.answer_path(session_id).read_text(encoding="utf-8")
    assert "turn one of the conversation" in answer


def test_a_cold_resume_restores_the_adapter_persisted_history(state_root: Path) -> None:
    """Without a daemon, `session/load` restores the adapter's durable history."""
    session_id = new_session("turn one of the conversation")
    run_turn(session_id, "turn one of the conversation")

    next_turn(session_id, "turn two, please build on turn one")

    answer = sessions.answer_path(session_id).read_text(encoding="utf-8")
    assert "turn one of the conversation" in answer


def test_a_daemon_cold_resume_consumes_replayed_history_silently(
    state_root: Path, live_daemon: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    method_file = tmp_path / "session-method"
    monkeypatch.setenv("ACPC_MOCK_SESSION_METHOD_FILE", str(method_file))
    session_id = new_session("turn one of the conversation")
    run_turn(session_id, "turn one of the conversation")
    asyncio.run(_stop_target())

    next_turn(session_id, "turn two, please build on turn one")

    answer = sessions.answer_path(session_id).read_text(encoding="utf-8")
    assert method_file.read_text(encoding="utf-8") == "load\n"
    assert not answer.startswith("history")
    assert 'You asked: "turn two, please build on turn one"' in answer
    events = sessions.transcript_path(session_id).read_text(encoding="utf-8").splitlines()
    assert not any('"text": "history"' in line for line in events)


def test_a_daemon_cold_resume_prefers_session_resume(
    state_root: Path, live_daemon: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    method_file = tmp_path / "session-method"
    monkeypatch.setenv("ACPC_MOCK_ADVERTISE_RESUME", "1")
    monkeypatch.setenv("ACPC_MOCK_SESSION_METHOD_FILE", str(method_file))
    session_id = new_session("turn one")
    run_turn(session_id, "turn one")
    asyncio.run(_stop_target())

    next_turn(session_id, "turn two")

    answer = sessions.answer_path(session_id).read_text(encoding="utf-8")
    assert method_file.read_text(encoding="utf-8") == "resume\n"
    assert not answer.startswith("history")
    assert 'You asked: "turn two"' in answer


# --- stopping ---------------------------------------------------------------


def test_daemon_stop_fails_its_active_sessions_rather_than_orphaning_them(
    state_root: Path, live_daemon: None
) -> None:
    session_id = new_session("slow:30 stop victim")
    problem = asyncio.run(
        runner.dispatch_background(
            session_id, runner.TurnRequest(resolution=resolve(), prompt="slow:30 stop victim")
        )
    )
    assert problem is None
    _wait_for(session_id, "running")

    asyncio.run(_stop_target())

    meta = _wait_for_finished(session_id)
    assert meta.state == "failed"
    assert meta.stop_reason
    assert meta.exit_code == vocab.EXIT_AGENT_ERROR
    error_events = [event for event in _events(session_id) if event.get("type") == "error"]
    assert error_events
    assert "daemon was stopped" in error_events[-1]["message"]
    # acpc ended this turn. Blaming the adapter would send the reader hunting
    # for a vendor problem that never happened.
    assert error_events[-1]["observation"].startswith("acpc ended the turn")
    assert "adapter failure" not in error_events[-1]["message"]


def test_killed_adapter_records_a_failure_event(state_root: Path, live_daemon: None) -> None:
    import os
    import signal

    session_id = new_session("slow:30 killed adapter")
    problem = asyncio.run(
        runner.dispatch_background(
            session_id,
            runner.TurnRequest(resolution=resolve(), prompt="slow:30 killed adapter"),
        )
    )
    assert problem is None
    _wait_for(session_id, "running")

    daemon_pid = asyncio.run(_daemon_pid())
    children_path = Path(f"/proc/{daemon_pid}/task/{daemon_pid}/children")
    if not children_path.exists():  # pragma: no cover - non-Linux
        pytest.skip("requires /proc/<pid>/children")
    adapters = [int(pid) for pid in children_path.read_text().split()]
    assert len(adapters) == 1
    os.kill(adapters[0], signal.SIGKILL)

    meta = _wait_for_finished(session_id)
    assert meta.state == "failed"
    error_events = [event for event in _events(session_id) if event.get("type") == "error"]
    assert error_events
    assert "adapter" in error_events[-1]["message"].lower(), error_events
    assert error_events[-1]["observation"]
    assert error_events[-1]["next_step"].startswith("inspect the daemon log at")


def test_authentication_refusal_records_the_remedy_even_with_an_empty_log(
    state_root: Path, live_daemon: None
) -> None:
    session_id = new_session("auth:vendor needs credentials")
    # "Empty log" is the condition under test, so establish it rather than
    # inherit it: an earlier daemon in this root leaves its own shutdown note
    # behind, and the assertion below would then be measuring test order.
    resolution = resolve()
    log_file = daemon.log_path_for_target(runner.call_target(resolution))
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_bytes(b"")
    problem = asyncio.run(
        runner.dispatch_background(
            session_id,
            runner.TurnRequest(resolution=resolution, prompt="auth:vendor needs credentials"),
        )
    )
    assert problem is None

    meta = _wait_for_finished(session_id)
    assert meta.state == "failed"
    error_events = [event for event in _events(session_id) if event.get("type") == "error"]
    assert error_events
    event = error_events[-1]
    assert "authentication was refused" in event["message"]
    assert event["next_step"] == "run 'mock login'"
    assert "adapter_log" in event
    assert "adapter_log_tail" not in event


def test_a_failure_quotes_its_own_turn_and_not_an_earlier_one(
    state_root: Path, live_daemon: None
) -> None:
    """The per-target log is shared and append-only across every session.

    So a failure may quote only what its own turn wrote: attributing an earlier
    session's stderr to this one would be a confident, wrong diagnosis.
    """
    earlier = new_session("stderr:words from an earlier session")
    run_turn(earlier, "stderr:words from an earlier session")
    log_file = daemon.log_path_for_target(target())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if "words from an earlier session" in log_file.read_text(encoding="utf-8"):
            break
        time.sleep(0.1)
    else:
        pytest.fail("adapter stderr never reached the log")

    session_id = new_session("stderr-crash:words from the failing turn")
    problem = asyncio.run(
        runner.dispatch_background(
            session_id,
            runner.TurnRequest(
                resolution=resolve(), prompt="stderr-crash:words from the failing turn"
            ),
        )
    )
    assert problem is None

    assert _wait_for_finished(session_id).state == "failed"
    event = [event for event in _events(session_id) if event.get("type") == "error"][-1]
    assert "words from the failing turn" in event["adapter_log_tail"]
    assert "words from an earlier session" not in event["adapter_log_tail"]
    assert "words from an earlier session" not in event["message"]


def test_an_auth_refusal_is_recognized_from_its_data_not_its_text(
    state_root: Path, live_daemon: None
) -> None:
    """Adapters mark auth failures in `data`; the message may say nothing."""
    session_id = new_session("auth-data:no readable text")
    problem = asyncio.run(
        runner.dispatch_background(
            session_id,
            runner.TurnRequest(resolution=resolve(), prompt="auth-data:no readable text"),
        )
    )
    assert problem is None

    assert _wait_for_finished(session_id).state == "failed"
    event = [event for event in _events(session_id) if event.get("type") == "error"][-1]
    assert "authentication was refused" in event["message"]
    assert event["next_step"] == "run 'mock login'"


def test_adapter_refusal_without_an_exception_records_a_failure_event(
    state_root: Path, live_daemon: None
) -> None:
    session_id = new_session("please fail this on purpose")
    problem = asyncio.run(
        runner.dispatch_background(
            session_id,
            runner.TurnRequest(resolution=resolve(), prompt="please fail this on purpose"),
        )
    )
    assert problem is None

    meta = _wait_for_finished(session_id)
    assert meta.state == "failed"
    events = _events(session_id)
    error_events = [event for event in events if event.get("type") == "error"]
    assert error_events
    assert "stop reason: refusal" in error_events[-1]["message"]
    failed_state_index = next(
        index
        for index, event in enumerate(events)
        if event.get("type") == "state" and event.get("to") == "failed"
    )
    assert all(
        index < failed_state_index
        for index, event in enumerate(events)
        if event.get("type") == "error"
    )


def test_daemon_stop_leaves_an_already_finished_session_alone(
    state_root: Path, live_daemon: None
) -> None:
    session_id = new_session("a finished session")
    run_turn(session_id, "a finished session")

    asyncio.run(_stop_target())

    assert sessions.read_meta(session_id).state == "done"


async def _stop_target() -> None:
    connection = await daemon_client.connect(target())
    assert connection is not None
    try:
        await connection.stop()
    finally:
        await connection.close()


def test_the_target_heals_after_its_adapter_dies(state_root: Path, live_daemon: None) -> None:
    import os
    import signal

    first = new_session("warm the adapter")
    assert run_turn(first, "warm the adapter").state == "done"

    daemon_pid = asyncio.run(_daemon_pid())
    children_path = Path(f"/proc/{daemon_pid}/task/{daemon_pid}/children")
    if not children_path.exists():  # pragma: no cover - non-Linux
        pytest.skip("requires /proc/<pid>/children")
    adapters = [int(pid) for pid in children_path.read_text().split()]
    assert len(adapters) == 1
    os.kill(adapters[0], signal.SIGKILL)

    # The daemon must recover the target with a fresh adapter; without the
    # heal every turn fails with a dead connection until `daemon stop`.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        session_id = new_session("after the crash")
        try:
            if run_turn(session_id, "after the crash").state == "done":
                return
        except runner.RunnerError:
            pass  # the daemon may still be tearing the dead turn down
        time.sleep(0.2)
    pytest.fail("the target never recovered after its adapter died")


# --- idle expiry ------------------------------------------------------------


def test_an_idle_daemon_expires_after_its_ttl(state_root: Path, live_daemon: None) -> None:
    configure(state_root, 'daemon_ttl = "1s"\n')
    session_id = new_session("start something warm")
    run_turn(session_id, "start something warm")

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if asyncio.run(daemon_client.connect(target())) is None:
            break
        time.sleep(0.25)
    else:  # pragma: no cover - only on a genuinely stuck daemon
        pytest.fail("the daemon never expired")

    assert asyncio.run(daemon_client.connect(target())) is None


# --- concurrency ------------------------------------------------------------


def test_a_turn_past_the_slot_limit_is_reported_as_queued(
    state_root: Path, live_daemon: None
) -> None:
    configure(state_root, "daemon_max_concurrent = 1\n")
    blocker = new_session("slow:30 slot holder")
    asyncio.run(
        runner.dispatch_background(
            blocker, runner.TurnRequest(resolution=resolve(), prompt="slow:30 slot holder")
        )
    )
    _wait_for(blocker, "running")

    second = new_session("waiting for a slot")
    outcome = asyncio.run(_start_and_report(second, "waiting for a slot"))

    assert outcome is True


async def _start_and_report(session_id: str, prompt: str) -> bool:
    connection = await daemon_client.connect(target())
    assert connection is not None
    try:
        reply = await connection.start_turn(
            session_id,
            runner.daemon_payload(runner.TurnRequest(resolution=resolve(), prompt=prompt)),
        )
        return bool(reply["queued"])
    finally:
        await connection.close()


# --- observability ----------------------------------------------------------


def test_status_reports_the_pid_uptime_and_log_path(state_root: Path, live_daemon: None) -> None:
    session_id = new_session("something to report")
    run_turn(session_id, "something to report")

    async def status() -> dict:
        connection = await daemon_client.connect(target())
        assert connection is not None
        try:
            return await connection.status()
        finally:
            await connection.close()

    reply = asyncio.run(status())

    assert reply["pid"] > 0
    assert reply["uptime"] >= 0
    assert reply["log"] == str(daemon.log_path_for_target(target()))


def test_adapter_stderr_lands_in_the_per_target_log(state_root: Path, live_daemon: None) -> None:
    session_id = new_session("stderr:a word from the adapter")

    run_turn(session_id, "stderr:a word from the adapter")

    log = daemon.log_path_for_target(target())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if "a word from the adapter" in log.read_text(encoding="utf-8"):
            return
        time.sleep(0.1)
    pytest.fail(f"adapter stderr never reached {log}")


def test_the_daemon_keeps_each_session_transcript_separate(
    state_root: Path, live_daemon: None
) -> None:
    """One adapter, many sessions: an update must not land on the wrong file."""
    first = new_session("echo:belongs to the first")
    second = new_session("echo:belongs to the second")

    run_turn(first, "echo:belongs to the first")
    run_turn(second, "echo:belongs to the second")

    first_text = _transcript_text(first)
    second_text = _transcript_text(second)
    assert "belongs to the first" in first_text
    assert "belongs to the first" not in second_text
    assert "belongs to the second" in second_text


def test_concurrent_sessions_never_land_on_each_others_transcripts(
    state_root: Path, live_daemon: None, tmp_path: Path
) -> None:
    """Two turns started in the same tick, so they are provably in flight together.

    That is the only window where the demux and the adapter start lock matter:
    each transcript must hold exactly one `holding` message — its own — and
    both turns must reach the one adapter the daemon started.
    """
    releases = [tmp_path / "release-a", tmp_path / "release-b"]
    prompts = [f"chunkhold:{release}" for release in releases]
    ids = [new_session(prompt) for prompt in prompts]

    asyncio.run(_start_together(list(zip(ids, prompts, strict=True))))
    for session_id in ids:
        _wait_for_message(session_id, "holding")

    try:
        assert [_count_messages(session_id, "holding") for session_id in ids] == [1, 1]
    finally:
        for release in releases:
            release.write_text("go", encoding="utf-8")
        for session_id in ids:
            _wait_for_finished(session_id)

    assert [sessions.read_meta(session_id).state for session_id in ids] == ["done", "done"]


async def _start_together(work: list[tuple[str, str]]) -> None:
    """Hand the daemon both turns before either has begun running."""
    routed = await daemon_client.ensure_daemon(target())
    assert not isinstance(routed, daemon_client.DaemonUnavailable)
    try:
        for session_id, prompt in work:
            reply = await routed.start_turn(
                session_id,
                runner.daemon_payload(runner.TurnRequest(resolution=resolve(), prompt=prompt)),
            )
            assert reply["ok"], reply
    finally:
        await routed.close()


def _messages(session_id: str) -> list[dict]:
    path = sessions.transcript_path(session_id)
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return [event for event in events if event.get("type") == "msg"]


def _count_messages(session_id: str, needle: str) -> int:
    return sum(needle in event.get("text", "") for event in _messages(session_id))


def _wait_for_message(session_id: str, needle: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _count_messages(session_id, needle):
            return
        time.sleep(0.1)
    pytest.fail(f"session {session_id} never emitted {needle!r}")


def _transcript_text(session_id: str) -> str:
    path = sessions.transcript_path(session_id)
    return "\n".join(
        json.dumps(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    )


def _events(session_id: str) -> list[dict]:
    path = sessions.transcript_path(session_id)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _wait_for(session_id: str, state: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sessions.read_meta(session_id).state == state:
            return
        time.sleep(0.1)
    pytest.fail(f"session {session_id} never reached {state}")


def _wait_for_finished(session_id: str, timeout: float = 20.0) -> sessions.SessionMeta:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        meta = sessions.read_meta(session_id)
        if meta.is_finished:
            return meta
        time.sleep(0.1)
    pytest.fail(f"session {session_id} never finished")


def test_a_daemon_whose_socket_is_gone_stops_itself(state_root: Path, live_daemon: None) -> None:
    """Nothing can reach a daemon whose endpoint was removed, so it retires."""
    session_id = new_session("start the daemon")
    run_turn(session_id, "start the daemon")

    pid = asyncio.run(_daemon_pid())
    socket_path = ipc.socket_path_for_target(target())
    assert socket_path.exists()
    socket_path.unlink()

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.25)
    pytest.fail("the daemon outlived its socket")


async def _daemon_pid() -> int:
    connection = await daemon_client.connect(target())
    assert connection is not None
    try:
        return (await connection.status())["pid"]
    finally:
        await connection.close()


def _pid_alive(pid: int) -> bool:
    """True only while the process is really running.

    Not `os.kill(pid, 0)`: the daemon is a child of this test process and
    nothing reaps it, so an exited daemon lingers as a zombie that still
    accepts signal 0. A zombie has no cmdline.
    """
    return bool(proc.process_cmdline(pid))


def test_a_daemon_that_loses_the_endpoint_retires(state_root: Path, live_daemon: None) -> None:
    """Binding unlinks whatever is at the path, so ownership can change hands.

    The displaced daemon has to notice: otherwise it lives on unreachable,
    holding an adapter and answering to nobody.
    """
    session_id = new_session("first daemon up")
    run_turn(session_id, "first daemon up")
    original = asyncio.run(_daemon_pid())

    socket_path = ipc.socket_path_for_target(target())
    socket_path.unlink()
    socket_path.touch()

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if not _pid_alive(original):
            return
        time.sleep(0.25)
    pytest.fail("the displaced daemon kept running")
