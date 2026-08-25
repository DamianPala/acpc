"""Behavioral tests for cumulative token and cost reporting."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from acpc import daemon_client, sessions
from acpc.cli import main

MOCK_AGENT_SCRIPT = str(Path(__file__).with_name("mock_agent.py"))

MOCK_ENTRY = f"""
name = "Mock Agent"
command = "{sys.executable} {MOCK_AGENT_SCRIPT}"
install_command = "true"
home = "~/.mock"
home_env = "MOCK_HOME"

[modes]
default = {{ grants = "read", delegates = true }}

[presets]
standard = {{ model = "mock-sonnet-5", effort = "high" }}
"""


@pytest.fixture(autouse=True)
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "state"
    agents = root / "agents"
    agents.mkdir(parents=True)
    (agents / "mock.toml").write_text(MOCK_ENTRY, encoding="utf-8")
    monkeypatch.setenv("ACPC_HOME", str(root))
    return root


@pytest.fixture
def cli() -> CliRunner:
    return CliRunner()


def invoke(cli: CliRunner, *args: str):
    return cli.invoke(main, list(args), catch_exceptions=False)


@pytest.mark.parametrize("use_daemon", [False, True], ids=["direct", "daemon"])
def test_prompt_meta_usage_accumulates_across_run_and_continue(
    cli: CliRunner,
    use_daemon: bool,
    live_daemon: None,
    monkeypatch: pytest.MonkeyPatch,
    state_root: Path,
) -> None:
    if not use_daemon:

        async def unavailable(target: str) -> daemon_client.DaemonUnavailable:
            return daemon_client.DaemonUnavailable(f"direct test ({target})")

        monkeypatch.setattr(daemon_client, "ensure_daemon", unavailable)

    first = invoke(cli, "run", "mock", "meta:120:1000000000:first turn", "--json")
    assert first.exit_code == 0, first.stderr
    first_payload = json.loads(first.stdout)
    session_id = first_payload["session_id"]
    assert first_payload["cost"] == pytest.approx(0.1)

    second = invoke(
        cli,
        "continue",
        session_id,
        "meta:240:2000000000:second turn",
        "--json",
    )
    assert second.exit_code == 0, second.stderr
    second_payload = json.loads(second.stdout)
    assert second_payload["cost"] == pytest.approx(0.3)
    assert "240 tok" in second.stderr
    assert "cost $0.30" in second.stderr

    final = sessions.read_meta(session_id)
    assert final.tokens == 240
    assert final.cost == pytest.approx(0.3)
    status = json.loads(invoke(cli, "status", session_id, "--json").stdout)
    assert status["tokens"] == 240
    assert status["cost"] == pytest.approx(0.3)
    meta = json.loads(
        (state_root / "sessions" / session_id / "meta.json").read_text(encoding="utf-8")
    )
    assert meta["tokens"] == 240
    assert meta["cost"] == pytest.approx(0.3)

    events = [
        json.loads(line)
        for line in (state_root / "sessions" / session_id / "transcript.ndjson")
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    usage = [event for event in events if event.get("type") == "usage"]
    assert [(event["tokens"], event["cost"]) for event in usage] == [
        (120, pytest.approx(0.1)),
        (240, pytest.approx(0.3)),
    ]


def test_streamed_usage_wins_over_prompt_meta(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "both:700:3000000000:stream wins", "--json")

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    final = sessions.read_meta(payload["session_id"])
    assert final.tokens == 700
    assert final.cost == pytest.approx(0.3)
    assert payload["cost"] == pytest.approx(0.3)
    assert "cost $0.30" in result.stderr


def test_prompt_meta_usage_flushes_pending_prose_before_usage(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "meta:321:1000000000:message first", "--json", "--quiet")

    assert result.exit_code == 0
    session_id = json.loads(result.stdout)["session_id"]
    events = [
        json.loads(line)
        for line in sessions.transcript_path(session_id).read_text(encoding="utf-8").splitlines()
        if line
    ]
    message_index = next(index for index, event in enumerate(events) if event.get("type") == "msg")
    usage_index = next(index for index, event in enumerate(events) if event.get("type") == "usage")
    assert message_index < usage_index
