"""Behavioral tests for one turn end to end on the direct path."""

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest
from acp import RequestError

from acpc import cache, runner, sessions, vocab
from acpc.registry import AgentRegistry

MOCK_AGENT_SCRIPT = str(Path(__file__).with_name("mock_agent.py"))

MOCK_ENTRY = f"""
name = "Mock Agent"
command = "{sys.executable} {MOCK_AGENT_SCRIPT}"
install_command = "true"
home = "~/.mock"
home_env = "MOCK_HOME"
efforts = ["low", "medium", "high", "xhigh"]
mode = "default"

[modes]
default = {{ grants = "read", delegates = true }}
plan = {{ grants = "read", delegates = true }}
yolo = {{ grants = "all", delegates = false }}

[presets]
fast = {{ model = "mock-haiku-4-5", effort = "high" }}
standard = {{ model = "mock-sonnet-5", effort = "high" }}
max = {{ model = "mock-opus-5", effort = "xhigh" }}
"""

PHANTOM_ENTRY = """
name = "Phantom Agent"
command = "definitely-not-installed-phantom-xyz"
install_command = "false"
"""


@pytest.fixture(autouse=True)
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own ACPC_HOME with the mock adapter registered."""
    root = tmp_path / "state"
    agents = root / "agents"
    agents.mkdir(parents=True)
    (agents / "mock.toml").write_text(MOCK_ENTRY, encoding="utf-8")
    (agents / "phantom.toml").write_text(PHANTOM_ENTRY, encoding="utf-8")
    monkeypatch.setenv("ACPC_HOME", str(root))
    return root


def resolve(agent: str = "mock", **overrides: str | None):
    return AgentRegistry().resolve_call(agent, **overrides)


def start_turn(
    prompt: str,
    *,
    agent: str = "mock",
    permissions: str | None = None,
    mode: str | None = None,
    **kwargs,
) -> tuple[str, runner.TurnOutcome]:
    """Create a session and run one turn against the mock, as `run` does."""
    resolution = resolve(agent, mode=mode, permissions=permissions)
    meta = sessions.create_session(
        entry=resolution.entry.entry,
        base_adapter=resolution.entry.base_adapter,
        prompt=prompt,
        resolution=runner.resolution_payload(resolution, cwd=None),
        target=runner.call_target(resolution),
    )
    request = runner.TurnRequest(resolution=resolution, prompt=prompt, **kwargs)
    return meta.session_id, runner.execute_turn(meta.session_id, request)


def transcript_events(session_id: str) -> list[dict]:
    path = sessions.transcript_path(session_id)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# --- exit codes -------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("done", vocab.EXIT_OK),
        ("failed", vocab.EXIT_AGENT_ERROR),
        ("timeout", vocab.EXIT_TIMEOUT),
        ("cancelled", vocab.EXIT_CANCELLED),
        ("detached", vocab.EXIT_SIGTERM),
        ("terminated", vocab.EXIT_SIGTERM),
        ("orphaned", vocab.EXIT_AGENT_ERROR),
    ],
)
def test_each_final_state_maps_to_its_fixed_exit_code(state: str, expected: int) -> None:
    assert runner.exit_code_for(state) == expected


def test_exit_code_ignores_the_stop_reason_for_a_finished_turn() -> None:
    assert runner.exit_code_for("done", "refusal") == vocab.EXIT_OK
    assert runner.exit_code_for("failed", "end_turn") == vocab.EXIT_AGENT_ERROR


# --- the happy path ---------------------------------------------------------


def test_a_successful_turn_finishes_the_session_on_disk() -> None:
    session_id, outcome = start_turn("summarize the module layout")

    assert outcome.state == "done"
    assert outcome.exit_code == vocab.EXIT_OK
    assert "summarize the module layout" in outcome.answer

    meta = sessions.read_meta(session_id)
    assert meta.state == "done"
    assert meta.exit_code == vocab.EXIT_OK
    assert meta.finished_at is not None

    assert sessions.answer_path(session_id).read_text(encoding="utf-8") == outcome.answer
    prompt = sessions.prompt_path(session_id).read_text(encoding="utf-8")
    assert prompt == "summarize the module layout"


def test_a_turn_records_its_start_and_end_as_state_events() -> None:
    session_id, _ = start_turn("record the state transitions")

    transitions = [
        (event["from"], event["to"])
        for event in transcript_events(session_id)
        if event.get("type") == "state"
    ]
    assert ("starting", "running") in transitions
    assert ("running", "done") in transitions


def test_token_usage_reaches_the_session_metadata() -> None:
    session_id, outcome = start_turn("report some usage")

    assert outcome.tokens > 0
    assert sessions.read_meta(session_id).tokens == outcome.tokens


# --- failure, timeout, cancellation -----------------------------------------


def test_a_refused_turn_is_a_failure_with_exit_1() -> None:
    session_id, outcome = start_turn("please fail this on purpose")

    assert outcome.stop_reason == "refusal"
    assert outcome.state == "failed"
    assert outcome.exit_code == vocab.EXIT_AGENT_ERROR
    assert sessions.read_meta(session_id).state == "failed"


def test_a_refused_turn_still_writes_the_partial_answer() -> None:
    session_id, outcome = start_turn("please fail this on purpose")

    answer = sessions.answer_path(session_id).read_text(encoding="utf-8")
    assert "Unable to complete" in answer
    assert answer == outcome.answer


def test_a_switch_above_the_ceiling_ends_the_turn_with_exit_2() -> None:
    session_id, outcome = start_turn("run the perm scenario", permissions="read")

    assert outcome.state == "failed"
    assert outcome.stop_reason == "permission_denied"
    assert outcome.exit_code == vocab.EXIT_USAGE
    events = transcript_events(session_id)
    errors = [event["message"] for event in events if event.get("type") == "error"]
    assert any("switch_mode yolo" in message for message in errors)
    assert any("--permissions all" in message for message in errors)
    switch_permissions = [
        event
        for event in events
        if event.get("type") == "permission" and event.get("kind") == "switch_mode"
    ]
    # The perm mock requests plan immediately after yolo; cancellation must win that race.
    assert len(switch_permissions) == 1
    assert switch_permissions[-1]["decision"] == "deny"
    assert not any(
        "switch_mode:plan" in event.get("message", "")
        for event in events
        if event.get("type") == "error"
    )


def test_timeout_cancels_the_turn_and_exits_124() -> None:
    session_id, outcome = start_turn("slow:30 timeout probe", timeout=1.0)

    assert outcome.state == "timeout"
    assert outcome.exit_code == vocab.EXIT_TIMEOUT
    meta = sessions.read_meta(session_id)
    assert meta.state == "timeout"
    assert meta.exit_code == vocab.EXIT_TIMEOUT


def test_a_timed_out_turn_leaves_an_answer_file_behind() -> None:
    session_id, _ = start_turn("slow:30 timeout probe", timeout=1.0)

    assert sessions.answer_path(session_id).exists()


def deliver_after(delay: float, signal_number: int) -> None:
    """Signal this very process once the turn is under way."""

    def send() -> None:
        time.sleep(delay)
        os.kill(os.getpid(), signal_number)

    threading.Thread(target=send, daemon=True).start()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal semantics")
def test_sigint_during_a_turn_cancels_it_and_exits_130() -> None:
    deliver_after(1.0, signal.SIGINT)

    session_id, outcome = start_turn("slow:30 sigint probe")

    assert outcome.state == "cancelled"
    assert outcome.exit_code == vocab.EXIT_CANCELLED
    assert sessions.read_meta(session_id).state == "cancelled"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal semantics")
def test_sigterm_on_the_direct_path_cancels_too_but_exits_143() -> None:
    # A direct child cannot outlive its parent, so SIGTERM cannot detach here.
    deliver_after(1.0, signal.SIGTERM)

    session_id, outcome = start_turn("slow:30 sigterm probe")

    assert outcome.exit_code == vocab.EXIT_SIGTERM
    assert sessions.read_meta(session_id).state == "cancelled"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal semantics")
def test_a_cancelled_turn_keeps_whatever_answer_had_arrived() -> None:
    deliver_after(1.0, signal.SIGINT)

    session_id, _ = start_turn("slow:30 partial answer probe")

    assert sessions.answer_path(session_id).exists()


# --- routing ----------------------------------------------------------------


def test_the_direct_child_fallback_is_reported_not_silent() -> None:
    _, outcome = start_turn("note the route")

    assert outcome.route_note is not None
    assert outcome.route_note.startswith("direct child")


def test_a_missing_adapter_binary_names_the_install_command() -> None:
    with pytest.raises(runner.RunnerError) as caught:
        runner.adapter_command(resolve("phantom"))

    message = str(caught.value)
    assert "definitely-not-installed-phantom-xyz" in message
    assert "acpc install phantom" in message


def test_a_turn_that_cannot_start_still_leaves_a_finished_session() -> None:
    resolution = resolve("phantom")
    meta = sessions.create_session(
        entry=resolution.entry.entry,
        base_adapter=resolution.entry.base_adapter,
        prompt="never runs",
        resolution=runner.resolution_payload(resolution, cwd=None),
        target=runner.call_target(resolution),
    )
    with pytest.raises(runner.RunnerError):
        runner.execute_turn(
            meta.session_id, runner.TurnRequest(resolution=resolution, prompt="never runs")
        )

    # The one state SPEC says no reader may ever be shown after the process is gone.
    assert sessions.read_meta(meta.session_id).state == "failed"


def test_two_entries_on_the_same_adapter_and_home_share_a_target() -> None:
    assert runner.call_target(resolve("mock")) == runner.call_target(resolve("mock"))


# --- call options reach the adapter -----------------------------------------


def test_model_and_effort_are_applied_to_the_adapter_session() -> None:
    _, outcome = start_turn("settings")

    model, effort, _mode, model_calls, _mode_calls, effort_calls = outcome.answer.strip().split("/")
    assert model == "mock-sonnet-5"
    assert effort == "high"
    assert model_calls == "1"
    assert effort_calls == "1"


def test_a_resolved_mode_is_always_applied_to_the_adapter_session() -> None:
    _, outcome = start_turn("settings")

    _model, _effort, mode, _model_calls, mode_calls, _effort_calls = outcome.answer.strip().split(
        "/"
    )
    assert mode == "default"
    assert mode_calls == "1"


def test_mode_is_applied_via_set_session_mode() -> None:
    _, outcome = start_turn("settings", mode="plan")

    _model, _effort, mode, _model_calls, mode_calls, _effort_calls = outcome.answer.strip().split(
        "/"
    )
    assert mode == "plan"
    assert mode_calls == "1"


def test_an_entry_mode_is_applied_via_set_session_mode(state_root: Path) -> None:
    (state_root / "agents" / "pinned.toml").write_text(
        'extends = "mock"\nmode = "plan"\n', encoding="utf-8"
    )

    _, outcome = start_turn("settings", agent="pinned")

    _model, _effort, mode, _model_calls, mode_calls, _effort_calls = outcome.answer.strip().split(
        "/"
    )
    assert mode == "plan"
    assert mode_calls == "1"


def test_an_unknown_mode_fails_the_turn_rather_than_running_it() -> None:
    session_id, outcome = start_turn("settings", mode="not-a-mode")

    assert outcome.state == "failed"
    assert sessions.read_meta(session_id).state == "failed"


def test_the_entrys_effort_config_id_names_the_wire_option(state_root: Path) -> None:
    """The effort config id is a vendor fact (codex speaks `reasoning_effort`,
    claude speaks `effort`), so the entry declares it and a wrong id fails the
    turn with the vendor's own diagnosis, not a bare 'Internal error'."""
    (state_root / "agents" / "custom.toml").write_text(
        'extends = "mock"\neffort_config_id = "custom_effort"\neffort = "high"\n',
        encoding="utf-8",
    )

    session_id, outcome = start_turn("echo:hi", agent="custom")

    assert outcome.state == "failed"
    answer = sessions.answer_path(session_id).read_text(encoding="utf-8")
    assert "the adapter rejected effort 'high' (config option 'custom_effort')" in answer
    assert "Unknown config option: custom_effort" in answer
    assert "(JSON-RPC -32603)" in answer


