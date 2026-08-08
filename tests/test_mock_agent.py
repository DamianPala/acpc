"""Driver tests proving the mock agent speaks real ACP over stdio.

The Stage 1 acceptance test: spawn the mock through the harvested
`acpc.spawn.spawn_adapter`, complete initialize → session/new → session/prompt
→ answer round-trips, and route its permission requests through the harvested
`acpc.permissions` policy — the same primitives the real client path will use.
"""

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest
from acp import PROTOCOL_VERSION, RequestError, text_block
from acp.schema import (
    AgentMessageChunk,
    AllowedOutcome,
    AvailableCommandsUpdate,
    DeniedOutcome,
    RequestPermissionResponse,
    ToolCallUpdate,
)

from acpc.permissions import PermissionLevel, classify_kind, find_option, should_allow
from acpc.spawn import spawn_adapter

MOCK_AGENT_SCRIPT = str(Path(__file__).parent / "mock_agent.py")


async def eventually(predicate, *, timeout: float = 5.0, message: str = "condition") -> None:
    """Wait for a condition that arrives via a concurrently dispatched notification.

    The acp library dispatches session/update notifications on their own tasks,
    so a prompt response can resolve before the final chunk's handler ran.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, f"timed out waiting for {message}"
        await asyncio.sleep(0.02)


class DriverClient:
    """Minimal ACP client: collects updates, answers permissions by policy."""

    def __init__(self, permission_level: PermissionLevel = PermissionLevel.ALL) -> None:
        self.permission_level = permission_level
        self.message_chunks: list[str] = []
        self.commands_updates: list[AvailableCommandsUpdate] = []
        self.permission_requests: list[tuple[str | None, str | None]] = []  # (kind, target)
        self.written_files: dict[str, str] = {}

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        if isinstance(update, AgentMessageChunk):
            text = getattr(update.content, "text", None)
            if isinstance(text, str):
                self.message_chunks.append(text)
        if isinstance(update, AvailableCommandsUpdate):
            self.commands_updates.append(update)

    async def request_permission(
        self,
        options: list[Any],
        session_id: str,
        tool_call: ToolCallUpdate,
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        raw_input = tool_call.raw_input if isinstance(tool_call.raw_input, dict) else {}
        target = raw_input.get("target")
        self.permission_requests.append((tool_call.kind, target))

        category = classify_kind(tool_call.kind)
        decision = should_allow(self.permission_level, category)
        allow = bool(decision)  # None (ask-the-human) counts as deny in this driver
        option_id = find_option(options, allow, self.permission_level)
        # Allowing answers "selected" with the chosen option; denials answer
        # "cancelled" (the donor client's wire behavior).
        if allow and option_id is not None:
            return RequestPermissionResponse(
                outcome=AllowedOutcome(outcome="selected", option_id=option_id)
            )
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

    async def read_text_file(self, path: str, session_id: str, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def write_text_file(self, content: str, path: str, session_id: str, **kwargs: Any) -> Any:
        self.written_files[path] = content
        return {}

    async def create_terminal(self, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def terminal_output(self, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def release_terminal(self, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def wait_for_terminal_exit(self, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def kill_terminal(self, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def create_elicitation(self, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def complete_elicitation(self, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        pass

    def on_connect(self, conn: Any) -> None:
        pass


def _spawn_mock(client: DriverClient, tmp_path: Path):
    return spawn_adapter(
        client,
        sys.executable,
        MOCK_AGENT_SCRIPT,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        cwd=str(tmp_path),
    )


def test_initialize_prompt_answer_round_trip(tmp_path: Path) -> None:
    """The acceptance round-trip: initialize → session/new → prompt → answer."""

    async def scenario() -> None:
        client = DriverClient()
        async with _spawn_mock(client, tmp_path) as (conn, _process):
            init = await conn.initialize(protocol_version=PROTOCOL_VERSION)
            assert init.agent_info is not None
            assert init.agent_info.name == "mock-agent"
            assert init.agent_capabilities is not None
            assert init.agent_capabilities.load_session is True

            session = await conn.new_session(cwd=str(tmp_path))
            assert session.session_id

            response = await conn.prompt(
                session_id=session.session_id,
                prompt=[text_block("echo:hello round trip")],
            )

            assert response.stop_reason == "end_turn"
            await eventually(
                lambda: "hello round trip" in "".join(client.message_chunks),
                message="the echoed answer chunk",
            )

    asyncio.run(scenario())


def test_advertised_dataset_modes_models_efforts_commands(tmp_path: Path) -> None:
    async def scenario() -> None:
        client = DriverClient()
        async with _spawn_mock(client, tmp_path) as (conn, _process):
            await conn.initialize(protocol_version=PROTOCOL_VERSION)
            session = await conn.new_session(cwd=str(tmp_path))

            modes = session.modes
            assert modes is not None
            assert modes.current_mode_id == "default"
            assert {mode.id for mode in modes.available_modes} == {
                "default",
                "acceptEdits",
                "plan",
                "yolo",
            }

            options = {option.id: option for option in session.config_options or []}
            model_values = [choice.value for choice in options["model"].options]
            assert model_values == ["mock-opus-5", "mock-sonnet-5", "mock-haiku-4-5"]
            effort_values = [choice.value for choice in options["reasoning_effort"].options]
            assert effort_values == ["low", "medium", "high", "xhigh"]

            # Supported model and effort are accepted...
            await conn.set_config_option(
                config_id="model", session_id=session.session_id, value="mock-opus-5"
            )
            await conn.set_config_option(
                config_id="reasoning_effort", session_id=session.session_id, value="xhigh"
            )
            # ...an unsupported effort level is a hard error naming the supported ones.
            with pytest.raises(RequestError):
                await conn.set_config_option(
                    config_id="reasoning_effort", session_id=session.session_id, value="ultra"
                )

            # Commands are advertised via session/update shortly after session/new.
            deadline = asyncio.get_running_loop().time() + 5
            while not client.commands_updates:
                assert asyncio.get_running_loop().time() < deadline, "no commands update"
                await asyncio.sleep(0.02)
            names = {
                command.name
                for update in client.commands_updates
                for command in update.available_commands
            }
            assert {"init", "review", "plan"} <= names

    asyncio.run(scenario())


def test_perm_scenario_routes_requests_through_the_policy(tmp_path: Path) -> None:
    """Six permission requests answered by the harvested read-tier policy."""

    async def scenario() -> None:
        client = DriverClient(permission_level=PermissionLevel.READ)
        async with _spawn_mock(client, tmp_path) as (conn, _process):
            await conn.initialize(protocol_version=PROTOCOL_VERSION)
            session = await conn.new_session(cwd=str(tmp_path))

            response = await conn.prompt(
                session_id=session.session_id,
                prompt=[text_block("run the perm scenario")],
            )

            assert response.stop_reason == "end_turn"
            kinds = [kind for kind, _ in client.permission_requests]
            assert kinds == ["read", "edit", "execute", "delete", "switch_mode", "switch_mode"]

            await eventually(
                lambda: "Denied:" in "".join(client.message_chunks),
                message="the permission summary answer",
            )
            answer = "".join(client.message_chunks)
            assert "Allowed: read:src/app.py" in answer
            assert "edit:src/app.py" in answer.split("Denied:")[1]
            assert "switch_mode:yolo" in answer.split("Denied:")[1]
            assert "switch_mode:plan" in answer.split("Denied:")[1]

    asyncio.run(scenario())


def test_execute_tier_allows_delete_and_denies_unknown_switches(tmp_path: Path) -> None:
    async def scenario() -> None:
        client = DriverClient(permission_level=PermissionLevel.EXECUTE)
        async with _spawn_mock(client, tmp_path) as (conn, _process):
            await conn.initialize(protocol_version=PROTOCOL_VERSION)
            session = await conn.new_session(cwd=str(tmp_path))

            await conn.prompt(
                session_id=session.session_id,
                prompt=[text_block("run the perm scenario")],
            )

            await eventually(
                lambda: "Denied:" in "".join(client.message_chunks),
                message="the permission summary answer",
            )
            answer = "".join(client.message_chunks)
            allowed_part = answer.split("Denied:")[0]
            denied_part = answer.split("Denied:")[1]
            assert "edit:src/app.py" in allowed_part
            assert "execute:rm -rf build/" in allowed_part
            assert "delete:old_report.md" in allowed_part
            assert "switch_mode:yolo" in denied_part
            assert "switch_mode:plan" in denied_part

    asyncio.run(scenario())


def test_multi_turn_keeps_session_context(tmp_path: Path) -> None:
    async def scenario() -> None:
        client = DriverClient()
        async with _spawn_mock(client, tmp_path) as (conn, _process):
            await conn.initialize(protocol_version=PROTOCOL_VERSION)
            session = await conn.new_session(cwd=str(tmp_path))

            await conn.prompt(session_id=session.session_id, prompt=[text_block("multi:one")])
            await conn.prompt(session_id=session.session_id, prompt=[text_block("multi:two")])

            await eventually(
                lambda: "turn 2: two" in "".join(client.message_chunks),
                message="the second turn's answer",
            )
            assert "turn 1: one" in "".join(client.message_chunks)

    asyncio.run(scenario())


def test_cancel_mid_turn_returns_cancelled(tmp_path: Path) -> None:
    async def scenario() -> None:
        client = DriverClient()
        async with _spawn_mock(client, tmp_path) as (conn, _process):
            await conn.initialize(protocol_version=PROTOCOL_VERSION)
            session = await conn.new_session(cwd=str(tmp_path))

            prompt_task = asyncio.create_task(
                conn.prompt(session_id=session.session_id, prompt=[text_block("chunkslow:20")])
            )
            deadline = asyncio.get_running_loop().time() + 5
            while "started" not in "".join(client.message_chunks):
                assert asyncio.get_running_loop().time() < deadline, "turn never started"
                await asyncio.sleep(0.02)

            await conn.cancel(session_id=session.session_id)
            response = await asyncio.wait_for(prompt_task, timeout=5)

            assert response.stop_reason == "cancelled"

    asyncio.run(scenario())


def test_huge_scenario_emits_multibyte_content_past_200kb(tmp_path: Path) -> None:
    """Also exercises the 10 MB stream limit — the answer arrives in one frame."""

    async def scenario() -> None:
        client = DriverClient()
        async with _spawn_mock(client, tmp_path) as (conn, _process):
            await conn.initialize(protocol_version=PROTOCOL_VERSION)
            session = await conn.new_session(cwd=str(tmp_path))

            response = await conn.prompt(
                session_id=session.session_id,
                prompt=[text_block("run the huge scenario")],
            )

            assert response.stop_reason == "end_turn"
            await eventually(
                lambda: "ACPC-HUGE-END" in "".join(client.message_chunks),
                message="the huge answer chunk",
            )
            answer = "".join(client.message_chunks)
            encoded = answer.encode("utf-8")
            assert len(encoded) > 200 * 1024
            # The straddling emoji starts at byte 1998 of the final chunk.
            final_chunk = client.message_chunks[-1].encode("utf-8")
            assert final_chunk[1998:2002] == "😀".encode()

    asyncio.run(scenario())


def test_fail_scenario_returns_refusal(tmp_path: Path) -> None:
    async def scenario() -> None:
        client = DriverClient()
        async with _spawn_mock(client, tmp_path) as (conn, _process):
            await conn.initialize(protocol_version=PROTOCOL_VERSION)
            session = await conn.new_session(cwd=str(tmp_path))

            response = await conn.prompt(
                session_id=session.session_id,
                prompt=[text_block("please fail this on purpose")],
            )

            assert response.stop_reason == "refusal"
            await eventually(
                lambda: "Unable to complete" in "".join(client.message_chunks),
                message="the refusal answer chunk",
            )

    asyncio.run(scenario())


def test_write_file_goes_through_permission_and_client_fs(tmp_path: Path) -> None:
    async def scenario() -> None:
        client = DriverClient(permission_level=PermissionLevel.EXECUTE)
        async with _spawn_mock(client, tmp_path) as (conn, _process):
            await conn.initialize(protocol_version=PROTOCOL_VERSION)
            session = await conn.new_session(cwd=str(tmp_path))

            await conn.prompt(
                session_id=session.session_id,
                prompt=[text_block("write-file:note.txt")],
            )

            await eventually(
                lambda: "write note.txt done" in "".join(client.message_chunks),
                message="the write confirmation chunk",
            )
            written_path = str(tmp_path / "note.txt")
            assert written_path in client.written_files

    asyncio.run(scenario())
