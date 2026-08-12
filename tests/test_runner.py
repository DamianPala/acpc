"""Behavioral tests for one turn end to end on the direct path."""

import asyncio
import contextlib
import json
import os
import signal
import sys
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from acp import RequestError, text_block
from acp.schema import UserMessageChunk

from acpc import cache, daemon_client, runner, sessions, vocab
from acpc.client import AcpcClient
from acpc.permissions import PermissionLevel
from acpc.registry import AgentRegistry
from acpc.transcript import Transcript

MOCK_AGENT_SCRIPT = str(Path(__file__).with_name("mock_agent.py"))

MOCK_ENTRY = f"""
name = "Mock Agent"
command = "{sys.executable} {MOCK_AGENT_SCRIPT}"
install_command = "true"
home = "~/.mock"
home_env = "MOCK_HOME"
efforts = ["low", "medium", "high", "xhigh"]
mode = "default"

[modes]
default = {{ grants = "read", delegates = true }}
plan = {{ grants = "read", delegates = true }}
yolo = {{ grants = "all", delegates = false }}

[presets]
fast = {{ model = "mock-haiku-4-5", effort = "high" }}
standard = {{ model = "mock-sonnet-5", effort = "high" }}
max = {{ model = "mock-opus-5", effort = "xhigh" }}
"""

PHANTOM_ENTRY = """
name = "Phantom Agent"
command = "definitely-not-installed-phantom-xyz"
install_command = "false"
"""


