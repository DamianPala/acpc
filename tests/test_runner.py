"""Tests for acpc.runner module."""

import asyncio
import contextlib
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, call, patch

import acp
import pytest

from acpc.client import AcpcClient, PermissionLevel
from acpc.daemon_client import (
    DaemonProtocolError,
    DaemonUnavailableError,
    daemon_status,
    shutdown_daemon,
)
from acpc.ipc import lock_path_for_target, socket_path_for_target
from acpc.output import OutputHandler, OutputMode
from acpc.runner import (
    EXIT_AGENT_ERROR,
    EXIT_PERMISSION_DENIED,
    EXIT_SIGINT,
    EXIT_SIGPIPE,
    EXIT_SIGTERM,
    EXIT_TIMEOUT,
    EXIT_USAGE_ERROR,
    _EXIT_TIMEOUT,
    RunConfig,
    _cache_available_models,
    _drain_notifications,
    _process_group_kwargs,
    _resolve_run_cwd,
    run,
    _spawn_agent,
    _try_set_model,
)


MOCK_AGENT_SCRIPT = Path(__file__).with_name("mock_agent.py")


def _quiet_client() -> AcpcClient:
    return AcpcClient(
        output=OutputHandler(OutputMode.QUIET),
        permission_level=PermissionLevel.NONE,
        is_tty=False,
    )


async def _run_mock_prompt(command: str, *args: str) -> None:
    client = _quiet_client()
    async with _spawn_agent(
        client,
        command,
        *args,
        cwd=str(Path.cwd()),
        drain_stderr=True,
    ) as (conn, _process):
        init_response = await conn.initialize(protocol_version=acp.PROTOCOL_VERSION)
        assert init_response.agent_capabilities is not None
        session = await conn.new_session(cwd=str(Path.cwd()))
        result = await conn.prompt(
            session.session_id,
            [acp.text_block("stderr:TEXT")],
        )
        await _drain_notifications(conn)
        assert result.stop_reason == "end_turn"


class TestRunConfig:
    def test_defaults(self) -> None:
        config = RunConfig(
            agent_identity="codex",
            prompt_text="hello",
        )
        assert config.agent_identity == "codex"
        assert config.prompt_text == "hello"
        assert config.model is None
        assert config.mode is None
        assert config.permission_level == "prompt"
        assert config.cwd is None
        assert config.session_id is None
        assert config.use_last is False
        assert config.output_mode == "text"
        assert config.output_file is None
        assert config.timeout is None
        assert config.is_tty is True
        assert config.env == {}

    def test_custom_values(self) -> None:
        config = RunConfig(
            agent_identity="claude",
            prompt_text="analyze",
            model="sonnet",
            mode="plan",
            permission_level="all",
            cwd="/tmp",
            session_id="sess-1",
            use_last=True,
            output_mode="json",
            output_file="out.md",
            timeout=60,
            is_tty=False,
            env={"KEY": "val"},
        )
        assert config.model == "sonnet"
        assert config.mode == "plan"
        assert config.permission_level == "all"
        assert config.cwd == "/tmp"
        assert config.session_id == "sess-1"
        assert config.use_last is True
        assert config.output_mode == "json"
        assert config.output_file == "out.md"
        assert config.timeout == 60
        assert config.is_tty is False
        assert config.env == {"KEY": "val"}


class TestExitCodes:
    def test_values(self) -> None:
        assert EXIT_AGENT_ERROR == 1
        assert EXIT_USAGE_ERROR == 2
        assert EXIT_PERMISSION_DENIED == 3
        assert EXIT_TIMEOUT == 124
        assert EXIT_SIGINT == 130
        assert EXIT_SIGPIPE == 141
        assert EXIT_SIGTERM == 143


