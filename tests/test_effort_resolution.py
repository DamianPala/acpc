"""Selectable effort, vendor defaults and continuation through the public surfaces."""

import json
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from acpc import runner
from acpc.cli import main
from acpc.registry import AgentRegistry
from acpc.vocab import EFFORT_VALUES

MODEL = "mock-sonnet-5"
SCRIPT = Path(__file__).with_name("mock_agent.py")
ENTRY = f'''command = "{sys.executable} {SCRIPT}"
model = "{MODEL}"
permissions = "read"
[modes]
default = {{ grants = "read", delegates = true }}
'''


@pytest.fixture(autouse=True)
def state_root(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="s6-") as temporary:
        root = Path(temporary)
        (root / "agents").mkdir()
        (root / "agents/mock.toml").write_text(ENTRY)
        monkeypatch.setenv("ACPC_HOME", temporary)
        monkeypatch.delenv("ACPC_CEILING", raising=False)
        yield root


def invoke(*args: str) -> Any:
    return CliRunner().invoke(main, list(args), catch_exceptions=False)


def observe(root: Path, adapter: str, model: str, levels: Any) -> None:
    directory = root / "cache" / adapter
    directory.mkdir(parents=True, exist_ok=True)
    document = {
        "cached_at": time.time(),
        "advertised": {
            "modes": [],
            "models": [],
            "commands": [],
            "efforts": {model: {"levels": levels, "current": None, "observed_at": time.time()}},
        },
    }
    (directory / "advertised.json").write_text(json.dumps(document))


@pytest.mark.parametrize(
    ("advertised", "operator", "values", "bounds"),
    [
        (["high", "low"], None, ["low", "high"], ["scale", "advertised"]),
        (None, ["high", "low"], ["low", "high"], ["scale", "entry"]),
        (["low", "high", "ultra"], ["high", "medium"], ["high"], ["scale", "advertised", "entry"]),
        ([], None, [], ["scale", "advertised"]),
        (["default", "low"], None, ["low"], ["scale", "advertised"]),
        (["ultra"], None, ["ultra"], ["scale", "advertised"]),
        (None, [], [], ["scale", "entry"]),
        (None, None, list(EFFORT_VALUES), ["scale"]),
        ("broken", None, list(EFFORT_VALUES), ["scale"]),
        ([1, "low"], None, list(EFFORT_VALUES), ["scale"]),
    ],
)
def test_allowlist_intersects_only_applicable_rows(
    state_root: Path, advertised: Any, operator: Any, values: list[str], bounds: list[str]
) -> None:
    if advertised is not None:
        observe(state_root, "mock", MODEL, advertised)
    if operator is not None:
        with (state_root / "agents/mock.toml").open("a") as file:
            file.write(f"\n[effort_by_model]\n{MODEL} = {json.dumps(operator)}\n")
    result = invoke("resolve", "mock", "--json")
    assert result.exit_code == 0, result.output
    effort = json.loads(result.stdout)["resolved"]["effort"]
    assert effort == {
        "value": None,
        "source": "vendor default",
        "levels": {"values": values, "bounded_by": bounds},
    }
    assert "default" not in effort["levels"]["values"]
    assert "-- effort:" not in result.stderr


@pytest.mark.parametrize("observed", [False, True])
def test_shipped_row_yields_to_observation(state_root: Path, observed: bool) -> None:
    if observed:
        observe(state_root, "codex", "gpt-5.6-sol", ["ultra"])
    result = invoke(
        "resolve", "codex", "--model", "gpt-5.6-sol", "--permissions", "execute", "--json"
    )
    effort = json.loads(result.stdout)["resolved"]["effort"]
    assert effort["levels"] == {
        "values": ["ultra"] if observed else ["low", "medium", "high", "xhigh"],
        "bounded_by": ["scale", "advertised" if observed else "shipped"],
    }
    accepted = invoke(
        "resolve",
        "codex",
        "--model",
        "gpt-5.6-sol",
        "--effort",
        "ultra",
        "--permissions",
        "execute",
    )
    assert accepted.exit_code == (0 if observed else 2), accepted.output


