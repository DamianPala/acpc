"""Direct behavioral coverage for the output and persistence contract."""

import json
import os
import pty
import queue
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from acpc import cli as cli_module
from acpc import paths, sessions, transcript, vocab
from acpc.cli import main

MOCK_AGENT_SCRIPT = str(Path(__file__).with_name("mock_agent.py"))
MOCK_ENTRY = f'''
name = "Mock Agent"
command = "{sys.executable} {MOCK_AGENT_SCRIPT}"
install_command = "true"
home = "~/.mock"
home_env = "MOCK_HOME"

[modes]
default = {{ grants = "read", delegates = true }}
'''

RAW_INVOKE = CliRunner.invoke


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
    return RAW_INVOKE(cli, main, list(args), catch_exceptions=False)


def create_finished(prompt: str = "finished") -> sessions.SessionMeta:
    meta = sessions.create_session(entry="mock", base_adapter="mock", prompt=prompt)
    return sessions.transition(meta.session_id, "succeeded", exit_code=0)


def test_non_tty_defaults_match_schema_and_explicit_text_is_available(cli: CliRunner) -> None:
    index = json.loads(invoke(cli, "schema").stdout)
    assert index["format_defaults"] == {"tty": "text", "non_tty": "json"}
    status_schema = json.loads(invoke(cli, "schema", "status").stdout)
    status_format = next(flag for flag in status_schema["flags"] if flag["name"] == "format")
    assert index["format_defaults"]["non_tty"] in status_format["enum"]
    list_schema = json.loads(invoke(cli, "schema", "list").stdout)
    list_format = next(flag for flag in list_schema["flags"] if flag["name"] == "format")
    assert "plain" in list_format["enum"]
    status_default = invoke(cli, "list")
    assert json.loads(status_default.stdout) == {"items": [], "has_more": False}
    assert isinstance(json.loads(invoke(cli, "agents", "list").stdout)["items"], list)
    assert isinstance(json.loads(invoke(cli, "skills", "list").stdout)["items"], list)
    assert json.loads(invoke(cli, "daemon", "status").stdout) == {
        "items": [],
        "has_more": False,
    }

    log_detail = json.loads(invoke(cli, "schema", "log").stdout)
    assert log_detail["format_defaults"] == {"tty": "text", "non_tty": "text"}
    text = invoke(cli, "list", "--format", "text")
    assert text.stdout.endswith("-- 0 of 0\n")


def test_tty_uses_the_schema_tty_default(tmp_path: Path) -> None:
    master, slave = pty.openpty()
    environment = os.environ.copy()
    environment["ACPC_HOME"] = str(tmp_path / "state")
    process = subprocess.Popen(
        [sys.executable, "-c", "from acpc.cli import main; main()", "list"],
        stdin=subprocess.DEVNULL,
        stdout=slave,
        stderr=subprocess.PIPE,
        env=environment,
        text=False,
    )
    os.close(slave)
    chunks: list[bytes] = []
    while True:
        try:
            chunk = os.read(master, 4096)
        except OSError:
            break
        if not chunk:
            break
        chunks.append(chunk)
    os.close(master)
    _, stderr = process.communicate(timeout=10)
    assert process.returncode == vocab.EXIT_OK, stderr.decode()
    assert b"ID" in b"".join(chunks)


def test_empty_collection_and_bounded_collection_have_the_pinned_shape(cli: CliRunner) -> None:
    empty = json.loads(invoke(cli, "list", "--json").stdout)
    assert empty == {"items": [], "has_more": False}

    for index in range(21):
        create_finished(f"prompt-{index}")
    bounded = json.loads(invoke(cli, "list", "--json").stdout)
    assert len(bounded["items"]) == 20
    assert bounded["has_more"] is True

    human = invoke(cli, "list", "--format", "text")
    assert "-- 20 of 21 — use --limit to change" in human.stdout


def test_plain_requires_an_explicit_limit_and_emits_one_identifier_per_line(
    cli: CliRunner,
) -> None:
    first = create_finished("first")
    second = create_finished("second")
    identifiers = {first.session_id, second.session_id}

    rejected = invoke(cli, "list", "--format", "plain")
    assert rejected.exit_code == vocab.EXIT_USAGE
    assert "--limit" in rejected.stderr

    result = invoke(cli, "list", "--format", "plain", "--limit", "1")
    assert result.exit_code == vocab.EXIT_OK
    assert result.stdout.endswith("\n")
    assert result.stdout.count("\n") == 1
    assert result.stdout.strip() in identifiers
    assert "--" not in result.stdout


