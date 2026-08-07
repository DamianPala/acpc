"""Behavioral tests for the ``agents`` family and ``install``."""

import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from acpc import cache, vocab
from acpc.cli import main
from acpc.registry import AgentRegistry

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
mode = "plan"
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


def test_agents_list_labels_variant_columns_and_adapts_to_long_values(
    cli: CliRunner, state_root: Path
) -> None:
    long_entry = "variant-name-longer-than-the-old-column"
    long_model = "vendor/model-with-a-deliberately-long-identifier"
    (state_root / "agents" / f"{long_entry}.toml").write_text(
        f'''
extends = "mock"
model = "{long_model}"
effort = "xhigh"
permissions = "write"
home = "~/.home-longer-than-the-old-column"
''',
        encoding="utf-8",
    )

    result = invoke(cli, "agents")
    assert result.exit_code == vocab.EXIT_OK
    lines = result.stdout.splitlines()
    header = next(line for line in lines if line.startswith("  ENTRY"))
    row = next(line for line in lines if line.startswith(f"  {long_entry}"))
    headings = ("ENTRY", "MODEL", "EFFORT", "PERMISSIONS", "HOME", "DESCRIPTION")

    assert header.split() == list(headings)
    assert lines.count(header) == 1
    assert [row.index(value) for value in (long_entry, long_model, "xhigh", "write")] == [
        header.index(value) for value in headings[:4]
    ]
    assert not any(line.startswith("ENTRY") for line in lines)


def test_agents_views_render_present_and_absent_descriptions(cli: CliRunner) -> None:
    listed = invoke(cli, "agents")

    assert listed.exit_code == vocab.EXIT_OK
    builder_row = next(line for line in listed.stdout.splitlines() if line.startswith("  builder"))
    mock_row = next(line for line in listed.stdout.splitlines() if line.startswith("mock"))
    assert builder_row.endswith("Implements a task against a plan.")
    assert mock_row.endswith("installed")
    assert "description" not in mock_row

    builder_detail = invoke(cli, "agents", "builder")
    assert "description  Implements a task against a plan." in builder_detail.stdout

    mock_detail = invoke(cli, "agents", "mock")
    assert "description  " not in mock_detail.stdout

    list_json = json.loads(invoke(cli, "agents", "--json").stdout)
    descriptions = {item["name"]: item["description"] for item in list_json["agents"]}
    assert descriptions["builder"] == "Implements a task against a plan."
    assert descriptions["mock"] is None

    builder_json = json.loads(invoke(cli, "agents", "builder", "--json").stdout)
    assert builder_json["description"] == "Implements a task against a plan."
    mock_json = json.loads(invoke(cli, "agents", "mock", "--json").stdout)
    assert mock_json["description"] is None


def test_agents_list_truncates_but_detail_and_json_keep_full_description(
    cli: CliRunner, state_root: Path
) -> None:
    full_description = (
        "A deliberately long description with   repeated whitespace\n"
        "and enough words to exceed the list view budget while preserving its full detail value."
    )
    adapter_description = (
        "An adapter description with   enough words to exercise the same list-only truncation path."
    )
    (state_root / "agents" / "mock.toml").write_text(
        MOCK_ENTRY.replace(
            "[presets]",
            f'description = """{adapter_description}"""\n\n[presets]',
        ),
        encoding="utf-8",
    )
    (state_root / "agents" / "builder.toml").write_text(
        BUILDER_ENTRY.replace(
            'description = "Implements a task against a plan."',
            f'description = """{full_description}"""',
        ),
        encoding="utf-8",
    )

    listed = invoke(cli, "agents")
    builder_row = next(line for line in listed.stdout.splitlines() if line.startswith("  builder"))
    mock_row = next(line for line in listed.stdout.splitlines() if line.startswith("mock"))
    builder_snippet = (
        "A deliberately long description with repeated whitespace and enough words to..."
    )
    mock_snippet = "An adapter description with enough words to exercise the same list-only..."
    assert builder_row.endswith(builder_snippet)
    assert mock_row.endswith(mock_snippet)
    assert full_description not in listed.stdout
    assert len(builder_snippet) <= 80
    assert len(mock_snippet) <= 80

    detail = invoke(cli, "agents", "builder")
    assert f"description  {full_description}" in detail.stdout

    list_payload = json.loads(invoke(cli, "agents", "--json").stdout)
    descriptions = {item["name"]: item["description"] for item in list_payload["agents"]}
    assert descriptions["builder"] == full_description
    assert descriptions["mock"] == adapter_description

    detail_payload = json.loads(invoke(cli, "agents", "builder", "--json").stdout)
    assert detail_payload["description"] == full_description


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


