"""Turn result identity and actionable failure contracts, over real ACP stdio."""

import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from acpc import runner, sessions
from acpc.cli import main
from acpc.registry import AgentRegistry

MOCK = Path(__file__).with_name("mock_agent.py")


@pytest.fixture(autouse=True)
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "state"
    agents = root / "agents"
    agents.mkdir(parents=True)
    (agents / "mock.toml").write_text(
        f'command = "{sys.executable} {MOCK}"\n'
        '[modes]\ndefault = { grants = "read", delegates = true }\n'
    )
    monkeypatch.setenv("ACPC_HOME", str(root))
    return root


def call(*args: str):
    return CliRunner().invoke(main, list(args), catch_exceptions=False)


@pytest.mark.parametrize(
    ("prompt", "served", "expected"),
    [
        ("hello", None, None),
        ("hello", "claude-sonnet-5-5", "claude-sonnet-5-5"),
        ("rawusage:codex", None, "gpt-6-sol"),
        ("rawusage:codex", "claude-sonnet-5-5", "claude-sonnet-5-5"),
    ],
)
def test_result_served_model_sources(prompt, served, expected, monkeypatch):
    if served is not None:
        monkeypatch.setenv("ACPC_MOCK_SERVED_MODEL", served)
    result = call("run", "mock", prompt, "--json", "--quiet")
    assert result.exit_code == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["served_model"] == expected
    assert document["failure"] is None
    status = call("status", document["session_id"], "--json")
    assert json.loads(status.stdout)["served_model"] == expected
    text = call("status", document["session_id"], "--format", "text")
    assert (f"served model: {expected}" in text.stdout) == (expected is not None)


def test_served_model_rotation_preserves_parked_turn(monkeypatch):
    monkeypatch.setenv("ACPC_MOCK_SERVED_MODEL", "first-model-1")
    first = json.loads(call("run", "mock", "hello", "--json", "--quiet").stdout)
    monkeypatch.setenv("ACPC_MOCK_SERVED_MODEL", "second-model-2")
    second = call("continue", first["session_id"], "hello again", "--json", "--quiet")
    assert second.exit_code == 0, second.stderr
    assert json.loads(second.stdout)["served_model"] == "second-model-2"
    parked = sessions.read_turn_meta(first["session_id"], 1)
    assert parked.served_model == "first-model-1"
    assert (
        json.loads(call("status", first["session_id"], "--json").stdout)["served_model"]
        == "second-model-2"
    )
    monkeypatch.delenv("ACPC_MOCK_SERVED_MODEL")
    third = call("continue", first["session_id"], "hello again again", "--json", "--quiet")
    assert json.loads(third.stdout)["served_model"] is None


def test_tagged_metadata_carries_model_and_failure(monkeypatch):
    monkeypatch.setenv("ACPC_MOCK_SERVED_MODEL", "claude-sonnet-5-5")
    result = call("run", "mock", "hello", "--quiet")
    metadata = json.loads(result.stdout.split("<metadata>\n", 1)[1].split("\n", 1)[0])
    assert metadata["served_model"] == "claude-sonnet-5-5"
    result = call("run", "mock", "crash-late:partial", "--quiet")
    metadata = json.loads(result.stdout.split("<metadata>\n", 1)[1].split("\n", 1)[0])
    assert metadata["failure"]["class"] == "adapter_error"


@pytest.mark.parametrize(
    ("requested", "served", "quiet", "note"),
    [
        ("claude-haiku-5-5", "claude-sonnet-5-5", False, True),
        ("claude-sonnet-5-5", "claude-sonnet-5-5-20261001", False, False),
        ("claude-sonnet-5-5-20261001", "claude-sonnet-5-5", False, False),
        ("claude-sonnet-5-5[1m]", "claude-sonnet-5-5", False, False),
        ("claude-sonnet-5-5[1m]", "claude-sonnet-5-5-20261001", False, False),
        ("default", "claude-sonnet-5-5", False, False),
        ("claude-haiku-5-5", "claude-sonnet-5-5", True, False),
    ],
)
def test_served_model_note(requested, served, quiet, note, state_root, monkeypatch):
    # Accept arbitrary requested ids at the adapter boundary.
    del state_root
    monkeypatch.setenv("ACPC_MOCK_SERVED_MODEL", served)
    monkeypatch.setenv("ACPC_MOCK_ACCEPT_ANY_MODEL", "1")
    args = ["run", "mock", "hello", "--model", requested, "--json"]
    if quiet:
        args.append("--quiet")
    result = call(*args)
    assert result.exit_code == 0, result.stderr
    expected = f"acpc: requested model {requested}, mock served {served}"
    assert (expected in result.stderr) == note
    document = json.loads(result.stdout)
    again = call("wait", document["session_id"], "--json")
    assert "requested model" not in again.stderr