@pytest.mark.parametrize("operator", [False, True])
def test_operator_exclusion_names_its_file(state_root: Path, operator: bool) -> None:
    observe(state_root, "mock", MODEL, ["low", "ultra"])
    if operator:
        (state_root / "agents/child.toml").write_text(
            f'extends = "mock"\n[effort_by_model]\n{MODEL} = ["low"]\n'
        )
    name = "child" if operator else "mock"
    result = invoke("resolve", name, "--effort", "ultra", "--json")
    assert result.exit_code == (2 if operator else 0), result.output
    if operator:
        error = json.loads(result.stderr.splitlines()[-1])["error"]
        assert error["kind"] == "invalid_input"
        assert error["message"] == (
            f"child: effort 'ultra' is not available for {MODEL}; allowed: low "
            f"(row in {state_root / 'agents/child.toml'})"
        )


def test_empty_observation_and_invalid_scale_refusals(state_root: Path) -> None:
    observe(state_root, "mock", MODEL, [])
    for level, message in (
        (
            "high",
            f"mock: effort 'high' is not available for {MODEL}; allowed: none (advertised by mock)",
        ),
        (
            "bogus",
            "mock: unsupported effort 'bogus'; supported levels: " + ", ".join(EFFORT_VALUES),
        ),
    ):
        result = invoke("resolve", "mock", "--effort", level, "--json")
        assert result.exit_code == 2, result.output
        assert json.loads(result.stderr.splitlines()[-1])["error"]["message"] == message
        assert json.loads(result.stderr.splitlines()[-1])["error"]["kind"] == "invalid_input"


@pytest.mark.parametrize("damaged", [False, True])
def test_unknown_model_uses_scale_and_vendor_rejection(state_root: Path, damaged: bool) -> None:
    if damaged:
        directory = state_root / "cache/mock"
        directory.mkdir(parents=True)
        (directory / "advertised.json").write_text('{"broken":')
    preview = invoke("resolve", "mock", "--effort", "ultra", "--json")
    assert preview.exit_code == 0, preview.output
    assert json.loads(preview.stdout)["resolved"]["effort"]["levels"] == {
        "values": list(EFFORT_VALUES),
        "bounded_by": ["scale"],
    }
    result = invoke("run", "mock", "settings", "--effort", "ultra", "--json")
    assert result.exit_code != 0
    error = json.loads(result.stderr.splitlines()[-1])["error"]
    assert error["kind"] == "operation_failed"
    output = json.loads(result.stdout)
    assert output["failure"]["class"] == "adapter_error"
    assert "unsupported effort: ultra" in output["answer"]
    assert f"-- effort: no effort row for {MODEL}; sending ultra as given" in result.stderr


@pytest.mark.parametrize("row", ["none", "entry", "advertised"])
@pytest.mark.parametrize("effort", [None, "high"])
def test_warning_only_for_unbounded_sent_effort(state_root: Path, row: str, effort: str | None):
    if row == "entry":
        with (state_root / "agents/mock.toml").open("a") as file:
            file.write(f'\n[effort_by_model]\n{MODEL} = ["high"]\n')
    elif row == "advertised":
        observe(state_root, "mock", MODEL, ["high"])
    result = invoke("run", "mock", "settings", *(["--effort", effort] if effort else []))
    assert result.exit_code == 0, result.output
    assert ("-- effort:" in result.stderr) == (row == "none" and effort is not None)


def test_views_publish_levels_but_session_does_not(state_root: Path) -> None:
    observe(state_root, "mock", MODEL, ["low", "high"])
    for args in [("resolve", "mock"), ("agents", "get", "mock")]:
        result = invoke(*args, "--json")
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["resolved"]["effort"] == {
            "value": None,
            "source": "vendor default",
            "levels": {"values": ["low", "high"], "bounded_by": ["scale", "advertised"]},
        }
        text = invoke(*args, "--format", "text")
        assert (
            "effort       · (vendor default)  levels low, high (scale ∩ advertised)" in text.stdout
        )
    observe(state_root, "mock", MODEL, [])
    assert "levels none (scale ∩ advertised)" in invoke("resolve", "mock").stdout
    started = invoke("run", "mock", "settings", "--json")
    assert started.exit_code == 0, started.output
    session = json.loads(started.stdout)["session_id"]
    meta = json.loads((state_root / "sessions" / session / "meta.json").read_text())
    assert meta["resolution"]["resolved"]["effort"] == {"value": None, "source": "vendor default"}


