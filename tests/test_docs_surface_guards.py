"""Contract tests tying the published specification to the live CLI surface."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

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
    (
        "bare agents group spelling",
        re.compile(r"\bacpc agents\b(?!\s+(?:list|get|check)\b)"),
    ),
    (
        "bare skills group spelling",
        re.compile(r"\bacpc skills\b(?!\s+(?:list|get)\b)"),
    ),
    ("removed agent creation verb", re.compile(r"\bagents init\b")),
    ("removed check spelling", re.compile(r"\bagents --check\b")),
    ("removed liveness state", re.compile(r"\borphaned\b")),
    ("removed run preview flag", re.compile(r"\brun --resolve\b")),
    (
        "status collection spelling",
        re.compile(r"`(?:acpc )?status \[id\]`|`acpc status --(?:json|limit|plain)"),
    ),
)

MIGRATION_HINT_CASES = (
    ("acpc agents", ("agents",)),
    ("acpc agents NAME", ("agents", "mock")),
    ("acpc agents NAME --models", ("agents", "mock", "--models")),
    ("acpc agents NAME --commands", ("agents", "mock", "--commands")),
    ("acpc agents --models", ("agents", "--models")),
    ("acpc agents --commands", ("agents", "--commands")),
    ("acpc agents --check [NAME]", ("agents", "--check")),
    (
        "acpc agents init NAME --extends BASE",
        ("agents", "init", "variant", "--extends", "mock"),
    ),
    ("acpc skills", ("skills",)),
    ("acpc skills NAME", ("skills", "adapter-bringup")),
    ("acpc rm ID", ("rm", "abcd")),
    ("acpc stop ID", ("stop", "abcd")),
    ("acpc log ID -f", ("log", "abcd", "-f")),
    ("acpc run AGENT PROMPT --dry-run", ("run", "mock", "hello", "--dry-run")),
    ("acpc continue ID PROMPT --dry-run", ("continue", "abcd", "hello", "--dry-run")),
    ("acpc continue ID PROMPT --resolve", ("continue", "abcd", "hello", "--resolve")),
    ("-o FILE", ("run", "mock", "hello", "-o", "answer.md")),
    ("--output FILE", ("run", "mock", "hello", "--output", "answer.md")),
    ("acpc status", ("status",)),
    ("acpc status --json", ("status", "--json")),
    ("acpc status --limit N", ("status", "--limit", "1")),
    ("acpc status --plain --limit N", ("status", "--plain", "--limit", "1")),
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


def test_public_docs_and_bundled_skills_contain_no_removed_surface_spellings() -> None:
    skill_docs = sorted(Path("src/acpc/data/skills").rglob("*.md"))
    documents = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (Path("SPEC.md"), Path("README.md"), *skill_docs)
    )
    for label, pattern in STALE_DOCUMENT_PATTERNS:
        assert pattern.search(documents) is None, label


def test_migration_rows_name_the_current_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli: CliRunner
) -> None:
    root = tmp_path / "state"
    agents = root / "agents"
    agents.mkdir(parents=True)
    (agents / "mock.toml").write_text(MOCK_ENTRY, encoding="utf-8")
    monkeypatch.setenv("ACPC_HOME", str(root))
    migration_rows: dict[str, str] = {}
    for line in Path("MIGRATION.md").read_text(encoding="utf-8").splitlines():
        cells = line.split("|")
        if len(cells) < 4:
            continue
        old_calls = re.findall(r"`([^`]+)`", cells[1])
        current_match = re.search(r"`([^`]+)`", cells[2])
        if current_match:
            for old_call in old_calls:
                migration_rows[old_call] = current_match.group(1)
    assert {old for old, _args in MIGRATION_HINT_CASES} <= migration_rows.keys()
    for old_call, args in MIGRATION_HINT_CASES:
        current_call = migration_rows[old_call]
        current_path = " ".join(
            token
            for token in current_call.removeprefix("acpc ").split()
            if not token.startswith(("-", "[")) and token.upper() != token
        )
        if not current_path:
            current_path = current_call.split()[0].strip("`")
        assert current_path
        result = invoke(cli, *args)
        assert result.exit_code == vocab.EXIT_USAGE, (args, result.output)
        assert current_path in result.stderr, (old_call, current_call, result.stderr)


def test_schema_publishes_parser_conflicts_in_flag_descriptions(cli: CliRunner) -> None:
    def flags(path: tuple[str, ...]) -> dict[str, dict[str, Any]]:
        detail = json.loads(invoke(cli, "schema", *path).stdout)
        return {flag["name"]: flag for flag in detail["flags"]}

    index = json.loads(invoke(cli, "schema").stdout)
    global_flags = {flag["name"]: flag for flag in index["global_flags"]}
    agent_flags = flags(("agents", "get"))
    assert "--commands" in agent_flags["models"]["description"]
    assert "--models" in agent_flags["commands"]["description"]

    log_flags = flags(("log",))
    assert "--json" in log_flags["prose"]["description"]
    assert "--prose" in global_flags["json"]["description"]
    assert "--prose" in log_flags["format"]["description"]
    assert "--follow" in log_flags["wait-new"]["description"]
    assert "--wait-new" in log_flags["follow"]["description"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    (
        (
            ("agents", "get", "mock", "--models", "--commands"),
            "--models and --commands are mutually exclusive",
        ),
        (
            ("log", "missing", "--prose", "--format", "ndjson"),
            "--prose and --json are mutually exclusive views",
        ),
        (
            ("log", "missing", "--wait-new", "--follow"),
            "--wait-new and --follow are mutually exclusive",
        ),
    ),
)
def test_parser_rejects_each_published_mutual_exclusion(
    cli: CliRunner, arguments: tuple[str, ...], message: str
) -> None:
    result = invoke(cli, *arguments)

    assert result.exit_code == vocab.EXIT_USAGE
    assert message in result.stderr
