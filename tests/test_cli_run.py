"""Behavioral tests for the `run` verb: flags, usage errors, TTY rules."""

import json
import os
import pty
import queue
import re
import select
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from acpc import cache, interaction, paths, sessions, vocab
from acpc import cli as cli_module
from acpc.cli import main
from acpc.permissions import ModeSelectionError
from acpc.registry import ModeSpec

MOCK_AGENT_SCRIPT = str(Path(__file__).with_name("mock_agent.py"))

MOCK_ENTRY = f"""
name = "Mock Agent"
command = "{sys.executable} {MOCK_AGENT_SCRIPT}"
install_command = "true"
home = "~/.mock"
home_env = "MOCK_HOME"

[modes]
default = {{ grants = "read", delegates = true }}
plan = {{ grants = "read", delegates = true }}
yolo = {{ grants = "all", delegates = false }}

[presets]
fast = {{ model = "mock-haiku-4-5", effort = "high" }}
standard = {{ model = "mock-sonnet-5", effort = "high" }}
max = {{ model = "mock-opus-5", effort = "xhigh" }}
"""

FLOOR_ENTRY = """
name = "Grok Build Mock"
command = "true"
install_command = "true"

[modes]
bypass = { grants = "all", delegates = false }
default = { grants = "execute", delegates = false }
"""

PHANTOM_ENTRY = """
name = "Phantom Agent"
command = "definitely-not-installed-phantom-xyz"
install_command = "false"

[modes]
default = { grants = "read", delegates = true }
"""


@pytest.fixture(autouse=True)
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "state"
    agents = root / "agents"
    agents.mkdir(parents=True)
    (agents / "mock.toml").write_text(MOCK_ENTRY, encoding="utf-8")
    (agents / "phantom.toml").write_text(PHANTOM_ENTRY, encoding="utf-8")
    monkeypatch.setenv("ACPC_HOME", str(root))
    return root


@pytest.fixture
def cli() -> CliRunner:
    # stdout is not a tty under CliRunner, which is the non-interactive branch
    # of every TTY rule below.
    return CliRunner()


@pytest.fixture
def grok_floor_entry(state_root: Path) -> None:
    (state_root / "agents" / "grok-floor.toml").write_text(FLOOR_ENTRY, encoding="utf-8")


@pytest.fixture
def fresh_permission_alias_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "_WARNED_PERMISSION_ALIASES", set())


def invoke(cli: CliRunner, *args: str, stdin: str | None = None):
    return cli.invoke(main, list(args), input=stdin, catch_exceptions=False)