@pytest.fixture(autouse=True)
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own ACPC_HOME with the mock adapter registered."""
    root = tmp_path / "state"
    agents = root / "agents"
    agents.mkdir(parents=True)
    (agents / "mock.toml").write_text(MOCK_ENTRY, encoding="utf-8")
    (agents / "phantom.toml").write_text(PHANTOM_ENTRY, encoding="utf-8")
    monkeypatch.setenv("ACPC_HOME", str(root))
    return root


def resolve(agent: str = "mock", **overrides: str | None):
    if overrides.get("permissions") is None:
        overrides["permissions"] = "read"
    return AgentRegistry().resolve_call(agent, **overrides)


def start_turn(
    prompt: str,
    *,
    agent: str = "mock",
    permissions: str | None = None,
    mode: str | None = None,
    **kwargs,
) -> tuple[str, runner.TurnOutcome]:
    """Create a session and run one turn against the mock, as `run` does."""
    resolution = resolve(agent, mode=mode, permissions=permissions)
    meta = sessions.create_session(
        entry=resolution.entry.entry,
        base_adapter=resolution.entry.base_adapter,
        prompt=prompt,
        resolution=runner.resolution_payload(resolution, cwd=None),
        target=runner.call_target(resolution),
    )
    request = runner.TurnRequest(resolution=resolution, prompt=prompt, **kwargs)
    return meta.session_id, runner.execute_turn(meta.session_id, request)


def transcript_events(session_id: str) -> list[dict]:
    path = sessions.transcript_path(session_id)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# --- exit codes -------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("done", vocab.EXIT_OK),
        ("failed", vocab.EXIT_AGENT_ERROR),
        ("timeout", vocab.EXIT_TIMEOUT),
        ("cancelled", vocab.EXIT_CANCELLED),
        ("detached", vocab.EXIT_SIGTERM),
        ("terminated", vocab.EXIT_SIGTERM),
        ("orphaned", vocab.EXIT_AGENT_ERROR),
    ],
)
def test_each_final_state_maps_to_its_fixed_exit_code(state: str, expected: int) -> None:
    assert runner.exit_code_for(state) == expected


def test_exit_code_ignores_the_stop_reason_for_a_finished_turn() -> None:
    assert runner.exit_code_for("done", "refusal") == vocab.EXIT_OK
    assert runner.exit_code_for("failed", "end_turn") == vocab.EXIT_AGENT_ERROR


# --- the happy path ---------------------------------------------------------


def test_a_successful_turn_finishes_the_session_on_disk() -> None:
    session_id, outcome = start_turn("summarize the module layout")

    assert outcome.state == "done"
    assert outcome.exit_code == vocab.EXIT_OK
    assert "summarize the module layout" in outcome.answer
    assert not any(event.get("type") == "error" for event in transcript_events(session_id))

    meta = sessions.read_meta(session_id)
    assert meta.state == "done"
    assert meta.exit_code == vocab.EXIT_OK
    assert meta.finished_at is not None

    assert sessions.answer_path(session_id).read_text(encoding="utf-8") == outcome.answer
    prompt = sessions.prompt_path(session_id).read_text(encoding="utf-8")
    assert prompt == "summarize the module layout"


class _NoObserverResumeConnection:
    """Restore stub whose callbacks finish after the restore response."""

    def __init__(self, client: AcpcClient, *, list_available: bool) -> None:
        self._conn = object()
        self._client = client
        self._list_available = list_available
        self.suffix_ready = asyncio.Event()
        self.suffix_sent = asyncio.Event()
        self.late_task: asyncio.Task[None] | None = None

    async def load_session(self, session_id: str, **kwargs: object) -> None:
        del kwargs
        await self._client.session_update(
            session_id,
            UserMessageChunk(
                session_update="user_message_chunk",
                content=text_block("stored prompt"),
                message_id="replay-1",
            ),
        )

        async def send_late_suffix() -> None:
            await self.suffix_ready.wait()
            await self._client.session_update(
                session_id,
                UserMessageChunk(
                    session_update="user_message_chunk",
                    content=text_block(" + mismatched suffix"),
                    message_id="replay-1",
                ),
            )
            self.suffix_sent.set()

        self.late_task = asyncio.create_task(send_late_suffix())

    async def list_sessions(self, **kwargs: object) -> SimpleNamespace:
        del kwargs
        if not self._list_available:
            raise AssertionError("session/list must not be called when unavailable")
        return SimpleNamespace(
            sessions=[SimpleNamespace(session_id="adapter-session", cwd="/tmp")],
            next_cursor=None,
        )


class _RawObserver:
    def __init__(self) -> None:
        self._observers: list[Any] = []

    def add_observer(self, observer: Any) -> None:
        self._observers.append(observer)

    def emit_user_message(self, session_id: str, text: str) -> None:
        event = SimpleNamespace(
            direction=SimpleNamespace(value="incoming"),
            message={
                "method": "session/update",
                "params": {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": "user_message_chunk",
                        "content": {"type": "text", "text": text},
                        "messageId": "replay-1",
                    },
                },
            },
        )
        for observer in self._observers:
            observer(event)


class _ReplayResumeConnection(_NoObserverResumeConnection):
    def __init__(self, client: AcpcClient, *, list_available: bool) -> None:
        super().__init__(client, list_available=list_available)
        self._raw = _RawObserver()
        self._conn = self._raw

    async def load_session(self, session_id: str, **kwargs: object) -> None:
        await super().load_session(session_id, **kwargs)
        self._raw.emit_user_message(session_id, "stored prompt")


def _resume_test_client(tmp_path: Path) -> AcpcClient:
    return AcpcClient(Transcript(tmp_path / "transcript.ndjson"), PermissionLevel.READ)


def _resume_test_capabilities(*, list_available: bool) -> SimpleNamespace:
    return SimpleNamespace(
        load_session=True,
        session_capabilities=SimpleNamespace(list=object() if list_available else None),
    )


async def _run_no_observer_resume(
    tmp_path: Path, *, list_available: bool
) -> tuple[str, _NoObserverResumeConnection]:
    client = _resume_test_client(tmp_path)
    connection = _NoObserverResumeConnection(client, list_available=list_available)
    status = await runner.verify_adapter_resume(
        connection,
        client,
        _resume_test_capabilities(list_available=list_available),
        "adapter-session",
        "/tmp",
        "session-id",
    )
    assert connection.late_task is not None
    connection.suffix_ready.set()
    await connection.late_task
    return status, connection


def test_no_observer_replay_is_unverified_until_late_chunks_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runner,
        "_stored_prompt_items",
        lambda _session_id: [(Path("prompt.md"), "stored prompt")],
    )

    status, connection = asyncio.run(_run_no_observer_resume(tmp_path, list_available=False))

    assert connection.suffix_sent.is_set()
    assert status == "unverified — session/list unavailable; conversation replay unavailable"


def test_no_observer_replay_does_not_block_independent_listing_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runner,
        "_stored_prompt_items",
        lambda _session_id: [(Path("prompt.md"), "stored prompt")],
    )

    status, connection = asyncio.run(_run_no_observer_resume(tmp_path, list_available=True))

    assert connection.suffix_sent.is_set()
    assert status == "verified"


def test_empty_prompt_comparison_is_not_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "_stored_prompt_items", lambda _session_id: [])
    client = _resume_test_client(tmp_path)
    connection = _ReplayResumeConnection(client, list_available=False)

    status = asyncio.run(
        runner.verify_adapter_resume(
            connection,
            client,
            _resume_test_capabilities(list_available=False),
            "adapter-session",
            "/tmp",
            "session-id",
        )
    )

    assert status == "unverified — session/list unavailable; conversation replay unavailable"


def test_failed_state_is_observable_only_after_its_explanation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_write_meta = sessions.write_meta
    observations: list[tuple[sessions.SessionMeta, list[dict]]] = []

    def observe_after_publish(meta: sessions.SessionMeta) -> None:
        original_write_meta(meta)
        if meta.state != "failed":
            return
        observed_meta = sessions.read_meta(meta.session_id)
        observed_events = transcript_events(meta.session_id)
        observations.append((observed_meta, observed_events))

    monkeypatch.setattr(sessions, "write_meta", observe_after_publish)
    _, outcome = start_turn("please fail this on purpose")

    assert outcome.state == "failed"
    assert observations
    observed_meta, observed_events = observations[-1]
    assert observed_meta.state == "failed"
    assert any(event.get("type") == "error" for event in observed_events)
    assert any(
        event.get("type") == "state" and event.get("to") == "failed" for event in observed_events
    )


def test_prompt_marker_is_retried_before_a_successful_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = sessions.mark_prompt_delivered
    attempts = 0

    def fail_once(session_id: str, prompt: str) -> sessions.SessionMeta:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("marker storage temporarily unavailable")
        return original(session_id, prompt)

    monkeypatch.setattr(sessions, "mark_prompt_delivered", fail_once)
    session_id, outcome = start_turn("persist the outgoing prompt")

    assert outcome.state == "done"
    assert attempts >= 2
    delivered = sessions.read_meta(session_id).extra["delivered_prompts"]
    assert [record["turn"] for record in delivered] == [1]
    assert sessions.DELIVERY_RECORD_INCOMPLETE not in sessions.read_meta(session_id).extra


def test_prompt_marker_failure_keeps_the_answer_and_marks_the_record_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def always_fail(session_id: str, prompt: str) -> sessions.SessionMeta:
        nonlocal attempts
        attempts += 1
        raise OSError(f"cannot persist marker for {session_id}: {prompt}")

    monkeypatch.setattr(sessions, "mark_prompt_delivered", always_fail)
    session_id, outcome = start_turn("marker must not disappear")

    assert attempts == 4  # the observer attempt plus the three existing retries
    assert outcome.state == "done"
    assert "marker must not disappear" in outcome.answer
    meta = sessions.read_meta(session_id)
    assert meta.state == "done"
    assert meta.extra[sessions.DELIVERY_RECORD_INCOMPLETE] is True
    assert sessions.answer_path(session_id).read_text(encoding="utf-8") == outcome.answer
    errors = [event for event in transcript_events(session_id) if event.get("type") == "error"]
    assert not errors


class _NoFrameObserverConnection:
    """Proxy a real ACP connection while hiding its raw-frame observer hook."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection
        self._conn = object()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


