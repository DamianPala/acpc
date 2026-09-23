"""Behavioral tests for usage-limit recognition and the wait-through-it turn.

`classify_limit` is pure (`acpc.limits`), so its table is exercised directly.
Everything else — a turn moving to `waiting`, the daemon resending the
prompt, `limit_wait_max`, `cancel` dropping the wait — is exercised through
the CLI against the mock adapter's `ACPC_MOCK_LIMIT_*` knobs (mock_agent.py),
same as the rest of this suite.
"""

import json
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from acp import RequestError
from click.testing import CliRunner

from acpc import limits, runner, sessions, vocab
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


def _await_alias(alias: str, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return sessions.resolve_selector(alias)
        except sessions.SessionError:
            time.sleep(0.02)
    pytest.fail(f"no session was named {alias!r}")


def _wait_for_state(session_id: str, state: str, timeout: float = 15.0) -> sessions.SessionMeta:
    deadline = time.monotonic() + timeout
    last: sessions.SessionMeta | None = None
    while time.monotonic() < deadline:
        last = sessions.load(session_id)
        if last.state == state:
            return last
        time.sleep(0.02)
    pytest.fail(f"session {session_id} never reached {state} (last: {last and last.state})")


def _run_in_background_thread(cli: CliRunner, *args: str) -> tuple[threading.Thread, dict]:
    holder: dict = {}

    def call() -> None:
        try:
            holder["result"] = invoke(cli, *args)
        except BaseException as error:  # noqa: BLE001 - surfaced on join
            holder["error"] = error

    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    return thread, holder


def _freeze_runner_clock(monkeypatch: pytest.MonkeyPatch, now: datetime) -> None:
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = now.astimezone(UTC)
            return instant if tz is None else instant.astimezone(tz)

    monkeypatch.setattr(runner, "datetime", FrozenDateTime)


def _codex_retry_text(at: datetime) -> str:
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    suffix = (
        "th" if 11 <= at.day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(at.day % 10, "th")
    )
    hour = at.hour % 12 or 12
    ampm = "AM" if at.hour < 12 else "PM"
    return f"{months[at.month - 1]} {at.day}{suffix}, {at.year} {hour}:{at.minute:02d} {ampm}"


# --- classify_limit: parser table --------------------------------------------

_NOW = datetime(2026, 9, 20, 10, 0, 0, tzinfo=UTC)


def _limit_error(text: str, *, with_data: bool = True) -> RequestError:
    data = {"errorKind": "rate_limit"} if with_data else None
    return RequestError(-32603, f"Internal error: You've hit your session limit · {text}", data)


def _codex_limit_error(
    text: str, *, field: str = "message", codex_error_info: str | None = "usageLimitExceeded"
) -> RequestError:
    data = {field: text}
    if codex_error_info is not None:
        data["codexErrorInfo"] = codex_error_info
    return RequestError(-32603, "Internal error", data)


@pytest.mark.parametrize(
    ("clause", "expected"),
    [
        ("resets 11:10pm (Europe/Warsaw)", datetime(2026, 9, 20, 21, 10, tzinfo=UTC)),
        ("resets 9am (America/New_York)", datetime(2026, 9, 20, 13, 0, tzinfo=UTC)),
        ("resets Sep 24 at 11am (Europe/Warsaw)", datetime(2026, 9, 24, 9, 0, tzinfo=UTC)),
        ("resets Sep 24, 11am (Europe/Warsaw)", datetime(2026, 9, 24, 9, 0, tzinfo=UTC)),
    ],
)
def test_classify_limit_parses_the_resets_clause(clause: str, expected: datetime) -> None:
    observation = limits.classify_limit(_limit_error(clause), None, _NOW)

    assert observation is not None
    assert observation.resume_at == expected
    assert observation.source == "text"
    assert observation.reason == "rate_limit"


def test_classify_limit_returns_no_time_for_text_with_no_resets_clause() -> None:
    observation = limits.classify_limit(_limit_error("no return time here"), None, _NOW)

    assert observation is not None
    assert observation.resume_at is None
    assert observation.source == "error_kind"


def test_classify_limit_returns_no_time_for_an_unknown_zone() -> None:
    observation = limits.classify_limit(
        _limit_error("resets 11am (Mars/Colony)", with_data=False), None, _NOW
    )

    assert observation is not None
    assert observation.resume_at is None
    assert observation.source == "text"


@pytest.mark.parametrize("apostrophe", ["'", "’"])
def test_classify_codex_limit_parses_a_local_time_without_a_date(apostrophe: str) -> None:
    now = datetime(2026, 9, 24, 18, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    error = _codex_limit_error(
        f"You{apostrophe}ve hit your usage limit. Upgrade to Pro or try again at 10:47 PM."
    )

    observation = limits.classify_limit(error, None, now)

    assert observation is not None
    assert observation.reason == "rate_limit"
    assert observation.source == "text"
    assert observation.resume_at == datetime(2026, 9, 25, 5, 47, tzinfo=UTC)


@pytest.mark.parametrize(
    ("date_text", "expected"),
    [
        (
            "Sep 24th, 2026 10:47 PM",
            datetime(2026, 9, 25, 5, 47, tzinfo=UTC),
        ),
        (
            "Sep 24th 10:47 PM",
            datetime(2026, 9, 25, 5, 47, tzinfo=UTC),
        ),
        # After the end of DST: the offset is the zone's rule for that date.
        ("Nov 2nd, 2026 10:47 AM", datetime(2026, 11, 2, 18, 47, tzinfo=UTC)),
        ("Jan 1st, 2027 1:00 AM", datetime(2027, 1, 1, 9, 0, tzinfo=UTC)),
    ],
)
def test_classify_codex_limit_parses_a_dated_local_time(date_text: str, expected: datetime) -> None:
    now = datetime(2026, 9, 20, 3, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    error = _codex_limit_error(f"You've hit your usage limit. Try again at {date_text}.")

    observation = limits.classify_limit(error, None, now)

    assert observation is not None
    assert observation.source == "text"
    assert observation.resume_at == expected


def test_classify_codex_limit_reads_the_machine_zone_across_dst(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The runner passes a UTC `now`, so the process zone applies to the text.
    text = "You've hit your usage limit. Try again at Oct 26th, 2026 10:47 AM."
    try:
        with monkeypatch.context() as patch:
            patch.setenv("TZ", "Europe/Warsaw")
            time.tzset()
            observation = limits.classify_limit(
                _codex_limit_error(text), None, datetime(2026, 10, 24, 10, 0, tzinfo=UTC)
            )
    finally:
        time.tzset()

    assert observation is not None
    assert observation.resume_at == datetime(2026, 10, 26, 9, 47, tzinfo=UTC)


def test_classify_codex_limit_keeps_a_same_day_time_that_is_already_past() -> None:
    now = datetime(2026, 9, 24, 23, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    error = _codex_limit_error("You've hit your usage limit. Try again at 10:47 PM.")

    observation = limits.classify_limit(error, None, now)

    assert observation is not None
    resume_at = observation.resume_at
    assert resume_at is not None
    assert resume_at == datetime(2026, 9, 25, 5, 47, tzinfo=UTC)
    assert resume_at < now.astimezone(UTC)


@pytest.mark.parametrize(
    "text",
    [
        "You've hit your usage limit. Try again later.",
        "You've hit your usage limit.",
    ],
)
def test_classify_codex_limit_without_a_time_stays_a_limit(text: str) -> None:
    observation = limits.classify_limit(_codex_limit_error(text), None, _NOW)

    assert observation is not None
    assert observation.reason == "rate_limit"
    assert observation.source == "text"
    assert observation.resume_at is None


@pytest.mark.parametrize(
    "text",
    [
        "To use Codex with your ChatGPT plan, upgrade to Plus: access details.",
        "Quota exceeded. Check your plan and billing details.",
    ],
)
def test_classify_codex_ignores_permanent_access_failures(text: str) -> None:
    assert limits.classify_limit(_codex_limit_error(text), None, _NOW) is None


def test_classify_codex_ignores_usage_limit_code_with_unknown_text() -> None:
    error = _codex_limit_error("Internal error", field="message")

    assert limits.classify_limit(error, None, _NOW) is None


def test_classify_codex_reads_limit_text_from_additional_details() -> None:
    error = RequestError(
        -32603,
        "Internal error",
        {
            "message": "Internal error",
            "additionalDetails": "You've hit your usage limit. Try again later.",
            "codexErrorInfo": "usageLimitExceeded",
        },
    )

    observation = limits.classify_limit(error, None, _NOW)

    assert observation is not None
    assert observation.source == "text"
    assert observation.resume_at is None


def test_classify_text_in_data_message_without_codex_code_like_claude() -> None:
    error = _codex_limit_error(
        "You've hit your usage limit. Try again later.", codex_error_info=None
    )

    observation = limits.classify_limit(error, None, _NOW)

    assert observation is not None
    assert observation.source == "text"
    assert observation.resume_at is None


@pytest.mark.parametrize(
    "prefix",
    [
        "You've hit your",
        "You’ve hit your",
        "You've reached your",
        "You’ve reached your",
    ],
)
def test_classify_claude_prefixes_accept_both_apostrophes(prefix: str) -> None:
    error = RequestError(-32603, f"Internal error: {prefix} session limit", None)

    observation = limits.classify_limit(error, None, _NOW)

    assert observation is not None
    assert observation.source == "text"
    assert observation.resume_at is None


def test_classify_limit_prefers_rate_limit_info_over_text() -> None:
    reset_at = _NOW.timestamp() + 30
    observation = limits.classify_limit(
        _limit_error("resets 11:10pm (Europe/Warsaw)"),
        {"status": "rejected", "resetsAt": reset_at},
        _NOW,
    )

    assert observation is not None
    assert observation.source == "rate_limit_info"
    assert observation.resume_at == datetime.fromtimestamp(reset_at, tz=UTC)


def test_classify_limit_recognizes_error_kind_alone_with_no_vendor_text() -> None:
    error = RequestError(-32603, "Internal error", {"errorKind": "rate_limit"})

    observation = limits.classify_limit(error, None, _NOW)

    assert observation is not None
    assert observation.source == "error_kind"
    assert observation.resume_at is None


def test_classify_limit_recognizes_rate_limit_info_with_no_reset_time() -> None:
    observation = limits.classify_limit(None, {"status": "rejected"}, _NOW)

    assert observation is not None
    assert observation.source == "rate_limit_info"
    assert observation.resume_at is None


def test_classify_limit_ignores_permanent_account_limits() -> None:
    error = RequestError(-32603, "Internal error: You're out of usage credits", None)

    assert limits.classify_limit(error, None, _NOW) is None


def test_classify_limit_ignores_an_unrelated_failure() -> None:
    error = RequestError(-32603, "Internal error: upstream connection reset", None)

    assert limits.classify_limit(error, None, _NOW) is None


# --- behavior: 1. wait through the limit and resume in the same turn --------


def test_run_waits_through_a_codex_limit_and_resumes_in_the_same_turn(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch, state_root: Path
) -> None:
    (state_root / "config.toml").write_text('limit_wait_max = "30s"\n', encoding="utf-8")
    target = datetime.now().astimezone().replace(second=0, microsecond=0) + timedelta(minutes=1)
    _freeze_runner_clock(monkeypatch, target - timedelta(seconds=1))
    monkeypatch.setenv("ACPC_MOCK_LIMIT_VENDOR", "codex")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_CODEX_CASE", "timed")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_CODEX_AT", _codex_retry_text(target))
    monkeypatch.setenv("ACPC_MOCK_LIMIT_PROMPTS", "1")

    thread, holder = _run_in_background_thread(
        cli, "run", "mock", "echo:x", "--json", "--quiet", "--name", "codex-limit-wait"
    )
    session_id = _await_alias("codex-limit-wait")
    meta = _wait_for_state(session_id, "waiting", timeout=3)

    assert meta.limit is not None
    assert meta.limit["source"] == "text"
    assert meta.limit["resume_at"] is not None
    assert datetime.fromisoformat(meta.limit["resume_at"]) == target.astimezone(UTC)
    assert meta.turns == 1

    thread.join(timeout=20)
    assert not thread.is_alive()
    assert "error" not in holder, holder
    result = holder["result"]
    assert result.exit_code == vocab.EXIT_OK, result.stderr
    document = json.loads(result.stdout)
    assert document["status"] == "succeeded"
    assert document["answer"] == "x"
    assert document["turn"] == 1
    assert document["limit"]["source"] == "text"
    events = [
        json.loads(line) for line in invoke(cli, "log", session_id, "--json").stdout.splitlines()
    ]
    transitions = [
        (event.get("from"), event.get("to")) for event in events if event["type"] == "state"
    ]
    assert ("running", "waiting") in transitions
    assert ("waiting", "running") in transitions


def test_run_codex_try_again_later_fails_with_an_unknown_resume_time(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPC_MOCK_LIMIT_VENDOR", "codex")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_CODEX_CASE", "later")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_PROMPTS", "1")

    result = invoke(cli, "run", "mock", "echo:x", "--json", "--quiet")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    document = json.loads(result.stdout)
    assert document["status"] == "failed"
    assert document["stop_reason"] == "rate_limit"
    assert document["limit"]["source"] == "text"
    assert document["limit"]["resume_at"] is None


def test_run_codex_permanent_access_error_fails_without_a_limit(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPC_MOCK_LIMIT_VENDOR", "codex")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_CODEX_CASE", "not_included")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_PROMPTS", "1")

    result = invoke(cli, "run", "mock", "echo:x", "--json", "--quiet")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    document = json.loads(result.stdout)
    assert document["status"] == "failed"
    assert document.get("limit") is None


def test_run_waits_through_a_limit_and_resumes_the_original_prompt(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPC_MOCK_LIMIT_PROMPTS", "1")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_META", "1")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_RESET_S", "1")

    thread, holder = _run_in_background_thread(
        cli, "run", "mock", "echo:x", "--json", "--quiet", "--name", "limit-wait-1"
    )
    session_id = _await_alias("limit-wait-1")
    meta = _wait_for_state(session_id, "waiting")

    assert meta.limit is not None
    assert meta.limit["reason"] == "rate_limit"
    assert meta.limit["auto_continue"] is True
    assert meta.limit["source"] == "rate_limit_info"
    assert meta.limit["resume_at"] is not None
    assert meta.turns == 1

    status_result = invoke(cli, "status", session_id, "--json")
    status_document = json.loads(status_result.stdout)
    assert status_document["status"] == "waiting"
    assert "on_limit" not in status_document
    assert "limit_waited_seconds" not in status_document

    timeout_result = invoke(cli, "wait", session_id, "--timeout", "0.2", "--json", "--quiet")
    assert timeout_result.exit_code == vocab.EXIT_TIMEOUT
    assert timeout_result.stdout == ""
    assert json.loads(timeout_result.stderr)["error"]["context"]["status"] == "waiting"

    resume_at = datetime.fromisoformat(meta.limit["resume_at"])

    thread.join(timeout=30)
    assert "error" not in holder, holder
    result = holder["result"]
    assert result.exit_code == vocab.EXIT_OK, result.stderr
    document = json.loads(result.stdout)
    assert document["status"] == "succeeded"
    assert document["answer"] == "x"
    assert document["turn"] == 1
    assert document["limit"]["auto_continue"] is False
    assert "on_limit" not in document
    assert "limit_waited_seconds" not in document

    final = sessions.load(session_id)
    assert final.turns == 1
    assert final.finished_at is not None
    # Resumption is `resume_at` plus the fixed 5 s cushion, no jitter: a
    # generous 5-10 s window catches scheduling slack without tolerating a
    # regression back to the old 5-30 s jittered range.
    resumed_after = final.finished_at - resume_at.timestamp()
    assert 5.0 <= resumed_after <= 10.0, resumed_after

    raw_meta = json.loads(sessions.meta_path(session_id).read_text(encoding="utf-8"))
    assert "on_limit" not in raw_meta
    assert "limit_waited_seconds" not in raw_meta
    events = [
        json.loads(line) for line in invoke(cli, "log", session_id, "--json").stdout.splitlines()
    ]
    types = [event["type"] for event in events]
    assert types.count("limit") == 1
    assert types.count("prompt") == 0  # prompts are not logged as their own event type
    state_events = [event for event in events if event["type"] == "state"]
    assert {"from": "running", "to": "waiting"} in [
        {"from": event["from"], "to": event["to"]} for event in state_events
    ]
    assert {"from": "waiting", "to": "running"} in [
        {"from": event["from"], "to": event["to"]} for event in state_events
    ]


# --- behavior: 2/8. text-only reset time, canceled before it resumes -------


def test_run_recognizes_a_text_only_limit_and_cancel_drops_the_wait(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ACPC_MOCK_LIMIT_PROMPTS", "1")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_RESET_S", "60")
    steering_log = tmp_path / "steering.log"
    monkeypatch.setenv("ACPC_MOCK_STEERING_LOG", str(steering_log))

    # `cancel` signals the process hosting the turn by pid; a plain foreground
    # `run` executes in-process on this thread, so `--timeout` is what routes
    # the turn through the real, separate `acpc.direct_worker` subprocess that
    # a pid-based signal can actually reach (see `_kill_turn_worker` in
    # test_conformance.py for the same requirement).
    thread, holder = _run_in_background_thread(
        cli,
        "run",
        "mock",
        "echo:x",
        "--json",
        "--quiet",
        "--name",
        "limit-wait-2",
        "--timeout",
        "30",
    )
    session_id = _await_alias("limit-wait-2")
    meta = _wait_for_state(session_id, "waiting")
    assert meta.limit is not None
    assert meta.limit["source"] == "text"
    assert meta.limit["resume_at"] is not None

    cancel_result = invoke(cli, "cancel", session_id, "--json")
    assert cancel_result.exit_code == vocab.EXIT_OK
    document = json.loads(cancel_result.stdout)
    assert document["status"] == "canceled"
    assert document["changed"] is True
    assert document["stop_reason"] == "canceled"

    thread.join(timeout=15)
    assert "error" not in holder, holder
    assert holder["result"].exit_code == vocab.EXIT_CANCELLED

    events = [
        json.loads(line) for line in invoke(cli, "log", session_id, "--json").stdout.splitlines()
    ]
    assert sum(1 for event in events if event["type"] == "limit") == 1

    # No resend happened: exactly the original prompt was ever delivered.
    final = sessions.load(session_id)
    delivered = [record["prompt"] for record in final.extra.get("delivered_prompts", [])]
    assert delivered == ["echo:x"]

    # The adapter never received ACP `session/cancel` for the waiting session
    # (SPEC.md `cancel`: a wait is dropped without contacting the adapter).
    log_text = steering_log.read_text(encoding="utf-8") if steering_log.exists() else ""
    assert f"cancel:{final.adapter_session_id}" not in log_text


# --- behavior: 3. a second send after a limit uses the continuation text ----


def test_resend_after_progress_uses_the_continuation_instruction(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPC_MOCK_LIMIT_PROMPTS", "1")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_META", "1")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_RESET_S", "1")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_AFTER_TEXT", "1")

    result = invoke(cli, "run", "mock", "echo:tail", "--json", "--quiet")

    assert result.exit_code == vocab.EXIT_OK, result.stderr
    document = json.loads(result.stdout)
    assert document["status"] == "succeeded"
    assert "working on it" in document["answer"]
    # Pinned literally, not just against the function under test (slice 23 review debt).
    assert "interrupted by a usage limit" in document["answer"]
    assert runner.continuation_instruction("rate_limit") in document["answer"]
    prose = invoke(cli, "log", document["session_id"], "--since", "0", "--prose", "--quiet")
    assert prose.exit_code == vocab.EXIT_OK, prose.stderr
    assert prose.stdout == document["answer"]
    assert "working on it\n\nWorking through:" in prose.stdout
    assert "working on it\n\n\nWorking through:" not in prose.stdout


def test_limit_wait_boundary_matches_answer_and_prose(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPC_MOCK_LIMIT_PROMPTS", "1")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_AFTER_TEXT", "1")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_META", "1")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_RESET_S", "1")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_SIMPLE_RESUME", "1")

    result = invoke(cli, "run", "mock", "echo:tail", "--json", "--quiet")

    assert result.exit_code == vocab.EXIT_OK, result.stderr
    answer = json.loads(result.stdout)["answer"]
    assert answer == "working on it\n\nresumed"
    prose = invoke(
        cli,
        "log",
        json.loads(result.stdout)["session_id"],
        "--since",
        "0",
        "--prose",
        "--quiet",
    )
    assert prose.exit_code == vocab.EXIT_OK, prose.stderr
    assert prose.stdout == answer
    assert prose.stdout.count("\n\n") == 1


# --- behavior: 4/5/6. limit_wait_max = "0s", unknown time, and the wait cap


def test_limit_wait_max_zero_ends_the_turn_immediately(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch, state_root: Path
) -> None:
    (state_root / "config.toml").write_text('limit_wait_max = "0s"\n', encoding="utf-8")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_PROMPTS", "1")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_META", "1")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_RESET_S", "60")

    result = invoke(cli, "run", "mock", "echo:x", "--json", "--quiet")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    document = json.loads(result.stdout)
    assert document["status"] == "failed"
    assert document["stop_reason"] == "rate_limit"
    assert document["limit"]["auto_continue"] is False
    assert document["limit"]["resume_at"] is not None

    session_id = document["session_id"]
    # The direct path spawns a fresh mock per turn, which would limit again.
    monkeypatch.delenv("ACPC_MOCK_LIMIT_PROMPTS")
    continued = invoke(cli, "continue", session_id, "again", "--json", "--quiet")
    assert continued.exit_code == vocab.EXIT_OK, continued.stderr


def test_no_resets_clause_fails_with_an_unknown_resume_time(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPC_MOCK_LIMIT_PROMPTS", "1")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_NO_TIME", "1")

    result = invoke(cli, "run", "mock", "echo:x", "--json", "--quiet")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    document = json.loads(result.stdout)
    assert document["status"] == "failed"
    assert document["stop_reason"] == "rate_limit"
    assert document["limit"]["resume_at"] is None
    assert document["limit"]["source"] == "error_kind"


def test_limit_wait_max_caps_how_long_a_turn_will_wait(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch, state_root: Path
) -> None:
    (state_root / "config.toml").write_text('limit_wait_max = "1s"\n', encoding="utf-8")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_PROMPTS", "1")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_META", "1")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_RESET_S", "30")

    result = invoke(cli, "run", "mock", "echo:x", "--json", "--quiet")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    document = json.loads(result.stdout)
    assert document["status"] == "failed"
    assert document["stop_reason"] == "rate_limit"
    assert document["limit"]["resume_at"] is not None