def test_named_agent_views_reject_collection_only_flags(cli: CliRunner) -> None:
    calls = (
        (("agents", "get", "mock", "--models", "--limit", "1"), "--limit"),
        (("agents", "get", "mock", "--commands", "--plain"), "--plain"),
        (("agents", "check", "mock", "--limit", "1"), "--limit"),
    )
    for arguments, flag in calls:
        result = invoke(cli, *arguments)
        assert result.exit_code == vocab.EXIT_USAGE
        assert flag in result.stderr


def test_schema_publishes_closed_format_choices_locally_and_color_globally(
    cli: CliRunner,
) -> None:
    index = json.loads(invoke(cli, "schema").stdout)
    globals_by_name = {flag["name"]: flag for flag in index["global_flags"]}
    assert "format" not in globals_by_name
    assert globals_by_name["color"]["enum"] == ["auto", "always", "never"]

    status = json.loads(invoke(cli, "schema", "list").stdout)
    log = json.loads(invoke(cli, "schema", "log").stdout)
    status_format = next(flag for flag in status["flags"] if flag["name"] == "format")
    log_format = next(flag for flag in log["flags"] if flag["name"] == "format")
    assert status_format["enum"] == ["text", "json", "plain"]
    assert log_format["enum"] == ["text", "ndjson"]


def test_help_states_the_applicable_format_default(cli: CliRunner) -> None:
    collection_help = invoke(cli, "list", "--help").stdout
    native_help = invoke(cli, "run", "--help").stdout
    stream_help = invoke(cli, "log", "--help").stdout

    assert "absent, text on a TTY and JSON on non-TTY" in " ".join(collection_help.split())
    assert "absent, text on both TTY and non-TTY" in " ".join(native_help.split())
    assert "absent, text on both TTY and non-TTY" in " ".join(stream_help.split())


def _assert_truncated(payload: dict[str, Any], session_id: str) -> None:
    assert payload["truncated"] is True
    answer_path = sessions.answer_path(session_id)
    assert payload["output_file"] == str(answer_path)
    assert answer_path.is_file()


def _make_mid_turn_session(cli: CliRunner) -> str:
    result = invoke(cli, "run", "mock", "seed", "--quiet", "--json")
    assert result.exit_code == vocab.EXIT_OK, result.stderr
    session_id = json.loads(result.stdout)["session_id"]
    sessions.rotate_turn(session_id)
    sessions.write_prompt(session_id, "the interrupted turn")
    sessions.write_answer(session_id, "partial")
    meta = sessions.read_meta(session_id)
    assert meta.adapter_session_id is not None
    store_path = Path(os.environ["ACPC_HOME"]) / "mock-sessions.json"
    store = json.loads(store_path.read_text(encoding="utf-8"))
    store[meta.adapter_session_id]["history"].append("the interrupted turn")
    store_path.write_text(json.dumps(store), encoding="utf-8")
    sessions.transition(session_id, "running", pid=None)
    return session_id


def test_every_answer_command_reports_truncation_and_answer_file(cli: CliRunner) -> None:
    run_result = invoke(
        cli,
        "run",
        "mock",
        "trigger the huge scenario",
        "--quiet",
        "--json",
        "--max-output",
        "4096",
    )
    run_payload = json.loads(run_result.stdout)
    _assert_truncated(run_payload, run_payload["session_id"])

    seed = invoke(cli, "run", "mock", "seed", "--quiet", "--json")
    session_id = json.loads(seed.stdout)["session_id"]
    continue_result = invoke(
        cli,
        "continue",
        session_id,
        "trigger the huge scenario",
        "--quiet",
        "--json",
        "--max-output",
        "4096",
    )
    _assert_truncated(json.loads(continue_result.stdout), session_id)

    wait_result = invoke(cli, "wait", session_id, "--quiet", "--json", "--max-output", "4096")
    _assert_truncated(json.loads(wait_result.stdout), session_id)

    steer_session = _make_mid_turn_session(cli)
    steer_result = invoke(
        cli,
        "steer",
        steer_session,
        "trigger the huge scenario",
        "--quiet",
        "--json",
        "--max-output",
        "4096",
    )
    _assert_truncated(json.loads(steer_result.stdout), steer_session)