class _ClosedBeforePromptConnection:
    """Proxy a real ACP connection that closes before its prompt preflight."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection
        self._conn = connection._conn

    async def prompt(self, **kwargs: Any) -> Any:
        await self._connection.close()
        return await self._connection.prompt(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def test_unobservable_delivery_finishes_and_stays_unverified_on_cold_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_spawn = runner.spawn_adapter

    @contextlib.asynccontextmanager
    async def spawn_without_observer(*args: Any, **kwargs: Any) -> AsyncIterator[tuple[Any, Any]]:
        async with original_spawn(*args, **kwargs) as (connection, process):
            yield _NoFrameObserverConnection(connection), process

    monkeypatch.setattr(runner, "spawn_adapter", spawn_without_observer)
    resolution = resolve()
    created = sessions.create_session(
        entry=resolution.entry.entry,
        base_adapter=resolution.entry.base_adapter,
        prompt="the unobservable prompt",
        resolution=runner.session_resolution(resolution, cwd=None),
        target=runner.call_target(resolution),
    )
    session_id = created.session_id
    first = runner.execute_turn(
        session_id,
        runner.TurnRequest(resolution=resolution, prompt="the unobservable prompt"),
    )

    assert first.state == "done"
    assert "the unobservable prompt" in sessions.answer_path(session_id).read_text(encoding="utf-8")
    meta = sessions.load(session_id)
    assert meta.extra[sessions.DELIVERY_RECORD_INCOMPLETE] is True
    assert "delivered_prompts" not in meta.extra

    request = runner.continue_request(meta, "the cold follow-up", defer_rotation=True)
    second = runner.execute_turn(session_id, request)

    assert second.state == "done"
    resumed = sessions.load(session_id)
    assert resumed.extra["resume"] == "unverified — delivery record incomplete"
    assert "the cold follow-up" in sessions.answer_path(session_id).read_text(encoding="utf-8")


def test_pre_send_prompt_failure_with_observer_keeps_record_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_spawn = runner.spawn_adapter

    @contextlib.asynccontextmanager
    async def spawn_closed_before_prompt(
        *args: Any, **kwargs: Any
    ) -> AsyncIterator[tuple[Any, Any]]:
        async with original_spawn(*args, **kwargs) as (connection, process):
            yield _ClosedBeforePromptConnection(connection), process

    monkeypatch.setattr(runner, "spawn_adapter", spawn_closed_before_prompt)
    resolution = resolve()
    created = sessions.create_session(
        entry=resolution.entry.entry,
        base_adapter=resolution.entry.base_adapter,
        prompt="never sent",
        resolution=runner.session_resolution(resolution, cwd=None),
        target=runner.call_target(resolution),
    )
    session_id = created.session_id
    first = runner.execute_turn(
        session_id,
        runner.TurnRequest(resolution=resolution, prompt="never sent"),
    )

    assert first.state == "failed"
    first_meta = sessions.load(session_id)
    assert first_meta.extra.get(sessions.DELIVERY_RECORD_INCOMPLETE) is not True
    assert "delivered_prompts" not in first_meta.extra

    monkeypatch.setattr(runner, "spawn_adapter", original_spawn)
    second = runner.execute_turn(
        session_id,
        runner.continue_request(first_meta, "later prompt", defer_rotation=True),
    )

    assert second.state == "done"
    resumed = sessions.load(session_id)
    assert resumed.extra["resume"] == "verified"
    assert "later prompt" in sessions.answer_path(session_id).read_text(encoding="utf-8")


def test_a_turn_records_its_start_and_end_as_state_events() -> None:
    session_id, _ = start_turn("record the state transitions")

    transitions = [
        (event["from"], event["to"])
        for event in transcript_events(session_id)
        if event.get("type") == "state"
    ]
    assert ("starting", "running") in transitions
    assert ("running", "done") in transitions


def test_token_usage_reaches_the_session_metadata() -> None:
    session_id, outcome = start_turn("report some usage")

    assert outcome.tokens > 0
    assert sessions.read_meta(session_id).tokens == outcome.tokens


# --- failure, timeout, cancellation -----------------------------------------


def test_a_refused_turn_is_a_failure_with_exit_1() -> None:
    session_id, outcome = start_turn("please fail this on purpose")

    assert outcome.stop_reason == "refusal"
    assert outcome.state == "failed"
    assert outcome.exit_code == vocab.EXIT_AGENT_ERROR
    assert sessions.read_meta(session_id).state == "failed"
    errors = [event for event in transcript_events(session_id) if event.get("type") == "error"]
    assert errors
    # Direct path: nothing was written to a daemon log, so nothing points there.
    assert "daemon log" not in errors[-1]["next_step"]
    assert "adapter_log_tail" not in errors[-1]


def test_a_refused_turn_still_writes_the_partial_answer() -> None:
    session_id, outcome = start_turn("please fail this on purpose")

    answer = sessions.answer_path(session_id).read_text(encoding="utf-8")
    assert "Unable to complete" in answer
    assert answer == outcome.answer


def test_a_switch_above_the_ceiling_ends_the_turn_with_exit_2() -> None:
    session_id, outcome = start_turn("run the perm scenario", permissions="read")

    assert outcome.state == "failed"
    assert outcome.stop_reason == "permission_denied"
    assert outcome.exit_code == vocab.EXIT_USAGE
    events = transcript_events(session_id)
    errors = [event["message"] for event in events if event.get("type") == "error"]
    assert any("switch_mode yolo" in message for message in errors)
    assert any("--permissions all" in message for message in errors)
    # acpc refused this; the adapter did not fail. A second, contradictory
    # "inspect the daemon log" remedy would send the caller the wrong way.
    assert not any("adapter failure" in message for message in errors)
    assert not any("next step: inspect" in message for message in errors)
    switch_permissions = [
        event
        for event in events
        if event.get("type") == "permission" and event.get("kind") == "switch_mode"
    ]
    # The perm mock requests plan immediately after yolo; cancellation must win that race.
    assert len(switch_permissions) == 1
    assert switch_permissions[-1]["decision"] == "deny"
    assert not any(
        "switch_mode:plan" in event.get("message", "")
        for event in events
        if event.get("type") == "error"
    )


def test_timeout_cancels_the_turn_and_exits_124() -> None:
    session_id, outcome = start_turn("slow:30 timeout probe", timeout=1.0)

    assert outcome.state == "timeout"
    assert outcome.exit_code == vocab.EXIT_TIMEOUT
    meta = sessions.read_meta(session_id)
    assert meta.state == "timeout"
    assert meta.exit_code == vocab.EXIT_TIMEOUT


def test_a_timed_out_turn_leaves_an_answer_file_behind() -> None:
    session_id, _ = start_turn("slow:30 timeout probe", timeout=1.0)

    assert sessions.answer_path(session_id).exists()


def test_authentication_refusal_records_the_login_remedy() -> None:
    session_id, outcome = start_turn("auth:vendor rejected the request")

    assert outcome.state == "failed"
    assert outcome.exit_code == vocab.EXIT_AGENT_ERROR
    errors = [event for event in transcript_events(session_id) if event.get("type") == "error"]
    assert errors
    event = errors[-1]
    assert "authentication was refused" in event["message"]
    assert event["next_step"] == "run 'mock login'"
    assert "mock login" in sessions.answer_path(session_id).read_text(encoding="utf-8")


def test_a_late_adapter_failure_keeps_the_partial_answer() -> None:
    session_id, outcome = start_turn("crash-late:half of the report was written")

    assert outcome.state == "failed"
    answer = sessions.answer_path(session_id).read_text(encoding="utf-8")
    assert "half of the report was written" in answer
    errors = [event for event in transcript_events(session_id) if event.get("type") == "error"]
    assert errors
    assert "upstream connection reset" in errors[-1]["message"]


def test_a_huge_adapter_log_does_not_flood_the_failure_message(tmp_path: Path) -> None:
    """The message rides `answer.md` and `wait`'s one-line summary, so it stays small."""
    log_file = tmp_path / "target.log"
    log_file.write_bytes(b"x" * (4 * runner.ADAPTER_LOG_TAIL_BYTES))

    tail = runner._read_adapter_log_tail(log_file, 0)

    assert tail is not None
    assert len(tail) <= runner.ADAPTER_LOG_TAIL_BYTES
    assert len(runner._single_line(tail)[: runner.MESSAGE_TAIL_CHARS]) <= runner.MESSAGE_TAIL_CHARS


