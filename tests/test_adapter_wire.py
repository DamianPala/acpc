"""Wire-path workarounds: model_via, effort_via, session rebuild, usage meta."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from acpc import runner, sessions
from acpc.client import AcpcClient
from acpc.permissions import PermissionLevel
from acpc.probe import ProbeReport
from acpc.registry import AgentRegistry, ModeSpec, RegistryError
from acpc.targets import target_for_call
from acpc.transcript import Transcript


@pytest.fixture(autouse=True)
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "state"
    (root / "agents").mkdir(parents=True)
    monkeypatch.setenv("ACPC_HOME", str(root))
    return root


def write_entry(directory: Path, name: str, text: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.toml").write_text(text, encoding="utf-8")


# --- registry defaults / inheritance ---------------------------------------


def test_shipped_claude_and_codex_default_to_config_option_vias(tmp_path: Path) -> None:
    registry = AgentRegistry(tmp_path / "agents")
    claude = registry.resolve("claude")
    codex = registry.resolve("codex")
    assert claude.model_via == "config_option"
    assert claude.effort_via == "config_option"
    assert codex.model_via == "config_option"
    assert codex.effort_via == "config_option"


def test_variant_inherits_model_and_effort_via(tmp_path: Path) -> None:
    agents = tmp_path / "state" / "agents"
    write_entry(
        agents,
        "base",
        """
name = "Base"
command = "base agent stdio"
model_via = "set_model"
effort_via = "cli"
effort_cli_flag = "--effort"
[modes]
default = { grants = "read", delegates = true }
""",
    )
    write_entry(
        agents,
        "child",
        """
extends = "base"
description = "variant"
model = "child-model"
""",
    )
    child = AgentRegistry(agents).resolve("child")
    assert child.model_via == "set_model"
    assert child.effort_via == "cli"
    assert child.effort_cli_flag == "--effort"
    assert child.model == "child-model"
    call = child.resolve_call(effort="low", permissions="read")
    assert call.command == ("base", "agent", "--effort", "low", "stdio")


def test_invalid_model_via_and_effort_via_are_refused(tmp_path: Path) -> None:
    agents = tmp_path / "state" / "agents"
    write_entry(
        agents,
        "bad_model",
        'name = "Bad"\ncommand = "x"\nmodel_via = "magic"\n',
    )
    with pytest.raises(RegistryError, match="model_via"):
        AgentRegistry(agents).resolve("bad_model")
    write_entry(
        agents,
        "bad_effort",
        'name = "Bad"\ncommand = "x"\neffort_via = "env"\n',
    )
    with pytest.raises(RegistryError, match="effort_via"):
        AgentRegistry(agents).resolve("bad_effort")


# --- spawn target identity -------------------------------------------------


def test_spawn_identity_changes_target_digest() -> None:
    low = target_for_call("grok", permissions="read", spawn_identity={"effort": "low"})
    high = target_for_call("grok", permissions="read", spawn_identity={"effort": "high"})
    none = target_for_call("grok", permissions="read")
    assert low != high
    assert low != none
    assert "low" not in low  # digest only — no plain secret/value leak of flag content optional


def test_call_target_separates_cli_effort_processes(tmp_path: Path) -> None:
    agents = tmp_path / "state" / "agents"
    write_entry(
        agents,
        "tool",
        """
name = "Tool"
command = "tool agent stdio"
effort_via = "cli"
effort_cli_flag = "--effort"
[modes]
default = { grants = "read", delegates = true }
""",
    )
    registry = AgentRegistry(agents)
    low = registry.resolve_call("tool", effort="low", permissions="read")
    high = registry.resolve_call("tool", effort="high", permissions="read")
    assert runner.call_target(low) != runner.call_target(high)


def test_call_target_ignores_effort_when_via_config_option(tmp_path: Path) -> None:
    agents = tmp_path / "state" / "agents"
    write_entry(
        agents,
        "tool",
        """
name = "Tool"
command = "tool"
[modes]
default = { grants = "read", delegates = true }
""",
    )
    registry = AgentRegistry(agents)
    low = registry.resolve_call("tool", effort="low", permissions="read")
    high = registry.resolve_call("tool", effort="high", permissions="read")
    assert runner.call_target(low) == runner.call_target(high)


# --- session persistence / continue rebuild --------------------------------


def test_session_resolution_round_trips_wire_vias_without_double_cli_effort(
    tmp_path: Path,
) -> None:
    agents = tmp_path / "state" / "agents"
    write_entry(
        agents,
        "tool",
        """
