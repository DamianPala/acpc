"""Subscription facts captured through ACP, published per home, and read by the CLI."""

import asyncio
import json
import re
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from acp.schema import UsageUpdate
from click.testing import CliRunner

from acpc import cache
from acpc.cli import main
from acpc.client import AcpcClient
from acpc.permissions import PermissionLevel
from acpc.transcript import Transcript

SCRIPT = Path(__file__).with_name("mock_agent.py")
ENTRY = f'''
command = "{sys.executable} {SCRIPT}"
home = "~/.mock-s8"
home_env = "MOCK_HOME"
model = "mock-sonnet-5"
permissions = "read"
[modes]
default = {{ grants = "read", delegates = true }}
'''


@pytest.fixture(autouse=True)
def state_root(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="s8-") as temporary:
        root = Path(temporary)
        (root / "agents").mkdir()
        (root / "agents/mock.toml").write_text(ENTRY)
        monkeypatch.setenv("ACPC_HOME", temporary)
        monkeypatch.setenv("ACPC_MOCK_RATE_WINDOWS", "1")
        monkeypatch.delenv("ACPC_CEILING", raising=False)
        yield root


def invoke(*args: str) -> Any:
    return CliRunner().invoke(main, list(args), catch_exceptions=False)


def limits_path(root: Path) -> Path:
    return root / "cache/mock/limits.json"


def homes(root: Path) -> dict[str, Any]:
    return json.loads(limits_path(root).read_text())["homes"]


def run(*args: str) -> Any:
    return invoke("run", "mock", "echo:windows", "--json", *args)


def seed(root: Path, row: Any, home: str = "~/.mock-s8") -> None:
    limits_path(root).parent.mkdir(parents=True, exist_ok=True)
    limits_path(root).write_text(json.dumps({"homes": {str(Path(home).expanduser()): row}}))


def observation() -> dict[str, Any]:
    return {
        "five_hour": {"utilization": 0.24, "resets_at": int(time.time()) + 3600},
        "seven_day": {"utilization": 0.06, "resets_at": int(time.time()) + 259200},
        "observed_at": time.time() - 10800,
        "session_id": "observed",
    }