def test_adapter_start_failure_records_an_error_event() -> None:
    resolution = resolve("phantom")
    meta = sessions.create_session(
        entry="phantom",
        base_adapter="phantom",
        prompt="start failure",
        resolution=runner.resolution_payload(resolution, cwd=None),
        target=runner.call_target(resolution),
    )

    with pytest.raises(runner.RunnerError):
        runner.execute_turn(meta.session_id, runner.TurnRequest(resolution=resolution, prompt="x"))

    errors = [event for event in transcript_events(meta.session_id) if event.get("type") == "error"]
    assert errors
    assert "not installed" in errors[-1]["message"]


def deliver_after(delay: float, signal_number: int) -> None:
    """Signal this very process once the turn is under way."""

    def send() -> None:
        time.sleep(delay)
        os.kill(os.getpid(), signal_number)

    threading.Thread(target=send, daemon=True).start()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal semantics")
def test_sigint_during_a_turn_cancels_it_and_exits_130() -> None:
    deliver_after(1.0, signal.SIGINT)

    session_id, outcome = start_turn("slow:30 sigint probe")

    assert outcome.state == "cancelled"
    assert outcome.exit_code == vocab.EXIT_CANCELLED
    assert sessions.read_meta(session_id).state == "cancelled"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal semantics")