def test_resume_cwd_is_loaded_and_deleted_metadata_is_evicted(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    from acpc.sessions import load_session_cwd, save_last_session

    monkeypatch.setenv("ACPC_STATE_DIR", str(tmp_path / "state"))
    cwd = tmp_path / "work"
    cwd.mkdir()
    save_last_session("mock", "sess-1", str(cwd))

    resolved, error = _resolve_run_cwd("mock", "sess-1", None)
    assert resolved == str(cwd)
    assert error is None

    cwd.rmdir()
    resolved, error = _resolve_run_cwd("mock", "sess-1", None)
    assert resolved is None
    assert error == f"session cwd no longer exists: {cwd}"
    assert load_session_cwd("mock", "sess-1") is None


class TestProcessGroupKwargs:
    def test_unix_uses_start_new_session(self) -> None:
        with patch.object(sys, "platform", "linux"):
            kwargs = _process_group_kwargs()
            assert kwargs == {"start_new_session": True}

    def test_windows_uses_create_new_process_group(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, create=True),
        ):
            kwargs = _process_group_kwargs()
            assert kwargs == {"creationflags": 0x00000200}

    def test_darwin_uses_start_new_session(self) -> None:
        with patch.object(sys, "platform", "darwin"):
            kwargs = _process_group_kwargs()
            assert kwargs == {"start_new_session": True}


class TestCacheAvailableModels:
    @patch("acpc.models_cache.save_models")
    def test_caches_models_from_legacy_models_state(self, save_models) -> None:
        response = SimpleNamespace(
            models=SimpleNamespace(
                available_models=[
                    SimpleNamespace(
                        model_id="gpt-5.5/high",
                        display_name="GPT-5.5 High",
                    )
                ]
            ),
            config_options=None,
        )

        cached = _cache_available_models("codex", response)

        assert cached is True
        save_models.assert_called_once_with(
            "codex",
            [{"model_id": "gpt-5.5/high", "display_name": "GPT-5.5 High"}],
        )

    @patch("acpc.models_cache.save_models")
    def test_caches_models_from_config_options(self, save_models) -> None:
        response = SimpleNamespace(
            models=None,
            config_options=[
                SimpleNamespace(
                    root=SimpleNamespace(
                        id="model",
                        category="model",
                        options=[
                            SimpleNamespace(
                                value="gpt-5.6-sol",
                                name="GPT-5.6-Sol",
                            ),
                            SimpleNamespace(
                                value="gpt-5.6-terra",
                                name="GPT-5.6-Terra",
                            ),
                        ],
                    )
                )
            ],
        )

        cached = _cache_available_models("codex", response)

        assert cached is True
        save_models.assert_called_once_with(
            "codex",
            [
                {"model_id": "gpt-5.6-sol", "display_name": "GPT-5.6-Sol"},
                {"model_id": "gpt-5.6-terra", "display_name": "GPT-5.6-Terra"},
            ],
        )

    @patch("acpc.models_cache.save_models")
    def test_reports_when_response_has_no_models(self, save_models) -> None:
        response = SimpleNamespace(models=None, config_options=[])

        cached = _cache_available_models("codex", response)

        assert cached is False
        save_models.assert_not_called()


class TestTrySetModel:
    def test_uses_current_config_option_api_when_advertised(self) -> None:
        connection = SimpleNamespace(
            set_config_option=AsyncMock(),
        )
        response = SimpleNamespace(
            models=None,
            config_options=[
                SimpleNamespace(
                    root=SimpleNamespace(
                        id="model",
                        category="model",
                        options=[SimpleNamespace(value="gpt-5.6-sol")],
                    )
                )
            ],
        )

        was_set = asyncio.run(
            _try_set_model(
                cast(Any, connection),
                "session-1",
                "gpt-5.6-luna/medium",
                response,
                lambda message: None,
            )
        )

        assert was_set is True
        assert connection.set_config_option.await_args_list == [
            call(
                config_id="model",
                session_id="session-1",
                value="gpt-5.6-luna",
            ),
            call(
                config_id="reasoning_effort",
                session_id="session-1",
                value="medium",
            ),
        ]

    def test_fails_when_session_has_no_model_capability(self) -> None:
        connection = SimpleNamespace(set_config_option=AsyncMock())
        response = SimpleNamespace(models=None, config_options=[])
        messages: list[str] = []

        was_set = asyncio.run(
            _try_set_model(
                cast(Any, connection),
                "session-1",
                "gpt-5.6-luna",
                response,
                messages.append,
            )
        )

        assert was_set is False
        connection.set_config_option.assert_not_awaited()
        assert messages == [
            "warning: cannot set model 'gpt-5.6-luna': session advertises no model config option"
        ]