def test_an_unknown_effort_option_names_the_model_that_may_not_take_one(
    state_root: Path,
) -> None:
    """An adapter that has no effort option at all answers with a generic
    internal error, so the bare rejection reads as a bug in acpc. The hint
    names the model, which is the thing the caller can actually change."""
    (state_root / "agents" / "custom.toml").write_text(
        'extends = "mock"\n'
        'effort_config_id = "custom_effort"\n'
        'effort = "high"\n'
        'model = "mock-opus-5"\n',
        encoding="utf-8",
    )

    session_id, outcome = start_turn("echo:hi", agent="custom")

    assert outcome.state == "failed"
    answer = sessions.answer_path(session_id).read_text(encoding="utf-8")
    assert "Unknown config option: custom_effort" in answer
    assert "model 'mock-opus-5' may not take an effort setting" in answer
    assert "drop --effort, or drop effort from the preset in the entry TOML" in answer


def test_describe_error_surfaces_the_json_rpc_data() -> None:
    """A JSON-RPC error's fixed message hides the vendor's diagnosis in data."""
    error = RequestError(
        -32603, "Internal error", {"details": "Unknown config option: reasoning_effort"}
    )

    described = runner.describe_error(error)

    assert described == (
        "Internal error: Unknown config option: reasoning_effort (JSON-RPC -32603)"
    )
    assert runner.describe_error(ValueError("plain failure")) == "plain failure"