def test_sigterm_on_the_direct_path_cancels_too_but_exits_143() -> None:
    # A direct child cannot outlive its parent, so SIGTERM cannot detach here.
    deliver_after(1.0, signal.SIGTERM)

    session_id, outcome = start_turn("slow:30 sigterm probe")

    assert outcome.exit_code == vocab.EXIT_SIGTERM
    assert sessions.read_meta(session_id).state == "cancelled"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal semantics")
def test_sigterm_after_daemon_route_resolution_detaches_before_the_turn_finishes(
    live_daemon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_ensure_daemon = daemon_client.ensure_daemon

    async def ensure_and_signal(target: str):
        daemon = await real_ensure_daemon(target)
        asyncio.get_running_loop().call_soon(os.kill, os.getpid(), signal.SIGTERM)
        return daemon

    monkeypatch.setattr(daemon_client, "ensure_daemon", ensure_and_signal)
    resolution = resolve()
    assert resolution.mode is not None
    resolution = replace(resolution, mode_spec=resolution.entry.modes[resolution.mode])
    meta = sessions.create_session(
        entry=resolution.entry.entry,
        base_adapter=resolution.entry.base_adapter,
        prompt="slow:30 sigterm route boundary probe",
        resolution=runner.resolution_payload(resolution, cwd=None),
        target=runner.call_target(resolution),
    )
    request = runner.TurnRequest(
        resolution=resolution, prompt="slow:30 sigterm route boundary probe"
    )
    session_id = meta.session_id
    outcome = runner.execute_turn(session_id, request)

    assert outcome.state == "detached"
    assert outcome.exit_code == vocab.EXIT_SIGTERM
    assert sessions.read_meta(session_id).state == "running"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal semantics")
def test_a_cancelled_turn_keeps_whatever_answer_had_arrived() -> None:
    deliver_after(1.0, signal.SIGINT)

    session_id, _ = start_turn("slow:30 partial answer probe")

    assert sessions.answer_path(session_id).exists()


# --- routing ----------------------------------------------------------------


def test_the_direct_child_fallback_is_reported_not_silent() -> None:
    _, outcome = start_turn("note the route")

    assert outcome.route_note is not None
    assert outcome.route_note.startswith("direct child")


def test_a_missing_adapter_binary_names_the_install_command() -> None:
    with pytest.raises(runner.RunnerError) as caught:
        runner.adapter_command(resolve("phantom"))

    message = str(caught.value)
    assert "definitely-not-installed-phantom-xyz" in message
    assert "acpc install phantom" in message


def test_a_turn_that_cannot_start_still_leaves_a_finished_session() -> None:
    resolution = resolve("phantom")
    meta = sessions.create_session(
        entry=resolution.entry.entry,
        base_adapter=resolution.entry.base_adapter,
        prompt="never runs",
        resolution=runner.resolution_payload(resolution, cwd=None),
        target=runner.call_target(resolution),
    )
    with pytest.raises(runner.RunnerError):
        runner.execute_turn(
            meta.session_id, runner.TurnRequest(resolution=resolution, prompt="never runs")
        )

    # The one state SPEC says no reader may ever be shown after the process is gone.
    assert sessions.read_meta(meta.session_id).state == "failed"


def test_two_entries_on_the_same_adapter_and_home_share_a_target() -> None:
    assert runner.call_target(resolve("mock")) == runner.call_target(resolve("mock"))


def test_call_target_includes_the_resolved_policy() -> None:
    read_target = runner.call_target(resolve("mock", permissions="read"))
    edit_target = runner.call_target(resolve("mock", permissions="edit"))

    assert read_target != edit_target


def test_call_target_rejects_an_unresolved_permission_policy() -> None:
    with pytest.raises(runner.RunnerError, match="permission policy is unresolved"):
        runner.call_target(AgentRegistry().resolve_call("mock"))


def test_legacy_session_resolution_selects_read_without_policy_facts() -> None:
    resolution = resolve()
    legacy_payload = runner.session_resolution(resolution, cwd=None)
    legacy_payload["resolved"].pop("permissions")
    for field in ("mode", "grants", "delegates"):
        legacy_payload["adapter"].pop(field, None)
    legacy_payload["adapter"].pop("modes", None)
    meta = sessions.create_session(
        entry=resolution.entry.entry,
        base_adapter=resolution.entry.base_adapter,
        prompt="legacy session",
        resolution=legacy_payload,
        target="mock~legacy-s3-target",
    )

    rebuilt = runner.resolution_from_session(meta)

    assert rebuilt.permissions == "read"
    assert rebuilt.mode_spec is None
    assert runner.call_target(rebuilt) != meta.target


# --- call options reach the adapter -----------------------------------------


def test_model_and_effort_are_applied_to_the_adapter_session() -> None:
    _, outcome = start_turn("settings")

    model, effort, _mode, model_calls, _mode_calls, effort_calls = outcome.answer.strip().split("/")
    assert model == "mock-sonnet-5"
    assert effort == "high"
    assert model_calls == "1"
    assert effort_calls == "1"


def test_a_resolved_mode_is_always_applied_to_the_adapter_session() -> None:
    _, outcome = start_turn("settings")

    _model, _effort, mode, _model_calls, mode_calls, _effort_calls = outcome.answer.strip().split(
        "/"
    )
    assert mode == "default"
    assert mode_calls == "1"


def test_mode_is_applied_via_set_session_mode() -> None:
    _, outcome = start_turn("settings", mode="plan")

    _model, _effort, mode, _model_calls, mode_calls, _effort_calls = outcome.answer.strip().split(
        "/"
    )
    assert mode == "plan"
    assert mode_calls == "1"


def test_an_entry_mode_is_applied_via_set_session_mode(state_root: Path) -> None:
    (state_root / "agents" / "pinned.toml").write_text(
        'extends = "mock"\nmode = "plan"\n', encoding="utf-8"
    )

    _, outcome = start_turn("settings", agent="pinned")

    _model, _effort, mode, _model_calls, mode_calls, _effort_calls = outcome.answer.strip().split(
        "/"
    )
    assert mode == "plan"
    assert mode_calls == "1"


def test_an_unknown_mode_fails_the_turn_rather_than_running_it() -> None:
    session_id, outcome = start_turn("settings", mode="not-a-mode")

    assert outcome.state == "failed"
    assert sessions.read_meta(session_id).state == "failed"


def test_the_entrys_effort_config_id_names_the_wire_option(state_root: Path) -> None:
    """The effort config id is a vendor fact (codex speaks `reasoning_effort`,
    claude speaks `effort`), so the entry declares it and a wrong id fails the
    turn with the vendor's own diagnosis, not a bare 'Internal error'."""
    (state_root / "agents" / "custom.toml").write_text(
        'extends = "mock"\neffort_config_id = "custom_effort"\neffort = "high"\n',
        encoding="utf-8",
    )

    session_id, outcome = start_turn("echo:hi", agent="custom")

    assert outcome.state == "failed"
    answer = sessions.answer_path(session_id).read_text(encoding="utf-8")
    assert "the adapter rejected effort 'high' (config option 'custom_effort')" in answer
    assert "Unknown config option: custom_effort" in answer
    assert "(JSON-RPC -32603)" in answer


def test_an_unknown_effort_option_names_the_model_that_may_not_take_one(
    state_root: Path,
) -> None:
    """An adapter that has no effort option at all answers with a generic
    internal error, so the bare rejection reads as a bug in acpc. The hint
    names the model, which is the thing the caller can actually change."""
    (state_root / "agents" / "custom.toml").write_text(
        'extends = "mock"\n'
        'effort_config_id = "custom_effort"\n'
        'effort = "high"\n'
        'model = "mock-opus-5"\n',
        encoding="utf-8",
    )

    session_id, outcome = start_turn("echo:hi", agent="custom")

    assert outcome.state == "failed"
    answer = sessions.answer_path(session_id).read_text(encoding="utf-8")
    assert "Unknown config option: custom_effort" in answer
    assert "model 'mock-opus-5' may not take an effort setting" in answer
    assert "drop --effort, or drop effort from the preset in the entry TOML" in answer


def test_describe_error_surfaces_the_json_rpc_data() -> None:
    """A JSON-RPC error's fixed message hides the vendor's diagnosis in data."""
    error = RequestError(
        -32603, "Internal error", {"details": "Unknown config option: reasoning_effort"}
    )

    described = runner.describe_error(error)

    assert described == (
        "Internal error: Unknown config option: reasoning_effort (JSON-RPC -32603)"
    )
    assert runner.describe_error(ValueError("plain failure")) == "plain failure"


def test_the_cwd_is_where_the_adapter_runs(tmp_path: Path) -> None:
    workdir = tmp_path / "callee"
    workdir.mkdir()

    start_turn("write-file:proof.txt", cwd=str(workdir), permissions="write")

    assert (workdir / "proof.txt").exists()


# --- seams ------------------------------------------------------------------


def test_advertised_data_is_handed_to_the_cache_after_a_successful_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        cache,
        "refresh_advertised",
        lambda agent, advertised: seen.append((agent, dict(advertised))),
    )

    start_turn("refresh the advertised data")

    assert seen
    agent, advertised = seen[0]
    assert agent == "mock"
    assert advertised