class TestAdapterStderr:
    def test_forwards_adapter_stderr(self, capsys: Any) -> None:
        asyncio.run(_run_mock_prompt(sys.executable, str(MOCK_AGENT_SCRIPT)))

        assert "TEXT" in capsys.readouterr().err

    def test_chatty_adapter_completes_prompt(self, tmp_path: Path) -> None:
        wrapper = tmp_path / "chatty_adapter.py"
        wrapper.write_text(
            """import os
import sys

payload = memoryview(b"x" * (32 * 1024 * 1024))
while payload:
    payload = payload[os.write(sys.stderr.fileno(), payload) :]
os.execv(sys.executable, [sys.executable, sys.argv[1]])
""",
            encoding="utf-8",
        )

        async def scenario() -> None:
            await _run_mock_prompt(sys.executable, str(wrapper), str(MOCK_AGENT_SCRIPT))

        asyncio.run(asyncio.wait_for(scenario(), timeout=1.0))


class TestTeardownIsBounded:
    """An adapter that ignores stdin EOF must not hold the CLI open.

    codex-acp is exactly this case: it exits on a flat internal timer of about
    two seconds regardless of session state, so acpc always ends up killing its
    process group. What must stay true is that the kill happens on our schedule,
    not the adapter's.
    """

    def test_stubborn_adapter_is_killed_within_budget(self) -> None:
        client = AcpcClient(
            output=OutputHandler(OutputMode.QUIET),
            permission_level=PermissionLevel.NONE,
            is_tty=False,
        )

        async def scenario() -> tuple[float, int | None]:
            start = time.perf_counter()
            # Never reads stdin, so write_eof cannot end it.
            async with _spawn_agent(
                client, sys.executable, "-c", "import time; time.sleep(30)"
            ) as (
                _conn,
                process,
            ):
                pass
            return time.perf_counter() - start, process.returncode

        elapsed, returncode = asyncio.run(scenario())

        assert returncode is not None, "adapter process was never reaped"
        budget = _EXIT_TIMEOUT * 2 + 2.0
        assert elapsed < budget, f"teardown took {elapsed:.1f}s, budget is {budget:.1f}s"


