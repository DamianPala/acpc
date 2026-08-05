"""Behavioral tests for the ``agents`` family and ``install``."""

import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from acpc import cache, vocab
from acpc.cli import main

MOCK_AGENT_SCRIPT = str(Path(__file__).with_name("mock_agent.py"))

MOCK_ENTRY = f'''
name = "Mock Agent"
command = "{sys.executable} {MOCK_AGENT_SCRIPT}"
install_command = "true"
home = "~/.mock"
home_env = "MOCK_HOME"
bypass_modes = ["yolo"]
efforts = ["low", "medium", "high", "xhigh"]

[presets]
fast = {{ model = "mock-haiku-4-5", effort = "high" }}
standard = {{ model = "mock-sonnet-5", effort = "high" }}
max = {{ model = "mock-opus-5", effort = "xhigh" }}
'''

BUILDER_ENTRY = """
extends = "mock"
description = "Implements a task against a plan."
model = "mock-opus-5"
effort = "xhigh"
permissions = "write"
"""

PHANTOM_ENTRY = """
command = "definitely-not-installed-phantom-xyz"
install_command = "false"
"""


@pytest.fixture(autouse=True)
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "state"
    agents = root / "agents"
    agents.mkdir(parents=True)
    (agents / "mock.toml").write_text(MOCK_ENTRY, encoding="utf-8")
    (agents / "builder.toml").write_text(BUILDER_ENTRY, encoding="utf-8")
    (agents / "phantom.toml").write_text(PHANTOM_ENTRY, encoding="utf-8")
    monkeypatch.setenv("ACPC_HOME", str(root))
    return root


@pytest.fixture
def cli() -> CliRunner:
    return CliRunner()


def invoke(cli: CliRunner, *args: str):
    return cli.invoke(main, list(args), catch_exceptions=False)


def test_agents_list_shows_variant_delta(cli: CliRunner) -> None:
    result = invoke(cli, "agents")

    assert result.exit_code == vocab.EXIT_OK
    assert "  builder" in result.stdout
    assert "mock-opus-5" in result.stdout


def test_agents_list_shows_missing_install_hint(cli: CliRunner) -> None:
    result = invoke(cli, "agents")

    assert result.exit_code == vocab.EXIT_OK
    assert "missing → acpc install phantom" in result.stdout


def test_agents_list_has_no_cache_footer(cli: CliRunner) -> None:
    result = invoke(cli, "agents")

    assert result.exit_code == vocab.EXIT_OK
    assert "cached" not in result.stdout


def test_variant_detail_shows_provenance(cli: CliRunner) -> None:
    result = invoke(cli, "agents", "builder")

    assert result.exit_code == vocab.EXIT_OK
    assert "model        mock-opus-5 (entry)" in result.stdout


def test_variant_detail_points_to_parent_catalog(cli: CliRunner) -> None:
    result = invoke(cli, "agents", "builder")

    assert result.exit_code == vocab.EXIT_OK
    assert "modes/models/commands: acpc agents mock" in result.stdout
    assert "modes        " not in result.stdout


def test_agents_list_abbreviates_variant_home(cli: CliRunner, state_root: Path) -> None:
    variant_home = Path.home() / "builder-home"
    (state_root / "agents" / "builder.toml").write_text(
        BUILDER_ENTRY + f"\nhome = {json.dumps(str(variant_home))}\n",
        encoding="utf-8",
    )

    result = invoke(cli, "agents")

    assert result.exit_code == vocab.EXIT_OK
    assert str(Path("~") / variant_home.relative_to(Path.home())) in result.stdout
    assert str(variant_home) not in result.stdout


def test_adapter_detail_probes_on_a_cache_miss(cli: CliRunner, state_root: Path) -> None:
    result = invoke(cli, "agents", "mock")

    assert result.exit_code == vocab.EXIT_OK
    assert "yolo" in result.stdout
    assert "cached" in result.stdout
    assert (state_root / "cache" / "mock" / "advertised.json").exists()


def test_named_models_view_prints_full_presets(cli: CliRunner) -> None:
    result = invoke(cli, "agents", "mock", "--models")

    assert result.exit_code == vocab.EXIT_OK
    assert "fast" in result.stdout and "mock-haiku-4-5" in result.stdout


def test_models_overview_lists_variants(cli: CliRunner) -> None:
    result = invoke(cli, "agents", "--models")

    assert result.exit_code == vocab.EXIT_OK
    assert "mock" in result.stdout and "builder" in result.stdout


