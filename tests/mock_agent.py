"""Mock ACP agent: a standalone script speaking real ACP over stdio.

The test harness's fake adapter. Harvested from the 0.3.0.dev1 mock and
extended with the MVP harness's scenario-by-keyword design and advertised
dataset, so the real client path — spawn, ipc, routing, permission handling —
is exercised without burning tokens.

Named scenarios (MVP design, case-insensitive substring of the prompt, first
match wins, checked after the exact-prefix triggers below):

- ``fail``  -> tool event + msg, then stop_reason=refusal
- ``perm``  -> six permission requests covering every policy tier:
  read / edit / execute / delete / switch_mode into the restricted mode ``yolo`` /
  switch_mode into the ordinary mode ``plan``
- ``huge``  -> >200 KB of markdown dense with multi-byte UTF-8 (Polish
  diacritics + emoji), with a 4-byte emoji starting at byte 1998 so a
  ``--max-output 2000`` cut straddles it
- ``slow``  -> ~32 steady events ~2s apart (~64s), msg events of varying
  length interleaved, cancellable

Exact-prefix triggers (donor design, for precise timing control in tests):

- ``echo:TEXT``      echo TEXT back as one agent_message_chunk
- ``error``          stop_reason=refusal immediately
- ``slow:N``         wait N seconds (cancellable), then answer
- ``chunkslow:N``    emit "started", wait N seconds (cancellable), "finished"
- ``chunkhold:PATH`` emit "holding", hold the turn until PATH exists
- ``large:N``        answer with N kilobytes of ASCII
- ``multi:TEXT``     history-aware echo, supports session/load
- ``burst:a|b|c``    several message chunks back to back
- ``tool:TITLE``     one completed read-kind tool call
- ``tool-edit:TITLE``one completed edit-kind tool call
- ``write-file:NAME``request edit permission, then write NAME in the cwd
- ``fs-write:NAME``write NAME through the ACP filesystem callback without asking permission
- ``env:NAME``       answer with the value of environment variable NAME
- ``settings``       answer with model/effort/mode state and call counts
- ``stderr:TEXT``    print TEXT to stderr, then echo it
- ``auth:``          fail the turn with a JSON-RPC authentication error
- ``crash-late:TEXT``stream TEXT, then fail the turn with a JSON-RPC error
- ``stderr-crash:TEXT`` print TEXT to stderr, then fail the turn
- ``auth-data:``     fail with an auth error marked in ``data``, not in the text
- ``meta:TOKENS:TICKS:TEXT`` -> prose plus per-turn PromptResponse ``_meta`` usage
- ``both:TOKENS:TICKS:TEXT`` -> streamed usage plus deliberately stale ``_meta``

Anything else runs the default scenario: three tool events, a progress msg, a
usage update, and a markdown answer quoting the prompt — history-aware, so a
continued session's answer references the previous turn.

Advertised dataset: modes ``default``/``acceptEdits``/``plan``/``yolo`` (the
restricted mode), models ``mock-opus-5``/``mock-sonnet-5``/``mock-haiku-4-5``,
efforts ``low``/``medium``/``high``/``xhigh`` — any other effort value is
rejected with a RequestError, giving the client a real "unsupported effort
level" to surface. A small command list is advertised after session/new.
"""

import asyncio
import contextlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from acp import (
    Agent,
    InitializeResponse,
    NewSessionResponse,
    PromptResponse,
    RequestError,
    run_agent,
    text_block,
    update_agent_message,
)
from acp.helpers import start_edit_tool_call, start_tool_call, update_tool_call
from acp.interfaces import Client
from acp.schema import (
    AcpMcpServer,
    AgentCapabilities,
    AgentMessageChunk,
    AgentThoughtChunk,
    AllowedOutcome,
    AudioContentBlock,
    AuthenticateResponse,
    AvailableCommand,
    AvailableCommandsUpdate,
    CloseSessionResponse,
    Cost,
    DeniedOutcome,
    EmbeddedResourceContentBlock,
    ForkSessionResponse,
    HttpMcpServer,
    ImageContentBlock,
    Implementation,
    ListSessionsResponse,
    LoadSessionResponse,
    McpServerStdio,
    PermissionOption,
    ResourceContentBlock,
    ResumeSessionResponse,
    SessionCapabilities,
    SessionConfigOptionBoolean,
    SessionConfigOptionSelect,
    SessionConfigSelectOption,
    SessionInfo,
    SessionListCapabilities,
    SessionMode,
    SessionModeState,
    SessionResumeCapabilities,
    SetSessionConfigOptionResponse,
    SetSessionModeResponse,
    SseMcpServer,
    TextContentBlock,
    ToolKind,
    UsageUpdate,
    UserMessageChunk,
)

from acpc import paths as acpc_paths

# Upper bound on "chunkhold" so a test that never writes its release file fails
# by assertion rather than by hanging until the suite timeout.
HOLD_LIMIT_SECONDS = 30.0
HOLD_POLL_SECONDS = 0.02
COMMAND_UPDATE_DELAY_SECONDS = 0.01

