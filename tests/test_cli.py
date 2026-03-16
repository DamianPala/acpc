"""Tests for acpc CLI commands using click.testing.CliRunner."""

from __future__ import annotations

from unittest.mock import patch

from click.testing import CliRunner

from acpc import __version__
from acpc.cli import cli


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
            "--session",
            "--model",
            "--mode",
            "--permissions",
            "--quiet",
            "--json",
        ):
            assert opt in result.output


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


class TestStopErrors:
    def test_no_args_shows_error(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["stop"])
        assert result.exit_code == 2

    def test_nonexistent_session_shows_error(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["stop", "-s", "nonexistent-session-id"])
        assert result.exit_code != 0


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
