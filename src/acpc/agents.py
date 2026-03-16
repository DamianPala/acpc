"""TOML agent registry with built-in and user override support."""

from __future__ import annotations

import importlib.resources
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

import platformdirs


class AgentNotFoundError(Exception):
    """Raised when agent identity is not found in registry."""


@dataclass(frozen=True)
class Agent:
    """An ACP agent definition loaded from a TOML file."""

    identity: str
    name: str
    author: str
    run_command: str
    install_command: str


def _user_agents_dir() -> Path:
    """Return platform-specific user config directory for agent overrides."""
    return Path(platformdirs.user_config_dir("acpc")) / "agents"


def _parse_agent(data: dict[str, str]) -> Agent:
    """Parse a dict (from TOML) into an Agent dataclass."""
    return Agent(
        identity=data["identity"],
        name=data["name"],
        author=data["author"],
        run_command=data["run_command"],
        install_command=data["install_command"],
    )


def _load_builtin_agents() -> dict[str, Agent]:
    """Load all built-in agent TOML files from the package data directory."""
    agents: dict[str, Agent] = {}
    package_dir = importlib.resources.files("acpc.data.agents")
    for item in package_dir.iterdir():
        if item.name.endswith(".toml"):
            data = tomllib.loads(item.read_text(encoding="utf-8"))
            agent = _parse_agent(data)
            agents[agent.identity] = agent
    return agents


def _load_user_agents() -> dict[str, Agent]:
    """Load user override agent TOML files from the platform config directory."""
    agents: dict[str, Agent] = {}
    user_dir = _user_agents_dir()
    if not user_dir.is_dir():
        return agents
    for toml_path in user_dir.glob("*.toml"):
        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        agent = _parse_agent(data)
        agents[agent.identity] = agent
    return agents


def load_agent(identity: str) -> Agent:
    """Load a single agent by identity.

    User overrides take priority over built-in agents.

    Raises:
        AgentNotFoundError: If no agent with the given identity exists.
    """
    user_agents = _load_user_agents()
    if identity in user_agents:
        return user_agents[identity]

    builtin_agents = _load_builtin_agents()
    if identity in builtin_agents:
        return builtin_agents[identity]

    raise AgentNotFoundError(
        f"Agent '{identity}' not found in registry. "
        f"Available: {', '.join(sorted({*_load_builtin_agents(), *_load_user_agents()}))}"
    )


def list_agents() -> list[Agent]:
    """List all available agents (built-in + user overrides, deduped by identity).

    User overrides replace built-in agents with the same identity.
    Results are sorted by identity for stable ordering.
    """
    agents = _load_builtin_agents()
    agents.update(_load_user_agents())
    return sorted(agents.values(), key=lambda a: a.identity)


def is_installed(agent: Agent) -> bool:
    """Check if the agent's run_command executable is available on PATH.

    Extracts the first word of run_command and checks with shutil.which().
    """
    executable = agent.run_command.split()[0]
    return shutil.which(executable) is not None
