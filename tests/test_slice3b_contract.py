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
    "list",
    "log",
    "probe",
    "prune",
    "resolve",
    "run",
    "skills get",
    "skills list",
    "status",
    "steer",
    "wait",
}

EXPECTED_REQUIRED: dict[str, tuple[str, ...]] = {
    "agents check": ("items", "has_more"),
    "agents create": ("name", "extends", "path", "changed"),
    "agents delete": ("name", "path", "changed"),
    "agents get": ("agent",),
    "agents list": ("items", "has_more"),
    "cancel": ("session_id", "status", "stop_reason", "changed"),
    "continue": (
        "status",
        "session_id",
        "turn",
        "created_at",
        "started_at",
        "finished_at",
        "paths",
        "truncated",
        "partial",
        "denied",
        "permissions_clamp",
        "capabilities",
        "changed",
    ),
    "daemon status": ("items", "has_more"),
    "daemon stop": ("targets", "changed", "requires_confirmation"),
    "delete": ("session_id", "removed", "changed", "paths"),
    "install": ("agent", "ok", "returncode", "changed"),
    "log": ("i", "ts", "type"),
    "probe": (
        "entry",
        "base_adapter",
        "discover_only",
        "turns",
        "current_mode",
        "advertised_modes",
        "mode_reports",
        "verdicts",
        "refusal_violations",
        "implied_modes",
        "unmeasured",
        "current_modes",
        "diff",
    ),
    "prune": ("targets", "changed", "requires_confirmation"),
    "resolve": (
        "entry",
        "base_adapter",
        "command",
        "cwd",
        "env",
        "env_passthrough",
        "resolved",
    ),
    "run": (
        "status",
        "session_id",
        "turn",
        "created_at",
        "started_at",
        "finished_at",
        "paths",
        "truncated",
        "partial",
        "denied",
        "permissions_clamp",
        "capabilities",
        "changed",
    ),
    "skills get": ("name", "description", "path", "body"),
    "skills list": ("items", "has_more"),
    "list": ("items", "has_more"),
    "status": (
        "session_id",
        "status",
        "pid",
        "turns",
        "entry",
        "base_adapter",
        "model",
        "name",
        "runtime_seconds",
        "idle_seconds",
        "tokens",
        "cost",
        "exit_code",
        "stop_reason",
        "failure",
        "capabilities",
        "paths",
        "created_at",
        "started_at",
        "finished_at",
    ),
    "steer": (
        "status",
        "session_id",
        "turn",
        "created_at",
        "started_at",
        "finished_at",
        "paths",
        "truncated",
        "denied",
        "permissions_clamp",
        "capabilities",
        "changed",
        "correction_result",
    ),
    "wait": (
        "status",
        "session_id",
        "turn",
        "created_at",
        "started_at",
        "finished_at",
        "stop_reason",
        "cost",
        "answer",
        "paths",
        "truncated",
        "partial",
        "denied",
        "permissions_clamp",
        "capabilities",
    ),
}