@pytest.mark.parametrize("explicit_policy", [False, True])
@pytest.mark.parametrize("initial_model", [None, MODEL])
@pytest.mark.parametrize("effort", [None, "high"])
def test_continue_freezes_effort_direct(
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    effort: str | None,
    *,
    initial_model: str | None,
    explicit_policy: bool,
) -> None:
    check_continue(state_root, monkeypatch, effort, initial_model, explicit_policy)


@pytest.mark.parametrize("explicit_policy", [False, True])
@pytest.mark.parametrize("initial_model", [None, MODEL])
@pytest.mark.parametrize("effort", [None, "high"])
def test_continue_freezes_effort_daemon(
    state_root: Path,
    live_daemon: None,
    monkeypatch: pytest.MonkeyPatch,
    effort: str | None,
    *,
    initial_model: str | None,
    explicit_policy: bool,
) -> None:
    check_continue(state_root, monkeypatch, effort, initial_model, explicit_policy)


def check_continue(
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    effort: str | None,
    initial_model: str | None,
    explicit_policy: bool,
) -> None:
    if initial_model is None:
        (state_root / "agents/mock.toml").write_text(ENTRY.replace(f'model = "{MODEL}"\n', ""))
    wire = state_root / "wire.ndjson"
    monkeypatch.setenv("ACPC_MOCK_WIRE_FILE", str(wire))
    started = invoke("run", "mock", "settings", "--json", *(["--effort", effort] if effort else []))
    assert started.exit_code == 0, started.output
    session = json.loads(started.stdout)["session_id"]
    (state_root / "agents/mock.toml").write_text(
        ENTRY.replace("permissions =", 'effort = "high"\npermissions =')
    )
    observe(state_root, "mock", MODEL, ["low"])
    continued = invoke(
        "continue",
        session,
        "settings",
        "--json",
        *(["--permissions", "read"] if explicit_policy else []),
    )
    assert continued.exit_code == 0, continued.output
    assert json.loads(continued.stdout)["answer"].split("/")[1] == (effort or "-")
    calls = [json.loads(line) for line in wire.read_text().splitlines()]
    sent = [
        call["params"]["value"]
        for call in calls
        if call["method"] == "session/set_config_option"
        and call["params"]["configId"] == "reasoning_effort"
    ]
    assert sent == (["high", "high"] if effort else [])


def test_cli_effort_argv_and_target_identity(state_root: Path) -> None:
    (state_root / "agents/grokish.toml").write_text('extends = "grok"\nmodel = "grok-4.7"\n')
    entry = AgentRegistry().resolve("grokish")
    first = entry.resolve_call(permissions="execute")
    second = entry.resolve_call(model="grok-4.7", permissions="execute")
    high = entry.resolve_call(effort="high", permissions="execute")
    assert "--reasoning-effort" not in first.command
    assert "--reasoning-effort" not in second.command
    assert "--reasoning-effort" in high.command
    assert runner.call_target(first) == runner.call_target(second)
    assert runner.call_target(first) != runner.call_target(high)


@pytest.mark.parametrize(
    ("entry_values", "options", "model", "effort", "source"),
    [
        ('model = "mock-opus-5"', [], "mock-opus-5", None, "vendor default"),
        ("", [], MODEL, "high", "adapter default"),
        ('model = "mock-opus-5"', ["--model", "standard"], MODEL, "high", "entry"),
        ('effort = "high"', ["--model", "mock-opus-5"], "mock-opus-5", None, "vendor default"),
        (
            'effort = "high"',
            ["--model", "mock-opus-5", "--effort", "low"],
            "mock-opus-5",
            "low",
            "call flag",
        ),
        ('model = "mock-opus-5"\neffort = "high"', [], "mock-opus-5", "high", "entry"),
    ],
)
def test_preset_effort_travels_only_with_its_model(
    state_root: Path,
    *,
    entry_values: str,
    options: list[str],
    model: str,
    effort: str | None,
    source: str,
) -> None:
    (state_root / "agents/mock.toml").write_text(
        ENTRY.replace(f'model = "{MODEL}"', entry_values)
        + f'\n[presets]\nstandard = {{ model = "{MODEL}", effort = "high" }}\n'
    )
    result = invoke("resolve", "mock", *options, "--json")
    assert result.exit_code == 0, result.output
    resolved = json.loads(result.stdout)["resolved"]
    assert resolved["model"]["value"] == model
    assert resolved["effort"]["value"] == effort
    assert resolved["effort"]["source"].startswith(source)