MODELS = ("mock-opus-5", "mock-sonnet-5", "mock-haiku-4-5")
DEFAULT_MODEL = "mock-sonnet-5"
EFFORTS = ("low", "medium", "high", "xhigh")
DEFAULT_EFFORT = "medium"
MODES = ("default", "acceptEdits", "plan", "yolo")
RESTRICTED_MODE = "yolo"
MODE_DESCRIPTIONS = {
    "default": "Mock default mode",
    "acceptEdits": "Mock editing mode",
    "plan": "Mock planning mode",
    "yolo": "Mock unrestricted mode",
}

_HUGE_TARGET_BYTES = 220 * 1024
_STRADDLE_OFFSET = 1998  # byte offset a 4-byte emoji starts at, see _huge_answer


# (tool title, ACP kind, target) for the perm scenario — one request per
# policy-relevant kind, including both switch_mode flavors.
PERM_REQUESTS: tuple[tuple[str, ToolKind, str], ...] = (
    ("Read", "read", "src/app.py"),
    ("Write", "edit", "src/app.py"),
    ("Bash", "execute", "rm -rf build/"),
    ("Delete", "delete", "old_report.md"),
    ("SwitchMode", "switch_mode", RESTRICTED_MODE),
    ("SwitchMode", "switch_mode", "plan"),
)

SLOW_EVENT_COUNT = 32
SLOW_EVENT_INTERVAL = 2.0


def _huge_answer(prompt: str) -> str:
    """>200KB of markdown dense with multi-byte UTF-8 characters throughout.

    A 4-byte emoji is placed to start at exactly ``_STRADDLE_OFFSET`` bytes in,
    so a ``--max-output 2000`` cap straddles it: bytes [1998, 2002) are the
    emoji and slicing at 2000 keeps only its first 2 bytes — this is what
    exercises UTF-8 boundary handling rather than a lucky ASCII cut.
    """
    intro = f'<!-- ACPC-HUGE-START -->\n# Raport\n\nReport for: "{prompt.strip()[:80]}"\n\n'
    intro_bytes = len(intro.encode("utf-8"))
    pad = "x" * max(0, _STRADDLE_OFFSET - intro_bytes)
    head = intro + pad + "😀\n\n"

    paragraph = (
        "Zażółć gęślą jaźń 🎉 — dolor sit amet, consectetur adipiscing elit, sed do "
        "eiusmod tempor incididunt ut labore. Święty miś jeżozwierz łąka źdźbło ó, "
        "ćma pręży się w słońcu 🌻 nad rzeką.\n\n"
    )
    parts = [head]
    size = len(head.encode("utf-8"))
    section = 0
    while size < _HUGE_TARGET_BYTES:
        section += 1
        chunk = f"## Sekcja {section} 📎\n\n{paragraph}"
        parts.append(chunk)
        size += len(chunk.encode("utf-8"))
    parts.append("\n<!-- ACPC-HUGE-END -->\n")
    return "".join(parts)


def _slow_messages() -> dict[int, str]:
    """Msg events of varying length interleaved among the slow scenario's tools.

    One entry is well past 200 chars so the default log view's snippet
    truncation is visible while ``--prose`` shows it in full.
    """
    long_text = (
        "Digging into this properly: I re-read the relevant modules, cross-checked "
        "the failing assertions against the fixture data, and I'm now fairly sure "
        "the mismatch traces back to how the fixture's timestamps are generated "
        "rather than anything in the code under test itself, so the next few steps "
        "will confirm that before touching any source."
    )
    return {
        5: "Getting oriented; the task looks like a multi-step refactor.",
        12: (
            "Halfway-ish: read through the main entry point and the tests that "
            "exercise it, nothing surprising so far."
        ),
        19: long_text,
        26: "Wrapping up the remaining steps now.",
    }


def _leading_delay(prompt: str) -> int:
    """Read the delay from a `slow:<n>` / `chunkslow:<n>` prompt.

    Callers append descriptive text after the number so that concurrently
    dispatched probes are distinguishable in `status` (SPEC: "five
    backgrounded codex runs must not look identical"), so only the first
    whitespace-delimited token after the colon is the delay.
    """
    return int(prompt.split(":", 1)[1].split()[0])


def select_scenario(prompt: str) -> str | None:
    """MVP keyword selection: case-insensitive substring, first match wins."""
    lower = prompt.lower()
    for name in ("fail", "perm", "huge", "slow"):
        if name in lower:
            return name
    return None