def test_agents_detail_shows_mode_and_its_source(cli: CliRunner) -> None:
    result = invoke(cli, "agents", "builder")

    assert result.exit_code == vocab.EXIT_OK
    assert "effort       xhigh (entry)" in result.stdout
    assert "mode         plan (entry)" in result.stdout
    assert "permissions  write (entry)" in result.stdout

    payload = json.loads(invoke(cli, "agents", "builder", "--json").stdout)
    assert payload["resolved"]["mode"] == {"value": "plan", "source": "entry"}


def test_agents_detail_renders_an_unset_mode_with_its_source(cli: CliRunner) -> None:
    result = invoke(cli, "agents", "mock")

    assert result.exit_code == vocab.EXIT_OK
    assert "mode         · (unset)" in result.stdout
    payload = json.loads(invoke(cli, "agents", "mock", "--json").stdout)
    assert payload["resolved"]["mode"] == {"value": None, "source": "unset"}


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


def test_named_models_view_labels_and_aligns_the_preset_table(cli: CliRunner) -> None:
    result = invoke(cli, "agents", "mock", "--models")

    assert result.exit_code == vocab.EXIT_OK
    lines = result.stdout.splitlines()
    header = lines[0]
    fast = next(line for line in lines if "fast" in line)

    assert header.split() == ["presets", "TIER", "MODEL", "EFFORT"]
    assert [fast.index(value) for value in ("fast", "mock-haiku-4-5", "high")] == [
        header.index(value) for value in ("TIER", "MODEL", "EFFORT")
    ]
    assert not any(line.split() == ["models"] for line in lines)
    json_result = invoke(cli, "agents", "mock", "--models", "--json")
    assert (
        json_result.stdout == json.dumps(json.loads(json_result.stdout), ensure_ascii=False) + "\n"
    )


def test_a_preset_without_an_effort_renders_as_absent_in_both_model_views(
    cli: CliRunner, state_root: Path
) -> None:
    """Absence renders as absence: a model whose vendor offers no effort knob
    shows `·`, not a level it is not actually running at."""
    (state_root / "agents" / "noeffort.toml").write_text(
        f'command = "{sys.executable} {MOCK_AGENT_SCRIPT}"\n'
        "\n[presets]\n"
        'fast = { model = "mock-haiku-4-5" }\n'
        'standard = { model = "mock-sonnet-5", effort = "high" }\n',
        encoding="utf-8",
    )

    named = invoke(cli, "agents", "noeffort", "--models")
    overview = invoke(cli, "agents", "--models")

    assert named.exit_code == vocab.EXIT_OK
    assert overview.exit_code == vocab.EXIT_OK
    header = next(line for line in named.stdout.splitlines() if "TIER" in line)
    fast = next(line for line in named.stdout.splitlines() if "mock-haiku-4-5" in line)
    standard = next(line for line in named.stdout.splitlines() if "mock-sonnet-5" in line)
    assert fast.index("·") == header.index("EFFORT")
    assert standard.index("high") == header.index("EFFORT")
    assert any("mock-haiku-4-5" in line and "·" in line for line in overview.stdout.splitlines())

    named_json = json.loads(invoke(cli, "agents", "noeffort", "--models", "--json").stdout)
    assert named_json["presets"]["fast"] == {"model": "mock-haiku-4-5", "effort": None}
    assert named_json["presets"]["standard"] == {"model": "mock-sonnet-5", "effort": "high"}


def test_models_overview_lists_variants(cli: CliRunner) -> None:
    result = invoke(cli, "agents", "--models")

    assert result.exit_code == vocab.EXIT_OK
    assert "mock" in result.stdout and "builder" in result.stdout


def test_models_overview_labels_and_aligns_both_of_its_tables(
    cli: CliRunner, state_root: Path
) -> None:
    long_entry = "variant-name-longer-than-the-old-column"
    long_model = "vendor/model-with-a-deliberately-long-identifier"
    (state_root / "agents" / f"{long_entry}.toml").write_text(
        f'''
extends = "mock"
model = "{long_model}"
effort = "xhigh"
''',
        encoding="utf-8",
    )

    result = invoke(cli, "agents", "--models")

    assert result.exit_code == vocab.EXIT_OK
    lines = result.stdout.splitlines()
    block = lines[lines.index("mock") :]
    preset_header = next(line for line in block if line.startswith("  presets"))
    fast = next(line for line in block if "fast" in line)
    variant_header = next(line for line in block if line.startswith("  variants"))
    variant_row = next(line for line in block if long_entry in line)

    assert preset_header.split() == ["presets", "TIER", "MODEL", "EFFORT"]
    assert [fast.index(value) for value in ("fast", "mock-haiku-4-5", "high")] == [
        preset_header.index(value) for value in ("TIER", "MODEL", "EFFORT")
    ]
    assert variant_header.split() == ["variants", "ENTRY", "MODEL", "EFFORT"]
    assert [variant_row.index(value) for value in (long_entry, long_model, "xhigh")] == [
        variant_header.index(value) for value in ("ENTRY", "MODEL", "EFFORT")
    ]


