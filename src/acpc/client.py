"""ACP client callbacks for transcript-backed acpc sessions."""

import asyncio
import inspect
import json
import logging
import time
import uuid
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any, cast
from weakref import WeakKeyDictionary

from acp import RequestError
from acp.schema import (
    AgentMessageChunk,
    AgentThoughtChunk,
    AllowedOutcome,
    AvailableCommandsUpdate,
    CreateTerminalResponse,
    CurrentModeUpdate,
    DeniedOutcome,
    KillTerminalResponse,
    NewSessionResponse,
    PermissionOption,
    ReadTextFileResponse,
    ReleaseTerminalResponse,
    RequestPermissionResponse,
    SessionConfigOptionBoolean,
    SessionConfigOptionSelect,
    TerminalOutputResponse,
    ToolCallProgress,
    ToolCallStart,
    UsageUpdate,
    UserMessageChunk,
    WaitForTerminalExitResponse,
    WriteTextFileResponse,
)

from acpc.permissions import (
    CLIENT_METHOD_CATEGORIES,
    ModeSelectionError,
    PermissionLevel,
    classify_kind,
    find_option,
    minimum_policy,
    select_mode,
    should_allow,
)
from acpc.registry import ModeSpec
from acpc.transcript import Transcript

_Clock = Callable[[], float]
_PermissionPrompt = Callable[[str, str], bool | Awaitable[bool]]
_EndTurn = Callable[[], None]
# 400 is application-defined because policy refusal is neither invalid params nor internal error.
CLIENT_PERMISSION_ERROR_CODE = 400

# Real adapters stream word-sized message chunks; one transcript event per
# chunk turns `log` into a per-fragment view and the cursor into a fragment
# counter. Consecutive same-type chunks therefore coalesce into one event, cut
# by whatever comes first: a different event type (the causal narrative keeps
# its interleaving), a pause in the stream, a bounded age so a long
# uninterrupted message still surfaces while running, or a size bound.
_CHUNK_GAP_SECONDS = 1.0
_CHUNK_MAX_AGE_SECONDS = 2.0
_CHUNK_MAX_CHARS = 4096
# A broken cancellation watcher must not hold an ACP permission response forever.
_CANCELLATION_DISPATCH_TIMEOUT = 1.0


@dataclass(slots=True)
class _ToolCall:
    title: str | None
    kind: str | None
    raw_input: Any
    started_at: float
    status: str | None = None
    finished: bool = False


@dataclass(slots=True)
class _PendingChunks:
    event_type: str
    parts: list[str]
    chars: int
    started_at: float
    last_at: float


class ReplaySink:
    """Collect user messages delivered while an adapter restores a session.

    The collected messages are what verified resume compares against the
    session's stored `prompt.md`/`prompt.<n>.md`, as an ordered subsequence.
    The sink keeps replay out of the active turn while its ordered messages
    feed resume verification.
    """

    def __init__(self, *, replay_available: bool = False) -> None:
        self._messages: list[str] = []
        self._message_indices: dict[str, int] = {}
        self._anonymous_message_index: int | None = None
        self._replay_available = replay_available

    @property
    def replay_available(self) -> bool:
        """Return whether collection was ordered by a transport observer."""
        return self._replay_available

    @property
    def user_messages(self) -> list[str]:
        """Return replayed user messages in their first-seen order."""
        return list(self._messages)

    def _consume_text(self, text: Any, message_id: Any) -> None:
        if not isinstance(text, str):
            return
        if isinstance(message_id, str) and message_id:
            message_index = self._message_indices.get(message_id)
            if message_index is None:
                message_index = len(self._messages)
                self._message_indices[message_id] = message_index
                self._messages.append("")
            self._messages[message_index] += text
            self._anonymous_message_index = None
            return
        if self._anonymous_message_index is None:
            self._anonymous_message_index = len(self._messages)
            self._messages.append("")
        self._messages[self._anonymous_message_index] += text

    def consume(self, update: Any) -> None:
        if not isinstance(update, UserMessageChunk):
            self._anonymous_message_index = None
            return
        text = getattr(update.content, "text", None)
        self._consume_text(text, getattr(update, "message_id", None))

    def consume_raw(self, update: Mapping[str, Any]) -> None:
        """Collect a user replay directly from an ordered ACP frame."""
        if update.get("sessionUpdate") != "user_message_chunk":
            self._anonymous_message_index = None
            return
        content = update.get("content")
        text = content.get("text") if isinstance(content, Mapping) else None
        self._consume_text(text, update.get("messageId"))