name = "Tool"
command = "tool agent stdio"
model_via = "set_model"
effort_via = "cli"
effort_cli_flag = "--effort"
[modes]
default = { grants = "read", delegates = true }
""",
    )
    resolution = AgentRegistry(agents).resolve_call(
        "tool", model="m1", effort="low", permissions="read"
    )
    assert "--effort" in resolution.command
    dry = runner.resolution_payload(resolution, cwd="/tmp")
    assert dry["command"] == "tool agent --effort low stdio"

    payload = runner.session_resolution(resolution, cwd="/tmp")
    # Persisted command is the base entry string — not the injected spawn argv.
    assert payload["command"] == "tool agent stdio"
    assert payload["adapter"]["model_via"] == "set_model"
    assert payload["adapter"]["effort_via"] == "cli"
    assert payload["adapter"]["effort_cli_flag"] == "--effort"

    meta = sessions.create_session(
        entry=resolution.entry.entry,
        base_adapter=resolution.entry.base_adapter,
        prompt="hi",
        resolution=payload,
        target="tool~test",
    )
    rebuilt = runner.resolution_from_session(meta)
    assert rebuilt.entry.model_via == "set_model"
    assert rebuilt.entry.effort_via == "cli"
    assert rebuilt.entry.effort_cli_flag == "--effort"
    assert rebuilt.model == "m1"
    assert rebuilt.effort == "low"
    # Re-inject once, not twice.
    assert rebuilt.command == ("tool", "agent", "--effort", "low", "stdio")


def test_legacy_sessions_default_missing_vias_to_config_option(tmp_path: Path) -> None:
    agents = tmp_path / "state" / "agents"
    write_entry(
        agents,
        "tool",
        """
name = "Tool"
command = "tool"
[modes]
default = { grants = "read", delegates = true }
""",
    )
    resolution = AgentRegistry(agents).resolve_call("tool", permissions="read")
    payload = runner.session_resolution(resolution, cwd=None)
    # Simulate pre-0.6.1 session payload without vias.
    payload["adapter"].pop("model_via", None)
    payload["adapter"].pop("effort_via", None)
    payload["adapter"].pop("effort_cli_flag", None)
    meta = sessions.create_session(
        entry=resolution.entry.entry,
        base_adapter=resolution.entry.base_adapter,
        prompt="legacy",
        resolution=payload,
        target="tool~legacy",
    )
    rebuilt = runner.resolution_from_session(meta)
    assert rebuilt.entry.model_via == "config_option"
    assert rebuilt.entry.effort_via == "config_option"
    assert rebuilt.entry.effort_cli_flag is None


# --- apply_call_options branching ------------------------------------------


class _FakeConn:
    def __init__(self) -> None:
        self.modes: list[str] = []
        self.config: list[tuple[str, str]] = []
        self.models: list[str] = []
        self._conn = self

    async def set_session_mode(self, *, session_id: str, mode_id: str) -> None:
        del session_id
        self.modes.append(mode_id)

    async def set_config_option(self, *, config_id: str, session_id: str, value: str) -> None:
        del session_id
        self.config.append((config_id, value))

    async def set_model(self, *, session_id: str, model_id: str) -> None:
        del session_id
        self.models.append(model_id)

    async def send_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "session/set_model":
            self.models.append(params["modelId"])
            return {}
        raise AssertionError(f"unexpected method {method}")


def test_apply_call_options_uses_set_model_and_skips_cli_effort(tmp_path: Path) -> None:
    agents = tmp_path / "state" / "agents"
    write_entry(
        agents,
        "tool",
        """
name = "Tool"
command = "tool agent stdio"
model_via = "set_model"
effort_via = "cli"
effort_cli_flag = "--effort"
[modes]
default = { grants = "read", delegates = true }
""",
    )
    resolution = AgentRegistry(agents).resolve_call(
        "tool", model="m1", effort="low", mode="default", permissions="read"
    )
    request = runner.TurnRequest(resolution=resolution, prompt="x")
    conn = _FakeConn()
    asyncio.run(runner.apply_call_options(conn, "sid", request))
    assert conn.modes == ["default"]
    assert conn.models == ["m1"]
    assert conn.config == []  # effort not via config option


def test_apply_call_options_uses_config_option_by_default(tmp_path: Path) -> None:
    agents = tmp_path / "state" / "agents"
    write_entry(
        agents,
        "tool",
        """
