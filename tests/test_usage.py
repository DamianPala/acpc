"""Behavioral tests for context-occupancy reporting (`used`/`size`/`peak`)."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from acpc import daemon_client, runner, sessions, transcript
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


def _set_usage_profile(state_root: Path, profile: str, *, billing: str | None = None) -> None:
    billing_line = f'billing = "{billing}"\n' if billing is not None else ""
    configured = MOCK_ENTRY.replace(
        'home_env = "MOCK_HOME"\n',
        f'home_env = "MOCK_HOME"\nusage_profile = "{profile}"\n{billing_line}',
    )
    (state_root / "agents" / "mock.toml").write_text(configured, encoding="utf-8")


def _wait_for_message(session_id: str, expected: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = transcript.Transcript(sessions.transcript_path(session_id)).read().events
        if any(
            event.get("type") == "msg" and expected in event.get("text", "") for event in events
        ):
            return
        time.sleep(0.02)
    pytest.fail(f"session {session_id} did not emit {expected!r}")


def _wait_for_state(session_id: str, expected: str, timeout: float = 10.0) -> sessions.SessionMeta:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        meta = sessions.read_meta(session_id)
        if meta.state == expected:
            return meta
        time.sleep(0.02)
    pytest.fail(f"session {session_id} did not reach {expected!r}")


def test_usage_is_cumulative_across_run_continue_status_and_meta(
    cli: CliRunner, state_root: Path
) -> None:
    _set_usage_profile(state_root, "claude_model_usage")

    first = invoke(cli, "run", "mock", "rawusage:claude:100,200", "--json", "--quiet")
    assert first.exit_code == 0, first.stderr
    first_payload = json.loads(first.stdout)
    session_id = first_payload["session_id"]
    model_id = "claude-opus-5[1m]"
    assert first_payload["usage"]["models"][model_id]["total_tokens"] == 135037
    assert first_payload["usage"]["quality"] == "exact"
    assert first_payload["usage"]["source"].endswith("_meta.quota.model_usage")
    assert "usage" not in first.stderr

    second = invoke(cli, "continue", session_id, "rawusage:claude:300", "--json", "--quiet")
    assert second.exit_code == 0, second.stderr
    second_payload = json.loads(second.stdout)
    assert second_payload["usage"]["models"][model_id]["total_tokens"] == 270074
    assert second_payload["usage"]["quality"] == "exact"

    status = invoke(cli, "status", session_id, "--json")
    assert status.exit_code == 0, status.stderr
    assert json.loads(status.stdout)["usage"] == second_payload["usage"]
    text_status = invoke(cli, "status", session_id, "--format", "text")
    assert "usage 270.1k tokens (exact)" in text_status.stdout
    meta_path = state_root / "sessions" / session_id / "meta.json"
    saved = json.loads(meta_path.read_text(encoding="utf-8"))
    assert saved["usage"] == second_payload["usage"]

    log = invoke(cli, "log", session_id, "--json", "--quiet")
    assert log.exit_code == 0, log.stderr
    events = [json.loads(line) for line in log.stdout.splitlines()]
    assert all("meta" not in event for event in events)

    saved.pop("usage")
    meta_path.write_text(json.dumps(saved), encoding="utf-8")
    assert sessions.read_meta(session_id).usage is None


def test_legacy_usage_without_drift_reads_as_null(cli: CliRunner, state_root: Path) -> None:
    _set_usage_profile(state_root, "claude_model_usage")
    result = invoke(cli, "run", "mock", "rawusage:claude", "--json", "--quiet")
    session_id = json.loads(result.stdout)["session_id"]
    meta_path = state_root / "sessions" / session_id / "meta.json"
    saved = json.loads(meta_path.read_text(encoding="utf-8"))
    saved["usage"].pop("drift")
    meta_path.write_text(json.dumps(saved), encoding="utf-8")

    usage_value = sessions.read_meta(session_id).usage
    assert usage_value is not None
    assert usage_value["drift"] is None


def test_usage_drift_is_persisted_and_noted_once_across_run_and_continue(
    cli: CliRunner, state_root: Path
) -> None:
    _set_usage_profile(state_root, "codex_usage_updates")

    first = invoke(cli, "run", "mock", "drift:codex", "--json")
    assert first.exit_code == 0, first.stderr
    session_id = json.loads(first.stdout)["session_id"]
    expected_drift = {
        "check": "codex_usage_updates",
        "declared": "last",
        "observed": "turn",
        "adapter": "mock-agent",
        "version": "0.1.0",
        "turn": 1,
    }
    expected_note = (
        f"acpc: mock-agent 0.1.0 reports usage as turn, the codex_usage_updates profile "
        f"expects last; usage for session {session_id} is marked estimate"
    )
    assert first.stderr.splitlines().count(expected_note) == 1
    assert expected_note not in first.stdout
    assert json.loads(first.stdout)["usage"]["drift"] == expected_drift

    second = invoke(cli, "continue", session_id, "drift:codex", "--json")
    assert second.exit_code == 0, second.stderr
    assert expected_note not in second.stderr
    assert json.loads(second.stdout)["usage"]["drift"] == expected_drift
    assert json.loads(invoke(cli, "status", session_id, "--json").stdout)["usage"]["drift"] == (
        expected_drift
    )
    saved = json.loads((state_root / "sessions" / session_id / "meta.json").read_text())
    assert saved["usage"]["drift"] == expected_drift


def test_clean_codex_turn_has_no_usage_drift_note(cli: CliRunner, state_root: Path) -> None:
    _set_usage_profile(state_root, "codex_usage_updates")

    result = invoke(cli, "run", "mock", "rawusage:codex", "--json")

    assert result.exit_code == 0, result.stderr
    assert "reports usage as" not in result.stderr
    assert json.loads(result.stdout)["usage"]["drift"] is None


def test_usage_update_above_context_size_drifts_through_the_cli(
    cli: CliRunner, state_root: Path
) -> None:
    _set_usage_profile(state_root, "codex_usage_updates")

    result = invoke(cli, "run", "mock", "drift:used-size", "--json")

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    drift = payload["usage"]["drift"]
    assert drift["check"] == "used_le_size"
    assert drift["declared"] == "last"
    assert drift["observed"] == "cumulative"
    assert drift["turn"] == 1
    assert "reports usage as cumulative" in result.stderr


def test_restore_delta_is_only_checked_on_cold_continue_without_context_drop(
    cli: CliRunner, state_root: Path
) -> None:
    _set_usage_profile(state_root, "claude_model_usage")

    first = invoke(cli, "run", "mock", "drift:claude-restore", "--json")
    assert first.exit_code == 0, first.stderr
    session_id = json.loads(first.stdout)["session_id"]
    assert json.loads(first.stdout)["usage"]["drift"] is None
    assert "reports usage as" not in first.stderr

    continued = invoke(cli, "continue", session_id, "drift:claude-restore", "--json")

    assert continued.exit_code == 0, continued.stderr
    drift = json.loads(continued.stdout)["usage"]["drift"]
    assert drift["check"] == "restore_delta"
    assert drift["declared"] == "turn"
    assert drift["observed"] == "cumulative"
    assert drift["turn"] == 2
    assert continued.stderr.count("reports usage as cumulative") == 1


def test_background_wait_emits_usage_drift_note_once(
    cli: CliRunner, live_daemon: None, state_root: Path
) -> None:
    _set_usage_profile(state_root, "codex_usage_updates")
    dispatched = invoke(cli, "run", "mock", "drift:codex", "--bg", "--json")
    assert dispatched.exit_code == 0, dispatched.stderr
    session_id = json.loads(dispatched.stdout)["session_id"]

    first_wait = invoke(cli, "wait", session_id, "--json")
    second_wait = invoke(cli, "wait", session_id, "--json")

    assert first_wait.exit_code == 0, first_wait.stderr
    assert first_wait.stderr.count("reports usage as turn") == 1
    assert second_wait.exit_code == 0, second_wait.stderr
    assert "reports usage as" not in second_wait.stderr


def test_usage_drift_names_the_continued_turn_it_was_found_in(
    cli: CliRunner, state_root: Path
) -> None:
    _set_usage_profile(state_root, "codex_usage_updates")
    first = invoke(cli, "run", "mock", "rawusage:codex", "--json")
    session_id = json.loads(first.stdout)["session_id"]

    second = invoke(cli, "continue", session_id, "drift:codex", "--json")

    assert second.exit_code == 0, second.stderr
    assert json.loads(second.stdout)["usage"]["drift"]["turn"] == 2


def test_quiet_turn_suppresses_and_consumes_the_usage_drift_note(
    cli: CliRunner, state_root: Path
) -> None:
    _set_usage_profile(state_root, "codex_usage_updates")
    first = invoke(cli, "run", "mock", "drift:codex", "--json", "--quiet")
    session_id = json.loads(first.stdout)["session_id"]

    second = invoke(cli, "continue", session_id, "drift:codex", "--json")

    assert "reports usage as" not in first.stderr
    assert "reports usage as" not in second.stderr


def test_unknown_status_adds_one_usage_gap(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch, state_root: Path
) -> None:
    _set_usage_profile(state_root, "claude_model_usage")
    result = invoke(cli, "run", "mock", "rawusage:claude", "--json", "--quiet")
    assert result.exit_code == 0, result.stderr
    session_id = json.loads(result.stdout)["session_id"]
    meta = sessions.read_meta(session_id)
    meta.state = "running"
    meta.pid = 999_999
    meta.finished_at = None
    meta.exit_code = None
    with sessions.session_lock(session_id):
        sessions.write_meta(meta)
    monkeypatch.setattr(sessions.proc, "process_liveness", lambda *_args: "dead")

    status = invoke(cli, "status", session_id, "--json")

    assert status.exit_code == 0, status.stderr
    payload = json.loads(status.stdout)
    assert payload["status"] == "unknown"
    assert payload["usage"]["gaps"] == 1
    assert payload["usage"]["quality"] == "estimate"


@pytest.mark.parametrize("billing", ["subscription", "api"])
def test_declared_billing_is_reported_without_pricing(
    cli: CliRunner, state_root: Path, billing: str
) -> None:
    _set_usage_profile(state_root, "claude_model_usage", billing=billing)

    result = invoke(cli, "run", "mock", "rawusage:claude", "--json", "--quiet")

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["usage"]["billing"] == billing
    session_id = payload["session_id"]
    status = json.loads(invoke(cli, "status", session_id, "--json").stdout)
    assert status["usage"] == payload["usage"]
    assert sessions.read_meta(session_id).usage == payload["usage"]


def test_limit_rejected_before_activity_preserves_exact_claude_usage(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch, state_root: Path
) -> None:
    _set_usage_profile(state_root, "claude_model_usage")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_PROMPTS", "1")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_RESET_S", "0.01")

    async def resume_immediately(delay: float, cancel: runner._CancelSignal) -> bool:
        del delay, cancel
        return False

    monkeypatch.setattr(runner, "_sleep_through_limit", resume_immediately)

    result = invoke(cli, "run", "mock", "rawusage:claude:100,200", "--json", "--quiet")

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["usage"]["gaps"] == 0
    assert payload["usage"]["quality"] == "exact"


def test_resend_failing_after_a_quiet_limit_is_still_a_claude_gap(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch, state_root: Path
) -> None:
    _set_usage_profile(state_root, "claude_model_usage")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_PROMPTS", "1")

    async def resume_immediately(delay: float, cancel: runner._CancelSignal) -> bool:
        del delay, cancel
        return False

    monkeypatch.setattr(runner, "_sleep_through_limit", resume_immediately)
    first = invoke(cli, "run", "mock", "rawusage:claude", "--json", "--quiet")
    session_id = json.loads(first.stdout)["session_id"]

    invoke(cli, "continue", session_id, "crash-late:partial", "--json", "--quiet")

    usage_value = sessions.read_meta(session_id).usage
    assert usage_value is not None and usage_value["gaps"] == 1
    assert usage_value["quality"] == "estimate"


def test_limit_after_activity_adds_one_claude_gap(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch, state_root: Path
) -> None:
    _set_usage_profile(state_root, "claude_model_usage")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_PROMPTS", "1")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_META", "1")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_RESET_S", "0.01")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_AFTER_TEXT", "1")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_CLAUDE_USAGE", "1")

    result = invoke(cli, "run", "mock", "work after a limit", "--json", "--quiet")

    assert result.exit_code == 0, result.stderr
    usage_value = json.loads(result.stdout)["usage"]
    assert usage_value["gaps"] == 1
    assert usage_value["quality"] == "estimate"


def test_daemon_stop_after_prompt_records_a_claude_usage_gap(
    cli: CliRunner, live_daemon: None, state_root: Path, tmp_path: Path
) -> None:
    _set_usage_profile(state_root, "claude_model_usage")
    blocked = invoke(
        cli,
        "run",
        "mock",
        f"chunkhold:{tmp_path / 'release'}",
        "--bg",
        "--json",
        "--quiet",
    )
    assert blocked.exit_code == 0, blocked.stderr
    session_id = json.loads(blocked.stdout)["session_id"]
    _wait_for_message(session_id, "holding")
    daemon_status = invoke(cli, "daemon", "status", "mock", "--json")
    assert any(
        session_id in daemon_entry.get("sessions", [])
        for daemon_entry in json.loads(daemon_status.stdout)["items"]
    ), daemon_status.stdout

    stopped = invoke(cli, "daemon", "stop", "mock", "--force", "--json")

    assert stopped.exit_code == 0, stopped.stderr
    assert json.loads(stopped.stdout)["targets"]
    final = _wait_for_state(session_id, "failed")
    assert final.state == "failed", stopped.stdout
    assert final.usage is not None
    assert final.usage["gaps"] == 1
    assert final.usage["quality"] == "estimate"


def test_direct_exception_after_prompt_records_a_claude_usage_gap(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch, state_root: Path
) -> None:
    _set_usage_profile(state_root, "claude_model_usage")
    first = invoke(cli, "run", "mock", "rawusage:claude", "--json", "--quiet")
    assert first.exit_code == 0, first.stderr
    session_id = json.loads(first.stdout)["session_id"]
    original_append = transcript.Transcript.append
    failed = False

    def fail_limit_record(self, event_type: str, **fields):
        nonlocal failed
        if event_type == "limit" and not failed:
            failed = True
            raise OSError("transcript write failed after prompt activity")
        return original_append(self, event_type, **fields)

    monkeypatch.setattr(transcript.Transcript, "append", fail_limit_record)
    monkeypatch.setenv("ACPC_MOCK_LIMIT_PROMPTS", "1")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_META", "1")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_RESET_S", "0.01")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_AFTER_TEXT", "1")

    result = invoke(cli, "continue", session_id, "rawusage:claude", "--json", "--quiet")

    assert result.exit_code != 0
    final = sessions.read_meta(session_id)
    assert final.usage is not None
    assert final.usage["gaps"] == 1
    assert final.usage["quality"] == "estimate"


def test_daemon_usage_matches_result_status_and_meta_across_warm_and_cold_resumes(
    cli: CliRunner, live_daemon: None, state_root: Path
) -> None:
    _set_usage_profile(state_root, "claude_model_usage")

    def assert_usage_surfaces(turn_result: Any) -> dict[str, Any]:
        turn_payload = json.loads(turn_result.stdout)
        status = invoke(cli, "status", turn_payload["session_id"], "--json")
        assert status.exit_code == 0, status.stderr
        saved = json.loads(
            (state_root / "sessions" / turn_payload["session_id"] / "meta.json").read_text(
                encoding="utf-8"
            )
        )
        assert json.loads(status.stdout)["usage"] == turn_payload["usage"]
        assert saved["usage"] == turn_payload["usage"]
        return turn_payload

    first = invoke(cli, "run", "mock", "rawusage:claude", "--json", "--quiet")
    assert first.exit_code == 0, first.stderr
    first_payload = assert_usage_surfaces(first)
    session_id = first_payload["session_id"]

    warm = invoke(cli, "continue", session_id, "rawusage:claude", "--json", "--quiet")
    assert warm.exit_code == 0, warm.stderr
    warm_payload = assert_usage_surfaces(warm)
    assert warm_payload["usage"]["models"]["claude-opus-5[1m]"]["total_tokens"] == 270074

    stopped = invoke(cli, "daemon", "stop", "mock", "--json")
    assert stopped.exit_code == 0, stopped.stderr
    cold = invoke(cli, "continue", session_id, "rawusage:claude", "--json", "--quiet")
    assert cold.exit_code == 0, cold.stderr
    cold_payload = assert_usage_surfaces(cold)
    cold_usage = cold_payload["usage"]
    assert cold_usage["models"]["claude-opus-5[1m]"]["total_tokens"] == 405111


def test_codex_cli_usage_update_sequence_matches_all_json_surfaces(
    cli: CliRunner, state_root: Path
) -> None:
    _set_usage_profile(state_root, "codex_usage_updates")
    result = invoke(
        cli,
        "run",
        "mock",
        "rawusage:codex:21429,31373,15232,21746,31771,15177,21726,31704,15141,21682,31657,15048,21554",
        "--json",
        "--quiet",
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    session_id = payload["session_id"]
    expected = payload["usage"]
    assert expected["models"]["gpt-6-sol"]["total_tokens"] == 234642
    assert expected["calls"] == 9
    assert expected["compactions"]["count"] == 4
    assert expected["compactions"]["unaccounted"] == 4
    assert json.loads(invoke(cli, "status", session_id, "--json").stdout)["usage"] == expected
    saved = json.loads(
        (state_root / "sessions" / session_id / "meta.json").read_text(encoding="utf-8")
    )
    assert saved["usage"] == expected


@pytest.mark.parametrize("use_daemon", [False, True], ids=["direct", "daemon"])
def test_claude_turn_failed_by_the_adapter_is_one_lasting_gap(
    cli: CliRunner,
    use_daemon: bool,
    live_daemon: None,
    monkeypatch: pytest.MonkeyPatch,
    state_root: Path,
) -> None:
    _set_usage_profile(state_root, "claude_model_usage")
    if not use_daemon:

        async def unavailable(target: str) -> daemon_client.DaemonUnavailable:
            return daemon_client.DaemonUnavailable(f"direct test ({target})")

        monkeypatch.setattr(daemon_client, "ensure_daemon", unavailable)
    first = invoke(cli, "run", "mock", "rawusage:claude", "--json", "--quiet")
    session_id = json.loads(first.stdout)["session_id"]

    failed = invoke(cli, "continue", session_id, "crash-late:partial", "--json", "--quiet")
    failed_usage = sessions.read_meta(session_id).usage
    recovered = invoke(cli, "continue", session_id, "rawusage:claude", "--json", "--quiet")

    assert failed.exit_code != 0
    assert failed_usage is not None and failed_usage["gaps"] == 1
    usage_value = json.loads(recovered.stdout)["usage"]
    assert usage_value["gaps"] == 1
    assert usage_value["quality"] == "estimate"
    assert usage_value["models"]["claude-opus-5[1m]"]["total_tokens"] == 270074
