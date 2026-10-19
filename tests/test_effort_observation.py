"""Adapter observations through real CLI turns and isolated cache files."""

import json
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from acpc.cli import main

MODELS = ["mock-opus-5", "mock-sonnet-5", "mock-haiku-4-5"]
LEVELS = {MODELS[0]: ["low", "high"], MODELS[1]: ["medium", "high"], MODELS[2]: []}
SCRIPT = Path(__file__).with_name("mock_agent.py")
ENTRY = f'''
command = "{sys.executable} {SCRIPT}"
model = "mock-sonnet-5"
permissions = "read"
[modes]
default = {{ grants = "read", delegates = true }}
[effort_by_model]
mock-opus-5 = ["low", "high", "xhigh"]
mock-sonnet-5 = ["medium", "high"]
mock-haiku-4-5 = []
unavailable = ["high"]
'''


@pytest.fixture(autouse=True)
def state_root(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="s5-") as temporary:
        root = Path(temporary)
        (root / "agents").mkdir()
        (root / "agents/mock.toml").write_text(ENTRY)
        monkeypatch.setenv("ACPC_HOME", temporary)
        monkeypatch.setenv("ACPC_MOCK_EFFORTS_BY_MODEL", json.dumps(LEVELS))
        monkeypatch.delenv("ACPC_CEILING", raising=False)
        yield root


def invoke(*args: str) -> Any:
    return CliRunner().invoke(main, list(args), catch_exceptions=False)


def rows(root: Path) -> dict[str, Any]:
    return json.loads((root / "cache/mock/advertised.json").read_text())["advertised"]["efforts"]


@pytest.mark.parametrize(
    ("model", "effort", "current"),
    [(MODELS[2], None, None), (MODELS[0], "high", "high"), (MODELS[0], None, "low")],
)
def test_direct_captures_adapter_choices(
    state_root: Path, model: str, effort: str | None, current: str | None
) -> None:
    args = ["run", "mock", "settings", "--model", model, "--json"]
    if effort:
        args += ["--effort", effort]
    result = invoke(*args)
    assert result.exit_code == 0, result.output
    observed = rows(state_root)[model]
    assert observed == {
        "levels": LEVELS[model],
        "current": current,
        "observed_at": observed["observed_at"],
    }
    assert isinstance(observed["observed_at"], float)
    assert rows(state_root)[MODELS[1]]["current"] == "medium"
    if effort is None:
        assert json.loads(result.stdout)["answer"].split("/")[-1] == "0"


@pytest.mark.parametrize("effort", [None, "high"])
def test_response_uses_requested_id_even_when_adapter_reports_alias(
    state_root: Path, monkeypatch: pytest.MonkeyPatch, effort: str | None
) -> None:
    monkeypatch.setenv("ACPC_MOCK_MODEL_ALIASES", "1")
    args = ["run", "mock", "settings", "--model", MODELS[1]]
    result = invoke(*args, *(["--effort", effort] if effort else []))
    assert result.exit_code == 0, result.output
    observed = rows(state_root)
    assert observed[MODELS[1]]["current"] == (effort or "medium")
    assert observed["sonnet"]["current"] == "medium"


