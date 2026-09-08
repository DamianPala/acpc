"""Behavioral tests for the structured failure envelope.

One shape, on stderr, never on stdout, and a stable `kind` next to the exit
code. The seam these tests move is `errors.stderr_is_tty`: a test process has
no terminal, and the rule that decides between the envelope and the one-line
diagnostic reads exactly that.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from acpc import errors, proc, sessions, vocab
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
yolo = {{ grants = "all", delegates = false }}

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


@pytest.fixture
def terminal_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put a person in front of stderr, as a real terminal would."""
    monkeypatch.setattr(errors, "stderr_is_tty", lambda: True)


def invoke(cli: CliRunner, *args: str):
    return cli.invoke(main, list(args), catch_exceptions=False)


def envelope(result) -> dict:
    """The failure object: always the last non-empty line of stderr."""
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    return json.loads(lines[-1])["error"]


def running_session(*, live: bool = False) -> tuple[sessions.SessionMeta, int | None]:
    """A session that reads as running, optionally behind a real process."""
    meta = sessions.create_session(entry="mock", base_adapter="mock", prompt="held open")
    if not live:
        sessions.mark_running(meta.session_id, pid=os.getpid())
        return sessions.read_meta(meta.session_id), None
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    sessions.mark_running(
        meta.session_id,
        pid=child.pid,
        process_start_time=proc.process_start_time(child.pid),
    )
    return sessions.read_meta(meta.session_id), child.pid


# --- where the envelope goes ------------------------------------------------


def test_the_envelope_is_the_last_non_empty_stderr_line_and_never_on_stdout(
    cli: CliRunner,
) -> None:
    """Metadata footers still print; the envelope closes the stream."""
    meta, _pid = running_session()

    result = invoke(cli, "log", meta.session_id, "--wait-new", "--timeout", "0")

    assert result.exit_code == vocab.EXIT_TIMEOUT
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert len(lines) > 1, "the footer should still be there"
    assert any(line.startswith("--") for line in lines[:-1])
    assert envelope(result)["kind"] == "timeout"
    assert "error" not in result.stdout


def test_a_piped_stderr_gets_the_envelope_without_asking_for_json(cli: CliRunner) -> None:
    """A caller reading stderr mechanically needs the object even in text mode."""
    result = invoke(cli, "status", "does-not-exist")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    assert envelope(result)["kind"] == "not_found"
    assert result.stdout == ""


def test_json_forces_the_envelope_even_when_stderr_is_a_terminal(
    cli: CliRunner, terminal_stderr: None
) -> None:
    result = invoke(cli, "status", "does-not-exist", "--json")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    assert envelope(result)["kind"] == "not_found"
    assert result.stdout == ""


def test_wait_not_found_carries_a_null_observed_status(cli: CliRunner) -> None:
    result = invoke(cli, "wait", "does-not-exist", "--json")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    error = envelope(result)
    assert error["kind"] == "not_found"
    assert error["context"] == {"session_id": "does-not-exist", "status": None}