REPLAY_GENERATION_KEY = "acpc_replay_generation"
VALIDATED_SESSION_ID_KEY = "acpc_validated_session_id"
_REPLAY_SUPPRESSION_LOG = logging.getLogger(__name__)

# This is a memory-versus-leak-window tradeoff. Healthy generations release by
# accounting, so this is only a backstop; eviction is safe because an evicted
# tag reads as unknown and leaks stale output rather than swallowing live
# output. G5 beats G1 when the two conflict.
MAX_RETAINED_CLOSED_REPLAY_GENERATIONS = 64


class ReplayError(RuntimeError):
    """A replay generation violated its connection or binding lifecycle."""


@dataclass(slots=True)
class _ReplayGeneration:
    """One restore window owned by a raw connection and adapter session."""

    generation_id: int
    session_id: str
    sink: ReplaySink | None
    active: bool = True
    received: int = 0
    tagged_frame_ids: set[int] = dataclass_field(default_factory=set)
    seen_frame_ids: set[int] = dataclass_field(default_factory=set)


class ReplayTracker:
    """Keep replay suppression state on one connection, not on a client."""

    _trackers: WeakKeyDictionary[Any, "ReplayTracker"] = WeakKeyDictionary()

    def __init__(self, raw_connection: Any) -> None:
        try:
            self._raw_connection = weakref.ref(raw_connection)
        except TypeError:
            self._raw_connection = None
        self._next_generation = 0
        self._next_frame_id = 0
        self._active: dict[str, _ReplayGeneration] = {}
        self._generations: dict[int, _ReplayGeneration] = {}
        self._connection_token = uuid.uuid4().hex
        self._tagging_available = False
        self._close_task: asyncio.Task[Any] | None = None
        self._install()

    @classmethod
    def for_connection(cls, raw_connection: Any | None) -> "ReplayTracker | None":
        """Return the tracker attached to one raw connection, if available."""
        if raw_connection is None:
            return None
        try:
            tracker = cls._trackers.get(raw_connection)
        except TypeError:
            tracker = getattr(raw_connection, "_acpc_replay_tracker", None)
        if tracker is not None:
            return tracker
        tracker = cls(raw_connection)
        try:
            cls._trackers[raw_connection] = tracker
        except TypeError:
            with suppress(AttributeError, TypeError):
                raw_connection._acpc_replay_tracker = tracker
        return tracker

    def open(self, session_id: str, sink: ReplaySink) -> int:
        """Open a restore generation for one adapter session."""
        previous = self._active.get(session_id)
        if previous is not None:
            self._warn_open_generation(previous, "a new restore started")
            raise ReplayError(
                f"replay generation {previous.generation_id} for session {session_id} is still open"
            )
        self._next_generation += 1
        generation = _ReplayGeneration(
            generation_id=self._next_generation,
            session_id=session_id,
            sink=sink,
        )
        self._active[session_id] = generation
        self._generations[generation.generation_id] = generation
        return generation.generation_id

    def close(self, session_id: str, generation_id: int) -> None:
        """Close a restore generation while retaining its suppression history."""
        generation = self._generations.get(generation_id)
        if generation is None or generation.session_id != session_id:
            return
        generation.active = False
        if self._active.get(session_id) is generation:
            del self._active[session_id]
        generation.sink = None
        if not self._tagging_available and generation.received:
            _REPLAY_SUPPRESSION_LOG.warning(
                "replay suppression unavailable for %s: received frames cannot be "
                "phase-attributed and may leak",
                self._warning_name(generation),
            )
        if not self._tagging_available:
            self._generations.pop(generation.generation_id, None)
        else:
            self._release_if_accounted(generation)
            self._evict_closed_generations()

    def active_generation_id(self, session_id: str) -> int | None:
        """Return the active generation for a session, if restore is in progress."""
        generation = self._active.get(session_id)
        return generation.generation_id if generation is not None else None

    def tag_message(self, message: Any) -> None:
        """Capture identity and tag restore updates before ACP dispatches them."""
        if not isinstance(message, Mapping) or message.get("method") != "session/update":
            return
        params = message.get("params")
        if not isinstance(params, Mapping):
            return
        session_id = params.get("sessionId")
        if not isinstance(session_id, str):
            return
        metadata = params.get("_meta")
        if not isinstance(metadata, dict):
            metadata = {}
            params["_meta"] = metadata  # type: ignore[index]
        # Capture the validated envelope identity for every update. ACP merges
        # peer metadata into callback kwargs after this seam, so routing must
        # consume this value rather than the potentially overwritten argument.
        metadata[VALIDATED_SESSION_ID_KEY] = session_id
        generation = self._active.get(session_id)
        if generation is None:
            return
        frame_id = self._next_frame_id
        self._next_frame_id += 1
        generation.received += 1
        generation.tagged_frame_ids.add(frame_id)
        metadata[REPLAY_GENERATION_KEY] = self._tag_value(generation, frame_id)

    def consume_tag(self, generation_id: Any) -> str:
        """Account for a tagged callback and return its suppression status."""
        generation = self._generation_for(generation_id)
        if generation is None:
            _REPLAY_SUPPRESSION_LOG.warning(
                "replay suppression: frame arrived for unknown generation tag %r",
                generation_id,
            )
            return "unknown"
        frame_id = generation_id.get("frame") if isinstance(generation_id, Mapping) else None
        if not isinstance(frame_id, int) or isinstance(frame_id, bool):
            _REPLAY_SUPPRESSION_LOG.warning(
                "replay suppression: frame arrived with unknown frame identity for %s",
                self._warning_name(generation),
            )
            return "unknown"
        if frame_id not in generation.tagged_frame_ids:
            _REPLAY_SUPPRESSION_LOG.warning(
                "replay suppression: frame identity %r was not tagged for %s",
                frame_id,
                self._warning_name(generation),
            )
            return "unknown"
        generation.seen_frame_ids.add(frame_id)
        status = "active" if generation.active else "closed"
        self._release_if_accounted(generation)
        return status

    def session_for_tag(self, generation_id: Any) -> str | None:
        """Return the validated top-level session captured in a replay tag."""
        if not isinstance(generation_id, Mapping):
            return None
        if generation_id.get("connection") != self._connection_token:
            return None
        session_id = generation_id.get("session_id")
        return session_id if isinstance(session_id, str) else None

    def _generation_for(self, generation_id: Any) -> _ReplayGeneration | None:
        if not isinstance(generation_id, Mapping):
            return None
        if generation_id.get("connection") != self._connection_token:
            return None
        number = generation_id.get("generation")
        if not isinstance(number, int) or isinstance(number, bool):
            return None
        generation = self._generations.get(number)
        if generation is None or generation.session_id != generation_id.get("session_id"):
            return None
        return generation

    def _install(self) -> None:
        raw_connection = self._connection()
        if raw_connection is None:
            return
        add_observer = getattr(raw_connection, "add_observer", None)
        if callable(add_observer):
            add_observer(self._observe)
        self._install_close_hook(raw_connection)
        process_message = getattr(raw_connection, "_process_message", None)
        if not callable(process_message):
            return
        try:
            if getattr(raw_connection, "_acpc_replay_tagging", False):
                self._tagging_available = True
                return

            # ACP observers receive a deep-copied snapshot, so they cannot put
            # the tag on the message that the router will later parse. Wrap the
            # receive-to-dispatch seam instead, before that original message
            # enters the notification queue.
            async def tagged_process(message: Any) -> Any:
                self.tag_message(message)
                return await cast(Awaitable[Any], process_message(message))

            raw_connection._process_message = tagged_process
            raw_connection._acpc_replay_tagging = True
        except (AttributeError, TypeError):
            return
        self._tagging_available = True

    def _connection(self) -> Any | None:
        return self._raw_connection() if self._raw_connection is not None else None

    def _install_close_hook(self, raw_connection: Any) -> None:
        close = getattr(raw_connection, "close", None)
        if callable(close) and not getattr(raw_connection, "_acpc_replay_close", False):

            async def closed(*args: Any, **kwargs: Any) -> Any:
                if self._close_task is None:

                    async def shutdown() -> Any:
                        try:
                            result = await cast(Awaitable[Any], close(*args, **kwargs))
                        except Exception as error:
                            # The waiters are shielded, so a failure here reaches
                            # nobody unless a later caller retries. Say so once,
                            # here, rather than leaving a silent dead task.
                            _REPLAY_SUPPRESSION_LOG.warning(
                                "replay suppression: closing the connection failed (%s); "
                                "replay records are kept because callbacks may still run",
                                error,
                            )
                            raise
                        self.invalidate_connection()
                        return result

                    self._close_task = asyncio.create_task(shutdown())
                return await asyncio.shield(self._close_task)

            try:
                raw_connection.close = closed
                raw_connection._acpc_replay_close = True
            except (AttributeError, TypeError):
                return

    def invalidate_connection(self) -> None:
        """Invalidate every generation when its transport can no longer drain it."""
        for generation in tuple(self._active.values()):
            self._warn_open_generation(generation, "the connection dropped")
            generation.active = False
            generation.sink = None
        self._active.clear()
        self._generations.clear()

    def _observe(self, event: Any) -> None:
        direction = getattr(getattr(event, "direction", None), "value", None)
        message = getattr(event, "message", {})
        if direction == "outgoing":
            return
        if direction != "incoming":
            return
        if not isinstance(message, Mapping) or message.get("method") != "session/update":
            return
        params = message.get("params")
        if not isinstance(params, Mapping):
            return
        session_id = params.get("sessionId")
        if not isinstance(session_id, str):
            return
        generation = self._active.get(session_id)
        update = params.get("update")
        if generation is None or generation.sink is None or not isinstance(update, Mapping):
            return
        if not self._tagging_available:
            generation.received += 1
        generation.sink.consume_raw(update)

    @staticmethod
    def _warning_name(generation: _ReplayGeneration) -> str:
        return f"generation {generation.generation_id} for session {generation.session_id}"

    def _warn_unaccounted(self, generation: _ReplayGeneration) -> None:
        unaccounted = (
            len(generation.tagged_frame_ids - generation.seen_frame_ids)
            if generation.tagged_frame_ids
            else generation.received
        )
        if unaccounted:
            _REPLAY_SUPPRESSION_LOG.warning(
                "replay suppression: %d frames unaccounted for %s",
                unaccounted,
                self._warning_name(generation),
            )

    def _warn_open_generation(self, generation: _ReplayGeneration, reason: str) -> None:
        _REPLAY_SUPPRESSION_LOG.warning(
            "replay suppression: %s was still open when %s",
            self._warning_name(generation),
            reason,
        )
        self._warn_unaccounted(generation)

    def _tag_value(self, generation: _ReplayGeneration, frame_id: int) -> dict[str, Any]:
        return {
            "connection": self._connection_token,
            "session_id": generation.session_id,
            "generation": generation.generation_id,
            "frame": frame_id,
        }

    def _release_if_accounted(self, generation: _ReplayGeneration) -> None:
        if generation.active or generation.tagged_frame_ids != generation.seen_frame_ids:
            return
        self._generations.pop(generation.generation_id, None)

    def _evict_closed_generations(self) -> None:
        retained = sum(not generation.active for generation in self._generations.values())
        if retained <= MAX_RETAINED_CLOSED_REPLAY_GENERATIONS:
            return
        for generation_id, generation in tuple(self._generations.items()):
            if retained <= MAX_RETAINED_CLOSED_REPLAY_GENERATIONS:
                break
            if generation.active:
                continue
            self._generations.pop(generation_id, None)
            retained -= 1
            _REPLAY_SUPPRESSION_LOG.warning(
                "replay suppression: evicted %s with %d unaccounted frame(s)",
                self._warning_name(generation),
                len(generation.tagged_frame_ids - generation.seen_frame_ids),
            )


