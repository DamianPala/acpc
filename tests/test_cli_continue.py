"""Behavioral tests for the ``continue`` verb."""

import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from acpc import cli as cli_module
from acpc import daemon_client, runner, sessions, vocab
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


def test_continue_reapplies_the_stored_mode(cli: CliRunner) -> None:
    first = invoke(cli, "run", "mock", "settings", "--mode", "plan", "--quiet", "--json")
    session_id = json.loads(first.stdout)["session_id"]

    stored = sessions.load(session_id).resolution["resolved"]["mode"]
    assert stored == {
        "value": "plan",
        "source": "call flag",
        "grants": "read",
        "delegates": True,
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
    edited = MOCK_ENTRY.replace(
        'default = { grants = "read", delegates = true }',
        'default = { grants = "execute", delegates = false }',
    ).replace(
        'plan = { grants = "read", delegates = true }',
        'plan = { grants = "execute", delegates = true }',
    )
    entry.write_text(edited, encoding="utf-8")

    result = invoke(cli, "continue", session_id, "settings", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    _model, _effort, mode, _model_calls, mode_calls, _effort_calls = result.stdout.split("/")
    assert mode == "default"
    assert mode_calls == "1"


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
    }
    assert stored["adapter"]["mode"] == "plan"
    assert stored["adapter"]["grants"] == "execute"
    assert stored["adapter"]["delegates"] is True


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
    assert "continue requires an adapter with the loadSession capability" in result.stderr


def test_continue_cold_resume_does_not_replay_adapter_history(cli: CliRunner) -> None:
    session_id = start_session(cli, "turn one of the conversation")

    result = invoke(cli, "continue", session_id, "turn two", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert "turn one of the conversation" not in result.stdout


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
    assert meta.denied == {"execute": 2, "unknown": 1}
    assert "denied:" not in fourth.stderr


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