name = "Tool"
command = "tool"
effort_config_id = "effort"
[modes]
default = { grants = "read", delegates = true }
""",
    )
    resolution = AgentRegistry(agents).resolve_call(
        "tool", model="m1", effort="high", mode="default", permissions="read"
    )
    request = runner.TurnRequest(resolution=resolution, prompt="x")
    conn = _FakeConn()
    asyncio.run(runner.apply_call_options(conn, "sid", request))
    assert ("model", "m1") in conn.config
    assert ("effort", "high") in conn.config
    assert conn.models == []  # config_option path never calls set_model


def test_set_session_model_falls_back_to_raw_send_request() -> None:
    class RawOnly:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []
            self._conn = self

        async def send_request(self, method: str, params: dict[str, Any]) -> dict[str, str]:
            self.calls.append((method, params))
            return {"ok": "yes"}

    raw = RawOnly()
    result = asyncio.run(runner._set_session_model(raw, "sid", "grok-4.5"))
    assert result == {"ok": "yes"}
    assert raw.calls == [("session/set_model", {"sessionId": "sid", "modelId": "grok-4.5"})]


# --- usage from prompt meta ------------------------------------------------


def test_record_prompt_usage_reads_prompt_response_meta(tmp_path: Path) -> None:
    client, transcript = _client(tmp_path)
    prompt = SimpleNamespace(
        stop_reason="end_turn",
        field_meta={
            "totalTokens": 120,
            "usage": {"costUsdTicks": 1_000_000_000},  # 0.1 USD
        },
    )
    client.record_prompt_usage(prompt)
    assert client.tokens == 120
    assert client.cost == pytest.approx(0.1)
    events = transcript.read().events
    assert [e for e in events if e.get("type") == "usage"] == [
        {"type": "usage", "tokens": 120, "cost": 0.1, "ts": 1.0, "i": 1}
    ]


def test_record_prompt_usage_does_not_duplicate_when_unchanged(tmp_path: Path) -> None:
    client, transcript = _client(tmp_path)
    prompt = SimpleNamespace(
        stop_reason="end_turn",
        field_meta={"totalTokens": 50},
    )
    client.record_prompt_usage(prompt)
    client.record_prompt_usage(prompt)
    usage_events = [e for e in transcript.read().events if e.get("type") == "usage"]
    assert len(usage_events) == 1
    assert usage_events[0]["tokens"] == 50


def test_models_from_session_meta_xai_session_config(tmp_path: Path) -> None:
    client, _transcript = _client(tmp_path)
    session = SimpleNamespace(
        modes=None,
        config_options=None,
        field_meta={
            "x.ai/sessionConfig": {
                "options": [
                    {"id": "grok-4.6", "category": "model", "selected": True},
                    {"id": "high", "category": "mode", "selected": True},
                    {"id": "grok-4.5", "category": "model", "selected": False},
                ]
            }
        },
    )
    # capture_advertised expects NewSessionResponse-like; call helpers directly.
    models = AcpcClient._models_from_session_meta(session)  # type: ignore[arg-type]
    assert models == ["grok-4.6", "grok-4.5"]
    client.capture_advertised(session)  # type: ignore[arg-type]
    assert client.advertised["models"] == ["grok-4.6", "grok-4.5"]


def _client(tmp_path: Path) -> tuple[AcpcClient, Transcript]:
    transcript = Transcript(tmp_path / "t.ndjson", clock=lambda: 1.0)
    return AcpcClient(transcript, PermissionLevel.READ, clock=lambda: 1.0), transcript


# --- probe Path B messaging ------------------------------------------------


def test_probe_report_marks_empty_discovery_as_path_b() -> None:
    entry = SimpleNamespace(
        entry="grok",
        base_adapter="grok",
        modes={
            "default": ModeSpec(grants="read", delegates=True),
            "bypassPermissions": ModeSpec(grants="all", delegates=False),
        },
    )
    report = ProbeReport(entry=entry, advertised_modes=[], current_mode=None)  # type: ignore[arg-type]
    text = report.text()
    assert "empty discovery" in text
    assert "Path B" in text
    assert "do not strip" in text
    assert "default" in text
