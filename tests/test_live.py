"""Live end-to-end tests with real ACP agents.

These tests call real agents (codex, claude) and cost API credits.
Skipped by default. Run explicitly:

    uv run pytest tests/test_live.py -v

Or with marker:

    uv run pytest -m live -v
"""

import shutil
import subprocess

import pytest

# Use the installed entry point, not python -m (no __main__.py)
_acpc_bin = shutil.which("acpc")
assert _acpc_bin is not None, "acpc not installed in venv. Run: uv sync"
ACPC = [_acpc_bin]

pytestmark = pytest.mark.live


def _run_acpc(
    *args: str,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    """Run acpc as subprocess."""
    return subprocess.run(
        [*ACPC, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.mark.parametrize("agent", ["codex", "claude"])
class TestHelloWorld:
    """Minimal smoke test: agent responds to a trivial prompt."""

    def test_hello_response(self, agent: str) -> None:
        """Agent returns a non-empty response to 'say hello'."""
        result = _run_acpc(
            "prompt", agent,
            "Respond with exactly one word: hello",
            "--permissions", "none",
            "--quiet",
            timeout=60,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert len(result.stdout.strip()) > 0, "Expected non-empty response"

    def test_session_id_emitted(self, agent: str) -> None:
        """Session ID appears on stderr."""
        result = _run_acpc(
            "prompt", agent,
            "Respond with exactly one word: hi",
            "--permissions", "none",
            "--quiet",
            timeout=60,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "[acpc] session:" in result.stderr
        assert "[acpc] resume:" in result.stderr