# --- behavior: 7. recognition from the text prefix alone -------------------


def test_no_data_still_recognizes_the_limit_from_the_text_prefix(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPC_MOCK_LIMIT_PROMPTS", "1")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_META", "1")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_RESET_S", "1")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_NO_DATA", "1")

    thread, holder = _run_in_background_thread(
        cli, "run", "mock", "echo:x", "--json", "--quiet", "--name", "limit-wait-7"
    )
    session_id = _await_alias("limit-wait-7")
    meta = _wait_for_state(session_id, "waiting")
    assert meta.limit is not None
    assert meta.limit["reason"] == "rate_limit"

    thread.join(timeout=15)
    assert "error" not in holder, holder
    assert holder["result"].exit_code == vocab.EXIT_OK


# --- behavior: 9/10. steer conflicts on a session with a turn in flight ----


def test_steer_in_place_on_a_waiting_session_is_a_conflict(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPC_MOCK_LIMIT_PROMPTS", "1")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_META", "1")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_RESET_S", "60")
    monkeypatch.setenv("ACPC_MOCK_STEERING", "1")

    # `--timeout` routes the turn through a separate `acpc.direct_worker`
    # process, the only direct host `cancel` can reach (see test 2/8 above).
    thread, holder = _run_in_background_thread(
        cli,
        "run",
        "mock",
        "echo:x",
        "--json",
        "--quiet",
        "--name",
        "limit-wait-9",
        "--timeout",
        "30",
    )
    session_id = _await_alias("limit-wait-9")
    _wait_for_state(session_id, "waiting")

    result = invoke(cli, "steer", session_id, "y", "--steer-mode", "in-place", "--json", "--quiet")
    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    envelope = json.loads(result.stderr)["error"]
    assert envelope["kind"] == "conflict"
    assert "cancel-then-start" in envelope["hint"]
    assert envelope["context"]["correction_result"]["message_state"] == "not_delivered"

    continue_result = invoke(cli, "continue", session_id, "z", "--json", "--quiet")
    assert continue_result.exit_code == vocab.EXIT_AGENT_ERROR
    assert json.loads(continue_result.stderr)["error"]["kind"] == "conflict"

    invoke(cli, "cancel", session_id, "--json")
    thread.join(timeout=15)
    assert "error" not in holder, holder
    assert holder["result"].exit_code == vocab.EXIT_CANCELLED


