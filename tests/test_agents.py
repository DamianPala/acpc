"""Tests for the TOML agent registry."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from acpc.agents import Agent, AgentNotFoundError, is_installed, list_agents, load_agent


class TestLoadBuiltinAgents:
    """Verify built-in agent definitions load correctly."""

    def test_load_codex(self) -> None:
        agent = load_agent("codex")
        assert agent.identity == "codex"
        assert agent.name == "Codex CLI"
        assert agent.author == "OpenAI"
        assert agent.run_command == "npx @zed-industries/codex-acp"
        assert agent.install_command == "npm install -g @zed-industries/codex-acp"

    def test_load_claude(self) -> None:
        agent = load_agent("claude")
        assert agent.identity == "claude"
        assert agent.name == "Claude Code"
        assert agent.author == "Anthropic"
        assert agent.run_command == "npx @zed-industries/claude-agent-acp"
        assert agent.install_command == "npm install -g @zed-industries/claude-agent-acp"

    def test_load_gemini(self) -> None:
        agent = load_agent("gemini")
        assert agent.identity == "gemini"
        assert agent.name == "Gemini CLI"
        assert agent.author == "Google"
        assert agent.run_command == "gemini --experimental-acp"
        assert agent.install_command == "npm install -g @google/gemini-cli"


class TestListAgents:
    """Verify list_agents returns all built-in agents."""

    def test_list_all_builtin(self) -> None:
        agents = list_agents()
        identities = [a.identity for a in agents]
        assert "claude" in identities
        assert "codex" in identities
        assert "gemini" in identities
        assert len(agents) >= 3


class TestAgentNotFound:
    """Verify error handling for missing agents."""

    def test_unknown_identity_raises(self) -> None:
        with pytest.raises(AgentNotFoundError, match="nonexistent"):
            load_agent("nonexistent")

    def test_no_hint_for_unknown_name(self) -> None:
        with pytest.raises(AgentNotFoundError, match="nonexistent") as exc_info:
            load_agent("nonexistent")
        assert "Hint" not in str(exc_info.value)

    @pytest.mark.parametrize(
        ("model_name", "expected_agent", "expected_model"),
        [
            ("sonnet", "claude", "standard"),
            ("haiku", "claude", "fast"),
            ("opus", "claude", "max"),
        ],
    )
    def test_model_name_hint(
        self, model_name: str, expected_agent: str, expected_model: str
    ) -> None:
        with pytest.raises(AgentNotFoundError, match="Hint") as exc_info:
            load_agent(model_name)
        msg = str(exc_info.value)
        assert f"acpc prompt {expected_agent} --model {expected_model}" in msg

    def test_no_hint_for_raw_model_id(self) -> None:
        """Raw model IDs like 'o3' are not in presets, so no hint."""
        with pytest.raises(AgentNotFoundError) as exc_info:
            load_agent("o3")
        assert "Hint" not in str(exc_info.value)


class TestUserOverride:
    """Verify user overrides replace built-in agents."""

    def test_user_override_replaces_builtin(self, tmp_path: Path) -> None:
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        override_toml = agents_dir / "codex.toml"
        override_toml.write_text(
            'identity = "codex"\n'
            'name = "My Custom Codex"\n'
            'author = "Me"\n'
            'run_command = "my-codex run"\n'
            'install_command = "pip install my-codex"\n'
        )

        with patch("acpc.agents._user_agents_dir", return_value=agents_dir):
            agent = load_agent("codex")
            assert agent.name == "My Custom Codex"
            assert agent.author == "Me"
            assert agent.run_command == "my-codex run"
            assert agent.install_command == "pip install my-codex"

    def test_user_override_in_list(self, tmp_path: Path) -> None:
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        override_toml = agents_dir / "codex.toml"
        override_toml.write_text(
            'identity = "codex"\n'
            'name = "My Custom Codex"\n'
            'author = "Me"\n'
            'run_command = "my-codex run"\n'
            'install_command = "pip install my-codex"\n'
        )

        with patch("acpc.agents._user_agents_dir", return_value=agents_dir):
            agents = list_agents()
            codex = next(a for a in agents if a.identity == "codex")
            assert codex.name == "My Custom Codex"


class TestIsInstalled:
    """Verify is_installed checks for executable availability."""

    def test_installed_command(self) -> None:
        agent = Agent(
            identity="test",
            name="Test",
            author="Test",
            run_command="python --version",
            install_command="echo noop",
        )
        with patch("acpc.agents.shutil.which", return_value="/usr/bin/python"):
            assert is_installed(agent) is True

    def test_missing_command(self) -> None:
        agent = Agent(
            identity="test",
            name="Test",
            author="Test",
            run_command="nonexistent-binary-xyz --flag",
            install_command="echo noop",
        )
        with patch("acpc.agents.shutil.which", return_value=None):
            assert is_installed(agent) is False
