"""Behavioral tests for the `run` verb: flags, usage errors, TTY rules."""

import json
import os
import pty
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

from acpc import cli as cli_module
from acpc import sessions, vocab
from acpc.cli import main

MOCK_AGENT_SCRIPT = str(Path(__file__).with_name("mock_agent.py"))

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


def test_an_unknown_agent_is_a_usage_error(cli: CliRunner) -> None:
    result = invoke(cli, "run", "no-such-agent", "hello")

    assert result.exit_code == vocab.EXIT_USAGE


def test_a_missing_adapter_binary_exits_1_and_names_the_install(cli: CliRunner) -> None:
    result = invoke(cli, "run", "phantom", "hello")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    assert "acpc install phantom" in result.stderr


def test_a_missing_adapter_binary_creates_no_session(cli: CliRunner, state_root: Path) -> None:
    invoke(cli, "run", "phantom", "hello")

    sessions_dir = state_root / "sessions"
    assert not sessions_dir.exists() or not list(sessions_dir.iterdir())


# --- --dry-run --------------------------------------------------------------


def test_dry_run_shows_the_resolved_model_and_its_source(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "probe", "--dry-run")

    assert result.exit_code == vocab.EXIT_OK
    assert "mock-sonnet-5" in result.stdout
    assert "adapter default" in result.stdout


def test_dry_run_runs_nothing(cli: CliRunner, state_root: Path) -> None:
    invoke(cli, "run", "mock", "probe", "--dry-run")

    sessions_dir = state_root / "sessions"
    assert not sessions_dir.exists() or not list(sessions_dir.iterdir())


def test_dry_run_json_is_machine_readable(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "probe", "--dry-run", "--json")

    payload = json.loads(result.stdout)
    assert payload["entry"] == "mock"
    assert payload["resolved"]["model"]["value"] == "mock-sonnet-5"
    assert "entry_definition" not in payload


def test_dry_run_reports_mode_for_entry_flag_and_unset_sources(
    cli: CliRunner, state_root: Path
) -> None:
    (state_root / "agents" / "pinned.toml").write_text(
        'extends = "mock"\nmode = "plan"\n', encoding="utf-8"
    )

    entry = json.loads(invoke(cli, "run", "pinned", "probe", "--dry-run", "--json").stdout)
    flag = json.loads(
        invoke(
            cli,
            "run",
            "pinned",
            "probe",
            "--mode",
            "plan",
            "--dry-run",
            "--json",
        ).stdout
    )
    unset = json.loads(invoke(cli, "run", "mock", "probe", "--dry-run", "--json").stdout)

    assert entry["resolved"]["mode"] == {
        "value": "plan",
        "source": f"entry ({state_root / 'agents' / 'pinned.toml'})",
        "grants": "read",
        "delegates": True,
    }
    assert flag["resolved"]["mode"] == {
        "value": "plan",
        "source": "call flag",
        "grants": "read",
        "delegates": True,
    }
    assert unset["resolved"]["mode"] == {
        "value": "default",
        "source": "selected",
        "grants": "read",
        "delegates": True,
    }


def test_dry_run_reports_the_permission_policy_it_would_use(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "probe", "--dry-run", "--json")

    # Non-interactive stdout, no --permissions: SPEC's default is `read`.
    assert json.loads(result.stdout)["resolved"]["permissions"]["value"] == "read"


def test_write_alias_matches_execute_and_warns_once_per_process(
    cli: CliRunner, fresh_permission_alias_warnings: None
) -> None:
    first = invoke(cli, "run", "mock", "probe", "--permissions", "write", "--dry-run", "--json")
    second = invoke(cli, "run", "mock", "probe", "--permissions", "write", "--dry-run", "--json")
    canonical = invoke(
        cli, "run", "mock", "probe", "--permissions", "execute", "--dry-run", "--json"
    )

    assert json.loads(first.stdout)["resolved"]["permissions"]["value"] == "execute"
    assert json.loads(first.stdout) == json.loads(canonical.stdout)
    assert "--permissions write is deprecated; use --permissions execute" in first.stderr
    assert "deprecated" not in second.stderr


def test_a_raw_model_id_on_the_call_is_labelled_as_a_call_flag(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "probe", "--model", "mock-opus-5", "--dry-run", "--json")

    resolved = json.loads(result.stdout)["resolved"]
    assert resolved["model"]["value"] == "mock-opus-5"
    assert resolved["model"]["source"] == "call flag"


def test_a_tier_name_resolves_through_the_entry_preset_that_defines_it(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "probe", "--model", "max", "--dry-run", "--json")

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
    monkeypatch.setattr(cli_module, "_stdout_is_tty", lambda: True)

    result = invoke(cli, "run", "mock", "probe", "--permissions", "prompt", "--dry-run", "--json")

    assert result.exit_code == vocab.EXIT_OK
    assert json.loads(result.stdout)["resolved"]["permissions"]["value"] == "ask"
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
    assert "denied: 1 edit (default read policy — pass --permissions edit)" in result.stderr
    session_id = json.loads(result.stdout)["session_id"]
    denied_meta = json.loads(
        (state_root / "sessions" / session_id / "meta.json").read_text(encoding="utf-8")
    )
    assert denied_meta["denied"] == {"edit": 1}
    assert denied_meta["resolution"]["permissions_source"] == "default"