def force_human_presentation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the terminal branch of `text` (V6a) under CliRunner's non-TTY pipes.

    `CliRunner` gives every test a non-TTY stdout, which now defaults to the
    tagged document; a handful of tests are specifically about the raw,
    byte-identical-to-answer.md human presentation, so they select it directly.
    """
    monkeypatch.setattr(cli_module, "_stdout_is_tty", lambda: True)


def error_envelope(result) -> dict:
    """The structured failure: always the last non-empty line of stderr."""
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    return json.loads(lines[-1])["error"]


def wire_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


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


# --- prompt sources ---------------------------------------------------------


def test_a_prompt_argument_runs_the_turn(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "echo:hello from the argument", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert "hello from the argument" in result.stdout


def test_a_dash_reads_the_prompt_from_stdin(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "-", "--quiet", stdin="echo:hello from stdin")

    assert result.exit_code == vocab.EXIT_OK
    assert "hello from stdin" in result.stdout


def test_prompt_file_reads_the_prompt_from_disk(cli: CliRunner, tmp_path: Path) -> None:
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("echo:hello from the file", encoding="utf-8")

    result = invoke(cli, "run", "mock", "--prompt-file", str(prompt_file), "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert "hello from the file" in result.stdout


def test_run_uses_raw_session_set_model_for_set_model_entries(
    cli: CliRunner, state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire_file = state_root / "wire.jsonl"
    monkeypatch.setenv("ACPC_MOCK_WIRE_FILE", str(wire_file))
    (state_root / "agents" / "mock.toml").write_text(
        MOCK_ENTRY.replace("\n[modes]", '\nmodel_via = "set_model"\n\n[modes]'),
        encoding="utf-8",
    )

    result = invoke(cli, "run", "mock", "settings", "--model", "mock-opus-5", "--quiet")

    assert result.exit_code == vocab.EXIT_OK, result.stderr
    assert "mock-opus-5/" in result.stdout
    events = wire_events(wire_file)
    set_model = [event for event in events if event["method"] == "session/set_model"]
    assert len(set_model) == 1
    assert set_model[0]["params"]["modelId"] == "mock-opus-5"
    assert not any(
        event["method"] == "session/set_config_option"
        and event["params"].get("configId") == "model"
        for event in events
    )


def test_run_uses_model_config_option_for_config_option_entries(
    cli: CliRunner, state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire_file = state_root / "wire.jsonl"
    monkeypatch.setenv("ACPC_MOCK_WIRE_FILE", str(wire_file))

    result = invoke(cli, "run", "mock", "settings", "--model", "mock-opus-5", "--quiet")

    assert result.exit_code == vocab.EXIT_OK, result.stderr
    assert "mock-opus-5/" in result.stdout
    events = wire_events(wire_file)
    model_options = [
        event
        for event in events
        if event["method"] == "session/set_config_option"
        and event["params"].get("configId") == "model"
    ]
    assert len(model_options) == 1
    assert model_options[0]["params"]["value"] == "mock-opus-5"
    assert not any(event["method"] == "session/set_model" for event in events)


def test_no_prompt_source_is_a_usage_error(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock")

    assert result.exit_code == vocab.EXIT_USAGE


def test_two_prompt_sources_are_a_usage_error(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "arg", "--prompt-file", "/dev/null")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "exactly one prompt source" in result.stderr


def test_an_unreadable_prompt_file_is_a_usage_error(cli: CliRunner, tmp_path: Path) -> None:
    result = invoke(cli, "run", "mock", "--prompt-file", str(tmp_path / "nope.txt"))

    assert result.exit_code == vocab.EXIT_USAGE
    assert "nope.txt" in result.stderr


# --- unknown agents and missing binaries ------------------------------------


def test_an_unknown_agent_is_not_found(cli: CliRunner) -> None:
    result = invoke(cli, "run", "no-such-agent", "hello")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    envelope = json.loads(result.stderr)["error"]
    assert envelope["kind"] == "not_found"
    assert envelope["hint"] == "Run: acpc agents list"


def test_a_missing_adapter_binary_exits_1_and_names_the_install(cli: CliRunner) -> None:
    result = invoke(cli, "run", "phantom", "hello")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    assert "acpc install phantom" in result.stderr


def test_a_missing_adapter_without_install_command_names_vendor_docs(
    cli: CliRunner, state_root: Path
) -> None:
    (state_root / "agents" / "vendorish.toml").write_text(
        'command = "definitely-not-installed-vendorish-xyz"\n'
        'install_docs = "https://example.test/cli"\n'
        '\n[modes]\ndefault = { grants = "read", delegates = true }\n',
        encoding="utf-8",
    )

    result = invoke(cli, "run", "vendorish", "hello")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    assert "https://example.test/cli" in result.stderr
    assert "already registered" in result.stderr
    assert "acpc install vendorish" not in result.stderr


def test_a_missing_adapter_binary_creates_no_session(cli: CliRunner, state_root: Path) -> None:
    invoke(cli, "run", "phantom", "hello")

    sessions_dir = state_root / "sessions"
    assert not sessions_dir.exists() or not list(sessions_dir.iterdir())


# --- resolve -----------------------------------------------------------------


def test_resolve_shows_the_resolved_model_and_its_source(cli: CliRunner) -> None:
    result = invoke(cli, "resolve", "mock")

    assert result.exit_code == vocab.EXIT_OK
    assert "mock-sonnet-5" in result.stdout
    assert "adapter default" in result.stdout


def test_resolve_runs_nothing(cli: CliRunner, state_root: Path) -> None:
    invoke(cli, "resolve", "mock")

    sessions_dir = state_root / "sessions"
    assert not sessions_dir.exists() or not list(sessions_dir.iterdir())


def test_resolve_json_is_machine_readable(cli: CliRunner) -> None:
    result = invoke(cli, "resolve", "mock", "--json")

    payload = json.loads(result.stdout)
    assert payload["entry"] == "mock"
    assert payload["resolved"]["model"]["value"] == "mock-sonnet-5"
    assert "entry_definition" not in payload


def test_resolve_reports_mode_for_entry_flag_and_unset_sources(
    cli: CliRunner, state_root: Path
) -> None:
    (state_root / "agents" / "pinned.toml").write_text(
        'extends = "mock"\nmode = "plan"\n', encoding="utf-8"
    )

    entry = json.loads(invoke(cli, "resolve", "pinned", "--json").stdout)
    flag = json.loads(
        invoke(
            cli,
            "resolve",
            "pinned",
            "--mode",
            "plan",
            "--json",
        ).stdout
    )
    unset = json.loads(invoke(cli, "resolve", "mock", "--json").stdout)

    assert entry["resolved"]["mode"] == {
        "value": "plan",
        "source": f"entry ({state_root / 'agents' / 'pinned.toml'})",
        "grants": "read",
        "delegates": True,
        "escalates": False,
        "selectable": True,
    }
    assert flag["resolved"]["mode"] == {
        "value": "plan",
        "source": "call flag",
        "grants": "read",
        "delegates": True,
        "escalates": False,
        "selectable": True,
    }
    assert unset["resolved"]["mode"] == {
        "value": "default",
        "source": "selected",
        "grants": "read",
        "delegates": True,
        "escalates": False,
        "selectable": True,
    }


def test_resolve_reports_the_permission_policy_it_would_use(cli: CliRunner) -> None:
    result = invoke(cli, "resolve", "mock", "--json")

    # Non-interactive stdout, no --permissions: SPEC's default is `read`.
    assert json.loads(result.stdout)["resolved"]["permissions"]["value"] == "read"


def test_inherited_ceiling_clamps_a_nested_all_policy_and_reports_it(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPC_CEILING", "edit")

    result = invoke(cli, "resolve", "mock", "--permissions", "all", "--json")

    permission = json.loads(result.stdout)["resolved"]["permissions"]
    assert permission["value"] == "edit"
    assert permission["clamp"] == {
        "requested": "all",
        "ceiling": "edit",
        "effective": "edit",
    }
    assert permission["source"] == "call flag"
    assert "clamped from" not in permission["source"]


def test_inherited_ceiling_keeps_a_lower_nested_policy(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPC_CEILING", "execute")

    result = invoke(cli, "resolve", "mock", "--permissions", "read", "--json")

    permission = json.loads(result.stdout)["resolved"]["permissions"]
    assert permission["value"] == "read"
    assert "clamp" not in permission


def test_inherited_ceiling_is_reported_by_a_real_run(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPC_CEILING", "edit")

    result = invoke(cli, "run", "mock", "echo:clamped", "--permissions", "all", "--json")

    assert result.exit_code == vocab.EXIT_OK
    assert "permissions clamped from all by inherited ceiling edit" in result.stderr
    assert json.loads(result.stdout)["permissions_clamp"] == {
        "requested": "all",
        "ceiling": "edit",
        "effective": "edit",
    }
    session_id = json.loads(result.stdout)["session_id"]
    stored = sessions.read_meta(session_id).resolution["resolved"]["permissions"]
    assert stored["source"] == "call flag"
    assert stored["clamp"] == json.loads(result.stdout)["permissions_clamp"]


@pytest.mark.parametrize("ceiling", ["edit", "read"])
def test_nested_ask_is_rejected_by_a_numeric_ceiling(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch, ceiling: str
) -> None:
    monkeypatch.setenv("ACPC_CEILING", ceiling)

    result = invoke(cli, "resolve", "mock", "--permissions", "ask")

    assert result.exit_code == vocab.EXIT_USAGE
    assert f"inherited ceiling {ceiling}" in result.stderr
    assert "available policies: none, read, edit, execute, all" in result.stderr


def test_nested_ask_remains_ask_under_an_all_ceiling(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPC_CEILING", "all")
    # `ask` needs the interactive context, which `--json` would revoke.
    monkeypatch.setattr(interaction, "stdin_is_tty", lambda: True)
    monkeypatch.setattr(interaction, "stdout_is_tty", lambda: True)

    result = invoke(cli, "resolve", "mock", "--permissions", "ask")

    assert result.exit_code == vocab.EXIT_OK
    assert "permissions ask" in " ".join(result.stdout.split())


def test_invalid_inherited_ceiling_is_a_usage_error(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPC_CEILING", "not-a-policy")

    result = invoke(cli, "resolve", "mock", "--permissions", "all")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "ACPC_CEILING='not-a-policy' is invalid" in result.stderr


def test_write_alias_matches_execute_and_warns_once_per_process(
    cli: CliRunner, fresh_permission_alias_warnings: None
) -> None:
    first = invoke(cli, "resolve", "mock", "--permissions", "write", "--json")
    second = invoke(cli, "resolve", "mock", "--permissions", "write", "--json")
    canonical = invoke(cli, "resolve", "mock", "--permissions", "execute", "--json")

    assert json.loads(first.stdout)["resolved"]["permissions"]["value"] == "execute"
    assert json.loads(first.stdout) == json.loads(canonical.stdout)
    assert "--permissions write is deprecated; use --permissions execute" in first.stderr
    assert "deprecated" not in second.stderr


def test_a_raw_model_id_on_the_call_is_labelled_as_a_call_flag(cli: CliRunner) -> None:
    result = invoke(cli, "resolve", "mock", "--model", "mock-opus-5", "--json")

    resolved = json.loads(result.stdout)["resolved"]
    assert resolved["model"]["value"] == "mock-opus-5"
    assert resolved["model"]["source"] == "call flag"


def test_a_tier_name_resolves_through_the_entry_preset_that_defines_it(cli: CliRunner) -> None:
    result = invoke(cli, "resolve", "mock", "--model", "max", "--json")

    resolved = json.loads(result.stdout)["resolved"]
    assert resolved["model"]["value"] == "mock-opus-5"
    # The tier is a call flag, but the value behind it comes from the entry.
    assert resolved["model"]["source"].startswith("entry")


# --- TTY rules --------------------------------------------------------------


def test_permissions_prompt_alias_without_a_terminal_is_a_usage_error(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "probe", "--permissions", "prompt")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "--permissions ask" in result.stderr


def test_prompt_alias_resolves_to_ask_on_a_tty(
    cli: CliRunner,
    fresh_permission_alias_warnings: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(interaction, "stdin_is_tty", lambda: True)
    monkeypatch.setattr(interaction, "stdout_is_tty", lambda: True)

    result = invoke(cli, "resolve", "mock", "--permissions", "prompt")

    assert result.exit_code == vocab.EXIT_OK
    assert "permissions ask" in " ".join(result.stdout.split())
    assert "--permissions prompt is deprecated; use --permissions ask" in result.stderr


def _ask_under_a_pty(answer: str) -> tuple[bool, str]:
    """Run the prompt in a child that owns a real controlling terminal.

    `pty.fork` is the only way to exercise the `/dev/tty` branch: the prompt
    deliberately ignores stdin, so redirecting it proves nothing.
    """
    child = (
        "import sys; from acpc.cli import _tty_permission_prompt as ask; "
        "sys.exit(0 if ask('edit', 'Write blocked.md') else 1)"
    )
    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - replaced by execv in the child
        os.execv(sys.executable, [sys.executable, "-c", child])
    os.write(fd, answer.encode())
    asked = b""
    try:
        while chunk := os.read(fd, 1024):
            asked += chunk
    except OSError:
        pass  # EIO is how a pty master reports the child closing its end
    finally:
        os.close(fd)
    return os.waitpid(pid, 0)[1] == 0, asked.decode(errors="replace")


# forkpty warns about threads (xdist runs us multi-threaded); the child execs at once.
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
@pytest.mark.parametrize(
    ("answer", "allowed"),
    [("y\n", True), ("yes\n", True), ("n\n", False), ("\n", False)],
)
def test_the_tty_prompt_asks_on_the_terminal_and_reads_the_answer(
    answer: str, allowed: bool
) -> None:
    granted, asked = _ask_under_a_pty(answer)

    assert granted is allowed
    assert "acpc: allow edit? Write blocked.md [y/N]" in asked


def test_default_policy_denials_are_visible_and_persisted(cli: CliRunner, state_root: Path) -> None:
    result = invoke(cli, "run", "mock", "write-file:blocked.md", "--json")

    assert result.exit_code == vocab.EXIT_OK
    assert "denied: 1 edit (pass --permissions edit)" in result.stderr
    payload = json.loads(result.stdout)
    assert payload["denied"] == [
        {
            "category": "edit",
            "count": 1,
            "minimum_policy": "edit",
            "remedy": "pass --permissions edit",
        }
    ]
    session_id = json.loads(result.stdout)["session_id"]
    denied_meta = json.loads(
        (state_root / "sessions" / session_id / "meta.json").read_text(encoding="utf-8")
    )
    assert denied_meta["denied"] == {"edit": 1}
    assert denied_meta["resolution"]["permissions_source"] == "default"


def test_explicit_read_denials_match_default_reporting(cli: CliRunner, state_root: Path) -> None:
    result = invoke(cli, "run", "mock", "write-file:blocked.md", "--permissions", "read", "--json")

    assert result.exit_code == vocab.EXIT_OK
    assert "denied: 1 edit (pass --permissions edit)" in result.stderr
    assert "default" not in result.stderr
    payload = json.loads(result.stdout)
    assert payload["denied"][0]["category"] == "edit"
    assert payload["denied"][0]["count"] == 1
    assert payload["denied"][0]["remedy"] == "pass --permissions edit"
    session_id = json.loads(result.stdout)["session_id"]
    denied_meta = json.loads(
        (state_root / "sessions" / session_id / "meta.json").read_text(encoding="utf-8")
    )
    assert denied_meta["resolution"].get("permissions_source") is None


def test_default_and_explicit_read_denials_have_the_same_summary_line(
    cli: CliRunner,
) -> None:
    default = invoke(cli, "run", "mock", "write-file:default.md")
    explicit = invoke(cli, "run", "mock", "write-file:explicit.md", "--permissions", "read")

    default_line = next(line for line in default.stderr.splitlines() if "denied:" in line)
    explicit_line = next(line for line in explicit.stderr.splitlines() if "denied:" in line)

    assert default_line.split(" | session ", 1)[0].endswith(
        "denied: 1 edit (pass --permissions edit)"
    )
    assert explicit_line.split(" | session ", 1)[0].endswith(
        "denied: 1 edit (pass --permissions edit)"
    )


def test_default_policy_without_denials_has_no_summary_segment(
    cli: CliRunner, state_root: Path
) -> None:
    result = invoke(cli, "run", "mock", "tool:read", "--json")

    assert result.exit_code == vocab.EXIT_OK
    assert "denied:" not in result.stderr
    session_id = json.loads(result.stdout)["session_id"]
    meta = json.loads(
        (state_root / "sessions" / session_id / "meta.json").read_text(encoding="utf-8")
    )
    assert meta["denied"] == {}
    assert meta["resolution"]["permissions_source"] == "default"


def test_default_policy_summary_uses_the_strongest_remedy_for_all_categories(
    cli: CliRunner,
) -> None:
    result = invoke(cli, "run", "mock", "perm scenario")

    assert result.exit_code == vocab.EXIT_USAGE
    assert (
        "denied: 1 edit (pass --permissions edit) · 2 execute (pass --permissions execute) · "
        "1 switch_mode yolo (pass --permissions all)"
    ) in result.stderr


def test_wait_shows_default_policy_denials_from_disk(cli: CliRunner, live_daemon: None) -> None:
    started = invoke(cli, "run", "mock", "write-file:background.md", "--bg", "--json")

    assert started.exit_code == vocab.EXIT_OK
    session_id = json.loads(started.stdout)["session_id"]

    waited = invoke(cli, "wait", session_id)

    assert waited.exit_code == vocab.EXIT_OK
    assert "denied: 1 edit (pass --permissions edit)" in waited.stderr


def test_a_daemon_routed_failure_still_reports_the_result(
    cli: CliRunner, live_daemon: None
) -> None:
    """A turn that ran and failed is a result, not an aborted command.

    The daemon has already finalized it on disk, so the failure is reported by
    the shared error envelope on stderr and by the result document on stdout:
    a refusal is a complete answer for the call, so `partial` stays false.
    """
    result = invoke(cli, "run", "mock", "auth:vendor rejected the request", "--json")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    payload = error_envelope(result)
    assert payload["kind"] == "operation_failed"
    assert payload["context"]["status"] == "failed"
    document = json.loads(result.stdout)
    assert document["status"] == "failed"
    assert document["partial"] is False
    assert document["capabilities"] == {
        "steer_mode": "cancel-then-start",
        "continue_without_message": True,
    }
    assert document["answer"] != ""
    assert "-- failed" in result.stderr


def test_an_unknown_permission_value_is_a_usage_error(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "probe", "--permissions", "sudo-everything")

    assert result.exit_code == vocab.EXIT_USAGE


# --- explicit mode ceiling ---------------------------------------------------


def test_an_explicit_mode_is_rejected_above_the_policy(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "probe", "--mode", "yolo", "--permissions", "write")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "mode yolo grants all, which exceeds permissions execute" in result.stderr
    assert "lowest policy that admits it is all" in result.stderr


def test_an_entry_mode_is_rejected_at_resolution(cli: CliRunner, state_root: Path) -> None:
    (state_root / "agents" / "unsafe.toml").write_text(
        'extends = "mock"\nmode = "yolo"\n', encoding="utf-8"
    )

    result = invoke(cli, "run", "unsafe", "probe", "--permissions", "write")

    assert result.exit_code == vocab.EXIT_USAGE
    assert (
        f"agent 'unsafe' resolves mode yolo ({state_root / 'agents' / 'unsafe.toml'}): "
        "mode yolo grants all, which exceeds permissions execute; "
        "the lowest policy that admits it is all; edit the mode or permissions in the entry"
    ) in result.stderr
    assert not (state_root / "sessions").exists()


def test_an_inherited_mode_names_the_file_that_pins_it(cli: CliRunner, state_root: Path) -> None:
    """The entry named on the command line is not where the mode lives."""
    agents = state_root / "agents"
    (agents / "parent.toml").write_text('extends = "mock"\nmode = "yolo"\n', encoding="utf-8")
    (agents / "child.toml").write_text('extends = "parent"\n', encoding="utf-8")

    result = invoke(cli, "run", "child", "probe", "--permissions", "write")

    assert result.exit_code == vocab.EXIT_USAGE
    assert f"agent 'child' resolves mode yolo ({agents / 'parent.toml'})" in result.stderr


def test_an_entry_mode_is_accepted_with_permissions_all(cli: CliRunner, state_root: Path) -> None:
    (state_root / "agents" / "unsafe.toml").write_text(
        'extends = "mock"\nmode = "yolo"\n', encoding="utf-8"
    )

    result = invoke(cli, "run", "unsafe", "settings", "--permissions", "all", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert "/yolo/" in result.stdout


def test_an_explicit_mode_is_accepted_with_permissions_all(cli: CliRunner) -> None:
    result = invoke(
        cli, "run", "mock", "settings", "--mode", "yolo", "--permissions", "all", "--quiet"
    )

    assert result.exit_code == vocab.EXIT_OK
    assert "/yolo/" in result.stdout


def test_an_explicit_read_mode_needs_no_special_permissions(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "settings", "--mode", "plan", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert "/plan/" in result.stdout


# --- output contract --------------------------------------------------------


def test_blocking_run_emits_the_early_line_before_a_slow_turn_finishes(
    live_daemon: None,
) -> None:
    returncode, early_line, stdout, _stderr, still_running = run_cli_until_early_line(
        "run", "mock", "slow:2 early dispatch probe"
    )

    assert still_running
    assert returncode == vocab.EXIT_OK
    assert early_line.startswith("-- session ")
    assert "waited 2s" in stdout


def test_background_run_does_not_emit_the_early_line(cli: CliRunner, live_daemon: None) -> None:
    result = invoke(cli, "run", "mock", "echo:background", "--bg")

    assert result.exit_code == vocab.EXIT_OK
    assert "-- session " not in result.stderr
    # Non-TTY stdout: the tagged receipt without an answer section (V6a).
    lines = result.stdout.splitlines()
    assert lines[0].startswith('<result session_id="') and lines[1] == "<metadata>"
    assert "<answer" not in result.stdout and lines[-1] == "</result>"
    metadata = json.loads(lines[2])
    assert metadata["next"][:2] == ["acpc", "wait"] and "paths" in metadata


def test_cold_background_receipt_waits_for_initialize(
    cli: CliRunner, live_daemon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPC_MOCK_STEERING", "1")
    monkeypatch.setenv("ACPC_MOCK_INIT_DELAY", "1")

    started_at = time.monotonic()
    result = invoke(cli, "run", "mock", "echo:delayed receipt", "--bg", "--json")
    elapsed = time.monotonic() - started_at

    assert result.exit_code == vocab.EXIT_OK, result.stderr
    # Only the lower bound proves the receipt waited for `initialize`; the
    # cold daemon and adapter start on a loaded host are not under test.
    assert elapsed >= 0.9
    document = json.loads(result.stdout)
    assert document["capabilities"] == {
        "steer_mode": "in-place",
        "continue_without_message": True,
    }
    status = json.loads(invoke(cli, "status", document["session_id"], "--json").stdout)
    assert status["capabilities"] == document["capabilities"]


def test_cold_timeout_covers_initialize_and_leaves_turn_running(
    cli: CliRunner, live_daemon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPC_MOCK_INIT_DELAY", "8")

    started_at = time.monotonic()
    result = invoke(cli, "run", "mock", "echo:cold timeout", "--timeout", "2", "--json")
    elapsed = time.monotonic() - started_at

    assert result.exit_code == vocab.EXIT_TIMEOUT, result.stderr
    assert elapsed < 5
    assert result.stdout == ""
    error = error_envelope(result)
    assert error["kind"] == "timeout"
    session_id = error["context"]["session_id"]
    assert error["context"]["status"] in {"starting", "preparing"}

    waited = invoke(cli, "wait", session_id, "--json")

    assert waited.exit_code == vocab.EXIT_OK, waited.stderr
    assert "cold timeout" in json.loads(waited.stdout)["answer"]


def test_background_receipt_and_immediate_status_share_capabilities(
    cli: CliRunner, live_daemon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPC_MOCK_STEERING", "1")

    result = invoke(cli, "run", "mock", "echo:matching capability", "--bg", "--json")

    assert result.exit_code == vocab.EXIT_OK, result.stderr
    document = json.loads(result.stdout)
    status = json.loads(invoke(cli, "status", document["session_id"], "--json").stdout)
    assert document["capabilities"] == {
        "steer_mode": "in-place",
        "continue_without_message": True,
    }
    assert status["capabilities"] == document["capabilities"]


@pytest.mark.parametrize(
    "command",
    [
        ("status", "missing", "--json"),
        ("wait", "missing", "--json"),
        ("cancel", "missing", "--json"),
        ("steer", "missing", "x", "--json"),
        ("continue", "missing", "x", "--json"),
        ("log", "missing", "--json"),
        ("delete", "missing", "--yes", "--json"),
    ],
)
def test_missing_session_errors_identify_the_supplied_selector(
    cli: CliRunner, command: tuple[str, ...]
) -> None:
    result = invoke(cli, *command)

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    error = error_envelope(result)
    assert error["kind"] == "not_found"
    assert error["context"] == {"session_id": "missing", "status": None}


def test_quiet_suppresses_the_stderr_summary(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "echo:quiet please", "--quiet")

    assert result.stderr == ""


def test_without_quiet_the_summary_is_exactly_one_stderr_line(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "echo:summarize me")

    metadata_lines = [line for line in result.stderr.splitlines() if line.startswith("-- ")]
    summary_lines = [line for line in metadata_lines if "exit 0" in line]
    assert len(summary_lines) == 1
    assert len(metadata_lines) == 2


def test_early_line_segments_match_the_summary_and_stdout_matches_answer_file(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_human_presentation(monkeypatch)
    result = invoke(cli, "run", "mock", "echo:format and bytes")

    metadata_lines = [line for line in result.stderr.splitlines() if line.startswith("-- ")]
    early_line = next(line for line in metadata_lines if line.startswith("-- session "))
    summary_line = next(line for line in metadata_lines if "exit 0" in line)
    early_segments = early_line.removeprefix("-- ").split(" | ")
    summary_segments = summary_line.removeprefix("-- ").split(" | ")

    assert early_segments == [
        segment for segment in summary_segments if segment.startswith(("session ", "dir "))
    ]
    session_id = early_segments[0].removeprefix("session ")
    assert result.stdout == sessions.answer_path(session_id).read_text(encoding="utf-8")


def test_stdout_bytes_match_answer_file_without_a_usage_boundary(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_human_presentation(monkeypatch)
    result = invoke(cli, "run", "mock", "separator contract probe")

    assert "Working through:" in result.stdout
    assert "## Answer" in result.stdout
    assert "\n\n## Answer" not in result.stdout
    metadata_lines = [line for line in result.stderr.splitlines() if line.startswith("-- ")]
    early_line = next(line for line in metadata_lines if line.startswith("-- session "))
    session_id = early_line.split()[2]
    assert result.stdout.encode("utf-8") == sessions.answer_path(session_id).read_bytes()


def test_the_summary_starts_on_a_fresh_line_after_an_unterminated_answer(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SPEC output contract: `--` separates only at a line boundary, and the
    compensating newline goes to stderr — stdout stays byte-identical to
    answer.md."""
    force_human_presentation(monkeypatch)
    result = invoke(cli, "run", "mock", "echo:no trailing newline")

    assert result.stdout == "no trailing newline"
    # The early session line precedes the answer, so the compensating newline
    # is no longer stderr's first byte — it is the blank line that separates
    # the unterminated answer from the summary in the merged blob.
    lines = result.stderr.split("\n")
    summary_index = next(index for index, line in enumerate(lines) if "exit 0" in line)
    assert lines[summary_index - 1] == ""


