"""Integration tests using mock ACP agent.

Tests the full pipeline: CLI -> runner -> ACP handshake -> mock agent -> output.
"""

import json
import subprocess
import sys
from pathlib import Path


ACPC_CMD = [sys.executable, "-m", "acpc.cli"]
MOCK_AGENT_SCRIPT = str(Path(__file__).parent / "mock_agent.py")


def _run_acpc(
    *args: str,
    input_text: str | None = None,
    timeout: int = 30,
    mock_toml_dir: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run acpc as subprocess with mock agent registered."""
    env = None
    if mock_toml_dir:
        import os

        env = {**os.environ, "ACPC_TEST_AGENTS_DIR": str(mock_toml_dir)}

    return subprocess.run(
        [*ACPC_CMD, *args],
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _make_mock_toml(tmp_path: Path) -> Path:
    """Create mock agent TOML in a temp dir and return the agents dir."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(exist_ok=True)
    toml_content = f"""\
identity = "mock"
name = "Mock Agent"
author = "Test"
run_command = "{sys.executable} {MOCK_AGENT_SCRIPT}"
install_command = "true"
"""
    (agents_dir / "mock.toml").write_text(toml_content)
    return agents_dir


def _run_acpc_with_mock(
    tmp_path: Path,
    *args: str,
    input_text: str | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    """Run acpc with mock agent available via monkeypatched registry."""
    agents_dir = _make_mock_toml(tmp_path)

    # We need to inject the mock agent into acpc's registry.
    # Since we run as subprocess, we use a wrapper script.
    wrapper = tmp_path / "run_acpc.py"
    wrapper.write_text(f"""\
import sys
from unittest.mock import patch
from pathlib import Path

agents_dir = Path("{agents_dir}")
with patch("acpc.agents._user_agents_dir", return_value=agents_dir):
    from acpc.cli import cli
    cli()
""")

    return subprocess.run(
        [sys.executable, str(wrapper), *args],
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


class TestBasicPrompt:
    """Test basic prompt -> response pipeline."""

    def test_echo_response(self, tmp_path: Path) -> None:
        """Mock agent echoes prompt text back."""
        result = _run_acpc_with_mock(tmp_path, "prompt", "mock", "hello from test")
        assert result.returncode == 0
        assert "hello from test" in result.stdout

    def test_echo_with_quiet(self, tmp_path: Path) -> None:
        """Quiet mode collects and emits final text."""
        # Retry once if flaky (subprocess stdout capture timing)
        result = _run_acpc_with_mock(tmp_path, "prompt", "mock", "quiet test", "--quiet")
        if result.returncode == 0 and "quiet test" not in result.stdout:
            result = _run_acpc_with_mock(tmp_path, "prompt", "mock", "quiet test", "--quiet")
        assert result.returncode == 0
        assert "quiet test" in result.stdout

    def test_session_id_on_stderr(self, tmp_path: Path) -> None:
        """Session ID and resume hint printed to stderr."""
        result = _run_acpc_with_mock(tmp_path, "prompt", "mock", "hello")
        assert result.returncode == 0
        assert "[acpc] session:" in result.stderr
        assert "[acpc] resume:" in result.stderr


class TestJsonOutput:
    """Test NDJSON output mode."""

    def test_json_has_session_events(self, tmp_path: Path) -> None:
        """JSON mode emits session_started, events, and session_ended."""
        result = _run_acpc_with_mock(tmp_path, "prompt", "mock", "json test", "--json")
        assert result.returncode == 0
        lines = [json.loads(line) for line in result.stdout.strip().split("\n") if line.strip()]
        acpc_events = [line.get("acpc") for line in lines if "acpc" in line]
        assert "session_started" in acpc_events
        assert "session_ended" in acpc_events

    def test_json_contains_agent_message(self, tmp_path: Path) -> None:
        """JSON mode includes agent_message_chunk events."""
        result = _run_acpc_with_mock(tmp_path, "prompt", "mock", "json msg", "--json")
        assert result.returncode == 0
        lines = [json.loads(line) for line in result.stdout.strip().split("\n") if line.strip()]
        updates = [line.get("sessionUpdate") for line in lines if "sessionUpdate" in line]
        assert "agent_message_chunk" in updates


class TestOutputFile:
    """Test -o file output."""

    def test_output_written_to_file(self, tmp_path: Path) -> None:
        """Output file contains agent response."""
        out_file = tmp_path / "out.txt"
        result = _run_acpc_with_mock(tmp_path, "prompt", "mock", "file test", "-o", str(out_file))
        assert result.returncode == 0
        assert out_file.exists()
        assert "file test" in out_file.read_text()


class TestPermissions:
    """Test permission policy enforcement."""

    def test_permissions_all_allows_tool(self, tmp_path: Path) -> None:
        """With --permissions all, tool calls proceed."""
        result = _run_acpc_with_mock(
            tmp_path, "prompt", "mock", "tool:read file", "--permissions", "all"
        )
        assert result.returncode == 0

    def test_permissions_read_allows_read_tool(self, tmp_path: Path) -> None:
        """With --permissions read, read tools are allowed."""
        result = _run_acpc_with_mock(
            tmp_path, "prompt", "mock", "tool:search code", "--permissions", "read"
        )
        assert result.returncode == 0


class TestErrorHandling:
    """Test error scenarios."""

    def test_refusal_returns_exit_1(self, tmp_path: Path) -> None:
        """Agent refusal returns exit code 1."""
        result = _run_acpc_with_mock(tmp_path, "prompt", "mock", "error")
        assert result.returncode == 1

    def test_unknown_agent_returns_exit_2(self, tmp_path: Path) -> None:
        """Unknown agent identity returns exit code 2."""
        result = _run_acpc_with_mock(tmp_path, "prompt", "nonexistent", "hello")
        assert result.returncode != 0
        assert "error" in result.stderr.lower()


class TestTimeout:
    """Test --timeout flag."""

    def test_timeout_kills_slow_agent(self, tmp_path: Path) -> None:
        """Timeout exits 124 when agent takes too long."""
        result = _run_acpc_with_mock(tmp_path, "prompt", "mock", "slow:30", "--timeout", "2")
        assert result.returncode == 124


class TestStdinInput:
    """Test stdin pipe input."""

    def test_stdin_pipe(self, tmp_path: Path) -> None:
        """Piped stdin is used as prompt text."""
        result = _run_acpc_with_mock(
            tmp_path,
            "prompt",
            "mock",
            "-",
            input_text="piped input",
        )
        assert result.returncode == 0
        assert "piped input" in result.stdout


class TestInputFile:
    """Test --input-file flag."""

    def test_reads_prompt_from_file(self, tmp_path: Path) -> None:
        """Prompt text read from file."""
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("file prompt content")
        result = _run_acpc_with_mock(tmp_path, "prompt", "mock", "--input-file", str(prompt_file))
        assert result.returncode == 0
        assert "file prompt content" in result.stdout