def test_variant_models_view_delegates_to_parent_catalog(cli: CliRunner) -> None:
    cache.refresh_advertised("mock", {"models": ["parent-model"]}, clock=lambda: 100.0)

    result = invoke(cli, "agents", "builder", "--models")

    assert result.exit_code == vocab.EXIT_OK
    assert "parent-model" in result.stdout


def test_variant_commands_view_reads_parent_cache(cli: CliRunner) -> None:
    cache.refresh_advertised(
        "mock",
        {"commands": [{"name": "parent-command", "description": "From the parent."}]},
        clock=lambda: 100.0,
    )

    result = invoke(cli, "agents", "builder", "--commands")

    assert result.exit_code == vocab.EXIT_OK
    assert "/parent-command" in result.stdout


def test_adapter_detail_caps_models_and_commands_but_never_modes(cli: CliRunner) -> None:
    cache.refresh_advertised(
        "mock",
        {
            "modes": ["default", "acceptEdits", "plan", "yolo", "extra-mode"],
            "models": ["model-1", "model-2", "model-3", "model-4"],
            "commands": [
                {"name": "command-1", "description": "One."},
                {"name": "command-2", "description": "Two."},
                {"name": "command-3", "description": "Three."},
                {"name": "command-4", "description": "Four."},
            ],
        },
        clock=lambda: 100.0,
    )

    result = invoke(cli, "agents", "mock")

    assert result.exit_code == vocab.EXIT_OK
    assert "modes        5 · default · acceptEdits · plan · yolo · extra-mode" in result.stdout
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


def test_commands_view_aligns_descriptions_past_the_longest_name(cli: CliRunner) -> None:
    long_name = "command-name-longer-than-the-old-column"
    cache.refresh_advertised(
        "mock",
        {
            "commands": [
                {"name": "short", "description": "Short one."},
                {"name": long_name, "description": "Long one."},
            ]
        },
        clock=lambda: 100.0,
    )

    result = invoke(cli, "agents", "mock", "--commands")

    assert result.exit_code == vocab.EXIT_OK
    lines = result.stdout.splitlines()
    short_row = next(line for line in lines if line.startswith("/short"))
    long_row = next(line for line in lines if line.startswith(f"/{long_name}"))

    assert short_row.index("Short one.") == long_row.index("Long one.")


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


def test_check_applies_the_resolved_options_so_a_bad_config_fails_it(
    cli: CliRunner, state_root: Path
) -> None:
    """--check must fail on a config the adapter rejects (a wrong effort id
    made every claude run die while --check kept saying ok)."""
    (state_root / "agents" / "brokenfx.toml").write_text(
        MOCK_ENTRY.replace("[presets]", 'effort_config_id = "bogus_effort_id"\n\n[presets]'),
        encoding="utf-8",
    )

    result = invoke(cli, "agents", "brokenfx", "--check")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    assert "brokenfx failed" in result.stdout
    assert "Unknown config option: bogus_effort_id" in result.stdout


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
        "--mode",
        "plan",
        "--permissions",
        "write",
        "--home",
        "~/.variant",
    )

    assert result.exit_code == vocab.EXIT_OK
    created = (state_root / "agents" / "smoke-variant.toml").read_text(encoding="utf-8")
    assert 'extends = "mock"' in created
    assert 'permissions = "write"' in created
    assert 'mode = "plan"' in created
    assert AgentRegistry(state_root / "agents").resolve("smoke-variant").mode == "plan"


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
    assert result.stdout == json.dumps(payload, ensure_ascii=False) + "\n"
    assert payload["agent"] == "mock"
    assert "cached" not in result.stdout
    assert "cached" in result.stderr


def test_install_json_is_one_object(cli: CliRunner) -> None:
    result = invoke(cli, "install", "mock", "--json")

    assert result.exit_code == vocab.EXIT_OK
    assert json.loads(result.stdout) == {"agent": "mock", "ok": True, "returncode": 0}