def test_json_output_file_is_exact_stdout_payload_and_stdout_stays_empty(
    cli: CliRunner, tmp_path: Path
) -> None:
    target = tmp_path / "result.json"
    result = invoke(
        cli,
        "run",
        "mock",
        "echo:json file",
        "--quiet",
        "--json",
        "--output-file",
        str(target),
    )
    assert result.exit_code == vocab.EXIT_OK
    assert result.stdout == ""
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["answer"].strip() == "json file"
    assert payload["truncated"] is False


@pytest.mark.skipif(not Path("/dev/null").exists(), reason="requires /dev/null")
def test_run_can_write_its_output_file_to_dev_null(cli: CliRunner) -> None:
    result = invoke(
        cli,
        "run",
        "mock",
        "echo:discarded",
        "--quiet",
        "--output-file",
        "/dev/null",
    )

    assert result.exit_code == vocab.EXIT_OK, result.stderr
    assert result.stdout == ""


@pytest.mark.skipif(not Path("/dev/null").exists(), reason="requires /dev/null")
def test_wait_can_write_its_output_file_to_dev_null(cli: CliRunner) -> None:
    run_result = invoke(cli, "run", "mock", "echo:done", "--quiet", "--json")
    session_id = json.loads(run_result.stdout)["session_id"]

    result = invoke(cli, "wait", session_id, "--quiet", "--output-file", "/dev/null")

    assert result.exit_code == vocab.EXIT_OK, result.stderr
    assert result.stdout == ""


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires FIFO support")
def test_output_file_writes_to_an_existing_fifo_in_place(cli: CliRunner, tmp_path: Path) -> None:
    target = tmp_path / "result.fifo"
    os.mkfifo(target)
    reader_fd = os.open(target, os.O_RDONLY | os.O_NONBLOCK)
    writer_fd = os.open(target, os.O_WRONLY | os.O_NONBLOCK)
    os.set_blocking(reader_fd, True)
    received: queue.Queue[str] = queue.Queue()

    def read_fifo() -> None:
        with os.fdopen(reader_fd, "r", encoding="utf-8") as fifo:
            received.put(fifo.read())

    reader = threading.Thread(target=read_fifo, daemon=True)
    reader.start()
    try:
        result = invoke(
            cli,
            "run",
            "mock",
            "echo:fifo content",
            "--quiet",
            "--output-file",
            str(target),
        )
    finally:
        os.close(writer_fd)

    assert result.exit_code == vocab.EXIT_OK, result.stderr
    assert result.stdout == ""
    assert "fifo content" in received.get(timeout=5)
    assert target.is_fifo()