def test_the_cwd_is_where_the_adapter_runs(tmp_path: Path) -> None:
    workdir = tmp_path / "callee"
    workdir.mkdir()

    start_turn("write-file:proof.txt", cwd=str(workdir), permissions="write")

    assert (workdir / "proof.txt").exists()


# --- seams ------------------------------------------------------------------


def test_advertised_data_is_handed_to_the_cache_after_a_successful_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        cache,
        "refresh_advertised",
        lambda agent, advertised: seen.append((agent, dict(advertised))),
    )

    start_turn("refresh the advertised data")

    assert seen
    agent, advertised = seen[0]
    assert agent == "mock"
    assert advertised


def test_a_broken_cache_never_fails_a_turn_that_produced_an_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(agent: str, advertised: object) -> None:
        raise RuntimeError("cache is on fire")

    monkeypatch.setattr(cache, "refresh_advertised", explode)

    session_id, outcome = start_turn("survive a broken cache")

    assert outcome.state == "done"
    assert sessions.read_meta(session_id).state == "done"


def test_auto_prune_swallows_a_failing_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise OSError("state root is gone")

    monkeypatch.setattr(sessions, "prune_sessions", explode)

    runner.auto_prune(3600.0)  # must not raise


# --- the --dry-run view -----------------------------------------------------


def test_the_dry_run_payload_reports_every_value_with_its_source() -> None:
    payload = runner.resolution_payload(resolve("mock"), cwd=None)

    assert payload["entry"] == "mock"
    assert payload["base_adapter"] == "mock"
    assert payload["resolved"]["model"]["value"] == "mock-sonnet-5"
    assert payload["resolved"]["model"]["source"] == "adapter default"


def test_a_call_flag_is_labelled_as_coming_from_the_call() -> None:
    payload = runner.resolution_payload(resolve("mock", model="mock-opus-5"), cwd=None)

    assert payload["resolved"]["model"]["value"] == "mock-opus-5"
    assert payload["resolved"]["model"]["source"] == "call flag"


def test_the_dry_run_payload_carries_the_cwd_it_was_given() -> None:
    payload = runner.resolution_payload(resolve("mock"), cwd="/somewhere")

    assert payload["cwd"] == "/somewhere"