EXPECTED_ENUMS: dict[str, dict[str, tuple[str, ...]]] = {
    "agents list": {"$.items[].kind": ("adapter", "variant")},
    "cancel": {
        "$.status": (
            "running",
            "succeeded",
            "failed",
            "canceled",
            "unknown",
        )
    },
    "continue": {
        "$.status": ("running", "succeeded", "failed", "canceled", "unknown"),
        "$.capabilities.steer_mode": ("in-place", "cancel-then-start"),
    },
    "log": {
        "$.type": tuple(
            sorted(("error", "msg", "permission", "state", "steer", "thought", "tool", "usage"))
        )
    },
    "probe": {
        "$.diff[].status": ("advertised-missing", "entry-missing"),
    },
    "run": {
        "$.status": ("running", "succeeded", "failed", "canceled", "unknown"),
        "$.capabilities.steer_mode": ("in-place", "cancel-then-start"),
    },
    "list": {
        "$.items[].status": (
            "starting",
            "running",
            "preparing",
            "succeeded",
            "failed",
            "canceled",
            "unknown",
        )
    },
    "status": {
        "$.status": (
            "starting",
            "running",
            "preparing",
            "succeeded",
            "failed",
            "canceled",
            "unknown",
        ),
        "$.capabilities.steer_mode": ("in-place", "cancel-then-start"),
    },
    "steer": {
        "$.status": ("running", "succeeded"),
        "$.capabilities.steer_mode": ("in-place", "cancel-then-start"),
    },
    "wait": {
        "$.status": ("succeeded", "failed", "canceled", "unknown"),
        "$.capabilities.steer_mode": ("in-place", "cancel-then-start"),
    },
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


def test_status_and_list_have_separate_non_empty_contracts(cli: CliRunner) -> None:
    status = schema_for(cli, "status")
    listing = schema_for(cli, "list")
    assert status["required"][:2] == ["session_id", "status"]
    assert listing["required"] == ["items", "has_more"]

    old_spelling = invoke(cli, "status", "--json")
    assert old_spelling.exit_code == vocab.EXIT_USAGE
    assert "acpc list" in old_spelling.stderr


def test_output_required_fields_are_independent_contract_expectations(cli: CliRunner) -> None:
    for command, required in EXPECTED_REQUIRED.items():
        assert schema_for(cli, command)["required"] == list(required), command


def test_output_enums_are_independent_reachable_value_expectations(cli: CliRunner) -> None:
    for command in COMMANDS_WITH_OUTPUT:
        contract = schema_for(cli, command)
        found = {
            path: tuple(node["enum"]) for path, node in _schema_nodes(contract) if "enum" in node
        }
        assert found == EXPECTED_ENUMS.get(command, {}), command


def test_session_states_are_partitioned_for_status_selection() -> None:
    assert set(vocab.SESSION_STATES) == vocab.ACTIVE_STATES | vocab.FINISHED_STATES
    assert "timeout" not in vocab.SESSION_STATES


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
        expected_type = (
            {"type": ["boolean", "null"]} if entry["name"] == "install" else {"type": "boolean"}
        )
        assert contract["properties"]["changed"] == expected_type
        assert "changed" in contract["required"], entry["name"]


def _schema_nodes(
    contract: Mapping[str, Any], path: str = "$"
) -> list[tuple[str, Mapping[str, Any]]]:
    nodes = [(path, contract)]
    properties = contract.get("properties")
    if isinstance(properties, Mapping):
        nodes.extend(
            child_nodes
            for name, child in properties.items()
            if isinstance(child, Mapping)
            for child_nodes in _schema_nodes(child, f"{path}.{name}")
        )
    items = contract.get("items")
    if isinstance(items, Mapping):
        nodes.extend(_schema_nodes(items, f"{path}[]"))
    return nodes


def test_real_collection_payloads_match_their_published_schemas(cli: CliRunner) -> None:
    for command in (
        ("agents", "list"),
        ("skills", "list"),
        ("list",),
        ("daemon", "status"),
        ("agents", "check"),
    ):
        options = ("--limit", "0") if command == ("agents", "check") else ()
        result = invoke(cli, *command, *options, "--json")
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
    assert_json_payload(cli, "agents delete", "work", "--yes")
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

    truncated_log = invoke(cli, "log", log_id, "--json", "--max-output", "1")
    assert truncated_log.exit_code == vocab.EXIT_OK
    assert "truncated" not in truncated_log.stdout
    assert f"full transcript: {sessions.transcript_path(log_id)}" in truncated_log.stderr

    failed_run = invoke(cli, "run", "mock", "failure-details:contract", "--json", "--quiet")
    assert failed_run.exit_code == vocab.EXIT_AGENT_ERROR
    failure = json.loads(failed_run.stderr.splitlines()[-1])["error"]
    failed_id = failure["context"]["session_id"]
    failed_log = invoke(cli, "log", failed_id, "--json", "--quiet")
    assert failed_log.exit_code == vocab.EXIT_OK
    for line in failed_log.stdout.splitlines():
        validate_json(json.loads(line), log_schema)

    assert_json_payload(cli, "prune", "--older-than", "0d", "--dry-run")
    assert_json_payload(cli, "prune", "--older-than", "0d", "--yes")

    deleted = sessions.create_session(entry="mock", base_adapter="mock", prompt="delete me")
    sessions.transition(deleted.session_id, "succeeded", exit_code=0)
    assert_json_payload(cli, "delete", deleted.session_id, "--yes")


def test_real_payload_validation_catches_a_removed_schema_property(cli: CliRunner) -> None:
    payload = json.loads(invoke(cli, "list", "--json").stdout)
    contract = schema_for(cli, "list")
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