def test_background_wait_claims_model_note_once(live_daemon, monkeypatch):
    monkeypatch.setenv("ACPC_MOCK_SERVED_MODEL", "claude-sonnet-5-5")
    monkeypatch.setenv("ACPC_MOCK_ACCEPT_ANY_MODEL", "1")
    receipt = call("run", "mock", "hello", "--model", "claude-haiku-5-5", "--bg", "--json")
    assert receipt.exit_code == 0, receipt.stderr
    document = json.loads(receipt.stdout)
    assert "served_model" not in document and "failure" not in document
    first = call("wait", document["session_id"], "--json")
    assert first.exit_code == 0, first.stderr
    assert json.loads(first.stdout)["served_model"] == "claude-sonnet-5-5"
    assert "requested model claude-haiku-5-5, mock served claude-sonnet-5-5" in first.stderr
    assert "requested model" not in call("wait", document["session_id"], "--json").stderr


@pytest.mark.parametrize(
    ("prompt", "expected", "remedy"),
    [
        ("auth:no", "auth", "run 'mock login'"),
        ("auth-data:no", "auth", "run 'mock login'"),
        ("stop:max_tokens", "adapter_limit", "split the task"),
        ("fail", "adapter_error", "re-run the turn"),
        ("crash-late:partial", "adapter_error", "re-run the turn"),
    ],
)
def test_failure_object_and_log(prompt, expected, remedy):
    result = call("run", "mock", prompt, "--json", "--quiet")
    assert result.exit_code == 1, result.stderr
    document = json.loads(result.stdout)
    failure = document["failure"]
    assert set(failure) == {"class", "message", "next_step"}
    assert failure["class"] == expected
    assert remedy in failure["next_step"]
    assert len(failure["message"]) <= 300 and "\n" not in failure["message"]
    sid = document["session_id"]
    assert json.loads(call("status", sid, "--json").stdout)["failure"] == failure
    assert json.loads(call("wait", sid, "--json", "--quiet").stdout)["failure"] == failure
    text = call("status", sid, "--format", "text").stdout
    assert f"failure  {expected}: {failure['message']}" in text
    assert f"next     {failure['next_step']}" in text
    events = [json.loads(line) for line in sessions.transcript_path(sid).read_text().splitlines()]
    error = next(event for event in events if event.get("type") == "error")
    assert error["class"] == expected
    assert error["next_step"] == failure["next_step"]
    stored = json.loads(sessions.meta_path(sid).read_text())
    assert isinstance(stored["failure"], str)


@pytest.mark.parametrize("auth", [False, True])
def test_configuration_rejection_class(auth, monkeypatch):
    if auth:
        monkeypatch.setenv("ACPC_MOCK_CONFIG_AUTH", "1")
    result = call("run", "mock", "hello", "--model", "unavailable-1", "--json", "--quiet")
    assert result.exit_code == 1, result.stderr
    failure = json.loads(result.stdout)["failure"]
    assert failure["class"] == ("auth" if auth else "adapter_error")
    if auth:
        assert failure["next_step"] == "run 'mock login'"


@pytest.mark.parametrize(
    ("knobs", "expected"),
    [
        ({"ACPC_MOCK_DISCONNECT_BEFORE_PROMPT": "1"}, "connection"),
        (
            {
                "ACPC_MOCK_LIMIT_PROMPTS": "1",
                "ACPC_MOCK_LIMIT_VENDOR": "codex",
                "ACPC_MOCK_LIMIT_CODEX_CASE": "not_included",
            },
            "adapter_error",
        ),
    ],
)
def test_direct_closed_pipe_and_unrecognized_limit_classes(knobs, expected, monkeypatch):
    for name, value in knobs.items():
        monkeypatch.setenv(name, value)
    result = call("run", "mock", "echo:x", "--mode", "default", "--limit-wait", "0s", "--json")
    assert result.exit_code == 1, result.stderr
    assert json.loads(result.stdout)["failure"]["class"] == expected


