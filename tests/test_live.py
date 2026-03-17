"""Live end-to-end tests with real ACP agents.

These tests call real agents (codex, claude) and cost API credits.
Skipped by default. Run explicitly:

    uv run pytest tests/test_live.py -v -m live

All tests use the cheapest available model per agent to minimize cost.
Estimated run time: ~3 minutes.

Isolation: tests run with HOME=~/.agent-test-home to prevent loading
user skills and config. Setup: copy auth files to that directory
(see _build_test_env). Known limitation: claude-agent-acp still loads
the real ~/.claude/CLAUDE.md regardless of HOME override.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

# Use the installed entry point, not python -m (no __main__.py)
_acpc_bin = shutil.which("acpc")
assert _acpc_bin is not None, "acpc not installed in venv. Run: uv sync"
ACPC = [_acpc_bin]

pytestmark = pytest.mark.live

# Models used in live tests
TEST_MODELS: dict[str, str] = {
    "codex": "gpt-5.1-codex-mini",
    "claude": "default",
}

# Isolated env: no skills, no user config, just auth.
# Setup: see tests/codex-test-home.sh for instructions.
_AGENT_TEST_HOME = Path(os.environ.get("AGENT_TEST_HOME", Path.home() / ".agent-test-home"))


def _build_test_env() -> dict[str, str]:
    """Build env with isolated agent home (no skills, no user config).

    Structure: ~/.agent-test-homes/{.codex/,.claude/} with auth only.
    HOME override prevents loading user skills from ~/.agents/ etc.
    """
    env = dict(os.environ)
    if _AGENT_TEST_HOME.exists():
        env["HOME"] = str(_AGENT_TEST_HOME)
        codex_dir = _AGENT_TEST_HOME / ".codex"
        if codex_dir.exists():
            env["CODEX_HOME"] = str(codex_dir)
        claude_dir = _AGENT_TEST_HOME / ".claude"
        if claude_dir.exists():
            env["CLAUDE_CONFIG_DIR"] = str(claude_dir)
    return env


_TEST_ENV = _build_test_env()


def _run_acpc(
    *args: str,
    input_text: str | None = None,
    timeout: int = 60,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run acpc as subprocess with isolated agent env."""
    return subprocess.run(
        [*ACPC, *args],
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env or _TEST_ENV,
    )