def test_runs_keep_separate_expanded_homes_and_ignore_missing_events(
    state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = run()
    assert first.exit_code == 0, first.output
    key = str(Path("~/.mock-s8").expanduser())
    row = homes(state_root)[key]
    assert row["five_hour"]["utilization"] == 0.24
    assert row["seven_day"]["utilization"] == 0.06
    assert type(row["five_hour"]["resets_at"]) is int
    assert type(row["seven_day"]["resets_at"]) is int
    assert type(row["observed_at"]) is float
    assert row["session_id"] == json.loads(first.stdout)["session_id"]
    second = run("--home", "~/.other-s8")
    assert second.exit_code == 0, second.output
    assert homes(state_root)[key] == row
    assert set(homes(state_root)) == {key, str(Path("~/.other-s8").expanduser())}
    before = limits_path(state_root).read_bytes()
    for knob in ("skip", "bare"):
        monkeypatch.setenv("ACPC_MOCK_RATE_WINDOWS", knob)
        assert run().exit_code == 0
        assert limits_path(state_root).read_bytes() == before
    assert "five_hour" not in (state_root / "cache/mock/advertised.json").read_text()


def test_new_observation_replaces_whole_row_but_never_a_newer_one(
    state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(state_root, observation())
    monkeypatch.setenv("ACPC_MOCK_RATE_FIVE_HOUR", "none")
    result = run()
    assert result.exit_code == 0, result.output
    row = homes(state_root)[str(Path("~/.mock-s8").expanduser())]
    assert row["five_hour"] is None
    assert row["seven_day"]["utilization"] == 0.06
    row["observed_at"] = time.time() + 86400
    seed(state_root, row)
    before = limits_path(state_root).read_bytes()
    assert run().exit_code == 0
    assert limits_path(state_root).read_bytes() == before


@pytest.mark.parametrize("background", [False, True])
def test_daemon_and_detached_worker_publish_windows(
    state_root: Path, live_daemon: None, monkeypatch: pytest.MonkeyPatch, background: bool
) -> None:
    if background:
        monkeypatch.setenv("ACPC_DIRECT_WORKER", "1")
    args = ("--timeout", "0.1", "--name", "windows-worker") if background else ()
    result = run(*args)
    assert result.exit_code in (0, 124), result.output
    session = "windows-worker" if background else json.loads(result.stdout)["session_id"]
    if background:
        waited = invoke("wait", session, "--json")
        assert waited.exit_code == 0, waited.output
        session = json.loads(waited.stdout)["session_id"]
    deadline = time.monotonic() + 5
    while not limits_path(state_root).exists():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    row = homes(state_root)[str(Path("~/.mock-s8").expanduser())]
    assert row["session_id"] == session
    assert row["five_hour"]["utilization"] == 0.24
    assert row["seven_day"]["utilization"] == 0.06


def test_failed_limit_turn_publishes_its_rejected_windows(
    state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPC_MOCK_LIMIT_PROMPTS", "1")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_META", "1")
    result = run("--limit-wait", "0s")
    assert result.exit_code != 0, result.output
    row = homes(state_root)[str(Path("~/.mock-s8").expanduser())]
    assert row["five_hour"]["utilization"] == 0.24
    session = row["session_id"]
    events = [
        json.loads(line)
        for line in (state_root / "sessions" / session / "transcript.ndjson")
        .read_text()
        .splitlines()
    ]
    assert any(
        event.get("meta", {}).get("_claude/rateLimit", {}).get("status") == "rejected"
        and "unifiedWindows" in event["meta"]["_claude/rateLimit"]
        for event in events
    )


def test_get_and_check_read_the_entry_home_without_prompting(
    state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = observation()
    seed(state_root, row)
    (state_root / "agents/variant.toml").write_text('extends = "mock"\nhome = "~/.variant-s8"\n')
    own = {**row, "five_hour": {"utilization": 0.73, "resets_at": int(time.time()) + 3600}}
    cache.refresh_windows("mock", "~/.variant-s8", own, session_id="variant")
    own["session_id"] = "variant"
    for name, expected in (("mock", row), ("variant", own)):
        detail = invoke("agents", "get", name, "--json")
        assert detail.exit_code == 0, detail.output
        assert json.loads(detail.stdout)["windows"] == expected
        text = invoke("agents", "get", name, "--format", "text").stdout
        assert (
            f"windows      5h {round(expected['five_hour']['utilization'] * 100)}% resets" in text
        )
        assert "7d 6% resets" in text and "observed 3h ago" in text
        # Zone-independent shape: within 24 h only the time, later the day too.
        shape = r"5h \d+% resets \d\d:\d\d · 7d 6% resets \w{3} \d\d \w{3} \d\d:\d\d · observed"
        assert re.search(shape, text)
        if name == "mock":
            lines = text.splitlines()
            effort_line = next(
                i for i, line in enumerate(lines) if line.startswith("efforts      ")
            )
            assert lines[effort_line + 1].startswith("windows      ")
        else:
            assert text.index("residency    ") < text.index("windows      ") < text.index("env ")
        before = limits_path(state_root).read_bytes()
        wire = state_root / "events.txt"
        wire.write_text("")
        monkeypatch.setenv("ACPC_MOCK_EVENT_FILE", str(wire))
        checked = invoke("agents", "check", name, "--json")
        assert json.loads(checked.stdout)["items"][0]["windows"] == expected
        assert limits_path(state_root).read_bytes() == before
        assert not any(line.startswith("prompt:") for line in wire.read_text().splitlines())
        text = invoke("agents", "check", name, "--format", "text").stdout
        assert f"{name} ok · 5h" in text and "observed 3h ago" in text


@pytest.mark.parametrize(
    "damage",
    [
        None,
        "bad json",
        {},
        {"observed_at": "bad"},
        {**observation(), "five_hour": {"utilization": True, "resets_at": 123}},
        {**observation(), "seven_day": {"utilization": 0.2, "resets_at": True}},
        {**observation(), "observed_at": True},
        {**observation(), "session_id": 42},
        {**observation(), "five_hour": {"utilization": float("nan"), "resets_at": 2**31}},
        {**observation(), "five_hour": {"utilization": 0.2, "resets_at": 10**17}},
        {**observation(), "observed_at": float("inf")},
    ],
)
def test_missing_or_damaged_rows_are_none_on_both_surfaces(state_root: Path, damage: Any) -> None:
    if damage is not None:
        seed(state_root, damage)
        if damage == "bad json":
            limits_path(state_root).write_text("{")
    get = invoke("agents", "get", "mock", "--json")
    assert json.loads(get.stdout)["windows"] is None
    assert (
        "windows      · (none observed)"
        in invoke("agents", "get", "mock", "--format", "text").stdout
    )
    checked = invoke("agents", "check", "mock", "--json")
    assert json.loads(checked.stdout)["items"][0]["windows"] is None
    assert invoke("agents", "check", "mock", "--format", "text").stdout == "mock ok\n"


def test_elapsed_windows_render_reset(state_root: Path) -> None:
    row = observation()
    row["five_hour"]["resets_at"] = int(time.time()) - 1
    row["seven_day"] = None
    seed(state_root, row)
    text = invoke("agents", "get", "mock", "--format", "text").stdout
    assert "windows      5h reset · observed 3h ago" in text
    assert "7d " not in text


@pytest.mark.parametrize("invalid", [True, "0.24", None])
def test_client_ignores_bad_windows_and_preserves_snapshot(state_root: Path, invalid: Any) -> None:
    client = AcpcClient(Transcript(state_root / "capture.ndjson"), PermissionLevel.READ)

    async def send(info: dict[str, Any]) -> None:
        await client.session_update(
            session_id="mock",
            update=UsageUpdate(
                session_update="usage_update",
                used=1200,
                size=200000,
                field_meta={"_claude/rateLimit": info},
            ),
        )

    good = {"utilization": 0.24, "resetsAt": 2000000000}
    asyncio.run(
        send(
            {
                "unifiedWindows": {
                    "five_hour": good,
                    "seven_day": {"utilization": invalid, "resetsAt": 2000000000},
                    "seven_day_overage_included": good,
                }
            }
        )
    )
    row = client.rate_windows
    assert row is not None
    assert row["five_hour"] == {"utilization": 0.24, "resets_at": 2000000000}
    assert row["seven_day"] is None
    assert set(row) == {"five_hour", "seven_day", "observed_at"}
    row["five_hour"]["utilization"] = 99
    client.clear_rate_limit_info()
    assert client.rate_limit_info is None
    for info in (
        {},
        {"rateLimitType": "five_hour", "utilization": 0.9, "resetsAt": 2000000000},
        {"unifiedWindows": [{"five_hour": good}]},
        {"unifiedWindows": {"five_hour": {"utilization": 0.9, "resetsAt": 1.5}}},
        {"unifiedWindows": {"five_hour": {"utilization": 0.9, "resetsAt": "2000000000"}}},
        {"unifiedWindows": {"five_hour": {"utilization": invalid, "resetsAt": 3}}},
        {"unifiedWindows": {"five_hour": {"utilization": 0.9, "resetsAt": True}}},
    ):
        asyncio.run(send(info))
        saved = client.rate_windows
        assert saved is not None
        assert saved["five_hour"]["utilization"] == 0.24
        assert saved["observed_at"] == row["observed_at"]


def test_data_residency_surfaces_and_session_snapshot(state_root: Path) -> None:
    for declaration, value, source in (
        ("", None, "unset"),
        ('data_residency = "eu"\n', "eu", "entry"),
    ):
        (state_root / "agents/mock.toml").write_text(declaration + ENTRY)
        for args in (("resolve", "mock"), ("agents", "get", "mock")):
            result = invoke(*args, "--json")
            assert result.exit_code == 0, result.output
            payload = json.loads(result.stdout)
            expected_source = (
                f"entry ({state_root / 'agents/mock.toml'})"
                if source == "entry" and args[0] == "resolve"
                else source
            )
            assert payload["resolved"]["data_residency"] == {
                "value": value,
                "source": expected_source,
            }
            if args[0] == "resolve":
                assert "windows" not in payload
            text = invoke(*args, "--format", "text").stdout
            assert f"residency    {value or '·'} ({expected_source})" in text
        result = run()
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert "data_residency" not in payload
        meta = json.loads(
            (state_root / "sessions" / payload["session_id"] / "meta.json").read_text()
        )
        assert meta["resolution"]["resolved"]["data_residency"] == {
            "value": value,
            "source": (
                f"entry ({state_root / 'agents/mock.toml'})" if source == "entry" else source
            ),
        }
        assert "data_residency" not in meta["resolution"]["adapter"]


def test_daemon_failed_turn_publishes_windows(
    state_root: Path, live_daemon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPC_MOCK_LIMIT_PROMPTS", "1")
    monkeypatch.setenv("ACPC_MOCK_LIMIT_META", "1")
    result = run("--limit-wait", "0s")
    assert result.exit_code != 0, result.output
    row = homes(state_root)[str(Path("~/.mock-s8").expanduser())]
    assert row["five_hour"]["utilization"] == 0.24
    assert row["seven_day"]["utilization"] == 0.06


def test_codex_profile_probe_has_no_windows(state_root: Path) -> None:
    (state_root / "agents/codex-mock.toml").write_text(
        'usage_profile = "codex_usage_updates"\n' + ENTRY
    )
    result = invoke("agents", "check", "codex-mock", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["items"][0]["windows"] is None
    assert invoke("agents", "check", "codex-mock", "--format", "text").stdout == "codex-mock ok\n"
    assert not (state_root / "cache/codex-mock/limits.json").exists()