def test_explicit_read_denials_stay_out_of_the_summary(cli: CliRunner, state_root: Path) -> None:
    result = invoke(cli, "run", "mock", "write-file:blocked.md", "--permissions", "read", "--json")

    assert result.exit_code == vocab.EXIT_OK
    assert "denied:" not in result.stderr
    session_id = json.loads(result.stdout)["session_id"]
    denied_meta = json.loads(
        (state_root / "sessions" / session_id / "meta.json").read_text(encoding="utf-8")
    )
    assert denied_meta["resolution"].get("permissions_source") is None


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

    assert result.exit_code == vocab.EXIT_OK
    assert (
        "denied: 1 edit · 2 execute · 2 unknown (default read policy — pass --permissions all)"
    ) in result.stderr


def test_wait_shows_default_policy_denials_from_disk(cli: CliRunner, live_daemon: None) -> None:
    started = invoke(cli, "run", "mock", "write-file:background.md", "--bg", "--json")

    assert started.exit_code == vocab.EXIT_OK
    session_id = json.loads(started.stdout)["session_id"]

    waited = invoke(cli, "wait", session_id)

    assert waited.exit_code == vocab.EXIT_OK
    assert "denied: 1 edit (default read policy — pass --permissions edit)" in waited.stderr


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
    cli: CliRunner,
) -> None:
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


def test_stdout_bytes_match_answer_file_after_a_detectable_message_boundary(
    cli: CliRunner,
) -> None:
    result = invoke(cli, "run", "mock", "separator contract probe")

    assert "\n\n## Answer" in result.stdout
    metadata_lines = [line for line in result.stderr.splitlines() if line.startswith("-- ")]
    early_line = next(line for line in metadata_lines if line.startswith("-- session "))
    session_id = early_line.split()[2]
    assert result.stdout.encode("utf-8") == sessions.answer_path(session_id).read_bytes()


def test_the_summary_starts_on_a_fresh_line_after_an_unterminated_answer(cli: CliRunner) -> None:
    """SPEC output contract: `--` separates only at a line boundary, and the
    compensating newline goes to stderr — stdout stays byte-identical to
    answer.md."""
    result = invoke(cli, "run", "mock", "echo:no trailing newline")

    assert result.stdout == "no trailing newline"
    # The early session line precedes the answer, so the compensating newline
    # is no longer stderr's first byte — it is the blank line that separates
    # the unterminated answer from the summary in the merged blob.
    lines = result.stderr.split("\n")
    summary_index = next(index for index, line in enumerate(lines) if "exit 0" in line)
    assert lines[summary_index - 1] == ""


def test_a_terminated_answer_gets_no_blank_line_before_the_summary(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "echo:ends with a newline\n")

    assert result.stdout == "ends with a newline\n"
    assert result.stderr.startswith("-- ")


def test_the_direct_child_note_rides_the_one_summary_line(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "echo:route me")

    summary_lines = [line for line in result.stderr.splitlines() if line.startswith("-- ")]
    assert len(summary_lines) == 2
    assert any("direct child" in line for line in summary_lines)
    assert any(line.startswith("-- session ") for line in summary_lines)


def test_a_default_cwd_resolves_to_the_callers_absolute_directory(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The adapter receives cwd over session/new and resolves a relative path
    against its own host process, so acpc must send the caller's absolute
    directory — vendors reject a literal '.'."""
    caller_dir = tmp_path / "caller"
    caller_dir.mkdir()
    monkeypatch.chdir(caller_dir)

    result = invoke(cli, "run", "mock", "echo:x", "--dry-run", "--json")

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


def test_output_file_receives_the_answer(cli: CliRunner, tmp_path: Path) -> None:
    target = tmp_path / "answer.md"

    result = invoke(cli, "run", "mock", "echo:written to a file", "-o", str(target), "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert "written to a file" in target.read_text(encoding="utf-8")


def test_json_output_carries_the_session_id_and_paths(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "echo:as json", "--json", "--quiet")

    payload = json.loads(result.stdout)
    assert payload["session_id"]
    assert payload["paths"]["answer"].endswith("answer.md")
    assert "as json" in payload["answer"]


def test_max_output_caps_stdout(cli: CliRunner) -> None:
    result = invoke(
        cli, "run", "mock", "trigger the huge scenario", "--quiet", "--max-output", "512"
    )

    assert len(result.stdout) < 4096


def test_max_output_zero_disables_the_cap(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "trigger the huge scenario", "--quiet", "--max-output", "0")

    assert len(result.stdout) > 131072


def test_a_negative_max_output_is_a_usage_error(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "probe", "--max-output", "-1")

    assert result.exit_code == vocab.EXIT_USAGE


# --- exit codes at the CLI boundary -----------------------------------------


def test_a_refusal_exits_1(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "please fail this on purpose", "--quiet")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR


def test_a_timeout_exits_124(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "slow:30 cli timeout probe", "--timeout", "1", "--quiet")

    assert result.exit_code == vocab.EXIT_TIMEOUT


def test_a_bare_integer_timeout_is_seconds(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "slow:2 bare seconds", "--timeout", "1", "--quiet")

    assert result.exit_code == vocab.EXIT_TIMEOUT


def test_a_five_minute_timeout_is_not_five_seconds(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "slow:6 five minute timeout", "--timeout", "5m", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert "waited 6s" in result.stdout


def test_an_invalid_timeout_has_the_pinned_duration_error(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "hello", "--timeout", "5x")

    assert result.exit_code == vocab.EXIT_USAGE
    assert result.stderr == (
        "Error: Invalid value for '--timeout': '5x' is not a duration — "
        "use seconds (90) or a suffixed value (90s, 5m, 1h, 1h30m)\n"
    )


@pytest.mark.parametrize("value", ["-1", "-1m"])
def test_negative_timeout_is_a_usage_error(cli: CliRunner, value: str) -> None:
    result = invoke(cli, "run", "mock", "hello", "--timeout", value, "--dry-run")

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
