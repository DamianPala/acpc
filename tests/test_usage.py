"""Behavioral tests for context-occupancy reporting (`used`/`size`/`peak`)."""

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
def test_prompt_meta_usage_sets_used_and_peak_with_null_size(
    cli: CliRunner,
    use_daemon: bool,
    live_daemon: None,
    monkeypatch: pytest.MonkeyPatch,
    state_root: Path,
) -> None:
    """The Grok `_meta.totalTokens` path never learns a context window size and
    never parses a cost — all cost parsing went with the `costUsdTicks` hack."""
    if not use_daemon:

        async def unavailable(target: str) -> daemon_client.DaemonUnavailable:
            return daemon_client.DaemonUnavailable(f"direct test ({target})")

        monkeypatch.setattr(daemon_client, "ensure_daemon", unavailable)

    first = invoke(cli, "run", "mock", "meta:120:1000000000:first turn", "--json")
    assert first.exit_code == 0, first.stderr
    first_payload = json.loads(first.stdout)
    session_id = first_payload["session_id"]
    assert first_payload["context"] == {"used": 120, "size": None, "peak": 120}

    second = invoke(
        cli,
        "continue",
        session_id,
        "meta:80:2000000000:second turn",
        "--json",
    )
    assert second.exit_code == 0, second.stderr
    second_payload = json.loads(second.stdout)
    assert second_payload["context"] == {"used": 80, "size": None, "peak": 120}
    assert "ctx 80, peak 120" in second.stderr
    assert "cost" not in second.stderr

    final = sessions.read_meta(session_id)
    assert final.context == {"used": 80, "size": None, "peak": 120}
    status = json.loads(invoke(cli, "status", session_id, "--json").stdout)
    assert status["context"] == {"used": 80, "size": None, "peak": 120}
    meta = json.loads(
        (state_root / "sessions" / session_id / "meta.json").read_text(encoding="utf-8")
    )
    assert meta["context"] == {"used": 80, "size": None, "peak": 120}
    assert "tokens" not in meta
    assert "cost" not in meta

    events = [
        json.loads(line)
        for line in (state_root / "sessions" / session_id / "transcript.ndjson")
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    usage = [event for event in events if event.get("type") == "usage"]
    assert [(event["used"], event["size"]) for event in usage] == [(120, None), (80, None)]
    assert all("cost" not in event for event in usage)


def test_continue_after_unobserved_usage_reports_the_first_observed_total(
    cli: CliRunner,
) -> None:
    """SPEC.md V6c: an unobserved first turn (`context: null`) does not stop a
    later turn's real usage from being reported once the adapter sends it."""
    first = invoke(cli, "run", "mock", "echo:no usage yet", "--json")
    assert first.exit_code == 0, first.stderr
    first_payload = json.loads(first.stdout)
    session_id = first_payload["session_id"]
    assert first_payload["context"] is None

    second = invoke(cli, "continue", session_id, "meta:1200:1000000000:now observed", "--json")
    assert second.exit_code == 0, second.stderr
    second_payload = json.loads(second.stdout)

    assert second_payload["context"] == {"used": 1200, "size": None, "peak": 1200}
    assert sessions.read_meta(session_id).context == {"used": 1200, "size": None, "peak": 1200}


def test_streamed_usage_wins_over_prompt_meta(cli: CliRunner) -> None:
    """The real ACP `usage_update` path (`size` always reported by the mock
    agent) wins over the `_meta`-only path; acpc never renders the adapter's
    own cost figure, even though the transcript keeps it."""
    result = invoke(cli, "run", "mock", "both:700:3000000000:stream wins", "--json")

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    final = sessions.read_meta(payload["session_id"])
    assert final.context == {"used": 700, "size": 200_000, "peak": 700}
    assert payload["context"] == {"used": 700, "size": 200_000, "peak": 700}
    assert "cost" not in result.stderr
    assert "ctx 700/200k, peak 700" in result.stderr


def test_streamed_usage_uses_latest_used_and_keeps_the_larger_peak(cli: CliRunner) -> None:
    """SPEC.md `status`: `peak` is the largest `used` observed over the
    session, earlier turns included — it survives a smaller `continue`."""
    first = invoke(cli, "run", "mock", "both:700:3000000000:first stream", "--json")
    assert first.exit_code == 0, first.stderr
    session_id = json.loads(first.stdout)["session_id"]

    second = invoke(
        cli,
        "continue",
        session_id,
        "both:500:1000000000:second stream",
        "--json",
    )
    assert second.exit_code == 0, second.stderr

    payload = json.loads(second.stdout)
    final = sessions.read_meta(session_id)
    assert final.context == {"used": 500, "size": 200_000, "peak": 700}
    assert payload["context"] == {"used": 500, "size": 200_000, "peak": 700}


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
