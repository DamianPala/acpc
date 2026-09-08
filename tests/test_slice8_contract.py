"""Contract tests tying the published specification to the live CLI surface."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from acpc import vocab
from acpc.cli import main

MOCK_AGENT_SCRIPT = str(Path(__file__).with_name("mock_agent.py"))
MOCK_ENTRY = f'''
name = "Mock Agent"
command = "{sys.executable} {MOCK_AGENT_SCRIPT}"
install_command = "true"
home = "~/.mock"
home_env = "MOCK_HOME"

[modes]
default = {{ grants = "read", delegates = true }}
'''

STALE_DOCUMENT_PATTERNS = (
    ("removed session verb", re.compile(r"\bacpc (?:stop|rm)\b")),
    ("removed agent creation verb", re.compile(r"\bagents init\b")),
    ("removed check spelling", re.compile(r"\bagents --check\b")),
    ("removed log flag", re.compile(r"--tail\b")),
    ("removed liveness state", re.compile(r"\borphaned\b")),
    ("removed run preview flag", re.compile(r"\brun --resolve\b")),
    (
        "status collection spelling",
        re.compile(r"`(?:acpc )?status \[id\]`|`acpc status --(?:json|limit|plain)"),
    ),
)

LEGACY_HINT_CASES = (
    (("agents",), "acpc agents list"),
    (("agents", "mock"), "acpc agents get"),
    (("agents", "mock", "--models"), "acpc agents get"),
    (("agents", "mock", "--commands"), "acpc agents get"),
    (("agents", "--models"), "acpc agents get <name> --models"),
    (("agents", "--commands"), "acpc agents get <name> --commands"),
    (("agents", "--check"), "acpc agents check"),
    (("agents", "init", "variant", "--extends", "mock"), "agents create"),
    (("skills",), "acpc skills list"),
    (("skills", "adapter-bringup"), "acpc skills get"),
    (("rm", "abcd"), "acpc delete"),
    (("stop", "abcd"), "acpc cancel"),
    (("log", "abcd", "--tail", "1"), "--limit"),
    (("log", "abcd", "-f"), "--follow"),
    (("run", "mock", "hello", "-o", "answer.md"), "--output-file"),
    (("run", "mock", "hello", "--resolve"), "acpc resolve <agent>"),
    (("continue", "abcd", "hello", "--model", "x"), "acpc run"),
    (("continue", "abcd", "hello", "--effort", "high"), "acpc run"),
    (("continue", "abcd", "hello", "--mode", "x"), "acpc run"),
    (("continue", "abcd", "hello", "--cwd", "."), "acpc run"),
    (("continue", "abcd", "hello", "--home", "."), "acpc run"),
    (("continue", "abcd", "hello", "--name", "x"), "acpc run"),
    (("continue", "abcd", "hello", "--resolve"), "acpc run"),
    (("continue", "abcd", "hello", "--dry-run"), "acpc resolve <agent>"),
    (("status",), "acpc list"),
    (("status", "--limit", "1"), "acpc list"),
    (("status", "--plain", "--limit", "1"), "acpc list"),
)


def invoke(cli: CliRunner, *args: str):
    return cli.invoke(main, list(args), catch_exceptions=False)


@pytest.fixture
def cli() -> CliRunner:
    return CliRunner()


def test_spec_command_index_matches_schema_both_ways(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli: CliRunner
) -> None:
    """The SPEC index cannot silently lose or invent a command path."""
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "state"))
    spec = Path(__file__).parents[1] / "SPEC.md"
    text = spec.read_text(encoding="utf-8")
    section_match = re.search(
        r"^## Command surface\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL
    )
    assert section_match is not None
    rows = re.findall(
        r"^\| `([^`]+)` \| `(read_only|idempotent|non_idempotent)` \|$",
        section_match.group(1),
        re.MULTILINE,
    )
    spec_commands = dict(rows)
    assert spec_commands, "SPEC command index table was not parsed"

    result = invoke(cli, "schema")
    assert result.exit_code == vocab.EXIT_OK, result.stderr
    schema_commands = {
        item["name"]: item["effects"] for item in json.loads(result.stdout)["commands"]
    }
    assert spec_commands == schema_commands


def test_live_docs_contain_no_removed_surface_spellings() -> None:
    documents = "\n".join(
        path.read_text(encoding="utf-8") for path in (Path("SPEC.md"), Path("README.md"))
    )
    for label, pattern in STALE_DOCUMENT_PATTERNS:
        assert pattern.search(documents) is None, label


def test_removed_spellings_name_the_current_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli: CliRunner
) -> None:
    root = tmp_path / "state"
    agents = root / "agents"
    agents.mkdir(parents=True)
    (agents / "mock.toml").write_text(MOCK_ENTRY, encoding="utf-8")
    monkeypatch.setenv("ACPC_HOME", str(root))
    for args, hint in LEGACY_HINT_CASES:
        result = invoke(cli, *args)
        assert result.exit_code == vocab.EXIT_USAGE, (args, result.output)
        assert hint in result.stderr, (args, result.stderr)