@pytest.mark.parametrize("overlay", [False, True])
def test_variant_row_overrides_parent_and_keeps_operator_origin(
    state_root: Path,
    overlay: bool,
) -> None:
    observe(state_root, "codex", "gpt-5.6-sol", ["low", "high", "ultra"])
    if overlay:
        (state_root / "agents/codex.toml").write_text(
            '[effort_by_model]\n"gpt-5.6-sol" = ["high"]\n'
        )
    (state_root / "agents/child.toml").write_text(
        'extends = "codex"\nmodel = "gpt-5.6-sol"\n'
        '[effort_by_model]\n"gpt-5.6-sol" = ["low", "ultra"]\n'
    )
    (state_root / "agents/grandchild.toml").write_text('extends = "child"\n')
    result = invoke("resolve", "grandchild", "--permissions", "execute", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["resolved"]["effort"]["levels"] == {
        "values": ["low", "ultra"],
        "bounded_by": ["scale", "advertised", "entry"],
    }
    refused = invoke("resolve", "grandchild", "--permissions", "execute", "--effort", "high")
    assert refused.exit_code == 2
    assert f"row in {state_root / 'agents/child.toml'}" in refused.stderr


def test_an_override_of_a_shipped_adapter_is_an_operator_row(state_root: Path) -> None:
    observe(state_root, "codex", "gpt-5.6-sol", ["low", "high", "ultra"])
    (state_root / "agents/codex.toml").write_text('[effort_by_model]\n"gpt-5.6-sol" = ["high"]\n')
    args = ("resolve", "codex", "--model", "gpt-5.6-sol", "--permissions", "execute")
    levels = json.loads(invoke(*args, "--json").stdout)["resolved"]["effort"]["levels"]
    assert levels == {"values": ["high"], "bounded_by": ["scale", "advertised", "entry"]}
    refused = invoke(*args, "--effort", "low")
    assert refused.exit_code == 2
    assert f"(row in {state_root / 'agents/codex.toml'})" in refused.stderr


def test_cold_agents_get_publishes_the_levels_its_probe_observed(state_root: Path) -> None:
    payload = json.loads(invoke("agents", "get", "mock", "--json").stdout)
    assert MODEL in payload["advertised"]["efforts"]
    assert payload["resolved"]["effort"]["levels"]["bounded_by"] == ["scale", "advertised"]


def test_no_resolved_model_has_only_the_scale(state_root: Path) -> None:
    (state_root / "agents/mock.toml").write_text(
        ENTRY.replace(f'model = "{MODEL}"\n', "") + f'\n[effort_by_model]\n{MODEL} = ["low"]\n'
    )
    observe(state_root, "mock", MODEL, [])
    result = invoke("resolve", "mock", "--effort", "ultra", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["resolved"]["effort"]["levels"] == {
        "values": list(EFFORT_VALUES),
        "bounded_by": ["scale"],
    }


def test_cli_effort_route_spawns_without_an_effort_flag(state_root: Path) -> None:
    (state_root / "agents/mock.toml").write_text(
        ENTRY.replace("permissions =", 'effort_via = "cli"\nmodel_via = "set_model"\npermissions =')
    )
    # mock_agent exits on an effort flag outside its scale (a valid injected level would
    # still start); the argv itself is pinned by test_cli_effort_argv_and_target_identity.
    result = invoke("run", "mock", "settings", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["answer"].split("/")[-1] == "0"