def test_continue_rejected_during_setup_publishes_rows(
    state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = invoke("run", "mock", "settings", "--model", MODELS[0], "--effort", "high", "--json")
    assert first.exit_code == 0, first.output
    session = json.loads(first.stdout)["session_id"]
    monkeypatch.setenv("ACPC_MOCK_EFFORTS_BY_MODEL", json.dumps({**LEVELS, MODELS[0]: ["low"]}))
    second = invoke("continue", session, "settings", "--json")
    assert second.exit_code != 0
    assert rows(state_root)[MODELS[0]]["levels"] == ["low"]


def test_rejected_effort_publishes_already_observed_model(state_root: Path) -> None:
    result = invoke("run", "mock", "settings", "--model", MODELS[0], "--effort", "xhigh")
    assert result.exit_code != 0
    assert rows(state_root)[MODELS[0]]["levels"] == ["low", "high"]
    assert rows(state_root)[MODELS[0]]["current"] == "low"


def test_updates_are_keyed_by_the_adapters_current_model(
    state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPC_MOCK_CONFIG_UPDATE", "1")
    monkeypatch.setenv("ACPC_MOCK_MODEL_ALIASES", "1")
    result = invoke("run", "mock", "settings", "--model", MODELS[0], "--effort", "high")
    assert result.exit_code == 0, result.output
    observed = rows(state_root)
    assert observed["opus"]["levels"] == ["low", "high"]
    assert observed["opus"]["current"] == "low"
    assert observed[MODELS[0]]["current"] == "high"


@pytest.mark.parametrize("background", [False, True])
def test_daemon_and_worker_capture_the_same_rows(
    state_root: Path, live_daemon: None, monkeypatch: pytest.MonkeyPatch, background: bool
) -> None:
    # A bounded observer on the direct route hands execution to a detached worker.
    if background:
        monkeypatch.setenv("ACPC_DIRECT_WORKER", "1")
    args = ["run", "mock", "settings", "--model", MODELS[0], "--effort", "high", "--json"]
    if background:
        args.extend(["--timeout", "0.1", "--name", "observed-worker"])
    result = invoke(*args)
    assert result.exit_code in (0, 124), result.output
    session = "observed-worker" if background else json.loads(result.stdout)["session_id"]
    if background:
        waited = invoke("wait", session, "--json")
        assert waited.exit_code == 0, waited.output
    deadline = time.monotonic() + 5
    while not (state_root / "cache/mock/advertised.json").exists():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    observed = rows(state_root)
    assert observed[MODELS[0]]["levels"] == ["low", "high"]
    assert observed[MODELS[0]]["current"] == "high"
    assert observed[MODELS[1]]["current"] == "medium"
    if not background:
        continued = invoke("continue", session, "settings", "--json")
        assert continued.exit_code == 0, continued.output
        assert rows(state_root)[MODELS[0]]["current"] == "high"


@pytest.mark.parametrize("load_config", [False, True])
def test_restore_options_do_not_change_capture(
    state_root: Path, monkeypatch: pytest.MonkeyPatch, load_config: bool
) -> None:
    if load_config:
        monkeypatch.setenv("ACPC_MOCK_LOAD_CONFIG", "1")
    first = invoke("run", "mock", "settings", "--model", MODELS[0], "--effort", "high", "--json")
    assert first.exit_code == 0, first.output
    session = json.loads(first.stdout)["session_id"]
    second = invoke("continue", session, "settings", "--json")
    assert second.exit_code == 0, second.output
    assert rows(state_root)[MODELS[0]]["current"] == "high"


@pytest.mark.parametrize("command", ["check", "get"])
def test_probe_fills_all_rows_and_views_show_them(state_root: Path, command: str) -> None:
    result = invoke("agents", command, "mock", "--json")
    assert result.exit_code == 0, result.output
    observed = rows(state_root)
    assert list(observed) == [MODELS[1], MODELS[0], MODELS[2]]
    assert {model: row["levels"] for model, row in observed.items()} == LEVELS
    assert "unavailable" not in observed
    if command == "check":
        item = json.loads(result.stdout)["items"][0]
        assert item == {"agent": "mock", "ok": True, "models": 3, "efforts": 3}
    detail = invoke("agents", "get", "mock", "--json")
    assert json.loads(detail.stdout)["advertised"]["efforts"] == observed
    text = invoke("agents", "get", "mock", "--format", "text").stdout
    assert "efforts      3 ·" in text
    assert f"{MODELS[2]} none" in text
    models = invoke("agents", "get", "mock", "--models", "--format", "text").stdout
    for model, row in observed.items():
        levels = " · ".join(row["levels"]) or "none"
        assert f"  {model}  {levels}  current {row['current'] or '·'}  observed now" in models


def test_daemon_rejected_effort_keeps_observations(state_root: Path, live_daemon: None) -> None:
    result = invoke("run", "mock", "settings", "--model", MODELS[0], "--effort", "xhigh")
    assert result.exit_code != 0
    assert rows(state_root)[MODELS[0]]["current"] == "low"
    assert rows(state_root)[MODELS[0]]["levels"] == ["low", "high"]


def test_probe_candidate_order_and_forced_refresh(
    state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = state_root / "agents/mock.toml"
    entry.write_text(ENTRY + '\n[presets]\nextra = { model = "mock-extra", effort = "high" }\n')
    wire = state_root / "wire.ndjson"
    monkeypatch.setenv("ACPC_MOCK_WIRE_FILE", str(wire))
    result = invoke("agents", "check", "mock", "--json")
    assert result.exit_code == 0, result.output
    requests = [json.loads(line) for line in wire.read_text().splitlines()]
    switches = [
        request["params"]["value"]
        for request in requests
        if request["method"] == "session/set_config_option"
        and request["params"]["configId"] == "model"
    ]
    assert switches == [MODELS[1], MODELS[1], MODELS[0], MODELS[2], "mock-extra", "unavailable"]
    before = json.loads((state_root / "cache/mock/advertised.json").read_text())
    result = invoke("agents", "check", "mock", "--json")
    assert result.exit_code == 0, result.output
    after = json.loads((state_root / "cache/mock/advertised.json").read_text())
    assert after["cached_at"] > before["cached_at"]
    for model in MODELS:
        assert (
            after["advertised"]["efforts"][model]["observed_at"]
            > (before["advertised"]["efforts"][model]["observed_at"])
        )


def test_probe_does_not_switch_models_through_set_model(
    state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (state_root / "agents/mock.toml").write_text(
        ENTRY.replace('permissions = "read"', 'permissions = "read"\nmodel_via = "set_model"')
    )
    wire = state_root / "wire.ndjson"
    monkeypatch.setenv("ACPC_MOCK_WIRE_FILE", str(wire))
    result = invoke("agents", "check", "mock", "--json")
    assert json.loads(result.stdout)["items"][0]["ok"] is True, result.output
    methods = [json.loads(line)["method"] for line in wire.read_text().splitlines()]
    assert methods.count("session/set_model") == 1
    assert list(rows(state_root)) == [MODELS[1]]


def test_damaged_cached_rows_are_dropped_on_read(state_root: Path) -> None:
    good = {"levels": ["low"], "current": None, "observed_at": 1.0}
    cache_dir = state_root / "cache/mock"
    cache_dir.mkdir(parents=True)
    efforts = {"good": good, "text": "broken", "nolevels": {"current": None, "observed_at": 1.0}}
    document = {"modes": [], "models": [], "commands": [], "efforts": efforts}
    (cache_dir / "advertised.json").write_text(
        json.dumps({"cached_at": time.time(), "advertised": document})
    )
    detail = invoke("agents", "get", "mock", "--json")
    assert json.loads(detail.stdout)["advertised"]["efforts"] == {"good": good}
    models = invoke("agents", "get", "mock", "--models", "--format", "text")
    assert "  good  low  current ·  observed" in models.stdout


@pytest.mark.parametrize("detection", ["category", "custom-id", "default-id"])
def test_effort_detection_and_independent_snapshot(state_root: Path, detection: str) -> None:
    from acp.schema import SessionConfigOptionSelect, SessionConfigSelectOption

    from acpc import cache
    from acpc.client import AcpcClient
    from acpc.permissions import PermissionLevel
    from acpc.transcript import Transcript

    config_id = "reasoning_effort" if detection == "default-id" else "effort"
    configured_id = "fallback" if detection == "category" else config_id
    client = AcpcClient(
        Transcript(state_root / "events.ndjson"),
        PermissionLevel.READ,
        effort_config_id=None if detection == "default-id" else configured_id,
    )
    effort = SessionConfigOptionSelect(
        id=config_id,
        name="Effort",
        type="select",
        current_value="ultra",
        category="thought_level" if detection == "category" else None,
        options=[SessionConfigSelectOption(name=value, value=value) for value in ["ultra", "low"]],
    )
    options = [effort]
    if detection == "category":
        options.insert(
            0,
            SessionConfigOptionSelect(
                id="fallback",
                name="Other",
                type="select",
                current_value="wrong",
                options=[SessionConfigSelectOption(name="Wrong", value="wrong")],
            ),
        )
    client.capture_config_options(options, model_key="sent-model")
    snapshot = client.advertised
    snapshot["efforts"]["sent-model"]["levels"].append("mutated")
    snapshot["efforts"]["sent-model"]["current"] = "mutated"
    cache.refresh_advertised("mock", client.advertised)
    assert rows(state_root)["sent-model"]["levels"] == ["ultra", "low"]
    assert rows(state_root)["sent-model"]["current"] == "ultra"