def test_variant_models_view_delegates_to_parent_catalog(cli: CliRunner) -> None:
    cache.refresh_advertised("mock", {"models": ["parent-model"]}, now=100.0)

    result = invoke(cli, "agents", "builder", "--models")

    assert result.exit_code == vocab.EXIT_OK
    assert "parent-model" in result.stdout


def test_variant_commands_view_reads_parent_cache(cli: CliRunner) -> None:
    cache.refresh_advertised(
        "mock",
        {"commands": [{"name": "parent-command", "description": "From the parent."}]},
        now=100.0,
    )

    result = invoke(cli, "agents", "builder", "--commands")

    assert result.exit_code == vocab.EXIT_OK
    assert "/parent-command" in result.stdout


def test_adapter_detail_caps_catalogs_at_three_items(cli: CliRunner) -> None:
    cache.refresh_advertised(
        "mock",
        {
            "modes": ["default", "acceptEdits", "plan", "yolo"],
            "models": ["model-1", "model-2", "model-3", "model-4"],
            "commands": [
                {"name": "command-1", "description": "One."},
                {"name": "command-2", "description": "Two."},
                {"name": "command-3", "description": "Three."},
                {"name": "command-4", "description": "Four."},
            ],
        },
        now=100.0,
    )

    result = invoke(cli, "agents", "mock")

    assert result.exit_code == vocab.EXIT_OK
    assert "modes        4 · default · acceptEdits · plan · yolo" in result.stdout
    assert "models       4 · model-1 · model-2 · model-3 · …" in result.stdout
    assert "commands     4 · /command-1 · /command-2 · /command-3 · …" in result.stdout


def test_named_models_view_accepts_options_before_the_name(cli: CliRunner) -> None:
    result = invoke(cli, "agents", "--models", "mock")

    assert result.exit_code == vocab.EXIT_OK
    assert "mock-haiku-4-5" in result.stdout


def test_commands_view_truncates_sentences(cli: CliRunner) -> None:
    invoke(cli, "agents", "mock")
    result = invoke(cli, "agents", "mock", "--commands")

    assert result.exit_code == vocab.EXIT_OK
    assert "/plan" in result.stdout
    assert "The complete explanation" not in result.stdout


def test_commands_view_names_full_cache_file(cli: CliRunner) -> None:
    invoke(cli, "agents", "mock")
    result = invoke(cli, "agents", "mock", "--commands")

    assert result.exit_code == vocab.EXIT_OK
    assert "commands.md" in result.stdout


def test_check_reports_a_missing_named_adapter_with_exit_one(cli: CliRunner) -> None:
    result = invoke(cli, "agents", "phantom", "--check")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    assert "phantom" in result.stdout
    assert "failed" in result.stdout


def test_agents_init_writes_the_requested_variant_fields(cli: CliRunner, state_root: Path) -> None:
    result = invoke(
        cli,
        "agents",
        "init",
        "smoke-variant",
        "--extends",
        "mock",
        "--model",
        "mock-opus-5",
        "--effort",
        "xhigh",
        "--permissions",
        "write",
        "--home",
        "~/.variant",
    )

    assert result.exit_code == vocab.EXIT_OK
    created = (state_root / "agents" / "smoke-variant.toml").read_text(encoding="utf-8")
    assert 'extends = "mock"' in created
    assert 'permissions = "write"' in created


def test_install_returns_one_for_a_failed_definition_command(cli: CliRunner) -> None:
    result = invoke(cli, "install", "phantom")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    assert "install phantom failed" in result.stderr


def test_install_unknown_agent_returns_usage_exit_two(cli: CliRunner) -> None:
    result = invoke(cli, "install", "unknown-agent-xyz")

    assert result.exit_code == vocab.EXIT_USAGE


def test_agents_json_keeps_cache_metadata_off_stdout(cli: CliRunner) -> None:
    result = invoke(cli, "agents", "mock", "--json")

    assert result.exit_code == vocab.EXIT_OK
    payload = json.loads(result.stdout)
    assert payload["agent"] == "mock"
    assert "cached" not in result.stdout
    assert "cached" in result.stderr


def test_install_json_is_one_object(cli: CliRunner) -> None:
    result = invoke(cli, "install", "mock", "--json")

    assert result.exit_code == vocab.EXIT_OK
    assert json.loads(result.stdout) == {"agent": "mock", "ok": True, "returncode": 0}