class AcpcClient:
    """Implement the ACP client callbacks used by a single session turn.

    The client keeps no terminal UI state. Agent content is appended to the
    transcript and to ``answer`` in the order received; permission prompting,
    when allowed by the caller, is supplied as a callback rather than read
    from stdin.
    """

    def __init__(
        self,
        transcript: Transcript,
        permission_level: PermissionLevel,
        *,
        modes: Mapping[str, ModeSpec] | None = None,
        end_turn: _EndTurn | None = None,
        cancellation_dispatched: asyncio.Event | None = None,
        permission_prompt: _PermissionPrompt | None = None,
        clock: _Clock | None = None,
    ) -> None:
        self.transcript = transcript
        self.permission_level = permission_level
        self.modes = {} if modes is None else modes
        self.end_turn = end_turn
        self.cancellation_dispatched = cancellation_dispatched
        self.permission_prompt = permission_prompt
        self._clock = time.monotonic if clock is None else clock
        self._pending: _PendingChunks | None = None
        self._answer_parts: list[str] = []
        self._answer_boundary_pending = False
        self._tool_calls: dict[str, _ToolCall] = {}
        self._tokens = 0
        self._cost: float | None = None
        self._denied: dict[str, int] = {}
        self._denial_details: dict[str, dict[str, Any]] = {}
        self._replay_sink: ReplaySink | None = None
        self._raw_connection: Any | None = None
        self._raw_replay_active = False
        self._advertised: dict[str, Any] = {
            "modes": [],
            "models": [],
            "commands": [],
        }

    @property
    def answer(self) -> str:
        """Return the agent-message content received so far."""
        return "".join(self._answer_parts)

    @property
    def tokens(self) -> int:
        """Return the cumulative token count reported by the adapter."""
        return self._tokens

    @property
    def cost(self) -> float | None:
        """Return the cumulative cost reported by the adapter."""
        return self._cost

    @property
    def denied(self) -> dict[str, int]:
        """Return this turn's permission-denial counts by category."""
        return dict(self._denied)

    @property
    def denial_details(self) -> dict[str, dict[str, Any]]:
        """Return remedies and targets for this turn's permission denials."""
        return {key: dict(detail) for key, detail in self._denial_details.items()}

    @property
    def advertised(self) -> dict[str, Any]:
        """Return the latest adapter modes, models, and commands advertisement."""
        return {
            "modes": [dict(item) for item in self._advertised["modes"]],
            "models": list(self._advertised["models"]),
            "commands": [dict(item) for item in self._advertised["commands"]],
        }

    def capture_advertised(self, session: NewSessionResponse) -> None:
        """Capture modes and models announced by ``session/new``."""
        modes = session.modes.available_modes if session.modes is not None else []
        self._advertised["modes"] = [
            mode.model_dump(mode="json", by_alias=True, exclude_none=True) for mode in modes
        ]
        self._advertised["models"] = self._models_from_options(session.config_options or [])

    @asynccontextmanager
    async def replaying(
        self, expected_session_id: str, connection: Any | None = None
    ) -> AsyncIterator[ReplaySink]:
        """Consume session-restore updates without changing turn-visible state."""
        previous = self._replay_sink
        previous_raw = self._raw_replay_active
        raw_connection = getattr(connection, "_conn", None)
        if raw_connection is None:
            raw_connection = connection or self._raw_connection
        tracker = ReplayTracker.for_connection(raw_connection)
        add_observer = getattr(raw_connection, "add_observer", None)
        uses_raw_frames = callable(add_observer)
        sink = ReplaySink(replay_available=uses_raw_frames)
        generation_id: int | None = None
        try:
            if tracker is not None:
                generation_id = tracker.open(expected_session_id, sink)
            self._replay_sink = sink
            self._raw_replay_active = uses_raw_frames
            yield sink
        finally:
            if tracker is not None and generation_id is not None:
                tracker.close(expected_session_id, generation_id)
            self._raw_replay_active = previous_raw
            self._replay_sink = previous

    def on_connect(self, conn: Any) -> None:
        """Keep the raw connection for ordered replay-frame observation."""
        self._raw_connection = getattr(conn, "_conn", None)

    def flush(self) -> None:
        """Write buffered agent prose to the transcript as one event.

        Called before any non-chunk event lands and at the end of a turn, so
        message text always precedes the event that interrupted it.
        """
        pending = self._pending
        if pending is None:
            return
        self._pending = None
        self.transcript.append(pending.event_type, text="".join(pending.parts))

    def _buffer_chunk(self, event_type: str, text: str) -> None:
        now = self._clock()
        pending = self._pending
        if pending is not None and (
            pending.event_type != event_type or now - pending.last_at >= _CHUNK_GAP_SECONDS
        ):
            self.flush()
            pending = None
        if pending is None:
            pending = _PendingChunks(
                event_type=event_type, parts=[], chars=0, started_at=now, last_at=now
            )
            self._pending = pending
        pending.parts.append(text)
        pending.chars += len(text)
        pending.last_at = now
        if now - pending.started_at >= _CHUNK_MAX_AGE_SECONDS or pending.chars >= _CHUNK_MAX_CHARS:
            self.flush()

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        """Record one ACP session update in the public transcript format."""
        generation_id = kwargs.pop(REPLAY_GENERATION_KEY, None)
        tracker = ReplayTracker.for_connection(self._raw_connection)
        if generation_id is not None:
            if tracker is not None:
                generation_status = tracker.consume_tag(generation_id)
                if generation_status in {"active", "closed"}:
                    return
            else:
                _REPLAY_SUPPRESSION_LOG.warning(
                    "replay suppression: frame arrived with generation tag for session %s "
                    "but its connection tracker is unavailable",
                    session_id,
                )
        if self._replay_sink is not None:
            if not self._raw_replay_active:
                self._replay_sink.consume(update)
            return
        update_type = getattr(update, "session_update", None)

        if update_type == "agent_message_chunk" and isinstance(update, AgentMessageChunk):
            text = getattr(update.content, "text", None)
            if isinstance(text, str):
                if (
                    self._answer_boundary_pending
                    and self.answer
                    and not self.answer.endswith("\n\n")
                ):
                    self._answer_parts.append("\n\n")
                self._answer_parts.append(text)
                self._answer_boundary_pending = False
                self._buffer_chunk("msg", text)
            return

        self._answer_boundary_pending = True

        if update_type == "agent_thought_chunk" and isinstance(update, AgentThoughtChunk):
            text = getattr(update.content, "text", None)
            if isinstance(text, str):
                self._buffer_chunk("thought", text)
            return

        # Every non-chunk update cuts the buffered prose, even one that writes
        # nothing itself (a tool start): the boundary is where the narrative
        # forked, not where the interrupting event was finally recorded.
        self.flush()

        if update_type == "tool_call" and isinstance(update, ToolCallStart):
            self._start_tool(update)
            return

        if update_type == "tool_call_update" and isinstance(update, ToolCallProgress):
            self._update_tool(update)
            return

        if update_type == "usage_update" and isinstance(update, UsageUpdate):
            self._record_usage(update)
            return

        if update_type == "available_commands_update" and isinstance(
            update, AvailableCommandsUpdate
        ):
            self._advertised["commands"] = [
                command.model_dump(mode="json", by_alias=True, exclude_none=True)
                for command in update.available_commands
            ]
            return

        if update_type == "config_option_update":
            self._advertised["models"] = self._models_from_options(
                getattr(update, "config_options", [])
            )
            return

        if update_type == "current_mode_update" and isinstance(update, CurrentModeUpdate):
            return

    async def request_permission(
        self,
        session_id: str,
        tool_call: Any,
        options: list[PermissionOption],
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        """Answer an ACP permission request and record the policy decision."""
        del session_id, kwargs
        self.flush()
        kind = getattr(tool_call, "kind", None) or "unknown"
        title = getattr(tool_call, "title", None) or ""
        category = classify_kind(kind)
        switch_reason: str | None = None
        denial_key: str | None = None
        denial_detail: dict[str, Any] | None = None
        if kind == "switch_mode":
            target = self._switch_mode_target(tool_call)
            decision, required = self._switch_mode_decision(target)
            auto = True
            if not decision:
                display_target = target if target is not None else "<unknown>"
                switch_reason = self._switch_mode_reason(display_target, required)
                denial_key = f"switch_mode:{display_target}"
                denial_detail = self._switch_mode_denial_detail(display_target, required)
        else:
            decision = should_allow(self.permission_level, category)
            auto = decision is not None
            if decision is None:
                decision = await self._ask_permission(kind, title)

        option_id = find_option(options, decision, self.permission_level)
        allowed = bool(decision and option_id is not None)
        reason: str | None = None
        if not allowed:
            reason = switch_reason or f"permission denied: {kind}"
            if switch_reason is None and decision and option_id is None:
                reason += " (no matching allow option)"
                denial_detail = self._missing_allow_detail(category, options)
        self._record_permission(
            kind,
            category,
            allowed,
            reason,
            auto=auto,
            denial_key=denial_key,
            denial_detail=denial_detail,
        )
        if not allowed:
            if switch_reason is not None and self.end_turn is not None:
                self.end_turn()
                await self._await_cancellation_dispatch()
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        assert option_id is not None
        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id=option_id)
        )

    async def _await_cancellation_dispatch(self) -> None:
        """Wait until the runner starts ACP cancellation, with a bounded fallback."""
        if self.cancellation_dispatched is None:
            return
        try:
            await asyncio.wait_for(
                self.cancellation_dispatched.wait(), timeout=_CANCELLATION_DISPATCH_TIMEOUT
            )
        except TimeoutError:
            return

    def _switch_mode_decision(self, target: str | None) -> tuple[bool, str]:
        """Resolve a runtime mode switch through the stored policy ceiling."""
        if target is None:
            return False, PermissionLevel.ALL.value
        try:
            _mode, spec = select_mode(self.modes, self.permission_level, explicit_mode=target)
        except ModeSelectionError:
            spec = self.modes.get(target)
            return False, spec.grants if spec is not None else PermissionLevel.ALL.value
        return True, spec.grants

    @staticmethod
    def _switch_mode_reason(target: str, required: str) -> str:
        """Describe why a runtime mode switch cannot be admitted."""
        return f"permission denied: switch_mode {target} (requires --permissions {required})"

    @staticmethod
    def _switch_mode_denial_detail(target: str, required: str) -> dict[str, Any]:
        """Keep the target and actionable remedy with the denial tally."""
        return {
            "category": "switch_mode",
            "target": target,
            "minimum_policy": required,
            "remedy": f"pass --permissions {required}",
        }

    @staticmethod
    def _switch_mode_target(tool_call: Any) -> str | None:
        """Read the requested mode from ACP's generic tool-call input."""
        raw_input = getattr(tool_call, "raw_input", None)
        if isinstance(raw_input, Mapping):
            for key in ("target", "mode"):
                target = raw_input.get(key)
                if isinstance(target, str) and target:
                    return target
        return None

    async def _authorize_client_method(self, kind: str, title: str) -> None:
        """Apply the policy to an ACP callback outside ``request_permission``."""
        self.flush()
        category = CLIENT_METHOD_CATEGORIES[kind]
        decision = should_allow(self.permission_level, category)
        auto = decision is not None
        if decision is None:
            decision = await self._ask_permission(kind, title)
        allowed = bool(decision)
        reason = None if allowed else f"permission denied: {kind}"
        self._record_permission(kind, category, allowed, reason, auto=auto)
        if not allowed:
            assert reason is not None
            raise RequestError(CLIENT_PERMISSION_ERROR_CODE, reason, {"category": category})

    def _record_permission(
        self,
        kind: str,
        category: str,
        allowed: bool,
        reason: str | None,
        *,
        auto: bool,
        denial_key: str | None = None,
        denial_detail: Mapping[str, Any] | None = None,
    ) -> None:
        """Append a permission decision and update the denial tally."""
        event: dict[str, Any] = {
            "type": "permission",
            "kind": kind,
            "decision": "allow" if allowed else "deny",
            "auto": auto,
        }
        self.transcript.append(event)
        if not allowed:
            key = denial_key or category
            self._denied[key] = self._denied.get(key, 0) + 1
            if denial_detail is None:
                minimum = self._minimum_policy_detail(category)
                denial_detail = {
                    "category": category,
                    "minimum_policy": minimum,
                    "remedy": f"pass --permissions {minimum}",
                }
            self._denial_details[key] = dict(denial_detail)
            self.transcript.append("error", message=reason or f"permission denied: {kind}")

    @staticmethod
    def _minimum_policy_detail(category: str) -> str:
        """Return the actionable policy for an ordinary denial category."""
        return minimum_policy(category)

    @staticmethod
    def _missing_allow_detail(category: str, options: list[PermissionOption]) -> dict[str, Any]:
        """Explain why a policy allow could not be represented by the options."""
        has_allow = any(option.kind in {"allow_once", "allow_always"} for option in options)
        minimum = PermissionLevel.ALL.value if has_allow else None
        remedy = "pass --permissions all" if has_allow else "agent offered no allow option"
        return {
            "category": category,
            "minimum_policy": minimum,
            "remedy": remedy,
        }

    def _authorize_existing_terminal(self, method: str, terminal_id: str) -> None:
        """Keep the terminal callbacks unsupported until terminal ownership exists."""
        self.flush()
        del method, terminal_id
        raise NotImplementedError("Terminal methods are not supported by acpc")

    async def read_text_file(
        self,
        path: str,
        session_id: str,
        line: int | None = None,
        limit: int | None = None,
        **kwargs: Any,
    ) -> ReadTextFileResponse:
        """Return text requested through ACP's filesystem callback."""
        await self._authorize_client_method("fs/read_text_file", f"Read {path}")
        del session_id, kwargs
        text = Path(path).read_text(encoding="utf-8")
        if line is not None:
            lines = text.splitlines(keepends=True)
            start = max(0, line - 1)
            text = "".join(lines[start : start + limit if limit is not None else None])
        elif limit is not None:
            text = text[:limit]
        return ReadTextFileResponse(content=text)

    async def write_text_file(
        self,
        path: str,
        content: str,
        session_id: str,
        **kwargs: Any,
    ) -> WriteTextFileResponse:
        """Write text requested through ACP's filesystem callback."""
        await self._authorize_client_method("fs/write_text_file", f"Write {path}")
        del session_id, kwargs
        file_path = Path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return WriteTextFileResponse()

    async def create_terminal(
        self,
        command: str,
        session_id: str,
        args: list[str] | None = None,
        env: list[Any] | None = None,
        cwd: str | None = None,
        output_byte_limit: int | None = None,
        **kwargs: Any,
    ) -> CreateTerminalResponse:
        await self._authorize_client_method("terminal/create", f"Create terminal: {command}")
        del command, session_id, args, env, cwd, output_byte_limit, kwargs
        raise NotImplementedError("Terminal methods are not supported by acpc")

    async def terminal_output(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> TerminalOutputResponse:
        self._authorize_existing_terminal("terminal/output", terminal_id)
        del session_id, terminal_id, kwargs
        raise NotImplementedError("Terminal methods are not supported by acpc")

    async def release_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> ReleaseTerminalResponse:
        self._authorize_existing_terminal("terminal/release", terminal_id)
        del session_id, terminal_id, kwargs
        raise NotImplementedError("Terminal methods are not supported by acpc")

    async def wait_for_terminal_exit(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> WaitForTerminalExitResponse:
        self._authorize_existing_terminal("terminal/wait_for_exit", terminal_id)
        del session_id, terminal_id, kwargs
        raise NotImplementedError("Terminal methods are not supported by acpc")

    async def kill_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> KillTerminalResponse:
        self._authorize_existing_terminal("terminal/kill", terminal_id)
        del session_id, terminal_id, kwargs
        raise NotImplementedError("Terminal methods are not supported by acpc")

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        del method, params
        return {}

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        del method, params

    def _start_tool(self, update: ToolCallStart) -> None:
        now = self._clock()
        current = self._tool_calls.get(update.tool_call_id)
        if current is None or current.status in {"completed", "failed"}:
            current = _ToolCall(
                title=update.title,
                kind=update.kind,
                raw_input=update.raw_input,
                started_at=now,
            )
            self._tool_calls[update.tool_call_id] = current
        else:
            self._merge_tool(current, update)
        current.status = update.status
        if current.status in {"completed", "failed"}:
            self._finish_tool(update.tool_call_id, current.status)

    def _update_tool(self, update: ToolCallProgress) -> None:
        current = self._tool_calls.get(update.tool_call_id)
        if current is None:
            current = _ToolCall(
                title=update.title,
                kind=update.kind,
                raw_input=update.raw_input,
                started_at=self._clock(),
            )
            self._tool_calls[update.tool_call_id] = current
        if current.finished:
            return
        self._merge_tool(current, update)
        if update.status is not None:
            current.status = update.status
        if current.status in {"completed", "failed"}:
            self._finish_tool(update.tool_call_id, current.status)

    def _merge_tool(self, current: _ToolCall, update: Any) -> None:
        title = getattr(update, "title", None)
        kind = getattr(update, "kind", None)
        raw_input = getattr(update, "raw_input", None)
        if title is not None:
            current.title = title
        if kind is not None:
            current.kind = kind
        if raw_input is not None:
            current.raw_input = raw_input

    def _finish_tool(self, tool_call_id: str, status: str) -> None:
        current = self._tool_calls[tool_call_id]
        if current.finished:
            return
        elapsed_ms = max(0.0, self._clock() - current.started_at) * 1000
        name, args_summary = self._tool_description(current)
        self.transcript.append(
            "tool",
            name=name,
            args_summary=args_summary,
            status=status,
            duration_ms=int(elapsed_ms),
        )
        current.status = status
        current.finished = True

    def _record_usage(self, update: UsageUpdate) -> None:
        self._tokens = max(self._tokens, update.used)
        if update.cost is not None:
            amount = update.cost.amount
            self._cost = amount if self._cost is None else max(self._cost, amount)
        self.transcript.append("usage", tokens=self._tokens, cost=self._cost)

    async def _ask_permission(self, kind: str, title: str) -> bool:
        if self.permission_prompt is None:
            return False
        answer = self.permission_prompt(kind, title)
        if inspect.isawaitable(answer):
            return bool(await answer)
        return bool(answer)

    @staticmethod
    def _tool_description(tool: _ToolCall) -> tuple[str, str]:
        title = (tool.title or "").strip()
        if title:
            name, separator, remainder = title.partition(" ")
            if separator:
                return name, remainder.strip()
            return name, AcpcClient._raw_input_summary(tool.raw_input)
        return tool.kind or "tool", AcpcClient._raw_input_summary(tool.raw_input)

    @staticmethod
    def _raw_input_summary(raw_input: Any) -> str:
        if raw_input is None:
            return ""
        if isinstance(raw_input, str):
            return raw_input
        try:
            return json.dumps(raw_input, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return str(raw_input)

    @staticmethod
    def _models_from_options(options: list[Any]) -> list[str]:
        models: list[str] = []
        for option in options:
            if not isinstance(option, (SessionConfigOptionSelect, SessionConfigOptionBoolean)):
                continue
            if option.id != "model" or not isinstance(option, SessionConfigOptionSelect):
                continue
            for choice in option.options:
                value = getattr(choice, "value", None)
                if isinstance(value, str):
                    models.append(value)
                else:
                    for nested in getattr(choice, "options", []):
                        nested_value = getattr(nested, "value", None)
                        if isinstance(nested_value, str):
                            models.append(nested_value)
        return models