def _run_acpc_cheap(
    agent: str,
    *args: str,
    input_text: str | None = None,
    timeout: int = 60,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run acpc prompt with the cheapest model for the given agent."""
    model = TEST_MODELS.get(agent)
    model_args = ("--model", model) if model else ()
    return _run_acpc("prompt", agent, *args, *model_args, input_text=input_text, timeout=timeout, env=env)


def _extract_session_id(stderr: str) -> str | None:
    """Extract session ID from [acpc] session: <id> line."""
    match = re.search(r"\[acpc\] session: (.+)", stderr)
    return match.group(1).strip() if match else None


# ---------------------------------------------------------------------------
# Tier 1: Tests that catch real bugs (mock agent can't find these)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("agent", ["codex", "claude"])
class TestHelloWorld:
    """Minimal smoke test: agent responds to a trivial prompt."""

    def test_hello_response(self, agent: str) -> None:
        """Agent returns a non-empty response."""
        result = _run_acpc_cheap(
            agent, "Respond with exactly one word: hello", "--permissions", "none", "--quiet"
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert len(result.stdout.strip()) > 0, "Expected non-empty response"

    def test_session_id_emitted(self, agent: str) -> None:
        """Session ID and resume hint appear on stderr."""
        result = _run_acpc_cheap(
            agent, "Respond with exactly one word: hi", "--permissions", "none", "--quiet"
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "[acpc] session:" in result.stderr
        assert "[acpc] resume:" in result.stderr


@pytest.mark.parametrize("agent", ["codex", "claude"])
class TestMultiTurn:
    """Multi-turn session: does the agent actually remember context?"""

    def test_context_preserved_with_session_id(self, agent: str) -> None:
        """Agent remembers context when resuming by session ID."""
        model = TEST_MODELS.get(agent)
        model_args = ("--model", model) if model else ()

        # Turn 1: give agent a fact to remember
        r1 = _run_acpc(
            "prompt",
            agent,
            "For this test session, the project name we are working on is 'FizzBuzz'. "
            "Please confirm by responding with just the project name.",
            *model_args,
            "--permissions",
            "none",
            "--quiet",
        )
        assert r1.returncode == 0, f"Turn 1 failed: {r1.stderr}"
        session_id = _extract_session_id(r1.stderr)
        assert session_id, f"No session ID in stderr: {r1.stderr}"

        # Turn 2: ask for the fact back (same model, resume session)
        r2 = _run_acpc(
            "prompt",
            agent,
            "-s",
            session_id,
            "What project name did I mention earlier in this session?",
            *model_args,
            "--permissions",
            "none",
            "--quiet",
        )
        assert r2.returncode == 0, f"Turn 2 failed: {r2.stderr}"
        assert "fizzbuzz" in r2.stdout.lower(), f"Agent forgot context. Got: {r2.stdout!r}"


@pytest.mark.parametrize("agent", ["codex", "claude"])
class TestJsonOutput:
    """JSON output mode produces valid NDJSON with required events."""

    def test_ndjson_structure(self, agent: str) -> None:
        """JSON output is valid NDJSON with session lifecycle events."""
        result = _run_acpc_cheap(
            agent, "Respond with exactly: json test ok", "--permissions", "none", "--json"
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

        lines = []
        for line in result.stdout.strip().split("\n"):
            if line.strip():
                parsed = json.loads(line)
                lines.append(parsed)

        assert len(lines) >= 3, f"Expected at least 3 NDJSON lines, got {len(lines)}"

        acpc_events = [ln.get("acpc") for ln in lines if "acpc" in ln]
        assert "session_started" in acpc_events, f"Missing session_started. Events: {acpc_events}"
        assert "session_ended" in acpc_events, f"Missing session_ended. Events: {acpc_events}"

        updates = [ln.get("sessionUpdate") for ln in lines if "sessionUpdate" in ln]
        assert "agent_message_chunk" in updates, f"No agent_message_chunk. Updates: {updates}"


@pytest.mark.parametrize("agent", ["codex", "claude"])
class TestToolCallPermissions:
    """Permission enforcement with real agent tool calls."""

    def test_permissions_all_allows_read(self, agent: str) -> None:
        """With --permissions all, agent can read files."""
        result = _run_acpc_cheap(
            agent,
            "Read the file pyproject.toml in the current directory "
            "and tell me the project name. Reply with just the name.",
            "--permissions",
            "all",
            "--quiet",
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "acpc" in result.stdout.lower(), f"Expected 'acpc' in response: {result.stdout!r}"

    def test_permissions_none_denies_tools(self, agent: str) -> None:
        """With --permissions none, tool calls are denied."""
        result = _run_acpc_cheap(
            agent,
            "Read the file pyproject.toml and tell me the version.",
            "--permissions",
            "none",
            "--quiet",
        )
        assert result.returncode in (0, 1), f"Unexpected exit: {result.returncode}, {result.stderr}"
        if "[acpc] permission:" in result.stderr:
            assert "deny" in result.stderr.lower()


# ---------------------------------------------------------------------------
# Tier 2: Edge cases and secondary features
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "agent,model,expected",
    [
        ("claude", "haiku", "haiku"),
    ],
)
class TestModelSelection:
    """Verify --model flag changes the model (uses specific models, not TEST_MODELS defaults)."""

    def test_model_identifies_itself(self, agent: str, model: str, expected: str) -> None:
        """Agent reports using the requested model."""
        result = _run_acpc(
            "prompt",
            agent,
            "What AI model are you? Reply with just your model name/identifier, nothing else.",
            "--model",
            model,
            "--permissions",
            "none",
            "--quiet",
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert expected in result.stdout.lower(), (
            f"Expected '{expected}' in model self-identification. Got: {result.stdout!r}"
        )


@pytest.mark.parametrize("agent", ["codex"])
class TestModelSelectionUnsupported:
    """Verify --model with unsupported adapter doesn't crash."""

    def test_unsupported_model_warns(self, agent: str) -> None:
        """Agent that doesn't support set_model still works (warns on stderr)."""
        result = _run_acpc(
            "prompt",
            agent,
            "Respond with exactly one word: hello",
            "--model",
            "o3-mini",
            "--permissions",
            "none",
            "--quiet",
        )
        assert result.returncode in (0, 1), f"Unexpected exit: {result.returncode}"
        if result.returncode == 1:
            assert "error" in result.stderr.lower()


@pytest.mark.parametrize("agent", ["codex", "claude"])
class TestOutputFile:
    """Output file (-o) with real agent."""

    def test_output_written_to_file(self, agent: str, tmp_path: Path) -> None:
        """Agent response is written to output file."""
        out_file = tmp_path / "response.txt"
        result = _run_acpc_cheap(
            agent,
            "Respond with exactly: file output works",
            "--permissions",
            "none",
            "-o",
            str(out_file),
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert out_file.exists(), "Output file not created"
        content = out_file.read_text()
        assert len(content.strip()) > 0, "Output file is empty"


@pytest.mark.parametrize("agent", ["codex", "claude"])
class TestStdinPipe:
    """Stdin pipe input with real agent."""

    def test_stdin_pipe_input(self, agent: str) -> None:
        """Piped stdin is used as prompt text."""
        result = _run_acpc_cheap(
            agent,
            "-",
            "--permissions",
            "none",
            "--quiet",
            input_text="Respond with exactly one word: piped",
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert len(result.stdout.strip()) > 0, "Expected non-empty response from piped input"


@pytest.mark.parametrize("agent", ["codex", "claude"])
class TestInputFile:
    """Input file (--input-file) with real agent."""

    def test_reads_prompt_from_file(self, agent: str, tmp_path: Path) -> None:
        """Prompt text read from file."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Respond with exactly one word: filed")
        result = _run_acpc_cheap(
            agent, "--input-file", str(prompt_file), "--permissions", "none", "--quiet"
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert len(result.stdout.strip()) > 0, "Expected non-empty response from file input"


@pytest.mark.parametrize("agent", ["codex", "claude"])
class TestTimeout:
    """Timeout flag with real agent."""

    def test_timeout_exits_124(self, agent: str) -> None:
        """Very short timeout causes exit 124."""
        result = _run_acpc_cheap(
            agent,
            "Write a detailed 5000-word analysis of the history of computing, "
            "covering every decade from the 1940s to 2020s with specific examples.",
            "--permissions",
            "none",
            "--quiet",
            "--timeout",
            "2",
        )
        assert result.returncode == 124, (
            f"Expected exit 124 (timeout), got {result.returncode}. stderr: {result.stderr}"
        )