def test_wait_pruned_session_is_not_found_without_a_status(
    cli: CliRunner,
) -> None:
    meta = sessions.create_session(entry="mock", base_adapter="mock", prompt="expired")
    sessions.mark_running(
        meta.session_id,
        pid=os.getpid(),
        process_start_time=proc.process_start_time(),
    )
    sessions.transition(meta.session_id, "succeeded", exit_code=0, stop_reason="test")
    path = sessions.meta_path(meta.session_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    finished_at = sessions.parse_timestamp(payload["finished_at"], "finished_at", path)
    assert finished_at is not None
    payload["finished_at"] = sessions.format_timestamp(finished_at - 200 * 86400)
    path.write_text(json.dumps(payload), encoding="utf-8")

    prune = invoke(cli, "prune", "--older-than", "100d", "--yes")
    result = invoke(cli, "wait", meta.session_id, "--json")

    assert prune.exit_code == vocab.EXIT_OK
    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    error = envelope(result)
    assert error["kind"] == "not_found"
    assert error["context"] == {"session_id": meta.session_id, "status": None}


def test_a_person_at_a_terminal_gets_the_line_and_its_hint_instead(
    cli: CliRunner, terminal_stderr: None
) -> None:
    result = invoke(cli, "status", "does-not-exist", "--format", "text")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    assert result.stderr.splitlines() == [
        "Error: unknown session 'does-not-exist'",
        "Run: acpc status",
    ]


def test_a_terminal_failure_without_a_hint_is_one_line(
    cli: CliRunner, terminal_stderr: None
) -> None:
    result = invoke(cli, "status", "some-id", "--bogus")

    assert result.exit_code == vocab.EXIT_USAGE
    assert result.stderr == "Error: No such option '--bogus'.\n"


# --- failures raised before the format was resolved -------------------------


def test_json_before_a_bare_dash_dash_arms_the_envelope_for_parse_failures(
    cli: CliRunner, terminal_stderr: None
) -> None:
    """The flag never reaches a command here — Click rejects the call first."""
    result = invoke(cli, "list", "--json", "--no-such-flag")

    assert result.exit_code == vocab.EXIT_USAGE
    error = envelope(result)
    assert error["kind"] == "invalid_input"
    assert "--no-such-flag" in error["message"]


def test_the_scan_stops_at_a_bare_dash_dash(cli: CliRunner, terminal_stderr: None) -> None:
    """Past `--` the word is an operand, so it cannot ask for a machine format."""
    result = invoke(cli, "--", "--json")

    assert result.exit_code == vocab.EXIT_USAGE
    assert result.stderr.startswith("Error: ")
    assert "{" not in result.stderr


def test_a_command_that_parses_json_itself_refines_the_argument_scan(
    cli: CliRunner, terminal_stderr: None
) -> None:
    """A bare `--json` can be another flag's value; only the command knows.

    The scan runs before Click and sees the word. The command then reports
    what it actually parsed, so a person at a terminal keeps their prose.
    """
    result = invoke(cli, "run", "no-such-agent", "--output-file", "--json", "hello")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    assert result.stderr.splitlines() == [
        "Error: unknown agent 'no-such-agent'",
        "Run: acpc agents list",
    ]


# --- the envelope's own shape -----------------------------------------------


def test_optional_fields_are_absent_rather_than_null(cli: CliRunner) -> None:
    result = invoke(cli, "status", "some-id", "--bogus")

    assert result.exit_code == vocab.EXIT_USAGE
    assert envelope(result) == {
        "kind": "invalid_input",
        "message": "No such option '--bogus'.",
    }


def test_the_document_has_exactly_one_top_level_field(cli: CliRunner) -> None:
    result = invoke(cli, "status", "does-not-exist", "--json")

    document = json.loads(result.stderr.splitlines()[-1])
    assert list(document) == ["error"]
    # Which fields are there, not what order they came in: key order in a JSON
    # object is not part of the contract, and unset fields are absent.
    assert set(document["error"]) == {"kind", "message", "hint"}


def test_a_failure_after_the_session_exists_carries_its_id(cli: CliRunner) -> None:
    """R7a: the caller has to be able to reach work that was already started."""
    meta, _pid = running_session()

    result = invoke(cli, "continue", meta.session_id, "turn two")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    error = envelope(result)
    assert error["kind"] == "conflict"
    assert error["retryable"] is True
    assert error["context"] == {"session_id": meta.session_id}


def test_a_non_ascii_message_stays_readable(cli: CliRunner) -> None:
    result = invoke(cli, "status", "sesja-zażółć")

    assert "zażółć" in result.stderr
    assert "\\u" not in result.stderr


# --- the published vocabularies ---------------------------------------------


def test_every_exit_code_has_exactly_one_documented_meaning() -> None:
    """The table is what `schema` publishes, so a code without one is a hole."""
    used = {
        vocab.EXIT_OK,
        vocab.EXIT_AGENT_ERROR,
        vocab.EXIT_USAGE,
        vocab.EXIT_BUDGET,
        vocab.EXIT_TIMEOUT,
        vocab.EXIT_CANCELLED,
        vocab.EXIT_SIGPIPE,
        vocab.EXIT_SIGTERM,
    }
    assert set(vocab.EXIT_DESCRIPTIONS) == {str(code) for code in used}
    assert all(text.strip() for text in vocab.EXIT_DESCRIPTIONS.values())


def test_the_kind_vocabulary_carries_the_shared_meanings() -> None:
    """A kind with a shared meaning must not go missing or gain a second one."""
    shared = {
        "invalid_input",
        "not_found",
        "conflict",
        "permission_denied",
        "unauthenticated",
        "timeout",
        "unavailable",
        "outcome_unknown",
        "interrupted",
        "cursor_unavailable",
        "confirmation_required",
        "operation_failed",
        "precondition_failed",
    }
    assert shared <= set(errors.KINDS)
    assert set(errors.KINDS) - shared == {"agent_error", "corrupt_state", "not_supported"}
    assert len(errors.KINDS) == len(set(errors.KINDS))


# --- interruption ------------------------------------------------------------


# The child announces itself once its imports are done, so the interrupt lands
# on the running command rather than somewhere in `import acpc`, where Python's
# own default handler would kill it before acpc could answer.
_READY_PROGRAM = (
    "import sys\n"
    "from acpc.cli import main\n"
    "sys.stderr.write('READY\\n')\n"
    "sys.stderr.flush()\n"
    "raise SystemExit(main())\n"
)


def run_cli(*args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", _READY_PROGRAM, *args],
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def wait_until_ready(process: subprocess.Popen[str]) -> None:
    """Block until the child says it is running the command."""
    assert process.stderr is not None
    assert process.stderr.readline() == "READY\n", "the child never started"
    time.sleep(0.3)
    assert process.poll() is None, "the process exited before it could be interrupted"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal semantics")
def test_ctrl_c_on_wait_exits_130_and_leaves_the_session_alone() -> None:
    """F5: interrupting observation must not change what is being observed."""
    meta, pid = running_session(live=True)
    before = (sessions.meta_path(meta.session_id)).read_text(encoding="utf-8")

    watcher = run_cli("wait", meta.session_id)
    try:
        wait_until_ready(watcher)
        watcher.send_signal(signal.SIGINT)
        _stdout, stderr = watcher.communicate(timeout=10)
    finally:
        if watcher.poll() is None:
            watcher.kill()
            watcher.wait(timeout=10)
        if pid is not None:
            proc.kill_process_tree(pid, None)

    assert watcher.returncode == vocab.EXIT_CANCELLED
    assert "Traceback" not in stderr
    error = json.loads(stderr.splitlines()[-1])["error"]
    assert error["kind"] == "interrupted"
    assert (sessions.meta_path(meta.session_id)).read_text(encoding="utf-8") == before


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal semantics")
def test_ctrl_c_on_log_follow_exits_130_and_leaves_the_session_alone() -> None:
    meta, pid = running_session(live=True)
    before = (sessions.meta_path(meta.session_id)).read_text(encoding="utf-8")

    follower = run_cli("log", meta.session_id, "--follow")
    try:
        wait_until_ready(follower)
        follower.send_signal(signal.SIGINT)
        _stdout, stderr = follower.communicate(timeout=10)
    finally:
        if follower.poll() is None:
            follower.kill()
            follower.wait(timeout=10)
        if pid is not None:
            proc.kill_process_tree(pid, None)

    assert follower.returncode == vocab.EXIT_CANCELLED
    assert "Traceback" not in stderr
    assert json.loads(stderr.splitlines()[-1])["error"]["kind"] == "interrupted"
    assert (sessions.meta_path(meta.session_id)).read_text(encoding="utf-8") == before


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal semantics")
def test_ctrl_c_on_a_turn_cancels_it_and_names_the_session(cli: CliRunner) -> None:
    """The turn's own process owns the turn, so it still cancels — and says so."""
    turn = run_cli("run", "mock", "slow:10 hold the turn open", "--quiet")
    try:
        deadline = time.monotonic() + 10
        session_id = None
        while time.monotonic() < deadline:
            active = [meta for meta in sessions.list_sessions() if meta.state == "running"]
            if active:
                session_id = active[0].session_id
                break
            time.sleep(0.05)
        assert session_id is not None, "the turn never started"
        turn.send_signal(signal.SIGINT)
        _stdout, stderr = turn.communicate(timeout=15)
    finally:
        if turn.poll() is None:
            turn.kill()
            turn.wait(timeout=10)

    assert turn.returncode == vocab.EXIT_CANCELLED
    assert "Traceback" not in stderr
    error = json.loads(stderr.splitlines()[-1])["error"]
    assert error["kind"] == "interrupted"
    assert error["context"]["session_id"] == session_id
    assert sessions.read_meta(session_id).state == "canceled"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal semantics")
def test_interrupted_is_this_command_being_stopped_not_a_cancelled_session() -> None:
    """The two readings of `cancelled` are different failures.

    Ctrl-C on `wait` stops the command: nothing about the session changed and
    the caller already knows why it ended. `wait` on a session somebody else
    cancelled watched an operation run to a bad end, which is what
    `operation_failed` and its `status` say.
    """
    meta, pid = running_session(live=True)

    watcher = run_cli("wait", meta.session_id)
    try:
        wait_until_ready(watcher)
        watcher.send_signal(signal.SIGINT)
        _stdout, interrupted_stderr = watcher.communicate(timeout=10)
    finally:
        if watcher.poll() is None:
            watcher.kill()
            watcher.wait(timeout=10)
        if pid is not None:
            proc.kill_process_tree(pid, None)
    sessions.transition(meta.session_id, "canceled")

    observer = run_cli("wait", meta.session_id)
    _stdout, observed_stderr = observer.communicate(timeout=10)

    assert watcher.returncode == vocab.EXIT_CANCELLED
    assert json.loads(interrupted_stderr.splitlines()[-1])["error"]["kind"] == "interrupted"

    assert observer.returncode == vocab.EXIT_CANCELLED
    observed = json.loads(observed_stderr.splitlines()[-1])["error"]
    assert observed["kind"] == "operation_failed"
    assert observed["context"] == {"session_id": meta.session_id, "status": "canceled"}


# --- kinds that are not the caller's syntax ---------------------------------


def test_a_state_root_acpc_cannot_write_is_a_refusal_not_a_stack_trace(
    cli: CliRunner, state_root: Path
) -> None:
    """Nothing classifies this deep down, so the last-resort catcher must."""
    state_root.chmod(0o555)
    try:
        result = invoke(cli, "run", "mock", "hello", "--json")
    finally:
        state_root.chmod(0o755)

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    assert "Traceback" not in result.stderr
    error = envelope(result)
    assert error["kind"] == "permission_denied"
    assert error["action"] == "user"
    assert result.stdout == ""


def test_an_unreadable_agent_entry_is_corrupt_state_not_a_bad_call(
    cli: CliRunner, state_root: Path
) -> None:
    """A file acpc reads is not an argument the caller typed."""
    (state_root / "agents" / "broken.toml").write_text("name = ", encoding="utf-8")

    result = invoke(cli, "run", "broken", "hello")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    assert envelope(result)["kind"] == "corrupt_state"


def test_an_exhausted_session_id_pool_is_unavailable_and_names_prune(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """acpc's own id space, not the call and not a missing target."""
    monkeypatch.setattr(sessions, "_ID_ALLOCATION_ATTEMPTS", 0)

    result = invoke(cli, "run", "mock", "hello")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    error = envelope(result)
    assert error["kind"] == "unavailable"
    assert error["action"] == "user"
    assert "prune" in error["hint"]


def test_an_unreadable_prompt_file_is_a_refusal_and_a_missing_one_is_not(
    cli: CliRunner, tmp_path: Path
) -> None:
    """The caller typed the path either way; only one of the two is theirs."""
    unreadable = tmp_path / "prompt.md"
    unreadable.write_text("hello", encoding="utf-8")
    unreadable.chmod(0o000)

    refused = invoke(cli, "run", "mock", "--prompt-file", str(unreadable))
    missing = invoke(cli, "run", "mock", "--prompt-file", str(tmp_path / "gone.md"))
    unreadable.chmod(0o600)

    assert envelope(refused)["kind"] == "permission_denied"
    assert refused.exit_code == vocab.EXIT_AGENT_ERROR
    assert envelope(missing)["kind"] == "invalid_input"
    assert missing.exit_code == vocab.EXIT_USAGE


def test_an_entry_directory_that_refuses_the_write_is_not_a_usage_error(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`agents create` was spelled correctly; the filesystem said no."""
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o555)
    monkeypatch.setenv("ACPC_HOME", str(locked))
    try:
        result = invoke(cli, "agents", "create", "variant", "--extends", "codex")
    finally:
        locked.chmod(0o755)

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    error = envelope(result)
    assert error["kind"] == "permission_denied"
    assert error["action"] == "user"
    assert "agents" in error["hint"]


def test_retryable_is_absent_where_repeating_the_call_cannot_help(
    cli: CliRunner, state_root: Path
) -> None:
    """F3b: `true` authorizes a retry, and `false` would still be a claim.

    A busy session settles on its own, so the same call may work. A damaged
    file on disk will read the same on every repeat, and the field is left out
    rather than guessed in either direction.
    """
    meta, _pid = running_session()
    (state_root / "agents" / "broken.toml").write_text("name = ", encoding="utf-8")

    busy = invoke(cli, "continue", meta.session_id, "turn two")
    damaged = invoke(cli, "run", "broken", "hello")

    assert envelope(busy)["retryable"] is True
    assert "retryable" not in envelope(damaged)