class MockAgent(Agent):
    _conn: Client

    def __init__(self) -> None:
        self._sessions: dict[str, list[str]] = {}
        self._session_history: dict[str, list[str]] = {}
        self._session_cwds: dict[str, Path] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._models: dict[str, str] = {}
        self._efforts: dict[str, str] = {}
        self._modes: dict[str, str] = {}
        self._model_calls: dict[str, int] = {}
        self._mode_calls: dict[str, int] = {}
        self._effort_calls: dict[str, int] = {}
        self._initialized = False
        self._late_calls: dict[str, tuple[str, Path, str]] = {}
        self._last_restored_session: str | None = None
        self._restore_settled = asyncio.Event()

    def on_connect(self, conn: Client) -> None:
        self._conn = conn

    @staticmethod
    def _store_path() -> Path | None:
        raw = os.environ.get("ACPC_MOCK_SESSION_STORE")
        if raw:
            return Path(raw)
        home = os.environ.get("ACPC_HOME")
        return Path(home) / "mock-sessions.json" if home else None

    @classmethod
    def _read_store(cls) -> dict[str, dict[str, Any]]:
        path = cls._store_path()
        if path is None or not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise RuntimeError(f"mock session store is unreadable: {path} ({error})") from error
        return raw if isinstance(raw, dict) else {}

    @classmethod
    def _write_store(cls, sessions: dict[str, dict[str, Any]]) -> None:
        path = cls._store_path()
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        acpc_paths.atomic_write(path, json.dumps(sessions))

    @classmethod
    @contextlib.contextmanager
    def _store_lock(cls):
        """Serialize the mock's durable store across adapter processes."""
        path = cls._store_path()
        if path is None:
            yield
            return
        lock_path = path.with_name(f"{path.name}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if sys.platform == "win32":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            if sys.platform == "win32":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                with contextlib.suppress(OSError):
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _persist_session(self, session_id: str) -> None:
        with self._store_lock():
            store = self._read_store()
            store[session_id] = {
                "cwd": str(self._session_cwds[session_id]),
                "history": list(self._sessions.get(session_id, [])),
            }
            self._write_store(store)

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: Any = None,
        client_info: Any = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        raw_delay = os.environ.get("ACPC_MOCK_INITIALIZE_DELAY")
        if raw_delay:
            await asyncio.sleep(float(raw_delay))
        self._initialized = True
        session_capabilities: SessionCapabilities | None = None
        if os.environ.get("ACPC_MOCK_ADVERTISE_LIST", "1") == "1":
            session_capabilities = SessionCapabilities(list=SessionListCapabilities())
        if os.environ.get("ACPC_MOCK_ADVERTISE_RESUME") == "1":
            session_capabilities = session_capabilities or SessionCapabilities()
            session_capabilities.resume = SessionResumeCapabilities()
        capabilities = AgentCapabilities(
            load_session=True, session_capabilities=session_capabilities
        )
        return InitializeResponse(
            protocol_version=protocol_version,
            agent_capabilities=capabilities,
            agent_info=Implementation(name="mock-agent", title="Mock Agent", version="0.1.0"),
        )

    def _config_options(
        self, session_id: str
    ) -> list[SessionConfigOptionSelect | SessionConfigOptionBoolean]:
        model_option = SessionConfigOptionSelect(
            type="select",
            id="model",
            name="Model",
            category="model",
            current_value=self._models.get(session_id, DEFAULT_MODEL),
            options=[
                SessionConfigSelectOption(name=model_id, value=model_id) for model_id in MODELS
            ],
        )
        effort_option = SessionConfigOptionSelect(
            type="select",
            id="reasoning_effort",
            name="Reasoning effort",
            category="thought_level",
            current_value=self._efforts.get(session_id, DEFAULT_EFFORT),
            options=[
                SessionConfigSelectOption(name=effort.capitalize(), value=effort)
                for effort in EFFORTS
            ],
        )
        return [model_option, effort_option]

    def _mode_state(self, session_id: str) -> SessionModeState:
        return SessionModeState(
            current_mode_id=self._modes.get(session_id, "default"),
            available_modes=[
                SessionMode(id=mode, name=mode, description=MODE_DESCRIPTIONS[mode])
                for mode in MODES
            ],
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
        self._session_cwds[session_id] = Path(cwd)
        self._cancel_events[session_id] = asyncio.Event()
        self._persist_session(session_id)
        late_ready = os.environ.get("ACPC_MOCK_LATE_RESTORE_FRAME_READY")
        if late_ready and self._last_restored_session is not None:
            await self._restore_settled.wait()
            await self._send_usage(self._last_restored_session, used=9999)
            Path(late_ready).touch()
        if not os.environ.get("MOCK_PROBE_BEHAVIOR"):
            asyncio.get_running_loop().create_task(self._send_commands_update(session_id))
        return NewSessionResponse(
            session_id=session_id,
            modes=self._mode_state(session_id),
            config_options=self._config_options(session_id),
        )

    async def _send_commands_update(self, session_id: str) -> None:
        """Advertise commands just after session/new returns to the client."""
        await asyncio.sleep(COMMAND_UPDATE_DELAY_SECONDS)
        commands = [
            AvailableCommand(name="init", description="Create an AGENTS.md file for this repo"),
            AvailableCommand(name="review", description="Review current changes and find issues"),
            AvailableCommand(
                name="plan",
                description=(
                    "Make a plan before touching code. Reads the repo, drafts steps, and "
                    "waits for approval — the long second sentence exercises first-sentence "
                    "truncation in the commands view."
                ),
            ),
        ]
        extra_command = os.environ.get("ACPC_MOCK_COMMAND_NAME")
        if extra_command:
            commands.append(AvailableCommand(name=extra_command, description="Extra command"))
        update = AvailableCommandsUpdate(
            session_update="available_commands_update",
            available_commands=commands,
        )
        try:
            await self._conn.session_update(session_id=session_id, update=update)
        except (ConnectionError, OSError, RuntimeError):
            pass

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
        self._record_session_method("load")
        self._record_event(f"restore-start:{session_id}")
        self._last_restored_session = session_id
        self._restore_settled.clear()
        await self._resume_delay()
        # Vendor-faithful to codex-acp#343: session/load resets the session's
        # model and effort to the adapter's defaults. acpc survives only because
        # it re-applies both *after* restoring, so the reset has to be modelled
        # here or that ordering is untested.
        self._models.pop(session_id, None)
        self._efforts.pop(session_id, None)
        if session_id == "load-fail":
            raise RuntimeError("load failed")
        if session_id not in self._sessions:
            stored = self._read_store().get(session_id, {})
            stored_history = stored.get("history", [])
            history = self._session_history.get(session_id, stored_history)
            if not isinstance(history, list) or any(not isinstance(item, str) for item in history):
                history = []
            self._sessions[session_id] = list(history) if history is not None else []
            self._cancel_events[session_id] = asyncio.Event()
            if history and os.environ.get("ACPC_MOCK_REPLAY_USER_MESSAGES", "1") == "1":
                await self._send_replayed_user_messages(session_id, history)
            elif session_id.startswith("load-"):
                self._sessions[session_id] = ["history"]
                await self._send_text(session_id, "history")
        elif not session_id.startswith("load-"):
            self._sessions[session_id].append("reloaded")
        self._session_cwds[session_id] = Path(cwd)
        self._persist_session(session_id)
        if session_id.startswith("load-"):
            await self._send_text(session_id, "history")
            if session_id == "load-slow":
                await asyncio.sleep(0.2)
        self._restore_settled.set()
        self._record_event(f"restore-end:{session_id}")
        return LoadSessionResponse()

    async def set_session_mode(
        self, session_id: str, mode_id: str, **kwargs: Any
    ) -> SetSessionModeResponse | None:
        block_path = os.environ.get("ACPC_MOCK_BLOCK_BEFORE_PROMPT")
        if block_path:
            ready_path = os.environ.get("ACPC_MOCK_BLOCK_BEFORE_PROMPT_READY")
            if ready_path:
                Path(ready_path).touch()
            deadline = time.monotonic() + HOLD_LIMIT_SECONDS
            while not Path(block_path).exists():
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"timed out waiting for {block_path}")
                await asyncio.sleep(HOLD_POLL_SECONDS)
        if os.environ.get("ACPC_MOCK_DISCONNECT_BEFORE_PROMPT") == "1":
            os._exit(17)
        if mode_id not in MODES:
            raise RequestError(400, f"unknown mode: {mode_id}")
        self._modes[session_id] = mode_id
        self._mode_calls[session_id] = self._mode_calls.get(session_id, 0) + 1
        return SetSessionModeResponse()

    async def set_config_option(
        self, config_id: str, session_id: str, value: str | bool, **kwargs: Any
    ) -> SetSessionConfigOptionResponse | None:
        if config_id not in {"model", "reasoning_effort"}:
            # Vendor-faithful: claude-agent-acp 0.64.0 answers an unknown
            # config id with exactly this JSON-RPC shape.
            raise RequestError(
                -32603, "Internal error", {"details": f"Unknown config option: {config_id}"}
            )
        if config_id == "model" and isinstance(value, str):
            if value not in MODELS:
                raise RequestError(400, f"unsupported model: {value}")
            self._models[session_id] = value
            self._model_calls[session_id] = self._model_calls.get(session_id, 0) + 1
        if config_id == "reasoning_effort" and isinstance(value, str):
            if value not in EFFORTS:
                supported = ", ".join(EFFORTS)
                raise RequestError(400, f"unsupported effort: {value} (supported: {supported})")
            self._efforts[session_id] = value
            self._effort_calls[session_id] = self._effort_calls.get(session_id, 0) + 1
        return SetSessionConfigOptionResponse(config_options=[])

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

        self._record_event(f"prompt:{session_id}:{prompt_text}")
        history = list(self._sessions.get(session_id, []))
        self._sessions.setdefault(session_id, []).append(prompt_text)
        self._session_cwds.setdefault(session_id, Path.cwd())
        self._persist_session(session_id)
        cancel_event = self._cancel_events.setdefault(session_id, asyncio.Event())
        cancel_event.clear()

        prefix_response = await self._prefix_trigger(session_id, prompt_text, cancel_event)
        if prefix_response is not None:
            return prefix_response

        scenario = select_scenario(prompt_text)
        if scenario == "fail":
            return await self._run_fail(session_id, prompt_text)
        if scenario == "perm":
            return await self._run_perm(session_id, prompt_text, cancel_event)
        if scenario == "huge":
            return await self._run_huge(session_id, prompt_text)
        if scenario == "slow":
            return await self._run_slow(session_id, prompt_text, history, cancel_event)
        return await self._run_default(session_id, prompt_text, history)

    async def _prefix_trigger(
        self, session_id: str, prompt_text: str, cancel_event: asyncio.Event
    ) -> PromptResponse | None:
        """Handle the donor's exact-prefix triggers; None means no match."""
        if prompt_text.startswith(("meta:", "both:")):
            source, token_text, ticks_text, answer = prompt_text.split(":", 3)
            tokens = int(token_text)
            ticks = int(ticks_text)
            await self._send_text(session_id, answer)
            if source == "both":
                await self._send_usage(
                    session_id,
                    used=tokens,
                    cost=ticks / 10_000_000_000,
                )
                meta_tokens = tokens + 100
                meta_ticks = ticks * 2
            else:
                meta_tokens = tokens
                meta_ticks = ticks
            return PromptResponse.model_validate(
                {
                    "stopReason": "end_turn",
                    "_meta": {
                        "numTurns": 1,
                        "totalTokens": meta_tokens,
                        "usage": {"costUsdTicks": meta_ticks},
                    },
                },
            )

        if prompt_text.startswith("echo:"):
            await self._send_text(session_id, prompt_text.split(":", 1)[1])
            return PromptResponse(stop_reason="end_turn")

        if prompt_text.startswith("error"):
            return PromptResponse(stop_reason="refusal")

        if prompt_text.startswith("slow:"):
            delay = _leading_delay(prompt_text)
            try:
                await asyncio.wait_for(cancel_event.wait(), timeout=delay)
                return PromptResponse(stop_reason="cancelled")
            except TimeoutError:
                pass
            await self._send_text(session_id, f"waited {delay}s")
            return PromptResponse(stop_reason="end_turn")

        if prompt_text.startswith("chunkslow:"):
            delay = _leading_delay(prompt_text)
            await self._send_text(session_id, "started")
            try:
                await asyncio.wait_for(cancel_event.wait(), timeout=delay)
                return PromptResponse(stop_reason="cancelled")
            except TimeoutError:
                pass
            await self._send_text(session_id, "finished")
            return PromptResponse(stop_reason="end_turn")

        if prompt_text.startswith("chunkhold:"):
            release_path = Path(prompt_text.split(":", 1)[1])
            await self._send_text(session_id, "holding")
            deadline = time.monotonic() + HOLD_LIMIT_SECONDS
            while not release_path.exists():
                if cancel_event.is_set():
                    return PromptResponse(stop_reason="cancelled")
                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(HOLD_POLL_SECONDS)
            await self._send_text(session_id, "finished")
            return PromptResponse(stop_reason="end_turn")

        if prompt_text.startswith("large:"):
            kilobytes = int(prompt_text.split(":")[1])
            await self._send_text(session_id, "X" * (kilobytes * 1024))
            return PromptResponse(stop_reason="end_turn")

        if prompt_text.startswith("multi:"):
            text = prompt_text.split(":", 1)[1]
            turn_count = len(self._sessions.get(session_id, []))
            await self._send_text(session_id, f"turn {turn_count}: {text}")
            return PromptResponse(stop_reason="end_turn")

        if prompt_text.startswith("burst:"):
            for chunk in prompt_text.split(":", 1)[1].split("|"):
                await self._send_text(session_id, chunk)
                await asyncio.sleep(0)
            return PromptResponse(stop_reason="end_turn")

        if prompt_text.startswith("tool:"):
            title = prompt_text.split(":", 1)[1]
            await self._completed_tool_call(session_id, title, "read")
            await self._send_text(session_id, f"tool {title} done")
            return PromptResponse(stop_reason="end_turn")

        if prompt_text.startswith("tool-edit:"):
            title = prompt_text.split(":", 1)[1]
            await self._completed_tool_call(session_id, title, "edit")
            await self._send_text(session_id, f"edit {title} done")
            return PromptResponse(stop_reason="end_turn")

        if prompt_text.startswith("write-file:"):
            name = prompt_text.split(":", 1)[1]
            allowed = await self._write_file_with_permission(session_id, name)
            result = "done" if allowed else "denied"
            await self._send_text(session_id, f"write {name} {result}")
            return PromptResponse(stop_reason="end_turn")

        if prompt_text.startswith("fs-write:"):
            name = prompt_text.split(":", 1)[1]
            path = self._session_cwds[session_id] / name
            content = f"written by mock agent: {name}\n"
            try:
                await self._conn.write_text_file(
                    session_id=session_id,
                    path=str(path),
                    content=content,
                )
            except RequestError as error:
                result = f"error: {error}"
            else:
                result = "done"
            await self._send_text(session_id, f"fs write {name} {result}")
            return PromptResponse(stop_reason="end_turn")

        if prompt_text.startswith("env:"):
            name = prompt_text.split(":", 1)[1]
            await self._send_text(session_id, os.environ.get(name, "-"))
            return PromptResponse(stop_reason="end_turn")

        if prompt_text == "settings":
            settings = (
                f"{self._models.get(session_id, '-')}/"
                f"{self._efforts.get(session_id, '-')}/"
                f"{self._modes.get(session_id, '-')}/"
                f"{self._model_calls.get(session_id, 0)}/"
                f"{self._mode_calls.get(session_id, 0)}/"
                f"{self._effort_calls.get(session_id, 0)}"
            )
            await self._send_text(session_id, settings)
            return PromptResponse(stop_reason="end_turn")

        if prompt_text.startswith("stderr:"):
            print(prompt_text.split(":", 1)[1], file=sys.stderr, flush=True)
            await self._send_text(session_id, prompt_text.split(":", 1)[1])
            return PromptResponse(stop_reason="end_turn")

        if prompt_text.startswith("auth:"):
            raise RequestError(-32000, "Authentication required")

        if prompt_text.startswith("auth-data:"):
            # The refusal is machine-readable in `data` and says nothing
            # recognizable in the message, which is the shape acpc must not
            # depend on text matching to classify.
            raise RequestError(-32000, "Internal error", {"code": "auth_required"})

        if prompt_text.startswith("stderr-crash:"):
            print(prompt_text.split(":", 1)[1], file=sys.stderr, flush=True)
            # Let the host drain the pipe before the failure travels over stdout:
            # the two are separate channels with no ordering between them.
            await asyncio.sleep(0.3)
            raise RequestError(-32603, "Internal error", {"details": "died after complaining"})

        if prompt_text.startswith("crash-late:"):
            await self._send_text(session_id, prompt_text.split(":", 1)[1])
            raise RequestError(-32603, "Internal error", {"details": "upstream connection reset"})

        return None

    def _history_reference(self, history: list[str]) -> str:
        if not history:
            return ""
        previous = history[-1].strip().replace("\n", " ")[:80]
        return f'\n\n> Continuing from turn {len(history)}: you previously asked "{previous}".\n'

    async def _run_default(
        self, session_id: str, prompt_text: str, history: list[str]
    ) -> PromptResponse:
        for title, kind, args in (
            ("Read", "read", "README.md"),
            ("Grep", "search", f'pattern matching "{prompt_text.strip()[:24]}"'),
            ("Bash", "execute", "pytest -q"),
        ):
            await self._completed_tool_call(session_id, f"{title} {args}", kind)  # type: ignore[arg-type]

        await self._send_text(session_id, f'Working through: "{prompt_text.strip()[:160]}"')
        await self._send_usage(session_id, used=1200)

        answer = (
            "## Answer\n\n"
            f'You asked: "{prompt_text.strip()}"\n\n'
            f"Here is a concise response addressing that directly (turn "
            f"{len(history) + 1}), produced with model "
            f"{self._models.get(session_id, DEFAULT_MODEL)} at "
            f"{self._efforts.get(session_id, DEFAULT_EFFORT)} effort.\n"
        ) + self._history_reference(history)
        await self._send_text(session_id, answer)
        return PromptResponse(stop_reason="end_turn")

    async def _run_fail(self, session_id: str, prompt_text: str) -> PromptResponse:
        await self._completed_tool_call(session_id, "Read README.md", "read")
        await self._send_text(session_id, f'Attempting: "{prompt_text.strip()[:120]}"')
        await self._send_text(
            session_id,
            "## Unable to complete\n\n"
            f'I looked into "{prompt_text.strip()[:120]}" but have to refuse: this falls '
            "outside what the mock adapter will attempt.\n",
        )
        return PromptResponse(stop_reason="refusal")

    async def _run_perm(
        self, session_id: str, prompt_text: str, cancel_event: asyncio.Event
    ) -> PromptResponse:
        await self._send_text(session_id, f'Exploring what "{prompt_text.strip()[:100]}" needs.')

        decisions: dict[str, str] = {}
        for title, kind, target in PERM_REQUESTS:
            tool_id = uuid4().hex[:8]
            tool_call = start_tool_call(
                tool_call_id=tool_id,
                title=f"{title} {target}",
                kind=kind,
                raw_input={"target": target},
            )
            await self._conn.session_update(session_id=session_id, update=tool_call)
            permission = await self._conn.request_permission(
                session_id=session_id,
                tool_call=update_tool_call(
                    tool_call_id=tool_id,
                    title=tool_call.title,
                    kind=kind,
                    status="in_progress",
                    raw_input={"target": target},
                ),
                options=[
                    PermissionOption(option_id="allow", name="Allow", kind="allow_once"),
                    PermissionOption(option_id="always", name="Always", kind="allow_always"),
                    PermissionOption(option_id="deny", name="Deny", kind="reject_once"),
                ],
            )
            allowed = isinstance(permission.outcome, AllowedOutcome)
            decisions[f"{kind}:{target}"] = "allowed" if allowed else "denied"
            status = "completed" if allowed else "failed"
            await self._conn.session_update(
                session_id=session_id,
                update=update_tool_call(tool_call_id=tool_id, status=status),
            )
            if cancel_event.is_set():
                await self._send_text(session_id, self._perm_summary(decisions))
                return PromptResponse(stop_reason="cancelled")

        await self._send_text(session_id, self._perm_summary(decisions))
        return PromptResponse(stop_reason="end_turn")

    @staticmethod
    def _perm_summary(decisions: dict[str, str]) -> str:
        allowed_keys = [key for key, value in decisions.items() if value == "allowed"]
        denied_keys = [key for key, value in decisions.items() if value == "denied"]
        return (
            "## Permission check summary\n\n"
            f"Requested: {', '.join(decisions)}.\n\n"
            f"Allowed: {', '.join(allowed_keys) or 'none'}.\n\n"
            f"Denied: {', '.join(denied_keys) or 'none'}.\n"
        )

    async def _run_huge(self, session_id: str, prompt_text: str) -> PromptResponse:
        await self._completed_tool_call(session_id, "Read big_file.md", "read")
        await self._send_text(
            session_id, f'Generating a large report for: "{prompt_text.strip()[:100]}"'
        )
        await self._send_text(session_id, _huge_answer(prompt_text))
        return PromptResponse(stop_reason="end_turn")

    async def _run_slow(
        self,
        session_id: str,
        prompt_text: str,
        history: list[str],
        cancel_event: asyncio.Event,
    ) -> PromptResponse:
        kinds_cycle: tuple[tuple[str, ToolKind, str], ...] = (
            ("Read", "read", "src/app.py"),
            ("Grep", "search", "TODO"),
            ("Bash", "execute", "make test"),
        )
        messages = _slow_messages()
        for index in range(SLOW_EVENT_COUNT):
            try:
                await asyncio.wait_for(cancel_event.wait(), timeout=SLOW_EVENT_INTERVAL)
                return PromptResponse(stop_reason="cancelled")
            except TimeoutError:
                pass
            if index in messages:
                await self._send_text(session_id, messages[index])
            else:
                title, kind, args = kinds_cycle[index % len(kinds_cycle)]
                await self._completed_tool_call(
                    session_id, f"{title} {args} (step {index + 1})", kind
                )

        await self._send_text(
            session_id,
            "## Long-running task complete\n\n"
            f'Finished the slow scenario for: "{prompt_text.strip()[:120]}"\n'
            + self._history_reference(history),
        )
        return PromptResponse(stop_reason="end_turn")

    async def _completed_tool_call(self, session_id: str, title: str, kind: ToolKind) -> None:
        tool_id = uuid4().hex[:8]
        start = start_tool_call(tool_call_id=tool_id, title=title, kind=kind)
        await self._conn.session_update(session_id=session_id, update=start)
        done = update_tool_call(tool_call_id=tool_id, status="completed")
        await self._conn.session_update(session_id=session_id, update=done)

    async def _send_text(self, session_id: str, text: str) -> None:
        chunk = update_agent_message(text_block(text))
        await self._conn.session_update(session_id=session_id, update=chunk)

    async def _send_usage(self, session_id: str, used: int, cost: float | None = None) -> None:
        update = UsageUpdate(
            session_update="usage_update",
            used=used,
            size=200_000,
            cost=Cost(amount=cost, currency="USD") if cost is not None else None,
        )
        try:
            await self._conn.session_update(session_id=session_id, update=update)
        except (ConnectionError, OSError, RuntimeError):
            pass

    async def _write_file_with_permission(self, session_id: str, name: str) -> bool:
        path = self._session_cwds[session_id] / name
        content = f"written by mock agent: {name}\n"
        tool_id = uuid4().hex[:8]
        tool_call = start_edit_tool_call(
            tool_call_id=tool_id,
            title=f"write {name}",
            path=str(path),
            content=content,
        )
        await self._conn.session_update(session_id=session_id, update=tool_call)
        permission = await self._conn.request_permission(
            session_id=session_id,
            tool_call=update_tool_call(
                tool_call_id=tool_id,
                title=tool_call.title,
                kind="edit",
                status="in_progress",
                locations=tool_call.locations,
                raw_input=tool_call.raw_input,
            ),
            options=[
                PermissionOption(option_id="allow", name="Allow", kind="allow_once"),
                PermissionOption(option_id="deny", name="Deny", kind="reject_once"),
            ],
        )
        if isinstance(permission.outcome, DeniedOutcome):
            done = update_tool_call(
                tool_call_id=tool_id,
                title=tool_call.title,
                status="failed",
                raw_output={"error": "permission denied"},
            )
            await self._conn.session_update(session_id=session_id, update=done)
            return False
        if not isinstance(permission.outcome, AllowedOutcome):
            raise TypeError(f"unexpected permission outcome: {permission.outcome!r}")

        await self._conn.write_text_file(
            session_id=session_id,
            path=str(path),
            content=content,
        )
        done = update_tool_call(
            tool_call_id=tool_id,
            title=tool_call.title,
            status="completed",
            raw_output={"path": str(path)},
        )
        await self._conn.session_update(session_id=session_id, update=done)
        return True

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        self._cancel_events.setdefault(session_id, asyncio.Event()).set()

    async def close_session(self, session_id: str, **kwargs: Any) -> CloseSessionResponse:
        if session_id not in self._sessions:
            raise ValueError(f"unknown session id: {session_id}")
        self._session_history[session_id] = list(self._sessions[session_id])
        self._sessions.pop(session_id)
        self._session_cwds.pop(session_id, None)
        self._cancel_events.pop(session_id, None)
        self._models.pop(session_id, None)
        self._modes.pop(session_id, None)
        return CloseSessionResponse()

    async def list_sessions(
        self, cwd: str | None = None, cursor: str | None = None, **kwargs: Any
    ) -> ListSessionsResponse:
        del kwargs
        stored = self._read_store()
        for session_id, session_cwd in self._session_cwds.items():
            stored[session_id] = {
                "cwd": str(session_cwd),
                "history": list(self._sessions.get(session_id, [])),
            }
        session_ids = sorted(
            session_id
            for session_id, record in stored.items()
            if isinstance(record, dict)
            and isinstance(record.get("cwd"), str)
            and (cwd is None or record["cwd"] == cwd)
        )
        start = int(cursor) if cursor is not None else 0
        page = session_ids[start : start + 1]
        next_cursor = str(start + 1) if start + 1 < len(session_ids) else None
        await asyncio.to_thread(self._record_session_list_cursor, cursor)
        return ListSessionsResponse(
            sessions=[
                SessionInfo(session_id=session_id, cwd=stored[session_id]["cwd"])
                for session_id in page
            ],
            next_cursor=next_cursor,
        )

    async def fork_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[HttpMcpServer | SseMcpServer | AcpMcpServer | McpServerStdio]
        | None = None,
        **kwargs: Any,
    ) -> ForkSessionResponse:
        new_session_id = uuid4().hex[:12]
        self._session_cwds[new_session_id] = Path(cwd)
        return ForkSessionResponse(session_id=new_session_id)

    async def resume_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[HttpMcpServer | SseMcpServer | AcpMcpServer | McpServerStdio]
        | None = None,
        **kwargs: Any,
    ) -> ResumeSessionResponse:
        self._record_session_method("resume")
        self._record_event(f"restore-start:{session_id}")
        self._last_restored_session = session_id
        self._restore_settled.clear()
        await self._resume_delay()
        if session_id not in self._sessions:
            stored = self._read_store().get(session_id, {})
            history = stored.get("history", [])
            self._sessions[session_id] = list(history) if isinstance(history, list) else []
            self._cancel_events[session_id] = asyncio.Event()
        self._session_cwds[session_id] = Path(cwd)
        self._persist_session(session_id)
        self._restore_settled.set()
        self._record_event(f"restore-end:{session_id}")
        return ResumeSessionResponse()

    async def _send_replayed_user_messages(self, session_id: str, messages: list[str]) -> None:
        if os.environ.get("ACPC_MOCK_REPLAY_EXTRA_EVENTS") == "1":
            await self._conn.session_update(
                session_id=session_id,
                update=AgentMessageChunk(
                    content=text_block("replayed agent text"),
                    message_id="replay-agent",
                    session_update="agent_message_chunk",
                ),
            )
            await self._conn.session_update(
                session_id=session_id,
                update=AgentThoughtChunk(
                    content=text_block("replayed thought"),
                    message_id="replay-thought",
                    session_update="agent_thought_chunk",
                ),
            )
            tool = start_tool_call(
                tool_call_id="replay-tool",
                title="replayed tool",
                kind="read",
                raw_input={"replayed": True},
            )
            await self._conn.session_update(session_id=session_id, update=tool)
            await self._conn.session_update(
                session_id=session_id,
                update=update_tool_call(tool_call_id="replay-tool", status="completed"),
            )
        for index, message in enumerate(messages):
            update = UserMessageChunk(
                session_update="user_message_chunk",
                content=text_block(message),
                message_id=f"replay-{index}",
            )
            await self._conn.session_update(session_id=session_id, update=update)

    async def _resume_delay(self) -> None:
        block_path = os.environ.get("ACPC_MOCK_BLOCK_DURING_RESTORE")
        if block_path:
            ready_path = os.environ.get("ACPC_MOCK_BLOCK_DURING_RESTORE_READY")
            if ready_path:
                Path(ready_path).touch()
            deadline = time.monotonic() + HOLD_LIMIT_SECONDS
            while not Path(block_path).exists():
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"timed out waiting for {block_path}")
                await asyncio.sleep(HOLD_POLL_SECONDS)
        raw = os.environ.get("ACPC_MOCK_RESUME_DELAY")
        if raw:
            await asyncio.sleep(float(raw))

    @staticmethod
    def _record_session_method(method: str) -> None:
        path = os.environ.get("ACPC_MOCK_SESSION_METHOD_FILE")
        if path:
            # Append, so a test that expects exactly one restore can see a
            # second one rather than have it overwrite the first.
            with Path(path).open("a", encoding="utf-8") as handle:
                handle.write(f"{method}\n")

    @staticmethod
    def _record_event(event: str) -> None:
        path = os.environ.get("ACPC_MOCK_EVENT_FILE")
        if path:
            with Path(path).open("a", encoding="utf-8") as handle:
                handle.write(f"{event}\n")

    @staticmethod
    def _record_session_list_cursor(cursor: str | None) -> None:
        path = os.environ.get("ACPC_MOCK_SESSION_LIST_CURSOR_FILE")
        if path:
            with Path(path).open("a", encoding="utf-8") as handle:
                handle.write(f"{cursor or '<none>'}\n")

    async def authenticate(self, method_id: str, **kwargs: Any) -> AuthenticateResponse | None:
        return AuthenticateResponse()

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        pass


async def main() -> None:
    await run_agent(
        MockAgent(), use_unstable_protocol=os.environ.get("ACPC_MOCK_ADVERTISE_RESUME") == "1"
    )


if __name__ == "__main__":
    asyncio.run(main())