class TestDrainNotifications:
    """The SDK hands notifications to background tasks, so a completed prompt
    does not imply the last agent_message_chunk has been delivered."""

    @staticmethod
    def _conn(queue: Any = None, tasks: Any = None) -> Any:
        """Mirror the SDK layout: ClientSideConnection._conn._tasks is the
        TaskSupervisor, whose own _tasks holds the live task set."""
        supervisor = None if tasks is None else SimpleNamespace(_tasks=tasks)
        return SimpleNamespace(_conn=SimpleNamespace(_queue=queue, _tasks=supervisor))

    def test_waits_for_pending_notification_task(self) -> None:
        delivered: list[str] = []

        async def scenario() -> None:
            async def late_notification() -> None:
                await asyncio.sleep(0.05)
                delivered.append("chunk")

            task = asyncio.create_task(late_notification(), name="acp.Dispatcher.notification")
            queue = SimpleNamespace(join=AsyncMock())
            await _drain_notifications(self._conn(queue=queue, tasks={task}))

        asyncio.run(scenario())
        assert delivered == ["chunk"], "drain returned before the notification handler ran"

    def test_joins_the_message_queue(self) -> None:
        queue = SimpleNamespace(join=AsyncMock())
        asyncio.run(_drain_notifications(self._conn(queue=queue, tasks=set())))
        queue.join.assert_awaited_once()

    def test_ignores_unrelated_tasks(self) -> None:
        """Only notification tasks are awaited; a long receive loop must not block."""

        async def scenario() -> None:
            forever = asyncio.create_task(asyncio.sleep(30), name="acp.Connection.receive")
            try:
                queue = SimpleNamespace(join=AsyncMock())
                await asyncio.wait_for(
                    _drain_notifications(self._conn(queue=queue, tasks={forever})),
                    timeout=2,
                )
            finally:
                forever.cancel()

        asyncio.run(scenario())

    def test_missing_internals_are_tolerated(self) -> None:
        """SDK layout changes must degrade, not crash a finished prompt."""
        asyncio.run(_drain_notifications(cast(Any, SimpleNamespace())))
        asyncio.run(_drain_notifications(self._conn()))

    def test_sdk_layout_we_depend_on_still_exists(self) -> None:
        """Canary: _drain_notifications reaches into SDK privates.

        If an SDK upgrade moves these, the drain silently degrades and the
        truncation bug returns. Fail loudly here instead.
        """
        from acp.task.dispatcher import DefaultMessageDispatcher
        from acp.task.queue import InMemoryMessageQueue
        from acp.task.supervisor import TaskSupervisor

        assert hasattr(InMemoryMessageQueue, "join")
        assert hasattr(TaskSupervisor(source="canary"), "_tasks")
        assert hasattr(DefaultMessageDispatcher, "_dispatch_notification")


def _configure_mock_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Expose the standalone mock agent to the runner and daemon subprocess."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "mock.toml").write_text(
        f'''identity = "mock"
name = "Mock Agent"
author = "Test"
run_command = "{sys.executable} {MOCK_AGENT_SCRIPT}"
install_command = "true"
''',
        encoding="utf-8",
    )
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("ACPC_USER_AGENTS_DIR", str(agents_dir))
    monkeypatch.setenv("ACPC_STATE_DIR", str(state_dir))
    return state_dir


def _mock_run_config(prompt_text: str = "hello", **overrides: Any) -> RunConfig:
    """Build a non-interactive config suitable for daemon routing tests."""
    values: dict[str, Any] = {
        "agent_identity": "mock",
        "prompt_text": prompt_text,
        "permission_level": "read",
        "is_tty": False,
        "output_mode": "quiet",
    }
    values.update(overrides)
    return RunConfig(**values)