def test_a_broken_cache_never_fails_a_turn_that_produced_an_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(agent: str, advertised: object) -> None:
        raise RuntimeError("cache is on fire")

    monkeypatch.setattr(cache, "refresh_advertised", explode)

    session_id, outcome = start_turn("survive a broken cache")

    assert outcome.state == "done"
    assert sessions.read_meta(session_id).state == "done"


def test_auto_prune_swallows_a_failing_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise OSError("state root is gone")

    monkeypatch.setattr(sessions, "prune_sessions", explode)

    runner.auto_prune(3600.0)  # must not raise


# --- the --dry-run view -----------------------------------------------------


def test_the_dry_run_payload_reports_every_value_with_its_source() -> None:
    payload = runner.resolution_payload(resolve("mock"), cwd=None)

    assert payload["entry"] == "mock"
    assert payload["base_adapter"] == "mock"
    assert payload["resolved"]["model"]["value"] == "mock-sonnet-5"
    assert payload["resolved"]["model"]["source"] == "adapter default"


def test_a_call_flag_is_labelled_as_coming_from_the_call() -> None:
    payload = runner.resolution_payload(resolve("mock", model="mock-opus-5"), cwd=None)

    assert payload["resolved"]["model"]["value"] == "mock-opus-5"
    assert payload["resolved"]["model"]["source"] == "call flag"


def test_the_dry_run_payload_carries_the_cwd_it_was_given() -> None:
    payload = runner.resolution_payload(resolve("mock"), cwd="/somewhere")

    assert payload["cwd"] == "/somewhere"