def test_continue_conflict_hint_on_a_waiting_session_names_cancel_then_start(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SPEC.md `continue`: a `waiting` session has nothing in flight for an
    in-place `steer` to correct, so the conflict's hint names
    `--steer-mode cancel-then-start` — and the hinted command actually works,
    unlike a plain `acpc steer <id> "<instruction>"` on this session (see
    `test_steer_in_place_on_a_waiting_session_is_a_conflict` above)."""
    monkeypatch.setenv("ACPC_MOCK_LIMIT_PROMPTS", "1")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_META", "1")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_RESET_S", "60")

    thread, holder = _run_in_background_thread(
        cli,
        "run",
        "mock",
        "echo:x",
        "--json",
        "--quiet",
        "--name",
        "limit-wait-hint",
        "--timeout",
        "30",
    )
    session_id = _await_alias("limit-wait-hint")
    _wait_for_state(session_id, "waiting")

    continue_result = invoke(cli, "continue", session_id, "z", "--json", "--quiet")
    assert continue_result.exit_code == vocab.EXIT_AGENT_ERROR
    envelope = json.loads(continue_result.stderr)["error"]
    assert envelope["kind"] == "conflict"
    hint = envelope["hint"]
    assert f"acpc steer {session_id}" in hint
    assert "--steer-mode cancel-then-start" in hint
    assert f"acpc wait {session_id}" in hint

    # A fresh turn 2's own limit counter restarts at 0; left set, the trigger
    # would fail turn 2's first call too, contradicting "never a second
    # conflict" below with a different error.
    monkeypatch.delenv("ACPC_MOCK_LIMIT_PROMPTS", raising=False)

    steered = invoke(
        cli, "steer", session_id, "y", "--steer-mode", "cancel-then-start", "--json", "--quiet"
    )
    # The hinted command must actually work — never a second `conflict`
    # (unlike plain `acpc steer` on this same session, asserted above).
    assert steered.exit_code == vocab.EXIT_OK, steered.stderr

    thread.join(timeout=15)
    assert "error" not in holder, holder


def test_steer_cancel_then_start_on_a_waiting_session_starts_turn_two(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario 9, second half: `cancel-then-start` on a `waiting` session
    cancels turn 1 without contacting the adapter (same as plain `cancel`)
    and starts turn 2 with the instruction, exactly like steering a
    `running` turn.
    """
    monkeypatch.setenv("ACPC_MOCK_LIMIT_PROMPTS", "1")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_META", "1")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_RESET_S", "60")

    # A short `--timeout` routes turn 1 through a separate, detached
    # `acpc.direct_worker` process (the only direct host `steer`'s cancel
    # step can signal by pid, see test 2/8 above) and returns quickly with a
    # `timeout` result; the worker itself keeps running past that, the same
    # way `test_timeout_direct_worker_survives_closing_the_client_pty` in
    # test_cli_run.py relies on it surviving its own client's exit. Nothing
    # is left running in this test's own process, so there is no supervisor
    # thread left to race the `steer` call below over the same session.
    result = invoke(
        cli,
        "run",
        "mock",
        "echo:x",
        "--json",
        "--quiet",
        "--name",
        "limit-wait-cts",
        "--timeout",
        "2",
    )
    assert result.exit_code == vocab.EXIT_TIMEOUT
    session_id = _await_alias("limit-wait-cts")
    _wait_for_state(session_id, "waiting")

    # Turn 2 starts a fresh mock-agent process (the one behind turn 1 is
    # gone once canceled), whose own `_limit_prompt_counts` restarts at 0;
    # left set, `ACPC_MOCK_LIMIT_PROMPTS=1` would fail turn 2's first call
    # too. Turn 2 is meant to succeed here, so the trigger comes off before
    # steering it.
    monkeypatch.delenv("ACPC_MOCK_LIMIT_PROMPTS", raising=False)

    result = invoke(
        cli,
        "steer",
        session_id,
        "y-steer-instruction",
        "--steer-mode",
        "cancel-then-start",
        "--json",
        "--quiet",
    )
    assert result.exit_code == vocab.EXIT_OK, result.stderr
    document = json.loads(result.stdout)
    assert document["status"] == "succeeded"
    assert document["turn"] == 2
    assert "y-steer-instruction" in document["answer"]

    assert sessions.read_turn_meta(session_id, 1).state == "canceled"
    assert sessions.load(session_id).turns == 2


