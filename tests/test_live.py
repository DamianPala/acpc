"""Live end-to-end tests with real ACP agents.

These tests call real agents (codex, claude) and cost API credits.
Skipped by default. Run explicitly:

    uv run pytest tests/test_live.py -v -m live

Estimated run time: ~3 minutes. Estimated cost: <$0.05.
"""

import json
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


def _run_acpc(
    *args: str,
    input_text: str | None = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    """Run acpc as subprocess."""
    return subprocess.run(
        [*ACPC, *args],
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


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
        result = _run_acpc(
            "prompt",
            agent,
            "Respond with exactly one word: hello",
            "--permissions",
            "none",
            "--quiet",
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert len(result.stdout.strip()) > 0, "Expected non-empty response"

    def test_session_id_emitted(self, agent: str) -> None:
        """Session ID and resume hint appear on stderr."""
        result = _run_acpc(
            "prompt",
            agent,
            "Respond with exactly one word: hi",
            "--permissions",
            "none",
            "--quiet",
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "[acpc] session:" in result.stderr
        assert "[acpc] resume:" in result.stderr


@pytest.mark.parametrize("agent", ["codex", "claude"])
class TestMultiTurn:
    """Multi-turn session: does the agent actually remember context?"""

    def test_context_preserved_with_session_id(self, agent: str) -> None:
        """Agent remembers context when resuming by session ID."""
        # Turn 1: tell agent a secret
        r1 = _run_acpc(
            "prompt",
            agent,
            "I will tell you a secret code. The code is: ACPC7742. "
            "Acknowledge by responding with just 'ok'.",
            "--permissions",
            "none",
            "--quiet",
        )
        assert r1.returncode == 0, f"Turn 1 failed: {r1.stderr}"
        session_id = _extract_session_id(r1.stderr)
        assert session_id, f"No session ID in stderr: {r1.stderr}"

        # Turn 2: ask for the secret back
        r2 = _run_acpc(
            "prompt",
            agent,
            "-s",
            session_id,
            "What was the secret code I told you? Reply with just the code.",
            "--permissions",
            "none",
            "--quiet",
        )
        assert r2.returncode == 0, f"Turn 2 failed: {r2.stderr}"
        assert "ACPC7742" in r2.stdout, f"Agent forgot context. Got: {r2.stdout!r}"


@pytest.mark.parametrize("agent", ["codex", "claude"])
class TestJsonOutput:
    """JSON output mode produces valid NDJSON with required events."""

    def test_ndjson_structure(self, agent: str) -> None:
        """JSON output is valid NDJSON with session lifecycle events."""
        result = _run_acpc(
            "prompt",
            agent,
            "Respond with exactly: json test ok",
            "--permissions",
            "none",
            "--json",
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

        lines = []
        for line in result.stdout.strip().split("\n"):
            if line.strip():
                parsed = json.loads(line)  # must be valid JSON
                lines.append(parsed)

        assert len(lines) >= 3, f"Expected at least 3 NDJSON lines, got {len(lines)}"

        # Must have session_started and session_ended meta-events
        acpc_events = [ln.get("acpc") for ln in lines if "acpc" in ln]
        assert "session_started" in acpc_events, f"Missing session_started. Events: {acpc_events}"
        assert "session_ended" in acpc_events, f"Missing session_ended. Events: {acpc_events}"

        # Must have at least one agent_message_chunk
        updates = [ln.get("sessionUpdate") for ln in lines if "sessionUpdate" in ln]
        assert "agent_message_chunk" in updates, f"No agent_message_chunk. Updates: {updates}"


@pytest.mark.parametrize("agent", ["codex", "claude"])
class TestToolCallPermissions:
    """Permission enforcement with real agent tool calls."""

    def test_permissions_all_allows_read(self, agent: str) -> None:
        """With --permissions all, agent can read files."""
        result = _run_acpc(
            "prompt",
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
        result = _run_acpc(
            "prompt",
            agent,
            "Read the file pyproject.toml and tell me the version.",
            "--permissions",
            "none",
            "--quiet",
        )
        # Agent should still return (maybe explaining it can't), not crash
        assert result.returncode in (0, 1), f"Unexpected exit: {result.returncode}, {result.stderr}"
        # Permission denials should appear on stderr
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
    # codex-acp returns "Internal error" on set_session_model (unsupported)
)
class TestModelSelection:
    """Verify --model flag changes the model."""

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
        # Should either succeed (ignoring model) or fail gracefully
        # codex-acp currently returns Internal error, so exit 1 is acceptable
        assert result.returncode in (0, 1), f"Unexpected exit: {result.returncode}"
        if result.returncode == 1:
            assert "error" in result.stderr.lower()


@pytest.mark.parametrize("agent", ["codex", "claude"])
class TestOutputFile:
    """Output file (-o) with real agent."""

    def test_output_written_to_file(self, agent: str, tmp_path: Path) -> None:
        """Agent response is written to output file."""
        out_file = tmp_path / "response.txt"
        result = _run_acpc(
            "prompt",
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
        result = _run_acpc(
            "prompt",
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
        result = _run_acpc(
            "prompt",
            agent,
            "--input-file",
            str(prompt_file),
            "--permissions",
            "none",
            "--quiet",
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert len(result.stdout.strip()) > 0, "Expected non-empty response from file input"


@pytest.mark.parametrize("agent", ["codex", "claude"])
class TestTimeout:
    """Timeout flag with real agent."""

    def test_timeout_exits_124(self, agent: str) -> None:
        """Very short timeout causes exit 124."""
        result = _run_acpc(
            "prompt",
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