def test_output_file_permission_error_names_the_path_without_acpc_home(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from acpc import errors

    output_dir = tmp_path / "read-only"
    output_dir.mkdir()
    output_dir.chmod(0o500)
    target = output_dir / "nested" / "answer.json"
    original_atomic_write = paths.atomic_write

    def deny_output(path: Path, data: dict[str, Any] | str, *, exclusive: bool = False) -> None:
        if path == target:
            raise PermissionError(13, "Permission denied", str(path.parent))
        original_atomic_write(path, data, exclusive=exclusive)

    monkeypatch.setattr(paths, "atomic_write", deny_output)
    try:
        result = invoke(
            cli,
            "run",
            "mock",
            "echo:cannot write",
            "--quiet",
            "--json",
            "--output-file",
            str(target),
        )
    finally:
        output_dir.chmod(0o700)

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    problem = json.loads(result.stderr)["error"]
    assert problem["kind"] == errors.PERMISSION_DENIED
    assert problem["action"] == "user"
    assert str(target) in problem["message"]
    assert "writable directory" in problem["hint"]
    assert "ACPC_HOME" not in problem["message"]
    assert "ACPC_HOME" not in problem["hint"]


def test_output_file_mirrors_text_and_json_wait_output_byte_for_byte(
    cli: CliRunner, tmp_path: Path
) -> None:
    seed = invoke(cli, "run", "mock", "echo:stable answer", "--quiet", "--json")
    session_id = json.loads(seed.stdout)["session_id"]

    direct_text = invoke(cli, "wait", session_id, "--quiet", "--format", "text")
    text_file = tmp_path / "answer.txt"
    stored_text = invoke(
        cli,
        "wait",
        session_id,
        "--quiet",
        "--format",
        "text",
        "--output-file",
        str(text_file),
    )
    assert stored_text.stdout == ""
    assert text_file.read_bytes() == direct_text.stdout.encode()

    direct_json = invoke(cli, "wait", session_id, "--quiet", "--json")
    json_file = tmp_path / "answer.json"
    stored_json = invoke(
        cli,
        "wait",
        session_id,
        "--quiet",
        "--json",
        "--output-file",
        str(json_file),
    )
    assert stored_json.stdout == ""
    assert json_file.read_bytes() == direct_json.stdout.encode()


def test_failed_turn_output_file_mirrors_the_result_that_is_returned(
    cli: CliRunner, tmp_path: Path
) -> None:
    text_file = tmp_path / "failed.txt"
    text = invoke(
        cli,
        "run",
        "mock",
        "fail this turn",
        "--quiet",
        "--format",
        "text",
        "--output-file",
        str(text_file),
    )
    assert text.exit_code == vocab.EXIT_AGENT_ERROR
    assert text.stdout == ""
    assert "Unable to complete" in text_file.read_text(encoding="utf-8")

    json_file = tmp_path / "failed.json"
    machine = invoke(
        cli,
        "run",
        "mock",
        "fail this turn",
        "--quiet",
        "--json",
        "--output-file",
        str(json_file),
    )
    assert machine.exit_code == vocab.EXIT_AGENT_ERROR
    assert machine.stdout == ""
    document = json.loads(json_file.read_text(encoding="utf-8"))
    assert document["status"] == "failed"
    assert document["partial"] is False
    assert "Unable to complete" in document["answer"]


def test_meta_status_and_transcript_timestamps_are_rfc3339(cli: CliRunner) -> None:
    meta = create_finished("timestamp check")
    transcript_file = transcript.Transcript(sessions.transcript_path(meta.session_id))
    transcript_file.append("msg", text="timestamped")

    stored = json.loads(sessions.meta_path(meta.session_id).read_text(encoding="utf-8"))
    timestamps = [stored["created_at"], stored["finished_at"]]
    payload = json.loads(invoke(cli, "status", meta.session_id, "--json").stdout)
    timestamps.extend([payload["created_at"], payload["finished_at"]])
    event = transcript_file.read().events[0]
    timestamps.append(event["ts"])
    for value in timestamps:
        parsed = datetime.fromisoformat(value)
        assert parsed.tzinfo is not None
        assert value.endswith("Z") or value[-6] in "+-"


def test_legacy_transcript_error_names_the_rejected_version() -> None:
    path = sessions.transcript_path("legacy")
    path.parent.mkdir(parents=True)
    path.write_text('{"schema": "acpc.transcript/1"}\n', encoding="utf-8")

    with pytest.raises(transcript.TranscriptError, match=r"acpc\.transcript/1"):
        transcript.Transcript(path).read()


def test_daemon_limit_zero_reports_truncation_instead_of_no_daemons(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_status(agent: str | None) -> list[dict[str, Any]]:
        del agent
        return [{"target": "mock~target", "version": "1", "pid": 1, "uptime_seconds": 0}]

    monkeypatch.setattr(cli_module, "_collect_daemon_status", fake_status)
    result = invoke(cli, "daemon", "status", "--limit", "0", "--format", "text")

    assert result.exit_code == vocab.EXIT_OK
    assert result.stdout == "-- 0 of 1 — use --limit to change\n"
    assert "no daemons running" not in result.stderr


def test_timestamp_guards_turn_corrupt_metadata_into_one_named_failure(
    cli: CliRunner,
) -> None:
    meta = create_finished("bad timestamp")
    path = sessions.meta_path(meta.session_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["created_at"] = 10**1000
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = invoke(cli, "status", meta.session_id, "--format", "text")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    assert "meta.json" in result.stderr
    assert "Traceback" not in result.stderr
    with pytest.raises(ValueError, match="supported range"):
        sessions.format_timestamp(10**1000)

    transcript_path = sessions.transcript_path(meta.session_id)
    transcript_path.write_text(
        '{"schema": "acpc.transcript/2"}\n{"i": 1, "ts": 1e300, "type": "msg", "text": "x"}\n',
        encoding="utf-8",
    )
    with pytest.raises(transcript.TranscriptError, match="outside the supported range"):
        transcript.Transcript(transcript_path).read()


def test_continue_classifies_legacy_transcript_as_corrupt_state_with_recovery(
    cli: CliRunner,
) -> None:
    seed = invoke(cli, "run", "mock", "seed", "--quiet", "--json")
    meta = sessions.read_meta(json.loads(seed.stdout)["session_id"])
    path = sessions.transcript_path(meta.session_id)
    path.write_text('{"schema": "acpc.transcript/1"}\n', encoding="utf-8")

    result = invoke(cli, "continue", meta.session_id, "next", "--json")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    error = json.loads(result.stderr.splitlines()[-1])["error"]
    assert error["kind"] == "corrupt_state"
    assert "acpc.transcript/1" in error["message"]
    assert "acpc delete" in error["hint"]


def test_log_limit_ends_follow_after_the_requested_number_of_records(cli: CliRunner) -> None:
    meta = create_finished()
    transcript_file = transcript.Transcript(sessions.transcript_path(meta.session_id))
    for index in range(5):
        transcript_file.append("msg", text=f"event-{index}")

    result = invoke(
        cli,
        "log",
        meta.session_id,
        "--follow",
        "--limit",
        "3",
        "--format",
        "ndjson",
        "--quiet",
    )
    assert result.exit_code == vocab.EXIT_OK
    assert len(result.stdout.splitlines()) == 3
    assert [json.loads(line)["text"] for line in result.stdout.splitlines()] == [
        "event-0",
        "event-1",
        "event-2",
    ]


def test_log_json_is_the_ndjson_alias(cli: CliRunner) -> None:
    meta = create_finished()
    transcript_file = transcript.Transcript(sessions.transcript_path(meta.session_id))
    transcript_file.append("msg", text="alias event")

    result = invoke(cli, "log", meta.session_id, "--json", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    assert json.loads(result.stdout)["text"] == "alias event"


def test_equals_ndjson_format_keeps_machine_error_envelope(cli: CliRunner) -> None:
    result = invoke(cli, "log", "missing", "--format=ndjson")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    assert json.loads(result.stderr)["error"]["kind"] == "not_found"


def test_human_output_escapes_ansi_in_session_and_agent_values(
    cli: CliRunner, state_root: Path
) -> None:
    meta = create_finished("prompt \x1b[31mred\x1b[0m")
    sessions.update_meta(meta.session_id, name="name\x08\x07\x9b31mhidden\x1b[0m")
    status = invoke(cli, "status", meta.session_id, "--format", "text")
    assert all(control not in status.stdout for control in ("\x08", "\x07", "\x9b", "\x1b"))
    assert "^[" in status.stdout

    description = 'description = "agent \\u001b[31mred\\u001b[0m"\n'
    (state_root / "agents" / "mock.toml").write_text(
        MOCK_ENTRY.replace('name = "Mock Agent"\n', 'name = "Mock Agent"\n' + description),
        encoding="utf-8",
    )
    agents = invoke(cli, "agents", "list", "--format", "text")
    assert "\x1b" not in agents.stdout


def test_color_policy_obeys_explicit_and_environment_precedence(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_module, "_COLOR_POLICY", None)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    assert cli_module.color_policy(stdout_tty=True) == "always"
    monkeypatch.setenv("NO_COLOR", "1")
    assert cli_module.color_policy(stdout_tty=True) == "never"
    monkeypatch.setenv("TERM", "dumb")
    assert cli_module.color_policy(stdout_tty=True) == "never"
    result = invoke(cli, "list", "--format", "text", "--color", "always")
    assert result.exit_code == vocab.EXIT_OK
    assert cli_module.color_policy(stdout_tty=False) == "always"