def test_missing_adapter_is_acpc_failure(state_root):
    (state_root / "agents" / "missing.toml").write_text(
        'command = "nonexistent-acpc-slice4-adapter"\ninstall_command = "install-the-adapter"\n'
    )
    resolution = AgentRegistry().resolve_call("missing", permissions="read")
    meta = sessions.create_session(
        entry="missing",
        base_adapter="missing",
        prompt="x",
        resolution=runner.resolution_payload(resolution, cwd=str(state_root)),
        target=runner.call_target(resolution),
    )
    with pytest.raises(runner.AdapterUnavailable):
        runner.execute_turn(meta.session_id, runner.TurnRequest(resolution=resolution, prompt="x"))
    result = call("wait", meta.session_id, "--json", "--quiet")
    assert result.exit_code == 1, result.stderr
    failure = json.loads(result.stdout)["failure"]
    assert failure["class"] == "acpc"
    assert failure["next_step"] == resolution.entry.missing_binary_error()


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("rate_limit", "rate_limit"),
        ("max_tokens", "adapter_limit"),
        ("max_turn_requests", "adapter_limit"),
        ("error", "adapter_error"),
    ],
)
def test_legacy_failure_string(reason, expected):
    document = json.loads(call("run", "mock", "hello", "--json", "--quiet").stdout)
    path = sessions.meta_path(document["session_id"])
    stored = json.loads(path.read_text())
    stored["status"] = "failed"
    stored["failure"] = "old failure"
    stored["stop_reason"] = reason
    stored.pop("failure_class")
    stored.pop("failure_next_step")
    path.write_text(json.dumps(stored))
    assert sessions.read_meta(document["session_id"]).failure_class == expected
    failure = json.loads(call("status", document["session_id"], "--json").stdout)["failure"]
    assert failure == {"class": expected, "message": "old failure", "next_step": None}


def test_cancel_after_records_no_failure():
    result = call("run", "mock", "slow:30", "--cancel-after", "0.1", "--json", "--quiet")
    assert result.exit_code == 1, result.stderr
    document = json.loads(result.stdout)
    assert document["status"] == "canceled"
    assert document["failure"] is None


def test_permission_denied_result_has_no_failure(state_root):
    entry = state_root / "agents" / "mock.toml"
    entry.write_text(entry.read_text() + 'yolo = { grants = "all", delegates = false }\n')
    result = call(
        "run", "mock", "run the perm scenario", "--permissions", "read", "--json", "--quiet"
    )
    assert result.exit_code == 2, result.stderr
    document = json.loads(result.stdout)
    assert document["stop_reason"] == "permission_denied"
    assert document["failure"] is None


@pytest.mark.timeout(45)
def test_wait_reports_earlier_model_after_continue_without_claiming_new_turn(
    state_root, monkeypatch
):
    monkeypatch.setenv("ACPC_MOCK_SERVED_MODEL", "first-model-1")
    prompt = state_root / "prompt.txt"
    prompt.write_text("a" * 524288)
    started = call(
        "run",
        "mock",
        "--prompt-file",
        str(prompt),
        "--model",
        "mock-sonnet-5",
        "--timeout",
        "0.1",
        "--json",
        "--quiet",
    )
    assert started.exit_code == 124, started.stderr
    sid = json.loads(started.stderr.splitlines()[-1])["error"]["context"]["session_id"]
    deadline = time.monotonic() + 15
    while not sessions.load(sid).is_finished:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    fifo = state_root / "output.fifo"
    os.mkfifo(fifo)
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    waiter = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "acpc",
            "wait",
            sid,
            "--json",
            "--max-output",
            "0",
            "--output-file",
            str(fifo),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=os.environ.copy(),
        text=True,
    )
    try:
        # Filled FIFO is the filesystem boundary: result chosen, delivery still blocked.
        assert select.select([reader], [], [], 15)[0]
        assert waiter.poll() is None
        monkeypatch.setenv("ACPC_MOCK_SERVED_MODEL", "second-model-2")
        second = call("continue", sid, "hello", "--json", "--quiet")
        assert second.exit_code == 0, second.stderr
        assert json.loads(second.stdout)["served_model"] == "second-model-2"
        before = sessions.read_meta(sid).to_dict()
        os.set_blocking(reader, True)
        with os.fdopen(reader, "rb", closefd=False) as stream:
            earlier = json.loads(stream.read())
        stdout, stderr = waiter.communicate(timeout=15)
        assert waiter.returncode == 0, stderr
        assert stdout == ""
        assert earlier["turn"] == 1
        assert earlier["served_model"] == "first-model-1"
        assert "requested model" not in stderr
        assert sessions.read_meta(sid).to_dict() == before
    finally:
        os.close(reader)
        if waiter.poll() is None:
            waiter.kill()
            waiter.wait(timeout=10)