def test_steer_cancel_then_start_fails_turn_two_when_limit_wait_max_is_zero(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch, state_root: Path
) -> None:
    """Scenario 9 combined with a lowered `limit_wait_max`: the cap is read
    fresh when each turn starts sending, so tightening it between turn 1 and
    turn 2 fails only the new turn `cancel-then-start` opens; turn 1 already
    waited under the old, generous default and is simply canceled, never
    given the chance to fail on its own limit.
    """
    monkeypatch.setenv("ACPC_MOCK_LIMIT_PROMPTS", "2")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_META", "1")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_RESET_S", "60")

    result = invoke(
        cli,
        "run",
        "mock",
        "echo:x",
        "--json",
        "--quiet",
        "--name",
        "limit-wait-cts-fail",
        "--timeout",
        "2",
    )
    assert result.exit_code == vocab.EXIT_TIMEOUT
    session_id = _await_alias("limit-wait-cts-fail")
    _wait_for_state(session_id, "waiting")

    (state_root / "config.toml").write_text('limit_wait_max = "0s"\n', encoding="utf-8")

    result = invoke(
        cli,
        "steer",
        session_id,
        "z",
        "--steer-mode",
        "cancel-then-start",
        "--json",
        "--quiet",
    )
    # `steer`'s redirect turn reports a failure as an error, not a result
    # document (`emit_failure_result=False`, unlike a plain `run`/`continue`
    # with a limit that fails); the session record is the source of truth
    # for what turn 2 actually ended as.
    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    assert result.stdout == ""
    # A directly spawned adapter's own stderr comes back on acpc's stderr
    # too (the hint on the error says as much), so the JSON envelope is the
    # last line, not the whole stream.
    envelope = json.loads(result.stderr.splitlines()[-1])["error"]
    assert envelope["context"]["status"] == "failed"
    assert envelope["context"]["correction_result"]["target_turn"] == 1

    assert sessions.read_turn_meta(session_id, 1).state == "canceled"
    final = sessions.load(session_id)
    assert final.turns == 2
    assert final.state == "failed"
    assert final.stop_reason == "rate_limit"