def test_a_terminated_answer_gets_no_blank_line_before_the_summary(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_human_presentation(monkeypatch)
    result = invoke(cli, "run", "mock", "echo:ends with a newline\n")

    assert result.stdout == "ends with a newline\n"
    assert result.stderr.startswith("-- ")


def test_run_human_presentation_escapes_control_bytes(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SPEC.md *Text presentation*: the terminal answer escapes control bytes
    exactly as the tagged document does, keeping line breaks."""
    force_human_presentation(monkeypatch)
    result = invoke(cli, "run", "mock", "echo:safe\x1b[31mred\x1b[0m\nline\x9btwo")

    assert "\x1b" not in result.stdout
    assert "\x9b" not in result.stdout
    assert "^[[31mred^[[0m" in result.stdout
    assert "\\u009b" in result.stdout
    assert "\nline" in result.stdout


def test_run_output_file_on_a_terminal_gets_the_escaped_text(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--output-file` on a terminal gets exactly what stdout would have."""
    force_human_presentation(monkeypatch)
    output_file = tmp_path / "answer.txt"

    result = invoke(
        cli, "run", "mock", "echo:danger\x1b[31mred\x1b[0m", "--output-file", str(output_file)
    )

    assert result.exit_code == vocab.EXIT_OK
    written = output_file.read_text(encoding="utf-8")
    assert "\x1b" not in written
    assert "^[[31mred^[[0m" in written


def test_wait_human_presentation_escapes_control_bytes(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch, live_daemon: None
) -> None:
    started = invoke(cli, "run", "mock", "echo:bg\x1b[31mred\x1b[0m", "--bg", "--json")
    assert started.exit_code == vocab.EXIT_OK
    session_id = json.loads(started.stdout)["session_id"]

    force_human_presentation(monkeypatch)
    result = invoke(cli, "wait", session_id)

    assert "\x1b" not in result.stdout
    assert "^[[31mred^[[0m" in result.stdout


def test_the_direct_child_note_rides_the_one_summary_line(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "echo:route me")

    summary_lines = [line for line in result.stderr.splitlines() if line.startswith("-- ")]
    assert len(summary_lines) == 2
    assert any("direct child" in line for line in summary_lines)
    assert any(line.startswith("-- session ") for line in summary_lines)


def test_background_run_reports_unavailable_when_the_socket_path_is_too_long(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, live_daemon: None
) -> None:
    """SPEC.md `daemon`: a socket path that still exceeds the platform limit
    even hashed starts no daemon — `run --bg` reports `unavailable` naming
    the limit and `ACPC_HOME`, with no daemon lock or pid file left behind
    (a spawn that was always going to fail never even starts)."""
    long_home = tmp_path / ("x" * 150)
    agents = long_home / "agents"
    agents.mkdir(parents=True)
    (agents / "mock.toml").write_text(MOCK_ENTRY, encoding="utf-8")
    monkeypatch.setenv("ACPC_HOME", str(long_home))
    cli = CliRunner()

    result = cli.invoke(main, ["run", "mock", "echo:x", "--bg", "--json"], catch_exceptions=False)

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    envelope = error_envelope(result)
    assert envelope["kind"] == "unavailable"
    assert "ACPC_HOME" in envelope["message"]
    assert "108" in envelope["message"] or "104" in envelope["message"]
    daemon_dir = long_home / "daemon"
    assert not daemon_dir.exists()


def test_blocking_run_falls_back_direct_when_the_socket_path_is_too_long(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, live_daemon: None
) -> None:
    """Same over-long `ACPC_HOME`, but a blocking `run`: it takes the direct
    path, as for any unavailable daemon, and names the cause in its stderr
    summary rather than failing the call."""
    long_home = tmp_path / ("x" * 150)
    agents = long_home / "agents"
    agents.mkdir(parents=True)
    (agents / "mock.toml").write_text(MOCK_ENTRY, encoding="utf-8")
    monkeypatch.setenv("ACPC_HOME", str(long_home))
    cli = CliRunner()

    result = cli.invoke(main, ["run", "mock", "echo:direct fallback"], catch_exceptions=False)

    assert result.exit_code == vocab.EXIT_OK, result.stderr
    assert "direct child" in result.stderr
    assert "ACPC_HOME" in result.stderr
    daemon_dir = long_home / "daemon"
    assert not daemon_dir.exists()


def test_a_default_cwd_resolves_to_the_callers_absolute_directory(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The adapter receives cwd over session/new and resolves a relative path
    against its own host process, so acpc must send the caller's absolute
    directory — vendors reject a literal '.'."""
    caller_dir = tmp_path / "caller"
    caller_dir.mkdir()
    monkeypatch.chdir(caller_dir)

    result = invoke(cli, "resolve", "mock", "--json")

    payload = json.loads(result.stdout)
    assert payload["cwd"] == str(caller_dir.resolve())


def test_the_stored_resolution_carries_the_default_cwd(
    cli: CliRunner, state_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """meta.json is what the daemon path and continue reuse, so the resolved
    default cwd has to be stored, not re-derived by another process."""
    caller_dir = tmp_path / "caller"
    caller_dir.mkdir()
    monkeypatch.chdir(caller_dir)

    result = invoke(cli, "run", "mock", "echo:x", "--quiet", "--json")

    session_id = json.loads(result.stdout)["session_id"]
    meta = json.loads(
        (state_root / "sessions" / session_id / "meta.json").read_text(encoding="utf-8")
    )
    assert meta["resolution"]["cwd"] == str(caller_dir.resolve())


@pytest.mark.parametrize("command", ["run", "resolve", "continue"])
@pytest.mark.parametrize("path_kind", ["missing", "file"])
def test_a_non_directory_cwd_is_invalid_before_session_or_daemon_creation(
    cli: CliRunner,
    state_root: Path,
    tmp_path: Path,
    command: str,
    path_kind: str,
) -> None:
    cwd = tmp_path / f"cwd-{path_kind}"
    if path_kind == "file":
        cwd.write_text("not a directory", encoding="utf-8")
    session_id = None
    if command == "continue":
        started = invoke(cli, "run", "mock", "existing session", "--quiet", "--json")
        assert started.exit_code == vocab.EXIT_OK, started.stderr
        session_id = json.loads(started.stdout)["session_id"]
    sessions_dir = state_root / "sessions"
    before_sessions = (
        {path.name for path in sessions_dir.iterdir()} if sessions_dir.exists() else set()
    )
    if command == "run":
        args = ["run", "mock", "new session"]
    elif command == "resolve":
        args = ["resolve", "mock"]
    else:
        assert session_id is not None
        args = ["continue", session_id, "follow up"]

    result = invoke(cli, *args, "--cwd", str(cwd), "--json")

    error = error_envelope(result)
    assert result.exit_code == vocab.EXIT_USAGE
    assert error["kind"] == "invalid_input"
    assert str(cwd.resolve()) in error["message"]
    assert error["hint"] == "Run acpc from an existing directory or pass --cwd."
    after_sessions = (
        {path.name for path in sessions_dir.iterdir()} if sessions_dir.exists() else set()
    )
    assert after_sessions == before_sessions
    assert not (state_root / "daemon").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permits removing the current directory")
def test_run_from_a_removed_caller_directory_is_invalid_input(
    cli: CliRunner, state_root: Path, tmp_path: Path
) -> None:
    original_cwd = Path.cwd()
    removed_cwd = tmp_path / "removed-caller"
    removed_cwd.mkdir()
    os.chdir(removed_cwd)
    removed_cwd.rmdir()
    try:
        result = invoke(cli, "run", "mock", "no cwd", "--json")
    finally:
        os.chdir(original_cwd)

    error = error_envelope(result)
    assert result.exit_code == vocab.EXIT_USAGE
    assert error["kind"] == "invalid_input"
    assert error["hint"] == "Run acpc from an existing directory or pass --cwd."
    assert "Traceback" not in result.stderr
    assert not (state_root / "sessions").exists()
    assert not (state_root / "daemon").exists()


def test_a_warm_daemon_survives_removal_of_its_start_directory(
    cli: CliRunner, state_root: Path, tmp_path: Path, live_daemon: None
) -> None:
    original_cwd = Path.cwd()
    start_cwd = tmp_path / "daemon-start-cwd"
    session_cwd = tmp_path / "second-session-cwd"
    start_cwd.mkdir()
    session_cwd.mkdir()

    def daemon_pid() -> int:
        result = invoke(cli, "daemon", "status", "mock", "--json")
        assert result.exit_code == vocab.EXIT_OK, result.stderr
        return json.loads(result.stdout)["items"][0]["pid"]

    try:
        os.chdir(start_cwd)
        first = invoke(cli, "run", "mock", "start daemon", "--quiet", "--json")
        assert first.exit_code == vocab.EXIT_OK, first.stderr
        pid_before = daemon_pid()
        proc_cwd = Path(f"/proc/{pid_before}/cwd")
        if proc_cwd.is_symlink():
            assert Path(os.readlink(proc_cwd)) == state_root / "daemon"

        os.chdir(tmp_path)
        start_cwd.rmdir()
        second = invoke(
            cli,
            "run",
            "mock",
            "cwd-info",
            "--cwd",
            str(session_cwd),
            "--quiet",
            "--json",
        )
    finally:
        os.chdir(original_cwd)

    assert second.exit_code == vocab.EXIT_OK, second.stderr
    result = json.loads(second.stdout)
    assert f"process_cwd={state_root / 'daemon'}" in result["answer"]
    assert f"session_cwd={session_cwd.resolve()}" in result["answer"]
    assert daemon_pid() == pid_before


def test_output_file_receives_the_answer(cli: CliRunner, tmp_path: Path) -> None:
    target = tmp_path / "answer.md"

    result = invoke(
        cli, "run", "mock", "echo:written to a file", "--output-file", str(target), "--quiet"
    )

    assert result.exit_code == vocab.EXIT_OK
    assert result.stdout == ""
    assert "written to a file" in target.read_text(encoding="utf-8")


def test_json_output_carries_the_session_id_and_paths(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "echo:as json", "--json", "--quiet")

    payload = json.loads(result.stdout)
    assert payload["session_id"]
    assert payload["paths"]["answer"].endswith("answer.md")
    assert payload["capabilities"] == {
        "steer_mode": "cancel-then-start",
        "continue_without_message": True,
    }
    assert "as json" in payload["answer"]


def test_unobserved_usage_reports_null_not_zero(cli: CliRunner) -> None:
    """SPEC.md V6c (draft.11): usage the tool never observed is `null`, never `0`.

    The `echo:` scenario answers without ever sending a `usage_update`, so
    acpc has nothing to report.
    """
    result = invoke(cli, "run", "mock", "echo:hi", "--json", "--quiet")

    payload = json.loads(result.stdout)
    assert payload["context"] is None


def test_unobserved_usage_is_absent_from_the_tagged_metadata(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "echo:hi", "--quiet")

    metadata_line = next(line for line in result.stdout.splitlines() if line.startswith("{"))
    metadata = json.loads(metadata_line)
    assert "context" not in metadata


def test_unobserved_usage_shows_a_dot_in_the_stderr_summary(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "echo:hi")

    summary_line = next(line for line in result.stderr.splitlines() if "exit 0" in line)
    assert "ctx ·" in summary_line


def test_observed_usage_still_reports_the_real_count(cli: CliRunner) -> None:
    """A scenario that streams `usage_update` keeps reporting an int (unchanged)."""
    result = invoke(cli, "run", "mock", "meta:1200:1000000000:tokens present", "--json", "--quiet")

    payload = json.loads(result.stdout)
    assert payload["context"] == {"used": 1200, "size": None, "peak": 1200}


@pytest.mark.parametrize(
    "profile,usage,meta,context",
    [
        (
            "claude",
            {
                "totalTokens": 135037,
                "inputTokens": 8,
                "outputTokens": 440,
                "cachedReadTokens": 133240,
                "cachedWriteTokens": 1349,
            },
            {
                "quota": {
                    "token_count": {
                        "totalTokens": 135037,
                        "inputTokens": 8,
                        "cachedInputTokens": 133240,
                        "cachedWriteTokens": 1349,
                        "outputTokens": 440,
                        "reasoningOutputTokens": 0,
                    },
                    "model_usage": [
                        {
                            "model": "claude-opus-5[1m]",
                            "token_count": {
                                "totalTokens": 135037,
                                "inputTokens": 8,
                                "cachedInputTokens": 133240,
                                "cachedWriteTokens": 1349,
                                "outputTokens": 440,
                                "reasoningOutputTokens": 0,
                            },
                        }
                    ],
                }
            },
            {"used": 135037, "size": 200000, "peak": 135037},
        ),
        (
            "codex",
            {
                "totalTokens": 21789,
                "inputTokens": 279,
                "cachedReadTokens": 21504,
                "outputTokens": 6,
                "thoughtTokens": 0,
            },
            {
                "quota": {
                    "token_count": {
                        "totalTokens": 21789,
                        "inputTokens": 279,
                        "cachedInputTokens": 21504,
                        "outputTokens": 6,
                        "reasoningOutputTokens": 0,
                    },
                    "model_usage": [
                        {
                            "model": "gpt-6-sol",
                            "token_count": {
                                "totalTokens": 21789,
                                "inputTokens": 279,
                                "cachedInputTokens": 21504,
                                "outputTokens": 6,
                                "reasoningOutputTokens": 0,
                            },
                        }
                    ],
                }
            },
            {"used": 21789, "size": 200000, "peak": 21789},
        ),
        (
            "grok",
            None,
            {
                "totalTokens": 25234,
                "modelId": "grok-4.7",
                "usage": {
                    "inputTokens": 99736,
                    "outputTokens": 414,
                    "totalTokens": 100150,
                    "cachedReadTokens": 76032,
                    "cacheCreationTokens": 0,
                    "reasoningTokens": 197,
                    "modelCalls": 4,
                    "apiDurationMs": 9064,
                    "costUsdTicks": 298887200,
                    "modelUsage": {
                        "grok-4.7-build": {
                            "inputTokens": 99736,
                            "outputTokens": 414,
                            "totalTokens": 100150,
                            "cachedReadTokens": 76032,
                            "cacheCreationTokens": 0,
                            "reasoningTokens": 197,
                            "modelCalls": 4,
                            "apiDurationMs": 9064,
                            "costUsdTicks": 298887200,
                        }
                    },
                    "numTurns": 4,
                },
            },
            {"used": 25234, "size": None, "peak": 25234},
        ),
    ],
)
def test_prompt_response_usage_is_recorded_verbatim(
    cli: CliRunner,
    state_root: Path,
    profile: str,
    usage: dict[str, object] | None,
    meta: dict[str, object],
    context: dict[str, int | None],
) -> None:
    result = invoke(cli, "run", "mock", f"rawusage:{profile}", "--json", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    payload = json.loads(result.stdout)
    events = [
        json.loads(line)
        for line in (state_root / "sessions" / payload["session_id"] / "transcript.ndjson")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    turn_usage = [event for event in events if event.get("meta", {}).get("scope") == "turn"]

    assert payload["context"] == context
    assert len(turn_usage) == 1
    message = next(event for event in events if event.get("type") == "msg")
    assert message["i"] < turn_usage[0]["i"]
    assert turn_usage[0]["used"] == context["used"]
    assert turn_usage[0]["size"] == context["size"]
    assert turn_usage[0]["meta"] == {
        "prompt_usage": {"usage": usage, "meta": meta},
        "adapter": {"name": "mock-agent", "version": "0.1.0"},
        "scope": "turn",
    }
    if profile == "grok":
        assert len([event for event in events if event.get("type") == "usage"]) == 1


def test_cancelled_prompt_response_without_usage_records_null_report(
    cli: CliRunner, state_root: Path
) -> None:
    result = invoke(cli, "run", "mock", "cancelled-no-usage:", "--json", "--quiet")

    assert result.exit_code == vocab.EXIT_CANCELLED
    session_id = sessions.list_sessions()[0].session_id
    events = [
        json.loads(line)
        for line in (state_root / "sessions" / session_id / "transcript.ndjson")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    turn_usage = [event for event in events if event.get("meta", {}).get("scope") == "turn"]

    assert len(turn_usage) == 1
    assert turn_usage[0]["used"] is None
    assert turn_usage[0]["size"] is None
    assert turn_usage[0]["meta"] == {
        "prompt_usage": {"usage": None, "meta": None},
        "adapter": {"name": "mock-agent", "version": "0.1.0"},
        "scope": "turn",
    }


def test_json_rpc_error_does_not_record_a_prompt_response(cli: CliRunner, state_root: Path) -> None:
    first = invoke(cli, "run", "mock", "echo:first", "--json", "--quiet")
    session_id = json.loads(first.stdout)["session_id"]
    failed = invoke(cli, "continue", session_id, "crash-late:partial", "--json", "--quiet")
    assert failed.exit_code == vocab.EXIT_AGENT_ERROR

    events = [
        json.loads(line)
        for line in (state_root / "sessions" / session_id / "transcript.ndjson")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    turn_usage = [event for event in events if event.get("meta", {}).get("scope") == "turn"]

    assert len(turn_usage) == 1
    assert turn_usage[0]["meta"]["prompt_usage"] == {"usage": None, "meta": None}


def test_max_output_caps_stdout(cli: CliRunner) -> None:
    result = invoke(
        cli, "run", "mock", "trigger the huge scenario", "--quiet", "--max-output", "4096"
    )

    assert len(result.stdout.encode("utf-8")) <= 4096


def test_max_output_zero_disables_the_cap(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "trigger the huge scenario", "--quiet", "--max-output", "0")

    assert len(result.stdout) > 131072


@pytest.mark.parametrize(
    "args",
    [
        ("run", "mock", "echo:valid"),
        ("continue", "missing", "message"),
        ("steer", "missing", "instruction"),
        ("wait", "missing"),
    ],
    ids=("run", "continue", "steer", "wait"),
)
@pytest.mark.parametrize("value", ["1", "4095"])
def test_answer_commands_reject_max_output_below_4096(
    cli: CliRunner, args: tuple[str, ...], value: str
) -> None:
    result = invoke(cli, *args, "--max-output", value)

    assert result.exit_code == vocab.EXIT_USAGE
    assert "4096" in result.stderr
    assert "--max-output" in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ("run", "mock", "echo:valid", "--quiet"),
        ("continue", "missing", "message"),
        ("steer", "missing", "instruction"),
        ("wait", "missing"),
    ],
    ids=("run", "continue", "steer", "wait"),
)
def test_answer_commands_accept_max_output_4096(cli: CliRunner, args: tuple[str, ...]) -> None:
    result = invoke(cli, *args, "--max-output", "4096")

    assert result.exit_code != vocab.EXIT_USAGE, result.stderr
    assert "--max-output must" not in result.stderr


def test_log_keeps_its_per_record_max_output_minimum(cli: CliRunner) -> None:
    result = invoke(cli, "log", "missing", "--since", "0", "--max-output", "1")

    assert result.exit_code != vocab.EXIT_USAGE
    assert "4096" not in result.stderr


def test_a_negative_max_output_is_a_usage_error(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "probe", "--max-output", "-1")

    assert result.exit_code == vocab.EXIT_USAGE


# --- exit codes at the CLI boundary -----------------------------------------


def test_a_refusal_exits_1(cli: CliRunner) -> None:
    """A turn that ran to an end acpc did not ask for is a failed operation."""
    result = invoke(cli, "run", "mock", "please fail this on purpose", "--quiet")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    envelope = error_envelope(result)
    assert envelope["kind"] == "operation_failed"
    assert envelope["context"]["session_id"]
    assert envelope["context"]["status"] == "failed"


def test_cancel_after_exits_1_and_cancels(cli: CliRunner) -> None:
    result = invoke(
        cli, "run", "mock", "slow:30 cli timeout probe", "--cancel-after", "1", "--quiet"
    )

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    envelope = error_envelope(result)
    assert envelope["kind"] == "operation_failed"
    assert envelope["context"]["status"] == "canceled"
    assert sessions.read_meta(envelope["context"]["session_id"]).state == "canceled"


def test_timeout_cannot_be_combined_with_background(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "hello", "--background", "--timeout", "1")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "--cancel-after" in result.stderr


@pytest.mark.parametrize("deadline_flag", ["--timeout", "--cancel-after"])
def test_resolve_rejects_deadlines_before_creating_a_session(
    cli: CliRunner, state_root: Path, deadline_flag: str
) -> None:
    result = invoke(cli, "resolve", "mock", deadline_flag, "1", "--json")

    assert result.exit_code == vocab.EXIT_USAGE
    envelope = error_envelope(result)
    assert envelope["kind"] == "invalid_input"
    assert deadline_flag in envelope["message"]
    assert not (state_root / "sessions").exists()


def test_run_resolve_flag_names_the_new_command(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "probe", "--resolve")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "acpc resolve <agent>" in error_envelope(result)["message"]


def test_timeout_only_stops_waiting_and_reports_observed_state(
    cli: CliRunner, live_daemon: None
) -> None:
    result = invoke(cli, "run", "mock", "slow:30 wait only", "--timeout", "1", "--quiet")

    assert result.exit_code == vocab.EXIT_TIMEOUT, result.stderr
    envelope = error_envelope(result)
    assert envelope["kind"] == "timeout"
    session_id = envelope["context"]["session_id"]
    assert envelope["context"]["status"] in {"starting", "running"}
    assert sessions.read_meta(session_id).state in {"starting", "running"}


def test_timeout_direct_fallback_keeps_the_session_alive(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "slow:2 direct wait only", "--timeout", "0.1", "--quiet")

    assert result.exit_code == vocab.EXIT_TIMEOUT, result.stderr
    envelope = error_envelope(result)
    session_id = envelope["context"]["session_id"]
    assert sessions.load(session_id).state in {"starting", "running"}

    # This deliberately exercises the direct fallback, where no daemon owns
    # an await_turn event.  The only observable completion signal is the
    # session state written by that child process, so polling is unavoidable.
    # The child itself sleeps for two seconds; 15 seconds leaves scheduling
    # room under the suite's 30-second per-test cap without changing the
    # production timeout contract.
    deadline = time.monotonic() + 15
    while sessions.load(session_id).is_active and time.monotonic() < deadline:
        time.sleep(0.05)
    assert sessions.load(session_id).state == "succeeded"


@pytest.mark.filterwarnings(
    "ignore:This process .* is multi-threaded, use of forkpty\\(\\) may lead to deadlocks.*:DeprecationWarning"
)
def test_timeout_direct_worker_survives_closing_the_client_pty(state_root: Path) -> None:
    # A file at the daemon directory makes the real subprocess take the
    # direct fallback, without changing the production routing code.
    (state_root / "daemon").write_text("direct fallback", encoding="utf-8")
    argv = [
        sys.executable,
        "-c",
        "from acpc.cli import main; main()",
        "run",
        "mock",
        "slow:2 pty direct timeout",
        "--permissions",
        "read",
        "--timeout",
        "0.1",
        "--quiet",
        "--json",
    ]
    child_pid, master_fd = pty.fork()
    if child_pid == 0:
        signal.signal(signal.SIGHUP, signal.SIG_DFL)
        os.environ["ACPC_HOME"] = str(state_root)
        os.execve(sys.executable, argv, os.environ.copy())

    output = bytearray()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master_fd], [], [], 0.2)
            if not ready:
                continue
            try:
                output.extend(os.read(master_fd, 4096))
            except OSError:
                break
            if b'"kind": "timeout"' in output or b'"kind":"timeout"' in output:
                break
        rendered = output.decode(errors="replace")
        match = re.search(r'"session_id"\s*:\s*"([a-z0-9]{4})"', rendered)
        assert match is not None, rendered
        session_id = match.group(1)
        os.close(master_fd)
        master_fd = -1

        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if sessions.load(session_id).state == "succeeded":
                break
            time.sleep(0.05)
        assert sessions.load(session_id).state == "succeeded"
    finally:
        if master_fd >= 0:
            os.close(master_fd)
        waited_pid, _ = os.waitpid(child_pid, os.WNOHANG)
        if waited_pid == 0:
            os.kill(child_pid, signal.SIGKILL)
            os.waitpid(child_pid, 0)


def test_timeout_direct_fallback_can_still_be_canceled(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "slow:30 cancel after wait", "--timeout", "0.1", "--quiet")

    assert result.exit_code == vocab.EXIT_TIMEOUT
    session_id = error_envelope(result)["context"]["session_id"]
    deadline = time.monotonic() + 5
    while sessions.load(session_id).state == "starting" and time.monotonic() < deadline:
        time.sleep(0.05)

    canceled = invoke(cli, "cancel", session_id, "--json")

    assert canceled.exit_code == vocab.EXIT_OK, canceled.stderr
    assert json.loads(canceled.stdout)["status"] == "canceled"


def test_a_direct_child_killed_during_turn_one_still_leaves_a_continuable_session(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SPEC.md *State on disk*: the adapter session id is recorded as soon as
    the adapter has accepted the session, before the prompt is sent — so a
    direct host killed mid-turn-1 still leaves `adapter_session_id` on disk
    and `continue` takes the normal cold-resume path instead of
    `corrupt_state`.

    The child process below is a real subprocess, not `CliRunner` — the
    autouse `no_daemon` stub in `conftest.py` only patches
    `daemon_client.ensure_daemon` inside this test's own process, so a plain
    `ACPC_HOME` would let the child spawn (and go through) a real daemon,
    leaving `runner.py`'s direct-path write untested. An `ACPC_HOME` whose
    socket path still exceeds the platform limit after hashing (item F) is
    the same deterministic, daemon-free mechanism the item F tests use to
    force the direct path, with no new test-only switch.
    """
    long_home = tmp_path / ("x" * 150)
    agents = long_home / "agents"
    agents.mkdir(parents=True)
    (agents / "mock.toml").write_text(MOCK_ENTRY, encoding="utf-8")
    monkeypatch.setenv("ACPC_HOME", str(long_home))
    release = tmp_path / "release-before-prompt"
    ready = tmp_path / "before-prompt-ready"
    monkeypatch.setenv("ACPC_MOCK_BLOCK_BEFORE_PROMPT", str(release))
    monkeypatch.setenv("ACPC_MOCK_BLOCK_BEFORE_PROMPT_READY", str(ready))
    command = [
        sys.executable,
        "-c",
        "from acpc.cli import main; raise SystemExit(main())",
        "run",
        "mock",
        "turn one killed",
        "--name",
        "direct-turn-one-killed",
        "--quiet",
    ]
    process = subprocess.Popen(
        command,
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not ready.exists():
            time.sleep(0.02)
        assert ready.exists()

        deadline = time.monotonic() + 5
        session_id = None
        while time.monotonic() < deadline:
            try:
                session_id = sessions.resolve_selector("direct-turn-one-killed")
                break
            except sessions.SessionError:
                time.sleep(0.02)
        assert session_id is not None

        running = sessions.read_meta(session_id)
        assert running.state == "running"
        assert running.turns == 1
        # The point of the long `ACPC_HOME` above: no daemon was started, so
        # `adapter_session_id` below can only have come from the direct path.
        # Without this the test would silently go back to proving the daemon's
        # write if the socket-limit mechanism ever stopped forcing direct.
        assert not (long_home / "daemon").exists()
        assert running.adapter_session_id is not None
        assert running.pid is not None

        os.kill(process.pid, signal.SIGKILL)
        os.kill(running.pid, signal.SIGKILL)
        release.touch()
        process.communicate(timeout=10)

        deadline = time.monotonic() + 5
        unknown = sessions.load(session_id)
        while unknown.state == "running" and time.monotonic() < deadline:
            time.sleep(0.05)
            unknown = sessions.load(session_id)
        assert unknown.state == "unknown"
        assert sessions.read_meta(session_id).adapter_session_id is not None
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)

    monkeypatch.delenv("ACPC_MOCK_BLOCK_BEFORE_PROMPT")
    monkeypatch.delenv("ACPC_MOCK_BLOCK_BEFORE_PROMPT_READY")

    resumed = invoke(cli, "continue", session_id, "again", "--quiet")

    assert resumed.exit_code == vocab.EXIT_OK, resumed.stderr
    assert sessions.load(session_id).state == "succeeded"


def test_a_bare_integer_timeout_is_seconds(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "slow:2 bare seconds", "--cancel-after", "1", "--quiet")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR


def test_a_five_minute_timeout_is_not_five_seconds(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "slow:6 five minute timeout", "--timeout", "5m", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert "waited 6s" in result.stdout


def test_an_invalid_timeout_has_the_pinned_duration_error(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "hello", "--timeout", "5x")

    assert result.exit_code == vocab.EXIT_USAGE
    envelope = json.loads(result.stderr)["error"]
    assert envelope["kind"] == "invalid_input"
    assert "'5x' is not a duration" in envelope["message"]
    assert "90s, 5m, 1h, 1h30m" in envelope["message"]


@pytest.mark.parametrize("value", ["-1", "-1m"])
def test_negative_timeout_is_a_usage_error(cli: CliRunner, value: str) -> None:
    result = invoke(cli, "run", "mock", "hello", "--timeout", value)

    assert result.exit_code == vocab.EXIT_USAGE
    assert "--timeout" in result.stderr


# --- session naming ---------------------------------------------------------


def test_a_name_can_be_claimed_for_a_session(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "echo:named run", "--name", "my-run", "--json", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    session_id = json.loads(result.stdout)["session_id"]
    assert sessions.read_meta(session_id).name == "my-run"


def test_a_name_still_active_on_another_session_is_refused(cli: CliRunner) -> None:
    invoke(cli, "run", "mock", "echo:first", "--name", "taken", "--quiet")

    # The first holder finished, so the name rebinds with a warning, not an error.
    result = invoke(cli, "run", "mock", "echo:second", "--name", "taken", "--quiet")

    assert result.exit_code == vocab.EXIT_OK


def test_the_reserved_name_last_is_refused(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "echo:nope", "--name", "last", "--quiet")

    assert result.exit_code == vocab.EXIT_USAGE


def test_an_execute_floor_refuses_policies_below_execute(
    cli: CliRunner, grok_floor_entry: None
) -> None:
    """The mock table records that nothing delegates, so the default
    non-TTY policy (read) must refuse instead of claiming a ceiling."""
    result = invoke(cli, "run", "grok-floor", "probe")

    assert result.exit_code == vocab.EXIT_USAGE
    envelope = error_envelope(result)
    assert envelope["kind"] == "permission_denied"
    assert "the lowest policy grok-floor runs under is execute" in envelope["message"]
    assert "declared modes: bypass (grants all), default (grants execute)" in envelope["message"]
    assert envelope["hint"] == "Run: acpc run grok-floor --permissions execute"


def test_a_non_selectable_mode_neither_sets_the_floor_nor_hides_in_the_listing(
    cli: CliRunner, state_root: Path
) -> None:
    (state_root / "agents" / "gated-floor.toml").write_text(
        'command = "true"\n[modes]\n'
        'plan = { grants = "edit", delegates = true, selectable = false }\n'
        'runner = { grants = "execute", delegates = true }\n',
        encoding="utf-8",
    )

    result = invoke(cli, "run", "gated-floor", "probe", "--permissions", "edit")

    envelope = error_envelope(result)
    assert envelope["kind"] == "permission_denied"
    assert "the lowest policy gated-floor runs under is execute" in envelope["message"]
    assert "plan (grants edit, not selectable), runner (grants execute)" in envelope["message"]
    assert envelope["hint"] == "Run: acpc run gated-floor --permissions execute"


def test_inherited_ceiling_below_an_entry_floor_names_the_ceiling(
    cli: CliRunner, grok_floor_entry: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPC_CEILING", "read")

    result = invoke(cli, "run", "grok-floor", "probe")

    assert result.exit_code == vocab.EXIT_USAGE
    envelope = error_envelope(result)
    assert envelope["kind"] == "permission_denied"
    assert (
        "no mode on grok-floor grants at most permissions read; the lowest policy "
        "grok-floor runs under is execute, above the inherited ceiling read "
        "(ACPC_CEILING); dispatch it from a caller whose ceiling allows execute"
    ) in envelope["message"]
    assert "pass --permissions" not in envelope["message"]
    assert envelope["hint"] == (
        "The ceiling comes from the calling acpc session; --permissions cannot raise it."
    )


@pytest.mark.parametrize("ceiling", ["execute", "all"])
def test_a_ceiling_at_or_above_the_floor_keeps_the_permissions_remedy(
    cli: CliRunner, grok_floor_entry: None, monkeypatch: pytest.MonkeyPatch, ceiling: str
) -> None:
    monkeypatch.setenv("ACPC_CEILING", ceiling)

    envelope = error_envelope(invoke(cli, "run", "grok-floor", "probe"))

    assert "pass --permissions execute" in envelope["message"]
    assert "ACPC_CEILING" not in envelope["message"]
    assert envelope["hint"] == "Run: acpc run grok-floor --permissions execute"


def test_mode_refusal_uses_the_selection_error_mode_table_for_floor_and_list(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected_modes = {
        "snapshot-execute": ModeSpec(grants="execute", delegates=False),
        "snapshot-all": ModeSpec(grants="all", delegates=False),
    }

    def reject_from_selection_table(modes, policy, explicit_mode=None):
        raise ModeSelectionError(
            "test table mismatch",
            policy=policy,
            modes=selected_modes,
            explicit_mode=explicit_mode,
        )

    monkeypatch.setattr(cli_module, "select_mode", reject_from_selection_table)

    result = invoke(cli, "run", "mock", "probe")

    assert result.exit_code == vocab.EXIT_USAGE
    message = error_envelope(result)["message"]
    assert "lowest policy mock runs under is execute" in message
    assert "declared modes: snapshot-execute (grants execute), snapshot-all (grants all)" in message
    assert "declared modes: default (grants read)" not in message


def test_an_execute_floor_resolve_refusal_names_the_permission_floor(
    cli: CliRunner, grok_floor_entry: None
) -> None:
    result = invoke(cli, "resolve", "grok-floor")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "the lowest policy grok-floor runs under is execute" in error_envelope(result)["message"]


def test_an_execute_floor_ask_refusal_names_the_permission_floor(
    cli: CliRunner, grok_floor_entry: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(interaction, "stdin_is_tty", lambda: True)
    monkeypatch.setattr(interaction, "stdout_is_tty", lambda: True)

    result = invoke(cli, "run", "grok-floor", "probe", "--permissions", "ask")

    assert result.exit_code == vocab.EXIT_USAGE
    # The terminal is stdout's; stderr is still a pipe, so the envelope goes out.
    message = error_envelope(result)["message"]
    assert "--permissions ask on grok-floor is not really asking anything" in message
    assert "the lowest policy grok-floor runs under is execute" in message


def test_real_grok_refusal_names_the_permission_floor(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The floor refusal is only reachable past the install check, which is a
    # `shutil.which` on the entry's command; a stand-in on PATH keeps the test
    # independent of whether the host has the vendor CLI (the CI runners don't).
    stand_in = tmp_path / "grok"
    stand_in.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stand_in.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    result = invoke(cli, "run", "grok", "probe")

    assert result.exit_code == vocab.EXIT_USAGE, result.output
    message = error_envelope(result)["message"]
    assert "the lowest policy grok runs under is execute" in message
    assert (
        "declared modes: default (grants execute), acceptEdits (grants execute), "
        "plan (grants execute), auto (grants all), dontAsk (grants execute), "
        "bypassPermissions (grants all)"
    ) in message


def test_empty_modes_refusal_keeps_the_existing_message(cli: CliRunner, state_root: Path) -> None:
    (state_root / "agents" / "empty.toml").write_text(
        'name = "Empty Modes"\ncommand = "true"\n\n[modes]\n', encoding="utf-8"
    )

    result = invoke(cli, "run", "empty", "probe")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "declared modes: none" in error_envelope(result)["message"]


def test_an_edit_floor_refusal_names_the_permission_floor(cli: CliRunner, state_root: Path) -> None:
    (state_root / "agents" / "edit-floor.toml").write_text(
        'command = "true"\n\n[modes]\n'
        'bypass = { grants = "all", delegates = false }\n'
        'default = { grants = "edit", delegates = false }\n',
        encoding="utf-8",
    )

    result = invoke(cli, "run", "edit-floor", "probe")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "the lowest policy edit-floor runs under is edit" in error_envelope(result)["message"]


def test_unlisted_model_warns_on_resolve(cli: CliRunner) -> None:
    result = invoke(
        cli,
        "resolve",
        "grok",
        "--model",
        "grok-4.7-build-fast",
        "--effort",
        "xhigh",
        "--permissions",
        "execute",
    )

    assert result.exit_code == vocab.EXIT_OK
    assert "grok-4.7-build-fast has no [effort_by_model] row" in result.stderr
    assert "using adapter efforts low, medium, high, xhigh" in result.stderr


def test_unlisted_model_warns_on_run(cli: CliRunner, state_root: Path) -> None:
    (state_root / "agents" / "mock.toml").write_text(
        MOCK_ENTRY + '\n[effort_by_model]\n"mock-sonnet-5" = ["low", "medium", "high"]\n',
        encoding="utf-8",
    )

    result = invoke(
        cli,
        "run",
        "mock",
        "echo:hi",
        "--model",
        "mock-opus-5",
        "--effort",
        "high",
        "--quiet",
    )

    assert result.exit_code == vocab.EXIT_OK
    assert "mock-opus-5 has no [effort_by_model] row" in result.stderr
    assert "using adapter efforts low, medium, high" in result.stderr


def test_listed_model_does_not_warn_on_resolve(cli: CliRunner) -> None:
    result = invoke(
        cli,
        "resolve",
        "grok",
        "--model",
        "grok-4.6",
        "--effort",
        "high",
        "--permissions",
        "execute",
    )

    assert result.exit_code == vocab.EXIT_OK
    assert "has no [effort_by_model] row" not in result.stderr


# --- system prompt and tool list (judge entries) ------------------------------

JUDGE_FACTS = 'system_prompt_via = "acp_meta"\ntools_via = "acp_meta"\n'


def write_judge_entry(state_root: Path, extra: str = "", *, name: str = "judge") -> None:
    """A mock-adapter entry whose adapter declares the acp_meta routes."""
    text = MOCK_ENTRY.replace("\n[modes]", f"\n{JUDGE_FACTS}{extra}\n[modes]", 1)
    (state_root / "agents" / f"{name}.toml").write_text(text, encoding="utf-8")


def session_new_meta(wire_file: Path) -> list[dict]:
    return [
        event["params"]["meta"]
        for event in wire_events(wire_file)
        if event["method"] == "session/new"
    ]


def sessions_on_disk() -> list[str]:
    root = paths.sessions_dir()
    return sorted(item.name for item in root.iterdir()) if root.exists() else []


def test_run_sends_the_entry_prompt_and_tool_list_as_session_new_meta(
    cli: CliRunner, state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire_file = state_root / "wire.jsonl"
    monkeypatch.setenv("ACPC_MOCK_WIRE_FILE", str(wire_file))
    (state_root / "agents" / "judge-prompt.md").write_text("You are a judge.\n", encoding="utf-8")
    write_judge_entry(
        state_root, 'system_prompt_file = "judge-prompt.md"\ntools = ["Read", "Grep"]\n'
    )

    result = invoke(cli, "run", "judge", "case one", "--quiet", "--json")

    assert result.exit_code == vocab.EXIT_OK, result.stderr
    assert session_new_meta(wire_file) == [
        {
            "systemPrompt": "You are a judge.\n",
            "claudeCode": {"options": {"tools": ["Read", "Grep"]}},
        }
    ]
    advertised = json.loads(result.stdout)["paths"]
    assert Path(advertised["system"]).read_text(encoding="utf-8") == "You are a judge.\n"
    assert Path(advertised["system"]).name == "system.md"


def test_an_empty_tool_list_arrives_as_an_empty_list_and_no_prompt_adds_no_system_path(
    cli: CliRunner, state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire_file = state_root / "wire.jsonl"
    monkeypatch.setenv("ACPC_MOCK_WIRE_FILE", str(wire_file))
    write_judge_entry(state_root, "tools = []\n")

    result = invoke(cli, "run", "judge", "case", "--quiet", "--json")

    assert result.exit_code == vocab.EXIT_OK, result.stderr
    assert session_new_meta(wire_file) == [{"claudeCode": {"options": {"tools": []}}}]
    assert "system" not in json.loads(result.stdout)["paths"]


def test_a_session_without_prompt_or_tools_sends_no_meta(
    cli: CliRunner, state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire_file = state_root / "wire.jsonl"
    monkeypatch.setenv("ACPC_MOCK_WIRE_FILE", str(wire_file))
    write_judge_entry(state_root)

    result = invoke(cli, "run", "judge", "case", "--quiet", "--json")

    assert result.exit_code == vocab.EXIT_OK, result.stderr
    assert session_new_meta(wire_file) == [{}]
    assert "system" not in json.loads(result.stdout)["paths"]
    session_dir = paths.session_dir(json.loads(result.stdout)["session_id"])
    assert not (session_dir / "system.md").exists()


def test_the_call_flag_wins_over_the_entry_key_and_is_recorded_as_call(
    cli: CliRunner, state_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wire_file = state_root / "wire.jsonl"
    monkeypatch.setenv("ACPC_MOCK_WIRE_FILE", str(wire_file))
    (state_root / "agents" / "entry.md").write_text("entry prompt", encoding="utf-8")
    call_file = tmp_path / "call.md"
    call_file.write_text("call prompt", encoding="utf-8")
    write_judge_entry(state_root, 'system_prompt_file = "entry.md"\n')

    result = invoke(
        cli, "run", "judge", "case", "--system-prompt-file", str(call_file), "--quiet", "--json"
    )

    assert result.exit_code == vocab.EXIT_OK, result.stderr
    assert session_new_meta(wire_file) == [{"systemPrompt": "call prompt"}]
    document = json.loads(result.stdout)
    assert Path(document["paths"]["system"]).read_text(encoding="utf-8") == "call prompt"
    stored = sessions.read_meta(document["session_id"]).resolution["resolved"]["system_prompt"]
    assert stored == {"value": str(call_file.resolve()), "source": "call", "bytes": 11}


def test_a_relative_call_flag_resolves_against_the_callers_directory(
    cli: CliRunner, state_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wire_file = state_root / "wire.jsonl"
    monkeypatch.setenv("ACPC_MOCK_WIRE_FILE", str(wire_file))
    (tmp_path / "rel.md").write_text("relative prompt", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    write_judge_entry(state_root)

    result = invoke(cli, "run", "judge", "case", "--system-prompt-file", "rel.md", "--quiet")

    assert result.exit_code == vocab.EXIT_OK, result.stderr
    assert session_new_meta(wire_file) == [{"systemPrompt": "relative prompt"}]


def test_resolve_shows_the_prompt_tools_and_route_facts(cli: CliRunner, state_root: Path) -> None:
    (state_root / "agents" / "entry.md").write_text("héllo", encoding="utf-8")
    write_judge_entry(state_root, 'system_prompt_file = "entry.md"\ntools = []\n')

    result = invoke(cli, "resolve", "judge", "--json")

    assert result.exit_code == vocab.EXIT_OK, result.stderr
    payload = json.loads(result.stdout)
    assert (payload["system_prompt_via"], payload["tools_via"]) == ("acp_meta", "acp_meta")
    assert payload["resolved"]["system_prompt"] == {
        "value": str(state_root / "agents" / "entry.md"),
        "source": "entry",
        "bytes": 6,
    }
    assert payload["resolved"]["tools"]["value"] == []
    assert payload["resolved"]["tools"]["source"].startswith("entry")
    text = invoke(cli, "resolve", "judge")
    assert f"prompt file  {state_root / 'agents' / 'entry.md'} (6 bytes, entry)" in text.stdout
    assert "6 bytes" in text.stdout
    assert "tools        none" in text.stdout
    assert sessions_on_disk() == []


def test_resolve_omits_prompt_and_tools_when_unset(cli: CliRunner, state_root: Path) -> None:
    write_judge_entry(state_root)

    payload = json.loads(invoke(cli, "resolve", "judge", "--json").stdout)

    assert "system_prompt" not in payload["resolved"]
    assert "tools" not in payload["resolved"]


def test_agents_get_shows_the_prompt_file_and_tools(cli: CliRunner, state_root: Path) -> None:
    (state_root / "agents" / "entry.md").write_text("12345", encoding="utf-8")
    write_judge_entry(state_root, 'system_prompt_file = "entry.md"\ntools = ["Read"]\n')

    result = invoke(cli, "agents", "get", "judge", "--json")

    assert result.exit_code == vocab.EXIT_OK, result.stderr
    payload = json.loads(result.stdout)
    assert payload["system_prompt_file"] == str(state_root / "agents" / "entry.md")
    assert payload["tools"] == ["Read"]
    assert (payload["system_prompt_via"], payload["tools_via"]) == ("acp_meta", "acp_meta")
    text = invoke(cli, "agents", "get", "judge", "--format", "text").stdout
    assert f"prompt file  {state_root / 'agents' / 'entry.md'} · 5 bytes" in text
    assert "tools        Read" in text
    assert payload["refused"] is None
    assert "refused" not in text


def test_agents_get_shows_an_empty_tool_list_as_none(cli: CliRunner, state_root: Path) -> None:
    write_judge_entry(state_root, "tools = []\n")

    text = invoke(cli, "agents", "get", "judge", "--format", "text").stdout

    assert "tools        none" in text


def test_agents_get_json_reports_null_for_unset_keys(cli: CliRunner, state_root: Path) -> None:
    write_judge_entry(state_root)

    payload = json.loads(invoke(cli, "agents", "get", "judge", "--json").stdout)

    assert payload["system_prompt_file"] is None
    assert payload["tools"] is None


@pytest.mark.parametrize(
    ("entry_extra", "args", "key"),
    [
        ('system_prompt_file = "p.md"\n', (), "system_prompt_file"),
        ("tools = []\n", (), "tools"),
        ("", ("--system-prompt-file", "p.md"), "--system-prompt-file"),
    ],
)
def test_a_key_on_an_adapter_without_a_route_is_refused_before_any_session(
    cli: CliRunner,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    entry_extra: str,
    args: tuple[str, ...],
    key: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "p.md").write_text("prompt", encoding="utf-8")
    (state_root / "agents" / "p.md").write_text("prompt", encoding="utf-8")
    text = MOCK_ENTRY.replace("\n[modes]", f"\n{entry_extra}\n[modes]", 1)
    (state_root / "agents" / "bare.toml").write_text(text, encoding="utf-8")

    result = invoke(cli, "run", "bare", "case", *args, "--name", "claimed", "--quiet")

    assert result.exit_code == vocab.EXIT_USAGE
    error = error_envelope(result)
    assert error["kind"] == "not_supported"
    assert "bare" in error["message"]
    assert key in error["message"]
    assert error["hint"]
    assert sessions_on_disk() == []
    if not args:  # resolve has no --system-prompt-file; only the entry keys apply to it
        assert error_envelope(invoke(cli, "resolve", "bare"))["kind"] == "not_supported"


def write_unroutable_entry(state_root: Path) -> None:
    """An entry that sets `tools` on an adapter with no tool route."""
    text = MOCK_ENTRY.replace("\n[modes]", "\ntools = []\n\n[modes]", 1)
    (state_root / "agents" / "bare.toml").write_text(text, encoding="utf-8")


def test_agents_get_shows_an_entry_whose_keys_have_no_route_and_says_why(
    cli: CliRunner, state_root: Path
) -> None:
    write_unroutable_entry(state_root)

    as_json = invoke(cli, "agents", "get", "bare", "--json")
    as_text = invoke(cli, "agents", "get", "bare", "--format", "text")

    assert as_json.exit_code == vocab.EXIT_OK, as_json.stderr
    assert as_text.exit_code == vocab.EXIT_OK, as_text.stderr
    payload = json.loads(as_json.stdout)
    assert payload["tools"] == []
    assert payload["tools_via"] == "none"
    assert payload["refused"] == (
        "entry bare sets tools, but adapter bare has no tool list route (tools_via = none)"
    )
    assert "tools        none" in as_text.stdout
    assert "refused      entry bare sets tools" in as_text.stdout


def test_agents_get_models_and_commands_work_on_an_entry_whose_keys_have_no_route(
    cli: CliRunner, state_root: Path
) -> None:
    write_unroutable_entry(state_root)

    models = invoke(cli, "agents", "get", "bare", "--models", "--json")
    commands = invoke(cli, "agents", "get", "bare", "--commands", "--json")

    assert models.exit_code == vocab.EXIT_OK, models.stderr
    assert commands.exit_code == vocab.EXIT_OK, commands.stderr
    assert json.loads(models.stdout)["agent"] == "bare"


def test_agents_check_reports_an_entry_whose_keys_have_no_route_as_one_more_row(
    cli: CliRunner, state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def advertised(_resolution: object) -> dict[str, list[object]]:
        return {"models": []}

    monkeypatch.setattr(cache, "probe_advertised", advertised)
    write_unroutable_entry(state_root)

    sweep = invoke(cli, "agents", "check", "--json")
    single = invoke(cli, "agents", "check", "bare", "--json")

    assert sweep.exit_code == vocab.EXIT_OK, sweep.stderr
    rows = {item["agent"]: item for item in json.loads(sweep.stdout)["items"]}
    assert {"bare", "mock", "claude"} <= set(rows)
    assert all(row["ok"] for row in rows.values())
    assert single.exit_code == vocab.EXIT_OK, single.stderr
    assert [item["agent"] for item in json.loads(single.stdout)["items"]] == ["bare"]


def test_agents_create_extends_an_entry_whose_keys_have_no_route(
    cli: CliRunner, state_root: Path
) -> None:
    write_unroutable_entry(state_root)

    result = invoke(cli, "agents", "create", "variant", "--extends", "bare", "--effort", "high")

    assert result.exit_code == vocab.EXIT_OK, result.stderr
    assert (state_root / "agents" / "variant.toml").exists()


def test_probe_connects_to_an_entry_whose_keys_have_no_route(
    cli: CliRunner, state_root: Path
) -> None:
    write_unroutable_entry(state_root)

    result = invoke(cli, "probe", "bare", "--discover", "--json")

    assert result.exit_code == vocab.EXIT_OK, result.stderr
    assert json.loads(result.stdout)["discover_only"] is True


def test_the_hint_names_the_flag_for_a_flag_refusal_and_the_key_for_an_entry_refusal(
    cli: CliRunner, state_root: Path, tmp_path: Path
) -> None:
    (tmp_path / "p.md").write_text("prompt", encoding="utf-8")
    (state_root / "agents" / "bare.toml").write_text(MOCK_ENTRY, encoding="utf-8")
    (state_root / "agents" / "keyed.toml").write_text(
        MOCK_ENTRY.replace("\n[modes]", "\ntools = []\n\n[modes]", 1), encoding="utf-8"
    )

    flag = invoke(cli, "run", "bare", "case", "--system-prompt-file", str(tmp_path / "p.md"))
    key = invoke(cli, "run", "keyed", "case")

    assert error_envelope(flag)["hint"].startswith("Drop the flag, or ")
    assert error_envelope(key)["hint"].startswith("Drop the key, or ")


def test_the_call_flag_on_a_codex_config_adapter_names_the_entry_key_as_the_route(
    cli: CliRunner, state_root: Path, tmp_path: Path
) -> None:
    (tmp_path / "p.md").write_text("prompt", encoding="utf-8")
    text = MOCK_ENTRY.replace("\n[modes]", '\nsystem_prompt_via = "codex_config"\n\n[modes]', 1)
    (state_root / "agents" / "cx.toml").write_text(text, encoding="utf-8")

    result = invoke(
        cli, "run", "cx", "case", "--system-prompt-file", str(tmp_path / "p.md"), "--quiet"
    )

    assert result.exit_code == vocab.EXIT_USAGE
    error = error_envelope(result)
    assert error["kind"] == "not_supported"
    assert "system_prompt_file on the entry" in error["message"]
    assert sessions_on_disk() == []


def test_a_missing_system_prompt_file_is_a_usage_error_naming_the_flag(
    cli: CliRunner, state_root: Path, tmp_path: Path
) -> None:
    write_judge_entry(state_root)

    result = invoke(
        cli, "run", "judge", "case", "--system-prompt-file", str(tmp_path / "nope.md"), "--quiet"
    )

    assert result.exit_code == vocab.EXIT_USAGE
    error = error_envelope(result)
    assert error["kind"] == "invalid_input"
    assert "--system-prompt-file" in error["message"]
    assert sessions_on_disk() == []


def test_a_missing_entry_prompt_file_names_the_entry_key(cli: CliRunner, state_root: Path) -> None:
    write_judge_entry(state_root, 'system_prompt_file = "gone.md"\n')

    result = invoke(cli, "run", "judge", "case", "--quiet")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "system_prompt_file" in error_envelope(result)["message"]
    assert sessions_on_disk() == []


def test_an_unreadable_system_prompt_file_is_permission_denied(
    cli: CliRunner, state_root: Path, tmp_path: Path
) -> None:
    write_judge_entry(state_root)
    locked = tmp_path / "locked.md"
    locked.write_text("secret", encoding="utf-8")
    locked.chmod(0)
    try:
        if os.access(locked, os.R_OK):
            pytest.skip("permission bits are not enforced for this user")
        result = invoke(cli, "run", "judge", "case", "--system-prompt-file", str(locked), "--quiet")
    finally:
        locked.chmod(0o600)

    assert error_envelope(result)["kind"] == "permission_denied"
    assert sessions_on_disk() == []


def test_an_oversized_or_empty_system_prompt_file_is_refused(
    cli: CliRunner, state_root: Path, tmp_path: Path
) -> None:
    write_judge_entry(state_root)
    big = tmp_path / "big.md"
    big.write_text("x" * (vocab.MAX_PROMPT_BYTES + 1), encoding="utf-8")
    empty = tmp_path / "empty.md"
    empty.write_text("  \n", encoding="utf-8")

    oversized = invoke(cli, "run", "judge", "case", "--system-prompt-file", str(big), "--quiet")
    blank = invoke(cli, "run", "judge", "case", "--system-prompt-file", str(empty), "--quiet")

    assert oversized.exit_code == vocab.EXIT_USAGE
    assert "--system-prompt-file" in error_envelope(oversized)["message"]
    assert blank.exit_code == vocab.EXIT_USAGE
    assert "empty" in error_envelope(blank)["message"]
    assert sessions_on_disk() == []


def test_the_system_prompt_file_survives_delete_listing_and_status(
    cli: CliRunner, state_root: Path, tmp_path: Path
) -> None:
    write_judge_entry(state_root)
    prompt = tmp_path / "p.md"
    prompt.write_text("keep me", encoding="utf-8")
    run = invoke(
        cli, "run", "judge", "case", "--system-prompt-file", str(prompt), "--quiet", "--json"
    )
    session_id = json.loads(run.stdout)["session_id"]

    status = json.loads(invoke(cli, "status", session_id, "--json").stdout)

    assert status["paths"]["system"].endswith("/system.md")
    deleted = json.loads(invoke(cli, "delete", session_id, "--yes", "--json").stdout)
    assert deleted["paths"]["system"] == status["paths"]["system"]


# --- one_shot (judge entries consumed once) -----------------------------------

STERILITY = {"persistSession": False, "settingSources": [], "strictMcpConfig": True}


def session_files(session_id: str) -> list[str]:
    return sorted(item.name for item in paths.session_dir(session_id).iterdir())


def dir_snapshot(session_id: str) -> dict[str, bytes]:
    directory = paths.session_dir(session_id)
    return {item.name: item.read_bytes() for item in sorted(directory.iterdir())}


def run_one_shot(cli: CliRunner, state_root: Path, prompt: str = "echo:one shot answer") -> dict:
    write_judge_entry(state_root, "one_shot = true\n")
    result = invoke(cli, "run", "judge", prompt, "--quiet", "--json")
    assert result.exit_code == vocab.EXIT_OK, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ("one_shot = true\n", {"claudeCode": {"options": STERILITY}}),
        (
            "one_shot = true\ntools = []\n",
            {"claudeCode": {"options": {"tools": [], **STERILITY}}},
        ),
        (
            'one_shot = true\ntools = ["Read"]\n',
            {"claudeCode": {"options": {"tools": ["Read"], **STERILITY}}},
        ),
    ],
)
def test_a_one_shot_session_sends_the_sterility_options_with_or_without_tools(
    cli: CliRunner,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra: str,
    expected: dict,
) -> None:
    wire_file = state_root / "wire.jsonl"
    monkeypatch.setenv("ACPC_MOCK_WIRE_FILE", str(wire_file))
    write_judge_entry(state_root, extra)

    result = invoke(cli, "run", "judge", "case", "--quiet", "--json")

    assert result.exit_code == vocab.EXIT_OK, result.stderr
    assert session_new_meta(wire_file) == [expected]


def test_a_one_shot_entry_on_an_adapter_without_a_tool_route_sends_nothing_extra(
    cli: CliRunner, state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire_file = state_root / "wire.jsonl"
    monkeypatch.setenv("ACPC_MOCK_WIRE_FILE", str(wire_file))
    text = MOCK_ENTRY.replace("\n[modes]", "\none_shot = true\n\n[modes]", 1)
    (state_root / "agents" / "plain-once.toml").write_text(text, encoding="utf-8")

    result = invoke(cli, "run", "plain-once", "echo:still reduced", "--quiet", "--json")

    assert result.exit_code == vocab.EXIT_OK, result.stderr
    assert session_new_meta(wire_file) == [{}]
    # The reduction is acpc's own part and needs no adapter route.
    document = json.loads(result.stdout)
    assert document["retained"] is False
    assert session_files(document["session_id"]) == ["answer.md", "lock", "meta.json"]


def test_a_succeeded_one_shot_session_keeps_only_lock_meta_and_answer(
    cli: CliRunner, state_root: Path
) -> None:
    document = run_one_shot(cli, state_root)

    session_id = document["session_id"]
    assert session_files(session_id) == ["answer.md", "lock", "meta.json"]
    assert document["retained"] is False
    assert document["answer"] == "one shot answer"
    assert paths.session_dir(session_id).joinpath("answer.md").read_text() == "one shot answer"
    assert (
        json.loads(paths.session_dir(session_id).joinpath("meta.json").read_text())["retained"]
        is False
    )


def test_status_list_and_wait_serve_a_reduced_session(cli: CliRunner, state_root: Path) -> None:
    document = run_one_shot(cli, state_root)
    session_id = document["session_id"]

    status = json.loads(invoke(cli, "status", session_id, "--json").stdout)
    listed = json.loads(invoke(cli, "list", "--json").stdout)["items"]
    text = invoke(cli, "status", session_id, "--format", "text").stdout
    waited = invoke(cli, "wait", session_id, "--json")
    waited_again = invoke(cli, "wait", session_id, "--json")

    assert status["retained"] is False
    assert status["status"] == "succeeded"
    # `paths` still names the files that were removed.
    assert status["paths"]["prompt"].endswith("/prompt.md")
    assert status["paths"]["transcript"].endswith("/transcript.ndjson")
    assert [item["retained"] for item in listed if item["session_id"] == session_id] == [False]
    assert "retained no" in text
    for result in (waited, waited_again):
        assert result.exit_code == vocab.EXIT_OK, result.stderr
        delivered = json.loads(result.stdout)
        assert delivered["answer"] == "one shot answer"
        assert delivered["status"] == "succeeded"
        assert delivered["retained"] is False


def test_a_plain_session_stays_retained_in_every_document(cli: CliRunner, state_root: Path) -> None:
    write_judge_entry(state_root)
    result = invoke(cli, "run", "judge", "echo:plain", "--quiet", "--json")
    session_id = json.loads(result.stdout)["session_id"]

    status = json.loads(invoke(cli, "status", session_id, "--json").stdout)
    listed = json.loads(invoke(cli, "list", "--json").stdout)["items"]

    assert json.loads(result.stdout)["retained"] is True
    assert status["retained"] is True
    assert [item["retained"] for item in listed] == [True]
    assert "retained" not in invoke(cli, "status", session_id, "--format", "text").stdout
    assert "prompt.md" in session_files(session_id)
    assert (
        json.loads(paths.session_dir(session_id).joinpath("meta.json").read_text())["retained"]
        is True
    )


def test_a_failed_one_shot_turn_keeps_the_whole_session(cli: CliRunner, state_root: Path) -> None:
    write_judge_entry(state_root, "one_shot = true\n")

    result = invoke(cli, "run", "judge", "auth:vendor rejected the request", "--json", "--quiet")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    document = json.loads(result.stdout)
    session_id = document["session_id"]
    assert document["status"] == "failed"
    assert document["retained"] is True
    files = session_files(session_id)
    for name in ("answer.md", "lock", "meta.json", "prompt.md", "transcript.ndjson"):
        assert name in files
    assert json.loads(invoke(cli, "status", session_id, "--json").stdout)["retained"] is True


def test_continue_and_log_refuse_a_reduced_session_and_change_nothing(
    cli: CliRunner, state_root: Path
) -> None:
    session_id = run_one_shot(cli, state_root)["session_id"]
    before = dir_snapshot(session_id)

    attempts = [
        ("continue", session_id, "more", "--quiet"),
        ("continue", session_id, "--quiet"),
        ("log", session_id),
        ("log", session_id, "--wait-new", "--timeout", "1"),
        ("log", session_id, "--follow", "--timeout", "1"),
    ]
    for args in attempts:
        result = invoke(cli, *args)
        assert result.exit_code == vocab.EXIT_AGENT_ERROR, (args, result.stderr)
        envelope = error_envelope(result)
        assert envelope["kind"] == "conflict", args
        assert "one_shot" in envelope["message"], args
        assert session_id in envelope["message"], args
        assert envelope["context"]["session_id"] == session_id, args

    assert dir_snapshot(session_id) == before


def test_continue_on_a_reduced_session_hints_at_a_new_run(cli: CliRunner, state_root: Path) -> None:
    session_id = run_one_shot(cli, state_root)["session_id"]

    envelope = error_envelope(invoke(cli, "continue", session_id, "more", "--quiet"))

    assert envelope["hint"] == "Start a new session: acpc run judge ..."


def test_steer_on_a_reduced_session_gives_the_finished_session_conflict(
    cli: CliRunner, state_root: Path
) -> None:
    session_id = run_one_shot(cli, state_root)["session_id"]
    before = dir_snapshot(session_id)

    result = invoke(cli, "steer", session_id, "stop", "--quiet")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    envelope = error_envelope(result)
    assert envelope["kind"] == "conflict"
    assert "there is no turn to interrupt" in envelope["message"]
    assert dir_snapshot(session_id) == before


def test_delete_and_prune_work_on_a_reduced_session(cli: CliRunner, state_root: Path) -> None:
    first = run_one_shot(cli, state_root)["session_id"]
    second = json.loads(invoke(cli, "run", "judge", "echo:second", "--quiet", "--json").stdout)[
        "session_id"
    ]

    deleted = invoke(cli, "delete", first, "--yes", "--json")
    pruned = invoke(cli, "prune", "--older-than", "0d", "--yes", "--json")

    assert deleted.exit_code == vocab.EXIT_OK, deleted.stderr
    assert json.loads(deleted.stdout)["removed"] is True
    assert session_files(first) == [".tombstone", "lock"]
    assert pruned.exit_code == vocab.EXIT_OK, pruned.stderr
    assert json.loads(pruned.stdout)["targets"] == [second]
    assert session_files(second) == [".tombstone", "lock"]
    assert invoke(cli, "status", second).exit_code == vocab.EXIT_AGENT_ERROR


def test_a_later_run_prunes_a_reduced_session_without_choking(
    cli: CliRunner, state_root: Path
) -> None:
    session_id = run_one_shot(cli, state_root)["session_id"]
    meta_file = paths.session_dir(session_id) / "meta.json"
    data = json.loads(meta_file.read_text())
    data["finished_at"] = "2020-01-01T00:00:00Z"
    meta_file.write_text(json.dumps(data), encoding="utf-8")

    other = invoke(cli, "run", "judge", "echo:next", "--quiet", "--json")

    assert other.exit_code == vocab.EXIT_OK, other.stderr
    assert session_files(session_id) == [".tombstone", "lock"]


def test_resolve_and_agents_get_show_one_shot(cli: CliRunner, state_root: Path) -> None:
    write_judge_entry(state_root, "one_shot = true\n")
    write_judge_entry(state_root, name="judge-plain")

    resolved = json.loads(invoke(cli, "resolve", "judge", "--json").stdout)
    resolved_plain = json.loads(invoke(cli, "resolve", "judge-plain", "--json").stdout)
    text = invoke(cli, "resolve", "judge").stdout
    text_plain = invoke(cli, "resolve", "judge-plain").stdout
    got = json.loads(invoke(cli, "agents", "get", "judge", "--json").stdout)
    got_plain = json.loads(invoke(cli, "agents", "get", "judge-plain", "--json").stdout)
    got_text = invoke(cli, "agents", "get", "judge", "--format", "text").stdout
    got_text_plain = invoke(cli, "agents", "get", "judge-plain", "--format", "text").stdout

    assert resolved["resolved"]["one_shot"]["value"] is True
    assert resolved["resolved"]["one_shot"]["source"].startswith("entry")
    assert resolved_plain["resolved"]["one_shot"] == {"value": False, "source": "default"}
    assert re.search(r"^one-shot +yes\b", text, re.MULTILINE)
    assert "one-shot" not in text_plain
    assert got["one_shot"] is True
    assert got_plain["one_shot"] is False
    assert re.search(r"^one-shot +yes\b", got_text, re.MULTILINE)
    assert "one-shot" not in got_text_plain


def test_the_session_snapshot_records_one_shot_with_its_source(
    cli: CliRunner, state_root: Path
) -> None:
    session_id = run_one_shot(cli, state_root)["session_id"]

    stored = sessions.read_meta(session_id).resolution["resolved"]["one_shot"]

    assert stored["value"] is True
    assert stored["source"].startswith("entry")


def test_a_timeout_worker_reduces_the_session_on_its_own(cli: CliRunner, state_root: Path) -> None:
    """`--timeout` hands the turn to a detached worker; it finalizes, so it reduces.

    The client gives up after 0.1 s while the turn needs a second, so only the
    worker, never the client, can be the process that reduces the session.
    """
    write_judge_entry(state_root, "one_shot = true\n")

    result = invoke(cli, "run", "judge", "slow:1 detached", "--timeout", "0.1", "--quiet")

    assert result.exit_code == vocab.EXIT_TIMEOUT
    session_id = error_envelope(result)["context"]["session_id"]
    deadline = time.monotonic() + 15
    while sessions.read_meta(session_id).retained and time.monotonic() < deadline:
        time.sleep(0.05)
    assert sessions.read_meta(session_id).retained is False
    assert sessions.read_meta(session_id).state == "succeeded"
    assert session_files(session_id) == ["answer.md", "lock", "meta.json"]
    waited = json.loads(invoke(cli, "wait", session_id, "--json").stdout)
    assert waited["answer"] != ""
    assert waited["retained"] is False


def test_a_reduced_session_result_carries_no_next(cli: CliRunner, state_root: Path) -> None:
    document = run_one_shot(cli, state_root)
    waited = json.loads(invoke(cli, "wait", document["session_id"], "--json").stdout)
    write_judge_entry(state_root, name="judge-plain")
    plain = json.loads(invoke(cli, "run", "judge-plain", "echo:x", "--quiet", "--json").stdout)

    assert "next" not in document
    assert "next" not in waited
    assert plain["next"] == ["acpc", "continue", plain["session_id"]]


def test_the_summary_of_a_reduced_session_has_no_next_segment(
    cli: CliRunner, state_root: Path
) -> None:
    write_judge_entry(state_root, "one_shot = true\n")
    write_judge_entry(state_root, name="judge-plain")

    reduced = invoke(cli, "run", "judge", "echo:once")
    plain = invoke(cli, "run", "judge-plain", "echo:again")

    def summary(result) -> str:
        return next(line for line in result.stderr.splitlines() if "exit 0" in line)

    assert reduced.exit_code == vocab.EXIT_OK, reduced.stderr
    assert "Next:" not in summary(reduced)
    assert "Next: acpc continue" in summary(plain)


def test_a_session_recorded_before_one_shot_existed_is_never_reduced(
    cli: CliRunner, state_root: Path
) -> None:
    write_judge_entry(state_root)
    session_id = json.loads(invoke(cli, "run", "judge", "echo:a", "--quiet", "--json").stdout)[
        "session_id"
    ]
    meta_file = paths.session_dir(session_id) / "meta.json"
    data = json.loads(meta_file.read_text())
    del data["resolution"]["resolved"]["one_shot"]
    meta_file.write_text(json.dumps(data), encoding="utf-8")

    result = invoke(cli, "continue", session_id, "echo:b", "--quiet", "--json")

    assert json.loads(result.stdout)["retained"] is True
    assert "prompt.1.md" in session_files(session_id)


def test_log_follow_across_the_reduction_ends_with_conflict_not_a_crash(
    cli: CliRunner, state_root: Path
) -> None:
    """The detached worker reduces the session while `log --follow` is reading it."""
    write_judge_entry(state_root, "one_shot = true\n")
    started = invoke(cli, "run", "judge", "chunkslow:2", "--timeout", "0.3", "--quiet")
    assert started.exit_code == vocab.EXIT_TIMEOUT
    session_id = error_envelope(started)["context"]["session_id"]

    followed = invoke(cli, "log", session_id, "--follow", "--timeout", "20")

    assert followed.exit_code == vocab.EXIT_AGENT_ERROR, followed.stderr
    assert "Traceback" not in followed.stderr
    envelope = error_envelope(followed)
    assert envelope["kind"] == "conflict"
    assert "one_shot" in envelope["message"]
    assert envelope["context"]["session_id"] == session_id


def test_a_reduced_judge_run_still_names_its_system_prompt_path(
    cli: CliRunner, state_root: Path
) -> None:
    write_judge_entry(state_root, "one_shot = true\n")
    prompt_file = state_root / "judge-prompt.md"
    prompt_file.write_text("You are a judge.\n", encoding="utf-8")

    result = invoke(
        cli,
        "run",
        "judge",
        "echo:verdict",
        "--system-prompt-file",
        str(prompt_file),
        "--quiet",
        "--json",
    )
    assert result.exit_code == vocab.EXIT_OK, result.stderr
    run_document = json.loads(result.stdout)
    session_id = run_document["session_id"]
    status = json.loads(invoke(cli, "status", session_id, "--json").stdout)
    waited = json.loads(invoke(cli, "wait", session_id, "--json").stdout)

    assert "system.md" not in session_files(session_id)
    expected = str(paths.session_dir(session_id) / "system.md")
    for document in (run_document, status, waited):
        assert document["paths"]["system"] == expected
    # A reduced session that never had a system prompt names none.
    plain = run_one_shot(cli, state_root)
    assert "system" not in plain["paths"]


def test_no_reader_sees_a_succeeded_one_shot_session_that_is_still_whole(
    cli: CliRunner, state_root: Path
) -> None:
    write_judge_entry(state_root, "one_shot = true\n")
    seen: list[tuple[str, bool]] = []
    stop = threading.Event()

    def poll() -> None:
        while not stop.is_set():
            root = paths.sessions_dir()
            for meta_file in root.glob("*/meta.json") if root.exists() else ():
                try:
                    data = json.loads(meta_file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                seen.append((data["status"], data.get("retained", True)))

    reader = threading.Thread(target=poll)
    reader.start()
    try:
        for _ in range(8):
            result = invoke(cli, "run", "judge", "echo:poll", "--quiet", "--json")
            assert result.exit_code == vocab.EXIT_OK, result.stderr
    finally:
        stop.set()
        reader.join()

    succeeded = [retained for state, retained in seen if state == "succeeded"]
    assert succeeded, "the reader never saw a finished session"
    assert not any(succeeded)
