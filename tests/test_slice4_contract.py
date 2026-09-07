"""Direct behavioral coverage for slice 4's output and persistence contract."""

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from acpc import sessions, transcript, vocab
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

# Keep this reference before the shared compatibility fixture wraps Click for
# older human-view assertions. These tests intentionally exercise defaults.
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
    assert json.loads(invoke(cli, "status").stdout) == {"items": [], "has_more": False}
    assert isinstance(json.loads(invoke(cli, "agents").stdout)["items"], list)
    assert isinstance(json.loads(invoke(cli, "skills").stdout)["items"], list)
    assert json.loads(invoke(cli, "daemon", "status").stdout) == {
        "items": [],
        "has_more": False,
    }

    log_detail = json.loads(invoke(cli, "schema", "log").stdout)
    assert log_detail["format_defaults"] == {"tty": "text", "non_tty": "text"}
    text = invoke(cli, "status", "--format", "text")
    assert text.stdout.endswith("-- 0 z 0\n")


def test_empty_collection_and_bounded_collection_have_the_pinned_shape(cli: CliRunner) -> None:
    empty = json.loads(invoke(cli, "status", "--json").stdout)
    assert empty == {"items": [], "has_more": False}

    for index in range(21):
        create_finished(f"prompt-{index}")
    bounded = json.loads(invoke(cli, "status", "--json").stdout)
    assert len(bounded["items"]) == 20
    assert bounded["has_more"] is True

    human = invoke(cli, "status", "--format", "text")
    assert "-- 20 z 21 — --limit żeby zmienić" in human.stdout


def test_plain_requires_an_explicit_limit_and_emits_one_identifier_per_line(
    cli: CliRunner,
) -> None:
    first = create_finished("first")
    second = create_finished("second")
    identifiers = {first.session_id, second.session_id}

    rejected = invoke(cli, "status", "--format", "plain")
    assert rejected.exit_code == vocab.EXIT_USAGE
    assert "--limit" in rejected.stderr

    result = invoke(cli, "status", "--format", "plain", "--limit", "1")
    assert result.exit_code == vocab.EXIT_OK
    assert result.stdout.endswith("\n")
    assert result.stdout.count("\n") == 1
    assert result.stdout.strip() in identifiers
    assert "--" not in result.stdout


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
        "512",
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
        "512",
    )
    _assert_truncated(json.loads(continue_result.stdout), session_id)

    wait_result = invoke(cli, "wait", session_id, "--quiet", "--json", "--max-output", "512")
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
        "512",
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


def test_human_output_escapes_ansi_in_session_and_agent_values(
    cli: CliRunner, state_root: Path
) -> None:
    meta = create_finished("prompt \x1b[31mred\x1b[0m")
    sessions.update_meta(meta.session_id, name="name\x1b[2mhidden\x1b[0m")
    status = invoke(cli, "status", meta.session_id, "--format", "text")
    assert "\x1b" not in status.stdout
    assert "^[" in status.stdout

    description = 'description = "agent \\u001b[31mred\\u001b[0m"\n'
    (state_root / "agents" / "mock.toml").write_text(
        MOCK_ENTRY.replace('name = "Mock Agent"\n', 'name = "Mock Agent"\n' + description),
        encoding="utf-8",
    )
    agents = invoke(cli, "agents", "--format", "text")
    assert "\x1b" not in agents.stdout


def test_no_color_and_dumb_terminal_do_not_change_machine_output(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = invoke(cli, "status", "--json").stdout
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")
    assert invoke(cli, "status", "--json").stdout == baseline
