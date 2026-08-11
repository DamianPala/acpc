"""Behavioral tests for the ``continue`` verb."""

import asyncio
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from acpc import cli as cli_module
from acpc import daemon_client, runner, sessions, transcript, vocab
from acpc.cli import main

MOCK_AGENT_SCRIPT = str(Path(__file__).with_name("mock_agent.py"))
NO_LOAD_AGENT_SCRIPT = str(Path(__file__).with_name("no_load_session_agent.py"))

MOCK_ENTRY = f"""
name = "Mock Agent"
command = "{sys.executable} {MOCK_AGENT_SCRIPT}"
install_command = "true"
home = "~/.mock"
home_env = "MOCK_HOME"
efforts = ["low", "medium", "high", "xhigh"]

[modes]
default = {{ grants = "read", delegates = true }}
plan = {{ grants = "read", delegates = true }}
yolo = {{ grants = "all", delegates = false }}

[presets]
fast = {{ model = "mock-haiku-4-5", effort = "high" }}
standard = {{ model = "mock-sonnet-5", effort = "high" }}
max = {{ model = "mock-opus-5", effort = "xhigh" }}
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


@pytest.fixture
def fresh_permission_alias_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "_WARNED_PERMISSION_ALIASES", set())


def invoke(cli: CliRunner, *args: str, stdin: str | None = None):
    return cli.invoke(main, list(args), input=stdin, catch_exceptions=False)


def continue_subprocess(
    session_id: str, prompt: str, *options: str
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        "-c",
        "from acpc.cli import main; raise SystemExit(main())",
        "continue",
        session_id,
        prompt,
        *options,
    ]
    return subprocess.run(
        command,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def run_cli_until_early_line(*args: str) -> tuple[int, str, str, str, bool]:
    process = subprocess.Popen(
        [sys.executable, "-c", "from acpc.cli import main; main()", *args],
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stderr is not None

    early_line_queue: queue.Queue[str] = queue.Queue()

    def read_early_line() -> None:
        assert process.stderr is not None
        early_line_queue.put(process.stderr.readline())

    threading.Thread(target=read_early_line, daemon=True).start()
    try:
        early_line = early_line_queue.get(timeout=5)
    except queue.Empty:
        process.kill()
        process.wait()
        pytest.fail("blocking CLI did not emit the early session line")

    still_running = process.poll() is None
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise
    stdout = process.stdout.read()
    stderr = early_line + process.stderr.read()
    return process.returncode, early_line, stdout, stderr, still_running


def start_session(cli: CliRunner, prompt: str = "turn one") -> str:
    result = invoke(cli, "run", "mock", prompt, "--quiet", "--json")
    assert result.exit_code == vocab.EXIT_OK
    return json.loads(result.stdout)["session_id"]


def test_continue_rotates_the_previous_turn_artifacts(cli: CliRunner) -> None:
    session_id = start_session(cli, "turn one")

    result = invoke(cli, "continue", session_id, "turn two", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert sessions.turn_path(session_id, "prompt", 1).is_file()
    assert sessions.turn_path(session_id, "answer", 1).is_file()
    assert sessions.prompt_path(session_id).read_text(encoding="utf-8") == "turn two"
    assert sessions.answer_path(session_id).is_file()


def test_continue_accepts_a_suffixed_timeout(cli: CliRunner) -> None:
    session_id = start_session(cli)

    result = invoke(
        cli, "continue", session_id, "slow:2 suffixed continue", "--timeout", "1m", "--quiet"
    )

    assert result.exit_code == vocab.EXIT_OK
    assert "waited 2s" in result.stdout


def test_blocking_continue_emits_the_early_line_before_a_slow_turn_finishes(
    cli: CliRunner, live_daemon: None
) -> None:
    session_id = start_session(cli)

    returncode, early_line, stdout, _stderr, still_running = run_cli_until_early_line(
        "continue", session_id, "slow:2 early continue probe"
    )

    assert still_running
    assert returncode == vocab.EXIT_OK
    assert early_line.startswith("-- session ")
    assert "waited 2s" in stdout


def test_quiet_continue_suppresses_the_early_line(cli: CliRunner) -> None:
    session_id = start_session(cli)

    result = invoke(cli, "continue", session_id, "turn two", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert result.stderr == ""


def test_continue_normalizes_legacy_stored_write_policy_after_entry_changes(
    cli: CliRunner, state_root: Path
) -> None:
    first = invoke(
        cli,
        "run",
        "mock",
        "settings",
        "--model",
        "max",
        "--permissions",
        "write",
        "--quiet",
        "--json",
    )
    session_id = json.loads(first.stdout)["session_id"]
    # Simulate pre-0.5 meta.json; continue must accept it without rewriting it.
    meta = sessions.load(session_id)
    meta.resolution["resolved"]["permissions"]["value"] = "write"
    with sessions.session_lock(session_id):
        sessions.write_meta(meta)
    (state_root / "agents" / "mock.toml").write_text(
        'name = "Changed"\ncommand = "missing-after-first-turn"\n', encoding="utf-8"
    )

    result = invoke(cli, "continue", session_id, "settings", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert "mock-opus-5/xhigh" in result.stdout
    assert sessions.load(session_id).resolution["resolved"]["permissions"]["value"] == "write"


def test_continue_migrates_legacy_mode_and_target_metadata(cli: CliRunner) -> None:
    session_id = start_session(cli)
    meta = sessions.load(session_id)
    old_target = "mock~legacy-s3-target"
    meta.target = old_target
    meta.resolution["resolved"].pop("permissions")
    for field in ("mode", "grants", "delegates"):
        meta.resolution["adapter"].pop(field, None)
    with sessions.session_lock(session_id):
        sessions.write_meta(meta)

    result = invoke(cli, "continue", session_id, "turn two", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    migrated = sessions.load(session_id)
    assert migrated.resolution["resolved"]["permissions"]["value"] == "read"
    assert migrated.resolution["adapter"] == {
        "home_env": "MOCK_HOME",
        "effort_config_id": None,
        "modes": {
            "default": {"grants": "read", "delegates": True},
            "plan": {"grants": "read", "delegates": True},
            "yolo": {"grants": "all", "delegates": False},
        },
        "mode": "default",
        "grants": "read",
        "delegates": True,
    }
    expected_target = runner.call_target(runner.resolution_from_session(migrated))
    assert migrated.target == expected_target
    assert migrated.target != old_target


def test_continue_preserves_meta_written_after_initial_load(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = start_session(cli)
    original_load = cli_module._load_view_session

    def load_then_write(selector: str) -> sessions.SessionMeta:
        loaded = original_load(selector)
        current = sessions.read_meta(loaded.session_id)
        current.name = "concurrent-name"
        with sessions.session_lock(loaded.session_id):
            sessions.write_meta(current)
        return loaded

    monkeypatch.setattr(cli_module, "_load_view_session", load_then_write)
    result = invoke(cli, "continue", session_id, "turn two", "--permissions", "edit", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert sessions.load(session_id).name == "concurrent-name"


def test_continue_uses_policy_returned_by_rotation(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first = invoke(
        cli,
        "run",
        "mock",
        "turn one",
        "--cwd",
        str(tmp_path),
        "--quiet",
        "--json",
    )
    session_id = json.loads(first.stdout)["session_id"]
    original_rotate = sessions.rotate_turn

    def rotate_after_a_concurrent_policy_change(
        session_id: str, **kwargs: Any
    ) -> sessions.SessionMeta:
        current = sessions.read_meta(session_id)
        current.resolution["resolved"]["permissions"] = {
            "value": "edit",
            "source": "call flag",
        }
        current.resolution.pop("permissions_source", None)
        with sessions.session_lock(session_id):
            sessions.write_meta(current)
        return original_rotate(session_id, **kwargs)

    monkeypatch.setattr(sessions, "rotate_turn", rotate_after_a_concurrent_policy_change)
    result = invoke(cli, "continue", session_id, "write-file:raced.md", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert (tmp_path / "raced.md").is_file()
    rotated = sessions.load(session_id)
    assert rotated.target == runner.call_target(runner.resolution_from_session(rotated))


def test_continue_routes_using_the_persisted_environment_snapshot(
    cli: CliRunner, state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = start_session(cli)
    (state_root / "agents" / "mock.toml").write_text(
        MOCK_ENTRY + '\n[env]\nTARGET_SHAPE = "changed"\n', encoding="utf-8"
    )
    routed_targets: list[str] = []
    original_execute = runner.execute_turn

    def record_route_target(session_id: str, request: runner.TurnRequest) -> runner.TurnOutcome:
        routed_targets.append(runner.call_target(request.resolution))
        return original_execute(session_id, request)

    monkeypatch.setattr(runner, "execute_turn", record_route_target)
    result = invoke(cli, "continue", session_id, "turn two", "--permissions", "edit", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert len(routed_targets) == 1
    rotated = sessions.load(session_id)
    assert rotated.resolution["env"] == {}
    assert rotated.target == routed_targets[0]
    assert rotated.target == runner.call_target(runner.resolution_from_session(rotated))


def test_continue_post_rotation_failure_finalizes_the_new_turn(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = start_session(cli)
    original_rotate = sessions.rotate_turn

    def rotate_after_a_concurrent_corruption(
        session_id: str, **kwargs: Any
    ) -> sessions.SessionMeta:
        current = sessions.read_meta(session_id)
        current.resolution["cwd"] = 42
        with sessions.session_lock(session_id):
            sessions.write_meta(current)
        return original_rotate(session_id, **kwargs)

    monkeypatch.setattr(sessions, "rotate_turn", rotate_after_a_concurrent_corruption)
    result = invoke(cli, "continue", session_id, "turn two")

    failed = sessions.load(session_id)
    assert result.exit_code == vocab.EXIT_USAGE
    assert failed.state == "failed"
    assert failed.turns == 2
    assert failed.stop_reason == "error"


def test_fault_after_claim_before_owner_finalizes_the_turn(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = start_session(cli)
    original_append = transcript.Transcript.append

    def fail_running_event(
        self: transcript.Transcript, event_type: str, **fields: Any
    ) -> dict[str, Any]:
        if event_type == "state" and fields.get("to") == "running":
            raise OSError("injected state-event failure")
        return original_append(self, event_type, **fields)

    monkeypatch.setattr(transcript.Transcript, "append", fail_running_event)

    result = invoke(cli, "continue", session_id, "must not prompt", "--quiet")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    failed = sessions.load(session_id)
    assert failed.state == "failed"
    assert failed.turns == 2
    assert sessions.prompt_path(session_id).read_text(encoding="utf-8") == "must not prompt"


def test_fault_before_prompt_dispatch_finalizes_the_claimed_turn(
    cli: CliRunner, state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = start_session(cli, "turn one")

    async def fail_options(*args: Any, **kwargs: Any) -> None:
        raise OSError("injected pre-prompt failure")

    monkeypatch.setattr(runner, "apply_call_options", fail_options)

    result = invoke(cli, "continue", session_id, "must not prompt", "--quiet")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    failed = sessions.load(session_id)
    assert failed.state == "failed"
    assert failed.turns == 2
    store = json.loads((state_root / "mock-sessions.json").read_text(encoding="utf-8"))
    adapter_session_id = failed.adapter_session_id
    assert adapter_session_id is not None
    assert "must not prompt" not in store[adapter_session_id]["history"]


def test_continue_reapplies_the_stored_mode(cli: CliRunner) -> None:
    first = invoke(cli, "run", "mock", "settings", "--mode", "plan", "--quiet", "--json")
    session_id = json.loads(first.stdout)["session_id"]

    stored = sessions.load(session_id).resolution["resolved"]["mode"]
    assert stored == {
        "value": "plan",
        "source": "call flag",
        "grants": "read",
        "delegates": True,
        "escalates": False,
    }

    result = invoke(cli, "continue", session_id, "settings", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert "/plan/1/" in result.stdout


def test_continue_without_a_stored_mode_selects_and_sends_a_mode(cli: CliRunner) -> None:
    session_id = start_session(cli, "settings")
    meta = sessions.load(session_id)
    del meta.resolution["resolved"]["mode"]
    for key in ("mode", "grants", "delegates"):
        meta.resolution["adapter"].pop(key, None)
    with sessions.session_lock(session_id):
        sessions.write_meta(meta)

    result = invoke(cli, "continue", session_id, "settings", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    _model, _effort, mode, _model_calls, mode_calls, _effort_calls = result.stdout.split("/")
    assert mode == "default"
    assert mode_calls == "1"


def test_continue_without_permissions_uses_stored_mode_facts_after_registry_edit(
    cli: CliRunner, state_root: Path
) -> None:
    session_id = start_session(cli, "settings")
    entry = state_root / "agents" / "mock.toml"
    edited = (
        MOCK_ENTRY.replace(
            'default = { grants = "read", delegates = true }',
            'default = { grants = "execute", delegates = false }',
        )
        .replace(
            'plan = { grants = "read", delegates = true }',
            'plan = { grants = "execute", delegates = true }',
        )
        .replace(
            'yolo = { grants = "all", delegates = false }',
            'yolo = { grants = "read", delegates = true }',
        )
    )
    entry.write_text(edited, encoding="utf-8")

    result = invoke(cli, "continue", session_id, "perm scenario", "--json")

    assert result.exit_code == vocab.EXIT_USAGE
    assert json.loads(result.stdout)["denied"][-1] == {
        "category": "switch_mode",
        "target": "yolo",
        "count": 1,
        "minimum_policy": "all",
        "remedy": "pass --permissions all",
    }

    stored = sessions.load(session_id).resolution["adapter"]["modes"]
    assert stored["yolo"] == {"grants": "all", "delegates": False}


def test_continue_with_a_higher_policy_reselects_and_stores_current_mode_facts(
    cli: CliRunner, state_root: Path
) -> None:
    session_id = start_session(cli, "settings")
    entry = state_root / "agents" / "mock.toml"
    edited = MOCK_ENTRY.replace(
        'default = { grants = "read", delegates = true }',
        'default = { grants = "execute", delegates = false }',
    ).replace(
        'plan = { grants = "read", delegates = true }',
        'plan = { grants = "execute", delegates = true }',
    )
    entry.write_text(edited, encoding="utf-8")

    result = invoke(
        cli,
        "continue",
        session_id,
        "settings",
        "--permissions",
        "execute",
        "--quiet",
        "--json",
    )

    assert result.exit_code == vocab.EXIT_OK
    stored = sessions.load(session_id).resolution
    assert stored["resolved"]["mode"] == {
        "value": "plan",
        "source": "selected",
        "grants": "execute",
        "delegates": True,
        "escalates": False,
    }
    assert stored["adapter"]["mode"] == "plan"
    assert stored["adapter"]["grants"] == "execute"
    assert stored["adapter"]["delegates"] is True
    assert stored["adapter"]["modes"]["default"] == {
        "grants": "execute",
        "delegates": False,
    }


def test_pre_05_session_without_mode_selects_once_from_stored_policy(
    cli: CliRunner,
) -> None:
    session_id = start_session(cli, "settings")
    meta = sessions.load(session_id)
    del meta.resolution["resolved"]["mode"]
    for key in ("mode", "grants", "delegates"):
        meta.resolution["adapter"].pop(key, None)
    with sessions.session_lock(session_id):
        sessions.write_meta(meta)

    result = invoke(cli, "continue", session_id, "settings", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    stored = sessions.load(session_id).resolution
    assert stored["resolved"]["mode"]["value"] == "default"
    assert stored["adapter"]["mode"] == "default"


def test_continue_preserves_the_stored_home_environment(cli: CliRunner) -> None:
    session_id = start_session(cli)

    result = invoke(cli, "continue", session_id, "env:MOCK_HOME", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert str(Path("~/.mock").expanduser()) in result.stdout


def test_continue_rejects_an_adapter_without_load_session(
    cli: CliRunner, state_root: Path, live_daemon: None
) -> None:
    (state_root / "agents" / "mock.toml").write_text(
        MOCK_ENTRY.replace(MOCK_AGENT_SCRIPT, NO_LOAD_AGENT_SCRIPT), encoding="utf-8"
    )
    session_id = start_session(cli)

    async def stop_daemon() -> None:
        resolution = runner.resolution_from_session(sessions.load(session_id))
        connection = await daemon_client.connect(runner.call_target(resolution))
        assert connection is not None
        try:
            await connection.stop()
        finally:
            await connection.close()

    asyncio.run(stop_daemon())

    result = invoke(cli, "continue", session_id, "turn two", "--quiet")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    assert "continue requires an adapter that can restore a session" in result.stderr
    assert "session/resume or session/load" in result.stderr


def test_continue_cold_resume_does_not_replay_adapter_history(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = start_session(cli, "turn one of the conversation")
    method_file = tmp_path / "session-method"
    monkeypatch.setenv("ACPC_MOCK_SESSION_METHOD_FILE", str(method_file))

    result = invoke(cli, "continue", session_id, "turn two", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    answer = sessions.answer_path(session_id).read_text(encoding="utf-8")
    assert method_file.read_text(encoding="utf-8") == "load\n"
    assert not answer.startswith("history")
    assert 'You asked: "turn two"' in answer


def test_cold_load_resume_reapplies_model_and_effort(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    method_file = tmp_path / "session-method"
    monkeypatch.setenv("ACPC_MOCK_SESSION_METHOD_FILE", str(method_file))
    first = invoke(
        cli,
        "run",
        "mock",
        "settings",
        "--model",
        "mock-opus-5",
        "--effort",
        "xhigh",
        "--quiet",
        "--json",
    )
    session_id = json.loads(first.stdout)["session_id"]

    result = invoke(cli, "continue", session_id, "settings", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert method_file.read_text(encoding="utf-8") == "load\n"
    answer = sessions.answer_path(session_id).read_text(encoding="utf-8")
    assert "mock-opus-5/xhigh" in answer


def test_cold_resume_prefers_session_resume_when_advertised(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    method_file = tmp_path / "session-method"
    monkeypatch.setenv("ACPC_MOCK_ADVERTISE_RESUME", "1")
    monkeypatch.setenv("ACPC_MOCK_SESSION_METHOD_FILE", str(method_file))
    session_id = start_session(cli, "turn one")

    result = invoke(cli, "continue", session_id, "turn two", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert method_file.read_text(encoding="utf-8") == "resume\n"
    answer = sessions.answer_path(session_id).read_text(encoding="utf-8")
    assert not answer.startswith("history")
    assert 'You asked: "turn two"' in answer


def test_resume_without_list_or_replay_is_explicitly_unverified(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    method_file = tmp_path / "session-method"
    monkeypatch.setenv("ACPC_MOCK_ADVERTISE_LIST", "0")
    monkeypatch.setenv("ACPC_MOCK_ADVERTISE_RESUME", "1")
    monkeypatch.setenv("ACPC_MOCK_SESSION_METHOD_FILE", str(method_file))
    session_id = start_session(cli, "turn one")

    result = invoke(cli, "continue", session_id, "turn two", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert method_file.read_text(encoding="utf-8") == "resume\n"
    assert "turn two" in sessions.answer_path(session_id).read_text(encoding="utf-8")


def test_cold_resume_reports_unverified_in_json_and_summary(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPC_MOCK_ADVERTISE_LIST", "0")
    monkeypatch.setenv("ACPC_MOCK_ADVERTISE_RESUME", "1")
    session_id = start_session(cli, "turn one")

    result = invoke(cli, "continue", session_id, "turn two", "--json")

    assert result.exit_code == vocab.EXIT_OK
    assert (
        json.loads(result.stdout)["resume"]
        == "unverified — session/list unavailable; conversation replay unavailable"
    )
    assert "resume: unverified" in result.stderr


def test_list_without_replay_is_verified_on_the_direct_path(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = start_session(cli, "turn one")
    monkeypatch.setenv("ACPC_MOCK_REPLAY_USER_MESSAGES", "0")

    result = invoke(cli, "continue", session_id, "turn two", "--json")

    assert result.exit_code == vocab.EXIT_OK
    assert json.loads(result.stdout)["resume"] == "verified"
    assert "resume: verified" in result.stderr


def test_replay_only_verification_is_verified_when_session_list_is_unavailable(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPC_MOCK_ADVERTISE_LIST", "0")
    session_id = start_session(cli, "turn one")

    result = invoke(cli, "continue", session_id, "turn two", "--json")

    assert result.exit_code == vocab.EXIT_OK
    assert json.loads(result.stdout)["resume"] == "verified"


def test_replay_only_mismatch_fails_before_rotation(
    cli: CliRunner, state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPC_MOCK_ADVERTISE_LIST", "0")
    session_id = start_session(cli, "recorded context")
    store_path = state_root / "mock-sessions.json"
    store = json.loads(store_path.read_text(encoding="utf-8"))
    adapter_session_id = sessions.read_meta(session_id).adapter_session_id
    assert adapter_session_id is not None
    store[adapter_session_id]["history"] = ["different context"]
    store_path.write_text(json.dumps(store), encoding="utf-8")
    before = sessions.read_meta(session_id)

    result = invoke(cli, "continue", session_id, "must not run", "--quiet")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    after = sessions.read_meta(session_id)
    assert after.state == before.state == "done"
    assert after.turns == before.turns == 1
    assert sessions.prompt_path(session_id).read_text(encoding="utf-8") == "recorded context"


def test_advertised_list_without_the_stored_adapter_id_fails_before_restore(
    cli: CliRunner, state_root: Path
) -> None:
    session_id = start_session(cli, "recorded context")
    store_path = state_root / "mock-sessions.json"
    store = json.loads(store_path.read_text(encoding="utf-8"))
    adapter_session_id = sessions.read_meta(session_id).adapter_session_id
    assert adapter_session_id is not None
    store.pop(adapter_session_id)
    store_path.write_text(json.dumps(store), encoding="utf-8")

    result = invoke(cli, "continue", session_id, "must not run", "--quiet")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    assert sessions.read_meta(session_id).turns == 1
    assert "was not found by session/list" in result.stderr


def test_list_mismatch_fails_even_when_replay_matches(cli: CliRunner, state_root: Path) -> None:
    session_id = start_session(cli, "recorded context")
    store_path = state_root / "mock-sessions.json"
    store = json.loads(store_path.read_text(encoding="utf-8"))
    adapter_session_id = sessions.read_meta(session_id).adapter_session_id
    assert adapter_session_id is not None
    store[adapter_session_id]["cwd"] = "/different/cwd"
    store_path.write_text(json.dumps(store), encoding="utf-8")

    result = invoke(cli, "continue", session_id, "must not run", "--quiet")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    assert "cwd" in result.stderr
    assert sessions.read_meta(session_id).turns == 1


def test_replay_ignores_agent_text_thoughts_and_tools(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPC_MOCK_ADVERTISE_LIST", "0")
    monkeypatch.setenv("ACPC_MOCK_REPLAY_EXTRA_EVENTS", "1")
    session_id = start_session(cli, "turn one")

    result = invoke(cli, "continue", session_id, "turn two", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    transcript = sessions.transcript_path(session_id).read_text(encoding="utf-8")
    assert "replayed agent text" not in transcript
    assert "replayed thought" not in transcript
    assert "replayed tool" not in transcript


def test_a_stored_but_undelivered_prompt_is_not_required_on_later_resume(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = start_session(cli, "turn one")
    monkeypatch.setenv("ACPC_MOCK_DISCONNECT_BEFORE_PROMPT", "1")

    failed = invoke(cli, "continue", session_id, "must not reach adapter", "--quiet")

    assert failed.exit_code == vocab.EXIT_AGENT_ERROR
    assert sessions.load(session_id).state == "failed"
    assert sessions.load(session_id).turns == 2
    delivered = sessions.load(session_id).extra["delivered_prompts"]
    assert [record["turn"] for record in delivered] == [1]

    monkeypatch.delenv("ACPC_MOCK_DISCONNECT_BEFORE_PROMPT")
    resumed = invoke(cli, "continue", session_id, "later prompt", "--quiet")

    assert resumed.exit_code == vocab.EXIT_OK
    assert sessions.load(session_id).state == "done"
    assert "later prompt" in sessions.answer_path(session_id).read_text(encoding="utf-8")


def test_process_death_after_claim_is_orphaned_and_can_be_cold_resumed(
    cli: CliRunner, state_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = start_session(cli, "turn one")
    release = tmp_path / "release-before-prompt"
    ready = tmp_path / "before-prompt-ready"
    monkeypatch.setenv("ACPC_MOCK_BLOCK_BEFORE_PROMPT", str(release))
    monkeypatch.setenv("ACPC_MOCK_BLOCK_BEFORE_PROMPT_READY", str(ready))
    command = [
        sys.executable,
        "-c",
        "from acpc.cli import main; raise SystemExit(main())",
        "continue",
        session_id,
        "must not cross the wire",
        "--quiet",
    ]
    process = subprocess.Popen(
        command,
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not ready.exists():
        time.sleep(0.02)
    assert ready.exists()
    running = sessions.read_meta(session_id)
    assert running.state == "running"
    assert running.turns == 2
    store = json.loads((state_root / "mock-sessions.json").read_text(encoding="utf-8"))
    adapter_session_id = running.adapter_session_id
    assert adapter_session_id is not None
    assert "must not cross the wire" not in store[adapter_session_id]["history"]
    assert running.pid is not None
    os.kill(process.pid, signal.SIGKILL)
    os.kill(running.pid, signal.SIGKILL)
    release.touch()
    _stdout, _stderr = process.communicate(timeout=10)
    assert process.returncode == -9

    deadline = time.monotonic() + 5
    orphaned = sessions.load(session_id)
    while orphaned.state == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
        orphaned = sessions.load(session_id)
    assert orphaned.state == "orphaned"
    monkeypatch.delenv("ACPC_MOCK_BLOCK_BEFORE_PROMPT")
    monkeypatch.delenv("ACPC_MOCK_BLOCK_BEFORE_PROMPT_READY")

    resumed = invoke(cli, "continue", session_id, "cold follow-up", "--quiet")

    assert resumed.exit_code == vocab.EXIT_OK
    assert sessions.load(session_id).state == "done"
    assert "cold follow-up" in sessions.answer_path(session_id).read_text(encoding="utf-8")


def test_load_without_list_or_replay_is_also_unverified(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    method_file = tmp_path / "session-method"
    monkeypatch.setenv("ACPC_MOCK_ADVERTISE_LIST", "0")
    monkeypatch.setenv("ACPC_MOCK_REPLAY_USER_MESSAGES", "0")
    monkeypatch.setenv("ACPC_MOCK_SESSION_METHOD_FILE", str(method_file))
    session_id = start_session(cli, "turn one")

    result = invoke(cli, "continue", session_id, "turn two", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert method_file.read_text(encoding="utf-8") == "load\n"


def test_a_cwd_mismatch_fails_before_rotation_or_prompt_dispatch(
    cli: CliRunner, state_root: Path, tmp_path: Path
) -> None:
    session_id = start_session(cli, "the recorded context")
    before = {
        name: path.read_bytes()
        for name, path in {
            "meta": sessions.meta_path(session_id),
            "prompt": sessions.prompt_path(session_id),
            "answer": sessions.answer_path(session_id),
            "transcript": sessions.transcript_path(session_id),
        }.items()
        if path.exists()
    }
    wrong_cwd = tmp_path / "wrong-cwd"
    wrong_cwd.mkdir()
    meta = sessions.read_meta(session_id)
    meta.resolution["cwd"] = str(wrong_cwd)
    with sessions.session_lock(session_id):
        sessions.write_meta(meta)
    before["meta"] = sessions.meta_path(session_id).read_bytes()

    result = invoke(cli, "continue", session_id, "must not run", "--quiet")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    assert "cwd" in result.stderr
    assert "must not run" not in sessions.prompt_path(session_id).read_text(encoding="utf-8")
    assert sessions.read_meta(session_id).state == "done"
    for name, content in before.items():
        assert {
            "meta": sessions.meta_path(session_id),
            "prompt": sessions.prompt_path(session_id),
            "answer": sessions.answer_path(session_id),
            "transcript": sessions.transcript_path(session_id),
        }[name].read_bytes() == content
    assert "must not run" not in (state_root / "mock-sessions.json").read_text(encoding="utf-8")


def test_a_replay_prompt_mismatch_fails_before_the_new_prompt(
    cli: CliRunner, state_root: Path
) -> None:
    session_id = start_session(cli, "the recorded context")
    store_path = state_root / "mock-sessions.json"
    store = json.loads(store_path.read_text(encoding="utf-8"))
    adapter_session_id = sessions.read_meta(session_id).adapter_session_id
    assert adapter_session_id is not None
    store[adapter_session_id]["history"] = ["a different context"]
    store_path.write_text(json.dumps(store), encoding="utf-8")
    before_prompt = sessions.prompt_path(session_id).read_bytes()
    before_answer = sessions.answer_path(session_id).read_bytes()
    before_transcript = sessions.transcript_path(session_id).read_bytes()

    result = invoke(cli, "continue", session_id, "must not run", "--quiet")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    assert "stored prompt" in result.stderr
    assert sessions.read_meta(session_id).state == "done"
    assert sessions.prompt_path(session_id).read_bytes() == before_prompt
    assert sessions.answer_path(session_id).read_bytes() == before_answer
    assert sessions.transcript_path(session_id).read_bytes() == before_transcript
    assert store[adapter_session_id]["history"] == ["a different context"]


def test_significant_whitespace_is_part_of_the_replayed_prompt(
    cli: CliRunner, state_root: Path
) -> None:
    session_id = start_session(cli, "if ready:\n    deploy()")
    store_path = state_root / "mock-sessions.json"
    store = json.loads(store_path.read_text(encoding="utf-8"))
    adapter_session_id = sessions.read_meta(session_id).adapter_session_id
    assert adapter_session_id is not None
    store[adapter_session_id]["history"] = ["if ready: deploy()"]
    store_path.write_text(json.dumps(store), encoding="utf-8")

    result = invoke(cli, "continue", session_id, "must not run", "--quiet")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    assert "was not found" in result.stderr
    assert sessions.read_meta(session_id).turns == 1


def test_reordered_replayed_prompts_name_order_not_absence(
    cli: CliRunner, state_root: Path
) -> None:
    session_id = start_session(cli, "first recorded context")
    assert invoke(cli, "continue", session_id, "second recorded context", "--quiet").exit_code == 0
    store_path = state_root / "mock-sessions.json"
    store = json.loads(store_path.read_text(encoding="utf-8"))
    adapter_session_id = sessions.read_meta(session_id).adapter_session_id
    assert adapter_session_id is not None
    store[adapter_session_id]["history"] = ["second recorded context", "first recorded context"]
    store_path.write_text(json.dumps(store), encoding="utf-8")

    result = invoke(cli, "continue", session_id, "must not run", "--quiet")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    assert "out of order" in result.stderr
    assert sessions.read_meta(session_id).turns == 2


def test_replayed_user_prompts_are_checked_as_an_ordered_subsequence(
    cli: CliRunner, state_root: Path
) -> None:
    session_id = start_session(cli, "first recorded context")
    assert invoke(cli, "continue", session_id, "second recorded context", "--quiet").exit_code == 0

    store_path = state_root / "mock-sessions.json"
    store = json.loads(store_path.read_text(encoding="utf-8"))
    adapter_session_id = sessions.read_meta(session_id).adapter_session_id
    assert adapter_session_id is not None
    store[adapter_session_id]["history"].insert(1, "adapter-only rolled-back turn")
    store_path.write_text(json.dumps(store), encoding="utf-8")

    result = invoke(cli, "continue", session_id, "third recorded context", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert "third recorded context" in sessions.answer_path(session_id).read_text(encoding="utf-8")


def test_two_background_sessions_resume_only_their_own_context(
    cli: CliRunner, live_daemon: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cursor_file = tmp_path / "list-cursors"
    monkeypatch.setenv("ACPC_MOCK_SESSION_LIST_CURSOR_FILE", str(cursor_file))
    first = json.loads(invoke(cli, "run", "mock", "alpha context", "--bg", "--json").stdout)[
        "session_id"
    ]
    second = json.loads(invoke(cli, "run", "mock", "beta context", "--bg", "--json").stdout)[
        "session_id"
    ]
    assert first != second

    assert invoke(cli, "wait", first, "--quiet").exit_code == vocab.EXIT_OK
    assert invoke(cli, "wait", second, "--quiet").exit_code == vocab.EXIT_OK
    resolution = runner.resolution_from_session(sessions.read_meta(first))

    async def stop_target() -> None:
        connection = await daemon_client.connect(runner.call_target(resolution))
        assert connection is not None
        try:
            await connection.stop()
        finally:
            await connection.close()

    asyncio.run(stop_target())

    first_resume = invoke(cli, "continue", first, "what was my context?", "--quiet")
    second_resume = invoke(cli, "continue", second, "what was my context?", "--quiet")

    assert first_resume.exit_code == vocab.EXIT_OK
    assert second_resume.exit_code == vocab.EXIT_OK
    first_answer = sessions.answer_path(first).read_text(encoding="utf-8")
    second_answer = sessions.answer_path(second).read_text(encoding="utf-8")
    assert "alpha context" in first_answer
    assert "beta context" not in first_answer
    assert "beta context" in second_answer
    assert "alpha context" not in second_answer
    assert "1\n" in cursor_file.read_text(encoding="utf-8")


def test_inherited_ceiling_clamps_a_background_continue_and_stop_uses_new_target(
    cli: CliRunner, live_daemon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = json.loads(
        invoke(cli, "run", "mock", "turn one", "--permissions", "all", "--quiet", "--json").stdout
    )["session_id"]
    original = sessions.load(session_id)
    monkeypatch.setenv("ACPC_CEILING", "read")

    dispatched = invoke(cli, "continue", session_id, "chunkslow:5 clamped", "--bg", "--json")

    assert dispatched.exit_code == vocab.EXIT_OK
    meta = sessions.load(session_id)
    assert meta.resolution["resolved"]["permissions"]["value"] == "read"
    assert meta.resolution["resolved"]["permissions"]["clamp"] == {
        "requested": "all",
        "ceiling": "read",
        "effective": "read",
    }
    assert meta.target != original.target
    assert meta.target == runner.call_target(runner.resolution_from_session(meta))

    stopped = invoke(cli, "stop", session_id)

    assert stopped.exit_code == vocab.EXIT_OK
    assert sessions.load(session_id).state == "cancelled"


def test_a_daemon_cold_resume_reports_list_verification_without_replay(
    cli: CliRunner, live_daemon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = start_session(cli, "turn one")
    resolution = runner.resolution_from_session(sessions.load(session_id))

    async def stop_target() -> None:
        connection = await daemon_client.connect(runner.call_target(resolution))
        assert connection is not None
        try:
            await connection.stop()
        finally:
            await connection.close()

    asyncio.run(stop_target())
    monkeypatch.setenv("ACPC_MOCK_REPLAY_USER_MESSAGES", "0")

    result = invoke(cli, "continue", session_id, "turn two", "--bg", "--json")

    assert result.exit_code == vocab.EXIT_OK
    assert json.loads(result.stdout)["resume"] == "verified"
    assert invoke(cli, "wait", session_id, "--quiet").exit_code == vocab.EXIT_OK


def test_background_json_reports_the_exact_unverified_resume_status(
    cli: CliRunner, live_daemon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = start_session(cli, "turn one")
    resolution = runner.resolution_from_session(sessions.load(session_id))

    async def stop_target() -> None:
        connection = await daemon_client.connect(runner.call_target(resolution))
        assert connection is not None
        try:
            await connection.stop()
        finally:
            await connection.close()

    asyncio.run(stop_target())
    monkeypatch.setenv("ACPC_MOCK_ADVERTISE_LIST", "0")
    monkeypatch.setenv("ACPC_MOCK_REPLAY_USER_MESSAGES", "0")

    result = invoke(cli, "continue", session_id, "turn two", "--bg", "--json")

    assert result.exit_code == vocab.EXIT_OK
    assert json.loads(result.stdout)["resume"] == (
        "unverified — session/list unavailable; conversation replay unavailable"
    )
    assert invoke(cli, "wait", session_id, "--quiet").exit_code == vocab.EXIT_OK


def test_a_daemon_verification_failure_preserves_the_finished_session(
    cli: CliRunner, live_daemon: None, state_root: Path
) -> None:
    session_id = start_session(cli, "recorded context")
    resolution = runner.resolution_from_session(sessions.load(session_id))

    async def stop_target() -> None:
        connection = await daemon_client.connect(runner.call_target(resolution))
        assert connection is not None
        try:
            await connection.stop()
        finally:
            await connection.close()

    asyncio.run(stop_target())

    store_path = state_root / "mock-sessions.json"
    store = json.loads(store_path.read_text(encoding="utf-8"))
    adapter_session_id = sessions.read_meta(session_id).adapter_session_id
    assert adapter_session_id is not None
    store[adapter_session_id]["history"] = ["different context"]
    store_path.write_text(json.dumps(store), encoding="utf-8")
    before = {
        path: path.read_bytes()
        for path in (
            sessions.meta_path(session_id),
            sessions.prompt_path(session_id),
            sessions.answer_path(session_id),
            sessions.transcript_path(session_id),
        )
    }

    result = invoke(cli, "continue", session_id, "must not run", "--quiet")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    assert sessions.read_meta(session_id).state == "done"
    assert sessions.read_meta(session_id).turns == 1
    assert {path: path.read_bytes() for path in before} == before


def test_a_queued_warm_background_continue_returns_before_the_slot_opens(
    cli: CliRunner, live_daemon: None, state_root: Path
) -> None:
    (state_root / "config.toml").write_text("daemon_max_concurrent = 1\n", encoding="utf-8")
    finished = start_session(cli, "turn one")
    blocker = json.loads(
        invoke(cli, "run", "mock", "chunkslow:3 blocker", "--bg", "--json").stdout
    )["session_id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and sessions.load(blocker).state != "running":
        time.sleep(0.05)
    assert sessions.load(blocker).state == "running"

    started_at = time.monotonic()
    result = invoke(cli, "continue", finished, "queued warm turn", "--bg", "--json")
    elapsed = time.monotonic() - started_at

    assert result.exit_code == vocab.EXIT_OK
    assert elapsed < 1.0
    assert invoke(cli, "wait", blocker, "--quiet").exit_code == vocab.EXIT_OK
    assert invoke(cli, "wait", finished, "--quiet").exit_code == vocab.EXIT_OK


def test_concurrent_continuations_have_one_atomic_winner(cli: CliRunner, live_daemon: None) -> None:
    session_id = start_session(cli, "turn one")
    environment = os.environ.copy()
    command = [
        sys.executable,
        "-c",
        "from acpc.cli import main; raise SystemExit(main())",
        "continue",
        session_id,
    ]
    first = subprocess.Popen(
        command + ["chunkslow:3 first winner", "--permissions", "edit", "--quiet", "--json"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    second = subprocess.Popen(
        command + ["chunkslow:3 second contender", "--permissions", "all", "--quiet", "--json"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    first_output, first_error = first.communicate(timeout=15)
    second_output, second_error = second.communicate(timeout=15)
    outcomes = [
        (first.returncode, first_output, first_error),
        (second.returncode, second_output, second_error),
    ]

    assert sum(returncode == vocab.EXIT_OK for returncode, _output, _error in outcomes) == 1
    assert sum(returncode != vocab.EXIT_OK for returncode, _output, _error in outcomes) == 1
    meta = sessions.load(session_id)
    assert meta.state == "done"
    assert meta.turns == 2
    prompt = sessions.prompt_path(session_id).read_text(encoding="utf-8")
    assert "first winner" in prompt or "second contender" in prompt


def test_same_target_cold_continuations_cannot_steal_replay(
    cli: CliRunner, live_daemon: None, state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = start_session(cli, "recorded context")
    resolution = runner.resolution_from_session(sessions.load(session_id))

    async def stop_target() -> None:
        connection = await daemon_client.connect(runner.call_target(resolution))
        assert connection is not None
        try:
            await connection.stop()
        finally:
            await connection.close()

    asyncio.run(stop_target())
    store_path = state_root / "mock-sessions.json"
    store = json.loads(store_path.read_text(encoding="utf-8"))
    adapter_session_id = sessions.read_meta(session_id).adapter_session_id
    assert adapter_session_id is not None
    store[adapter_session_id]["history"] = [f"wrong context {index}" for index in range(500)]
    store_path.write_text(json.dumps(store), encoding="utf-8")
    monkeypatch.setenv("ACPC_MOCK_ADVERTISE_LIST", "0")
    monkeypatch.setenv("ACPC_MOCK_RESUME_DELAY", "0.2")

    command = [
        sys.executable,
        "-c",
        "from acpc.cli import main; raise SystemExit(main())",
        "continue",
        session_id,
    ]
    environment = os.environ.copy()
    first = subprocess.Popen(
        command + ["must not run first", "--quiet"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    second = subprocess.Popen(
        command + ["must not run second", "--quiet"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    first_output, first_error = first.communicate(timeout=20)
    second_output, second_error = second.communicate(timeout=20)

    assert first.returncode != vocab.EXIT_OK, (first_output, first_error)
    assert second.returncode != vocab.EXIT_OK, (second_output, second_error)
    assert sessions.read_meta(session_id).state == "done"
    assert sessions.read_meta(session_id).turns == 1
    assert "must not run" not in store_path.read_text(encoding="utf-8")


def test_continue_by_name_uses_the_session_alias(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "turn one", "--name", "research", "--quiet", "--json")
    session_id = json.loads(result.stdout)["session_id"]

    continued = invoke(cli, "continue", "research", "turn two", "--quiet", "--json")

    assert continued.exit_code == vocab.EXIT_OK
    assert json.loads(continued.stdout)["session_id"] == session_id


def test_continue_permissions_apply_and_persist_for_later_turns(
    cli: CliRunner, tmp_path: Path
) -> None:
    first = invoke(
        cli,
        "run",
        "mock",
        "turn one",
        "--cwd",
        str(tmp_path),
        "--quiet",
        "--json",
    )
    session_id = json.loads(first.stdout)["session_id"]

    second = invoke(
        cli,
        "continue",
        session_id,
        "write-file:first.md",
        "--permissions",
        "edit",
        "--quiet",
    )
    third = invoke(cli, "continue", session_id, "write-file:second.md", "--quiet")
    fourth = invoke(cli, "continue", session_id, "perm scenario")

    assert second.exit_code == vocab.EXIT_OK
    assert third.exit_code == vocab.EXIT_OK
    assert fourth.exit_code == vocab.EXIT_USAGE
    assert (tmp_path / "first.md").is_file()
    assert (tmp_path / "second.md").is_file()
    meta = sessions.load(session_id)
    assert meta.resolution["resolved"]["permissions"]["value"] == "edit"
    assert meta.resolution.get("permissions_source") is None
    assert meta.denied == {"execute": 2, "switch_mode:yolo": 1}
    assert meta.denial_details["switch_mode:yolo"] == {
        "category": "switch_mode",
        "target": "yolo",
        "minimum_policy": "all",
        "remedy": "pass --permissions all",
    }
    assert "denied:" in fourth.stderr


def test_continuation_clamps_and_persists_an_inherited_ceiling(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPC_CEILING", "read")
    session_id = start_session(cli)

    result = invoke(cli, "continue", session_id, "turn two", "--permissions", "all", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    permissions = sessions.load(session_id).resolution["resolved"]["permissions"]
    assert permissions["value"] == "read"
    assert permissions["clamp"] == {
        "requested": "all",
        "ceiling": "read",
        "effective": "read",
    }


def test_continue_validation_failure_does_not_rotate_session(cli: CliRunner) -> None:
    session_id = start_session(cli)
    meta = sessions.load(session_id)
    meta.adapter_session_id = None
    with sessions.session_lock(session_id):
        sessions.write_meta(meta)

    result = invoke(cli, "continue", session_id, "turn two")

    unchanged = sessions.load(session_id)
    assert result.exit_code == vocab.EXIT_USAGE
    assert "cannot be continued" in result.stderr
    assert unchanged.state == "done"
    assert unchanged.turns == 1


def test_continue_rejects_a_malformed_stored_resolution(cli: CliRunner) -> None:
    session_id = start_session(cli)
    meta = sessions.load(session_id)
    meta.resolution["resolved"] = None
    with sessions.session_lock(session_id):
        sessions.write_meta(meta)

    result = invoke(cli, "continue", session_id, "turn two")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "stored permission resolution" in result.stderr


def test_continue_write_alias_is_canonical_and_warns(
    cli: CliRunner, fresh_permission_alias_warnings: None, tmp_path: Path
) -> None:
    first = invoke(
        cli,
        "run",
        "mock",
        "turn one",
        "--cwd",
        str(tmp_path),
        "--quiet",
        "--json",
    )
    session_id = json.loads(first.stdout)["session_id"]

    result = invoke(
        cli,
        "continue",
        session_id,
        "write-file:alias.md",
        "--permissions",
        "write",
        "--quiet",
    )

    assert result.exit_code == vocab.EXIT_OK
    assert (tmp_path / "alias.md").is_file()
    assert sessions.load(session_id).resolution["resolved"]["permissions"]["value"] == "execute"
    assert "--permissions write is deprecated; use --permissions execute" in result.stderr


def test_continue_on_a_running_session_is_a_usage_error(cli: CliRunner, live_daemon: None) -> None:
    result = invoke(cli, "run", "mock", "chunkslow:5 hold", "--bg", "--json")
    session_id = json.loads(result.stdout)["session_id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if sessions.load(session_id).state == "running":
            break
        time.sleep(0.05)
    else:
        pytest.fail("background session did not become running")

    continued = invoke(cli, "continue", session_id, "turn two")

    assert continued.exit_code == vocab.EXIT_USAGE
    assert "running" in continued.stderr


def test_continue_preserves_the_global_transcript_cursor(cli: CliRunner) -> None:
    session_id = start_session(cli, "turn one")
    first_lines = sessions.transcript_path(session_id).read_text(encoding="utf-8").splitlines()
    first_last = json.loads(first_lines[-1])["i"]
    result = invoke(cli, "continue", session_id, "turn two", "--quiet")
    assert result.exit_code == vocab.EXIT_OK

    lines = sessions.transcript_path(session_id).read_text(encoding="utf-8").splitlines()
    indices = [json.loads(line)["i"] for line in lines[1:]]

    assert indices == list(range(1, len(indices) + 1))
    assert indices[-1] > first_last


def test_continue_keeps_the_run_prompt_source_rules(cli: CliRunner) -> None:
    session_id = start_session(cli)

    result = invoke(cli, "continue", session_id)

    assert result.exit_code == vocab.EXIT_USAGE
    assert "exactly one prompt source" in result.stderr


def test_run_stores_the_tty_resolved_permission_policy(cli: CliRunner) -> None:
    session_id = start_session(cli)

    stored = sessions.load(session_id).resolution["resolved"]["permissions"]["value"]

    assert stored == "read"


def test_denial_tally_is_replaced_by_the_following_turn(cli: CliRunner, state_root: Path) -> None:
    first = invoke(cli, "run", "mock", "write-file:first.md", "--json")
    session_id = json.loads(first.stdout)["session_id"]

    second = invoke(cli, "continue", session_id, "tool:read")

    assert second.exit_code == vocab.EXIT_OK
    assert "denied:" not in second.stderr
    payload = json.loads(
        (state_root / "sessions" / session_id / "meta.json").read_text(encoding="utf-8")
    )
    assert payload["denied"] == {}
    assert payload["resolution"]["permissions_source"] == "default"


def _store_ask_policy(session_id: str) -> None:
    meta = sessions.load(session_id)
    meta.resolution["resolved"]["permissions"]["value"] = "ask"
    with sessions.session_lock(session_id):
        sessions.write_meta(meta)


def test_continue_of_an_ask_session_needs_a_terminal(cli: CliRunner) -> None:
    session_id = start_session(cli)
    _store_ask_policy(session_id)

    result = invoke(cli, "continue", session_id, "turn two")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "needs a terminal to ask on" in result.stderr
    assert "--bg" not in result.stderr


def test_continue_of_an_ask_session_rejects_bg_by_name(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from acpc import cli as cli_module

    monkeypatch.setattr(cli_module, "_stdout_is_tty", lambda: True)
    session_id = start_session(cli)
    _store_ask_policy(session_id)

    result = invoke(cli, "continue", session_id, "turn two", "--bg")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "--bg" in result.stderr


def test_continue_last_is_rejected_without_a_tty(cli: CliRunner) -> None:
    start_session(cli)

    result = invoke(cli, "continue", "last", "turn two")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "TTY" in result.stderr


def test_a_warm_continue_keeps_the_adapter_history(cli: CliRunner, live_daemon: None) -> None:
    session_id = start_session(cli, "turn one of the conversation")

    result = invoke(cli, "continue", session_id, "turn two", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert "turn one of the conversation" in result.stdout


def test_background_policy_change_updates_wait_and_stop_target(
    cli: CliRunner, live_daemon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = start_session(cli)
    old_target = sessions.load(session_id).target
    original_connect = daemon_client.connect
    connected: list[str] = []

    async def recording_connect(target: str):
        connected.append(target)
        return await original_connect(target)

    monkeypatch.setattr(daemon_client, "connect", recording_connect)
    dispatched = invoke(
        cli,
        "continue",
        session_id,
        "chunkslow:5 policy change",
        "--permissions",
        "edit",
        "--bg",
        "--json",
    )

    assert dispatched.exit_code == vocab.EXIT_OK
    rotated = sessions.load(session_id)
    new_target = rotated.target
    assert new_target is not None
    assert new_target != old_target
    assert new_target == runner.call_target(runner.resolution_from_session(rotated))

    connected.clear()
    waited = invoke(cli, "wait", session_id, "--timeout", "0.1", "--quiet")
    assert waited.exit_code == vocab.EXIT_TIMEOUT
    assert connected == [new_target]

    connected.clear()
    stopped = invoke(cli, "stop", session_id)
    assert stopped.exit_code == vocab.EXIT_OK
    assert sessions.load(session_id).state == "cancelled"
    assert connected == [new_target]
