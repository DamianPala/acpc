"""Behavioral coverage for D8 success schemas and real JSON payloads."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from acpc import cli as cli_module
from acpc import sessions, vocab
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

O4_KEYWORDS = {"type", "enum", "properties", "required", "items"}
COMMANDS_WITH_OUTPUT = {
    "agents check",
    "agents create",
    "agents delete",
    "agents get",
    "agents list",
    "cancel",
    "continue",
    "daemon status",
    "daemon stop",
    "delete",
    "install",
    "log",
    "probe",
    "prune",
    "run",
    "skills get",
    "skills list",
    "status",
    "steer",
    "wait",
}


@pytest.fixture(autouse=True)
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "state"
    agents = root / "agents"
    agents.mkdir(parents=True)
    (agents / "mock.toml").write_text(MOCK_ENTRY, encoding="utf-8")
    (agents / "variant.toml").write_text('extends = "mock"\n', encoding="utf-8")
    monkeypatch.setenv("ACPC_HOME", str(root))
    return root


@pytest.fixture
def cli() -> CliRunner:
    return CliRunner()


def invoke(cli: CliRunner, *args: str):
    return cli.invoke(main, list(args), catch_exceptions=False)


def schema_for(cli: CliRunner, command: str) -> dict[str, Any]:
    result = invoke(cli, "schema", *command.split())
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)["output"]


def validate_json(value: Any, contract: Mapping[str, Any], path: str = "$") -> None:
    """Validate the O4 subset and reject undeclared object properties."""
    if not contract:
        return
    assert set(contract) <= O4_KEYWORDS, path
    assert "type" in contract, path
    expected = contract["type"]
    nullable = isinstance(expected, list) and "null" in expected
    types = (
        [item for item in expected if item != "null"] if isinstance(expected, list) else [expected]
    )
    if value is None:
        assert nullable, path
        return
    assert any(_matches_type(value, item) for item in types), (path, expected, value)
    if "enum" in contract:
        assert value in contract["enum"], (path, value)
    if isinstance(value, dict):
        properties = contract["properties"]
        assert set(value) <= set(properties), (path, set(value) - set(properties))
        for name in contract["required"]:
            assert name in value, (path, name)
        for name, child in properties.items():
            if name in value:
                validate_json(value[name], child, f"{path}.{name}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            validate_json(item, contract["items"], f"{path}[{index}]")


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    raise AssertionError(f"unknown schema type {expected!r}")


def _assert_o4_schema(contract: Mapping[str, Any], path: str = "$") -> None:
    if not contract:
        return
    assert set(contract) <= O4_KEYWORDS, path
    assert "type" in contract, path
    expected = contract["type"]
    types = (
        [item for item in expected if item != "null"] if isinstance(expected, list) else [expected]
    )
    if "object" in types:
        assert "properties" in contract, path
        assert "required" in contract, path
        assert set(contract["required"]) <= set(contract["properties"]), path
        for name, child in contract["properties"].items():
            _assert_o4_schema(child, f"{path}.{name}")
    if "array" in types:
        assert "items" in contract, path
        _assert_o4_schema(contract["items"], f"{path}[]")
    if "enum" in contract:
        for value in contract["enum"]:
            validate_json(value, contract, f"{path}.enum")


def test_every_command_publishes_an_o4_output_schema(cli: CliRunner) -> None:
    index = json.loads(invoke(cli, "schema").stdout)
    names = {entry["name"] for entry in index["commands"]}
    assert names == COMMANDS_WITH_OUTPUT
    for name in sorted(names):
        detail = json.loads(invoke(cli, "schema", *name.split()).stdout)
        assert "output" in detail, name
        _assert_o4_schema(detail["output"], name)


def test_session_state_enums_follow_the_public_vocabulary(cli: CliRunner) -> None:
    for command in ("cancel", "run", "continue", "steer", "wait", "status"):
        contract = schema_for(cli, command)
        statuses = _find_properties(contract, "status")
        assert statuses, command
        for status in statuses:
            assert status["enum"] == list(vocab.SESSION_STATES), command


def test_next_is_declared_as_an_optional_breadcrumb(cli: CliRunner) -> None:
    for command in ("run", "continue", "steer", "wait"):
        contract = schema_for(cli, command)
        assert "next" in contract["properties"]
        assert "next" not in contract["required"]


def test_mutating_commands_require_a_boolean_changed_field(cli: CliRunner) -> None:
    index = json.loads(invoke(cli, "schema").stdout)
    for entry in index["commands"]:
        if entry["effects"] == "read_only":
            continue
        contract = schema_for(cli, entry["name"])
        assert contract["properties"]["changed"] == {"type": "boolean"}
        assert "changed" in contract["required"], entry["name"]


def _find_properties(contract: Mapping[str, Any], name: str) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []
    properties = contract.get("properties")
    if isinstance(properties, Mapping) and name in properties:
        candidate = properties[name]
        if isinstance(candidate, Mapping):
            found.append(candidate)
    if isinstance(properties, Mapping):
        for child in properties.values():
            if isinstance(child, Mapping):
                found.extend(_find_properties(child, name))
    items = contract.get("items")
    if isinstance(items, Mapping):
        found.extend(_find_properties(items, name))
    return found


def test_real_collection_payloads_match_their_published_schemas(cli: CliRunner) -> None:
    for command in (
        ("agents", "list"),
        ("skills", "list"),
        ("status",),
        ("daemon", "status"),
        ("agents", "check"),
    ):
        result = invoke(cli, *command, "--json")
        assert result.exit_code == vocab.EXIT_OK, result.stderr
        payload = json.loads(result.stdout)
        validate_json(payload, schema_for(cli, " ".join(command)))


def assert_json_payload(cli: CliRunner, command: str, *args: str) -> Any:
    result = invoke(cli, *command.split(), *args, "--json")
    assert result.exit_code == vocab.EXIT_OK, result.stderr
    payload = json.loads(result.stdout)
    validate_json(payload, schema_for(cli, command))
    return payload


def test_real_document_payloads_match_their_published_schemas(
    cli: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    live_daemon: None,
) -> None:
    assert_json_payload(cli, "agents get", "mock")
    assert_json_payload(cli, "agents get", "mock", "--models")
    assert_json_payload(cli, "agents get", "mock", "--commands")
    single_check = assert_json_payload(cli, "agents check", "mock")
    assert single_check["has_more"] is False
    assert len(single_check["items"]) == 1
    assert_json_payload(cli, "agents create", "work", "--extends", "mock")
    assert_json_payload(cli, "agents delete", "work")
    assert_json_payload(cli, "install", "mock", "--yes")
    assert_json_payload(cli, "probe", "mock", "--discover")
    assert_json_payload(cli, "skills get", "adapter-bringup")

    daemon_status = [
        {
            "target": "mock~target",
            "version": "0.7.1",
            "pid": 123,
            "uptime_seconds": 4.5,
            "log": "/tmp/mock.log",
            "sessions": [],
            "preparing": [],
            "restoring": [],
            "max_concurrent": 1,
            "idle_seconds": 2.0,
        }
    ]

    async def fake_status(agent: str | None) -> list[dict[str, Any]]:
        del agent
        return daemon_status

    monkeypatch.setattr(cli_module, "_collect_daemon_status", fake_status)
    assert_json_payload(cli, "daemon status")
    assert_json_payload(cli, "daemon stop", "mock")
    assert_json_payload(cli, "daemon stop", "--dry-run")

    run_payload = assert_json_payload(cli, "run", "mock", "echo:first", "--quiet")
    session_id = run_payload["session_id"]
    assert run_payload["changed"] is True
    assert_json_payload(cli, "status", session_id)
    assert_json_payload(cli, "wait", session_id, "--quiet")
    assert_json_payload(cli, "continue", session_id, "echo:second", "--quiet")
    assert_json_payload(cli, "continue", session_id, "echo:background", "--background")
    assert_json_payload(cli, "wait", session_id, "--quiet")

    release = tmp_path / "release"
    active = assert_json_payload(cli, "run", "mock", f"chunkhold:{release}", "--background")
    active_id = active["session_id"]
    wait_until_running(active_id)
    assert_json_payload(cli, "steer", active_id, "echo:steered", "--quiet")

    cancel_release = tmp_path / "cancel-release"
    cancel_start = assert_json_payload(
        cli, "run", "mock", f"chunkhold:{cancel_release}", "--background"
    )
    cancel_id = cancel_start["session_id"]
    wait_until_running(cancel_id)
    assert_json_payload(cli, "cancel", cancel_id)

    log_payload = assert_json_payload(cli, "run", "mock", "tool:logged", "--quiet")
    log_id = log_payload["session_id"]
    log_result = invoke(cli, "log", log_id, "--json", "--quiet")
    assert log_result.exit_code == vocab.EXIT_OK, log_result.stderr
    log_schema = schema_for(cli, "log")
    for line in log_result.stdout.splitlines():
        validate_json(json.loads(line), log_schema)

    assert_json_payload(cli, "prune", "--older-than", "0d", "--dry-run")
    assert_json_payload(cli, "prune", "--older-than", "0d", "--yes")

    deleted = sessions.create_session(entry="mock", base_adapter="mock", prompt="delete me")
    sessions.transition(deleted.session_id, "succeeded", exit_code=0)
    assert_json_payload(cli, "delete", deleted.session_id, "--yes")


def test_real_payload_validation_catches_a_removed_schema_property(cli: CliRunner) -> None:
    payload = json.loads(invoke(cli, "status", "--json").stdout)
    contract = schema_for(cli, "status")
    contract["properties"].pop("has_more")
    with pytest.raises(AssertionError):
        validate_json(payload, contract)


def test_output_schema_rejects_a_missing_required_field() -> None:
    contract = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }
    with pytest.raises(AssertionError):
        validate_json({}, contract)


def test_output_schema_rejects_an_undeclared_field() -> None:
    contract = {"type": "object", "properties": {}, "required": []}
    with pytest.raises(AssertionError):
        validate_json({"unexpected": True}, contract)


def wait_until_running(session_id: str) -> None:
    from acpc import sessions

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if sessions.read_meta(session_id).is_active:
            return
        time.sleep(0.02)
    pytest.fail(f"session {session_id} never became active")