# --- behavior: 11. --timeout during the wait reports waiting, not failed ---


def test_run_timeout_while_waiting_reports_context_status_waiting(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPC_MOCK_LIMIT_PROMPTS", "1")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_META", "1")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_RESET_S", "30")

    # The worker must spawn the mock and reach `waiting` before the deadline;
    # one second is not reliably enough for that under a loaded suite.
    result = invoke(cli, "run", "mock", "echo:x", "--json", "--quiet", "--timeout", "3")

    assert result.exit_code == vocab.EXIT_TIMEOUT
    assert result.stdout == ""
    envelope = json.loads(result.stderr)["error"]
    assert envelope["context"]["status"] == "waiting"


# --- behavior: 15. an untouched turn carries no limit field ----------------


def test_a_session_with_no_limit_reports_null_and_no_limit_field(cli: CliRunner) -> None:
    result = invoke(cli, "run", "mock", "echo:no-limit", "--json", "--quiet")

    assert result.exit_code == vocab.EXIT_OK
    document = json.loads(result.stdout)
    assert "limit" not in document

    status_result = invoke(cli, "status", document["session_id"], "--json")
    assert json.loads(status_result.stdout)["limit"] is None


# --- behavior: 14. schema has no --on-limit; the waiting status stays ------


def test_schema_run_has_no_on_limit_flag_and_status_keeps_the_waiting_status(
    cli: CliRunner,
) -> None:
    run_schema = json.loads(invoke(cli, "schema", "run").stdout)
    assert all(f["name"] != "on-limit" for f in run_schema["flags"])

    status_schema = json.loads(invoke(cli, "schema", "status").stdout)
    assert "waiting" in status_schema["output"]["properties"]["status"]["enum"]
    assert "limit" in status_schema["output"]["properties"]


# --- old on-disk state: pre-slice-20 keys are tolerated, not errors --------


def test_old_meta_json_with_on_limit_and_limit_waited_seconds_keys_reads_cleanly(
    cli: CliRunner,
) -> None:
    """A `meta.json` written before this slice carried `on_limit` and
    `limit_waited_seconds`; both keys are gone from `SessionMeta` now, and
    `meta_from_dict` folds anything it does not recognize into `extra`
    without raising (SPEC.md *State on disk*: unknown keys survive, they are
    never a hard error for session metadata — that rule is `config.toml`'s).
    """
    meta = sessions.create_session(entry="mock", base_adapter="mock", prompt="legacy probe")
    raw = json.loads(sessions.meta_path(meta.session_id).read_text(encoding="utf-8"))
    raw["on_limit"] = "wait"
    raw["limit_waited_seconds"] = 12.5
    sessions.meta_path(meta.session_id).write_text(json.dumps(raw), encoding="utf-8")

    reloaded = sessions.read_meta(meta.session_id)

    assert reloaded.extra["on_limit"] == "wait"
    assert reloaded.extra["limit_waited_seconds"] == 12.5

    status_result = invoke(cli, "status", meta.session_id, "--json")
    assert status_result.exit_code == vocab.EXIT_OK