def test_default_run_uses_a_real_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The normal runner decision reaches a real daemon and its adapter."""
    _configure_mock_agent(tmp_path, monkeypatch)
    monkeypatch.setenv("ACPC_DAEMON_TTL", "30")

    async def scenario() -> tuple[int, dict[str, Any]]:
        try:
            exit_code = await run(_mock_run_config("daemon route"))
            return exit_code, await daemon_status("mock")
        finally:
            with contextlib.suppress(DaemonUnavailableError, OSError):
                await shutdown_daemon("mock")

    exit_code, status = asyncio.run(scenario())

    assert exit_code == 0
    assert len(status["sessions"]) == 1


def test_no_daemon_flag_is_a_silent_direct_bypass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
) -> None:
    _configure_mock_agent(tmp_path, monkeypatch)
    with patch("acpc.daemon_client.run_daemon", new=AsyncMock(return_value=99)) as daemon_run:
        assert asyncio.run(run(_mock_run_config(no_daemon=True))) == 0

    daemon_run.assert_not_awaited()
    assert "daemon:" not in capsys.readouterr().err


def test_no_daemon_environment_is_a_silent_direct_bypass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
) -> None:
    _configure_mock_agent(tmp_path, monkeypatch)
    monkeypatch.setenv("ACPC_NO_DAEMON", "1")
    with patch("acpc.daemon_client.run_daemon", new=AsyncMock(return_value=99)) as daemon_run:
        assert asyncio.run(run(_mock_run_config())) == 0

    daemon_run.assert_not_awaited()
    assert "daemon:" not in capsys.readouterr().err


def test_windows_is_a_silent_direct_bypass_without_socket_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
) -> None:
    state_dir = _configure_mock_agent(tmp_path, monkeypatch)
    with (
        patch("acpc.runner.sys", SimpleNamespace(platform="win32")),
        patch("acpc.runner._process_group_kwargs", return_value={}),
        patch("acpc.daemon_client.run_daemon", new=AsyncMock(return_value=99)) as daemon_run,
    ):
        assert asyncio.run(run(_mock_run_config())) == 0

    daemon_run.assert_not_awaited()
    assert not socket_path_for_target("mock").exists()
    assert not lock_path_for_target("mock").exists()
    assert "daemon:" not in capsys.readouterr().err
    assert state_dir.exists()


def test_interactive_permissions_note_and_use_direct_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
) -> None:
    _configure_mock_agent(tmp_path, monkeypatch)
    config = _mock_run_config(permission_level="prompt", is_tty=True)
    with patch("acpc.daemon_client.run_daemon", new=AsyncMock(return_value=99)) as daemon_run:
        assert asyncio.run(run(config)) == 0

    daemon_run.assert_not_awaited()
    assert "[acpc] daemon: interactive permissions require direct mode" in capsys.readouterr().err


def test_daemon_failure_mid_attempt_falls_back_without_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
) -> None:
    _configure_mock_agent(tmp_path, monkeypatch)
    active_prompts = 0
    prompt_calls = 0

    class FailingDaemonClient:
        def __init__(self, target: str, **kwargs: Any) -> None:
            del target, kwargs

        async def prompt(self, **kwargs: Any) -> int:
            nonlocal active_prompts, prompt_calls
            del kwargs
            prompt_calls += 1
            active_prompts += 1
            try:
                raise DaemonProtocolError("connection closed mid-prompt")
            finally:
                active_prompts -= 1

    with patch("acpc.daemon_client.DaemonClient", FailingDaemonClient):
        assert asyncio.run(run(_mock_run_config("recover"))) == 0

    assert prompt_calls == 1
    assert active_prompts == 0
    assert "[acpc] daemon: unavailable (connection closed mid-prompt), running direct" in (
        capsys.readouterr().err
    )


def test_capacity_falls_back_for_a_new_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
) -> None:
    _configure_mock_agent(tmp_path, monkeypatch)
    prompt_sessions: list[str | None] = []

    class FullDaemonClient:
        def __init__(self, target: str, **kwargs: Any) -> None:
            del target, kwargs

        async def prompt(self, **kwargs: Any) -> int:
            prompt_sessions.append(kwargs["session_id"])
            raise DaemonUnavailableError("daemon is at capacity")

    with patch("acpc.daemon_client.DaemonClient", FullDaemonClient):
        assert asyncio.run(run(_mock_run_config("new"))) == 0

    assert prompt_sessions == [None]
    assert "[acpc] daemon: at capacity, running direct" in capsys.readouterr().err


def test_capacity_still_routes_a_live_session_through_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_mock_agent(tmp_path, monkeypatch)
    prompt_sessions: list[str | None] = []

    class FullDaemonClient:
        def __init__(self, target: str, **kwargs: Any) -> None:
            del target, kwargs

        async def prompt(self, **kwargs: Any) -> int:
            prompt_sessions.append(kwargs["session_id"])
            if kwargs["session_id"] is None:
                raise DaemonUnavailableError("daemon is at capacity")
            return 0

    with patch("acpc.daemon_client.DaemonClient", FullDaemonClient):
        config = _mock_run_config("continue", session_id="live", cwd=str(tmp_path))
        assert asyncio.run(run(config)) == 0

    assert prompt_sessions == ["live"]
