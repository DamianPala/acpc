"""Tests for acpc CLI commands using click.testing.CliRunner."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from acpc import __version__
from acpc.cli import _fetch_models_live, cli


class TestVersion:
    def test_version_output(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert __version__ in result.output
        assert "acpc" in result.output


class TestHelp:
    def test_help_shows_all_commands(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        for cmd in ("prompt", "agents", "sessions", "install", "stop", "status"):
            assert cmd in result.output

    def test_prompt_help_shows_options(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["prompt", "--help"])
        assert result.exit_code == 0
        for opt in (
            "--last",
            "--continue",
            "--session",
            "--model",
            "--mode",
            "--permissions",
            "--quiet",
            "--json",
            "--print-session-id",
        ):
            assert opt in result.output

    def test_daemon_commands_are_available(self) -> None:
        result = CliRunner().invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "daemon" in result.output


class TestAgents:
    def test_lists_builtin_agents(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["agents"])
        assert result.exit_code == 0
        assert "codex" in result.output
        assert "claude" in result.output
        assert "gemini" in result.output


class TestStatus:
    def test_no_running_sessions(self) -> None:
        runner = CliRunner()
        with patch("acpc.sessions._load_sessions", return_value={}):
            result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0
        assert "No running sessions" in result.output

    def test_status_marks_daemon_hosted_sessions(self) -> None:
        frame = {
            "pid": 123,
            "uptime_s": 4,
            "log": "/tmp/mock.log",
            "sessions": [{"id": "sess-1", "cwd": "/tmp/work", "state": "idle"}],
        }
        with (
            patch("acpc.sessions.list_running", return_value={}),
            patch("acpc.cli._daemon_statuses", return_value=[("mock", frame)]),
        ):
            result = CliRunner().invoke(cli, ["status"])
        assert result.exit_code == 0
        assert "sess-1" in result.output
        assert "(daemon)" in result.output

    def test_daemon_status_and_stop_use_management_frames(self) -> None:
        frame = {
            "pid": 123,
            "uptime_s": 4,
            "log": "/tmp/mock.log",
            "sessions": [{"id": "sess-1", "cwd": "/tmp/work", "state": "active"}],
        }
        with (
            patch("acpc.daemon_client.daemon_targets", return_value=["mock"]),
            patch("acpc.daemon_client.daemon_status", new=AsyncMock(return_value=frame)),
            patch("acpc.daemon_client.shutdown_daemon", new=AsyncMock()) as shutdown,
        ):
            status_result = CliRunner().invoke(cli, ["daemon", "status"])
            stop_result = CliRunner().invoke(cli, ["daemon", "stop", "mock"])
        assert status_result.exit_code == 0
        assert "sess-1" in status_result.output
        assert stop_result.exit_code == 0
        shutdown.assert_awaited_once_with("mock")


class TestPromptErrors:
    def test_no_args_shows_error(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["prompt"])
        assert result.exit_code != 0

    def test_nonexistent_agent_shows_error(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["prompt", "nonexistent-agent-xyz", "hello"])
        assert result.exit_code == 2

    def test_no_prompt_text_shows_error(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["prompt", "codex"])
        assert result.exit_code == 2

    def test_empty_pipe_shows_error(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["prompt", "codex", "-"], input="")
        assert result.exit_code == 2

    def test_whitespace_only_pipe_shows_error(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["prompt", "codex", "-"], input="   \n  \n")
        assert result.exit_code == 2

    @pytest.mark.parametrize(
        "options",
        [
            ["--print-session-id"],
            ["--print-session-id", "--quiet"],
            ["--print-session-id", "-o", "result.txt"],
            ["--print-session-id", "--quiet", "--json", "-o", "result.txt"],
        ],
    )
    def test_print_session_id_is_restricted_to_quiet_output_file(self, options: list[str]) -> None:
        result = CliRunner().invoke(cli, ["prompt", *options, "codex", "hello"])
        assert result.exit_code == 2
        assert "requires --quiet -o FILE" in result.output

    def test_print_session_id_accepts_only_the_documented_combination(
        self,
        tmp_path: Path,
    ) -> None:
        captured: list[object] = []

        async def fake_run(config: object) -> int:
            captured.append(config)
            return 0

        with patch("acpc.runner.run", new=fake_run):
            result = CliRunner().invoke(
                cli,
                [
                    "prompt",
                    "--print-session-id",
                    "--quiet",
                    "-o",
                    str(tmp_path / "result.txt"),
                    "codex",
                    "hello",
                ],
            )
        assert result.exit_code == 0
        assert len(captured) == 1
        assert getattr(captured[0], "print_session_id") is True

    def test_print_session_id_is_first_line_when_runner_records_session(
        self,
        tmp_path: Path,
    ) -> None:
        async def fake_run(config: object) -> int:  # noqa: ARG001
            return 0

        with (
            patch("acpc.runner.run", new=fake_run),
            patch("acpc.sessions.load_last_session", return_value="sess-1"),
        ):
            result = CliRunner().invoke(
                cli,
                [
                    "prompt",
                    "--print-session-id",
                    "--quiet",
                    "-o",
                    str(tmp_path / "result.txt"),
                    "codex",
                    "hello",
                ],
            )
        assert result.exit_code == 0
        assert result.output.splitlines()[0] == "sess-1"

    def test_continue_alias_sets_last_session_flag(self, monkeypatch) -> None:  # noqa: ANN001
        captured: list[object] = []

        async def fake_run(config: object) -> int:
            captured.append(config)
            return 0

        monkeypatch.setattr("acpc.runner.run", fake_run)
        result = CliRunner().invoke(cli, ["prompt", "-c", "codex", "hello"])
        assert result.exit_code == 0
        assert captured and getattr(captured[0], "use_last") is True

    def test_cwd_must_be_absolute_and_existing(self) -> None:
        result = CliRunner().invoke(cli, ["prompt", "--cwd", ".", "codex", "hello"])
        assert result.exit_code == 2
        assert "absolute existing directory" in result.output


class TestStopErrors:
    def test_no_args_shows_error(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["stop"])
        assert result.exit_code == 2

    def test_nonexistent_session_shows_error(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["stop", "-s", "nonexistent-session-id"])
        assert result.exit_code != 0

    def test_daemon_session_stop_sends_cancel(self) -> None:
        with (
            patch("acpc.sessions.list_running", return_value={}),
            patch(
                "acpc.cli._daemon_statuses",
                return_value=[
                    (
                        "mock",
                        {"sessions": [{"id": "sess-1"}]},
                    )
                ],
            ),
            patch("acpc.daemon_client.cancel_daemon_prompt", new=AsyncMock()) as cancel,
        ):
            result = CliRunner().invoke(cli, ["stop", "-s", "sess-1"])
        assert result.exit_code == 0
        cancel.assert_awaited_once_with("mock", session_id="sess-1")


class TestInstallErrors:
    def test_nonexistent_agent_shows_error(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["install", "nonexistent-agent-xyz"])
        assert result.exit_code == 2


class TestSessionsErrors:
    def test_nonexistent_agent_shows_error(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["sessions", "nonexistent-agent-xyz"])
        assert result.exit_code == 2

    def test_valid_agent_not_implemented(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["sessions", "codex"])
        assert result.exit_code == 1


class TestFetchModelsLive:
    def test_returns_failure_when_adapter_advertises_no_models(self) -> None:
        connection = SimpleNamespace(
            initialize=AsyncMock(),
            new_session=AsyncMock(return_value=SimpleNamespace()),
        )

        @asynccontextmanager
        async def fake_spawn(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
            yield connection, None

        agent = SimpleNamespace(run_command="fake-agent")
        with (
            patch("acpc.agents.load_agent", return_value=agent),
            patch("acpc.runner._spawn_agent", new=fake_spawn),
            patch("acpc.runner._cache_available_models", return_value=False),
        ):
            exit_code = asyncio.run(_fetch_models_live("codex"))

        assert exit_code == 1
