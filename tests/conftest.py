"""Shared fixtures for acpc tests."""

import sys
from pathlib import Path

import pytest

MOCK_AGENT_SCRIPT = str(Path(__file__).parent / "mock_agent.py")


@pytest.fixture()
def mock_agent_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a temporary agent registry with the mock agent.

    Patches acpc.agents._user_agents_dir so load_agent("mock") works.
    """
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()

    toml_content = f"""\
identity = "mock"
name = "Mock Agent"
author = "Test"
run_command = "{sys.executable} {MOCK_AGENT_SCRIPT}"
install_command = "true"
"""
    (agents_dir / "mock.toml").write_text(toml_content)

    monkeypatch.setattr("acpc.agents._user_agents_dir", lambda: agents_dir)
    return agents_dir
