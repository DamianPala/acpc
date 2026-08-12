"""Contract tests for the direct ACP mode discovery."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from acpc import probe as probe_engine
from acpc import vocab
from acpc.cli import main

MOCK_AGENT_SCRIPT = str(Path(__file__).with_name("mock_agent.py"))


def _entry(behavior: str, turns_file: Path | None = None) -> str:
    turns = ""
    if turns_file is not None:
        turns = f'\nMOCK_PROBE_TURNS_FILE = "{turns_file}"'
    return f'''
name = "Mock Agent"
command = "{sys.executable} {MOCK_AGENT_SCRIPT}"
home = "~/.mock"
home_env = "MOCK_HOME"

[env]
MOCK_PROBE_BEHAVIOR = "{behavior}"{turns}

[modes]
default = {{ grants = "none", delegates = false }}
acceptEdits = {{ grants = "execute", delegates = true }}
legacy = {{ grants = "all", delegates = false }}
'''


@pytest.fixture
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "state"
    agents = root / "agents"
    agents.mkdir(parents=True)
    monkeypatch.setenv("ACPC_HOME", str(root))
    return root


def _install(root: Path, behavior: str, turns_file: Path | None = None) -> None:
    (root / "agents" / "mock.toml").write_text(_entry(behavior, turns_file), encoding="utf-8")


def _invoke(*args: str):
    return CliRunner().invoke(main, list(args), catch_exceptions=False)


def _modes() -> str:
    return (
        "default:inside=allowed,outside=escalated,shell=asked,network=denied;"
        "acceptEdits:inside=not-attempted,outside=hidden,shell=claim-success,network=prose-fail;"
        "plan:inside=allowed,outside=allowed,shell=allowed,network=allowed;"
        "yolo:inside=allowed,outside=allowed,shell=allowed,network=allowed"
    )


def test_discover_reads_catalogue_and_sends_zero_turns(state_root: Path) -> None:
    turns_file = state_root / "turns.ndjson"
    _install(state_root, _modes(), turns_file)

    result = _invoke("probe", "mock", "--discover", "--json")

    assert result.exit_code == vocab.EXIT_OK, result.stderr
    payload = json.loads(result.stdout)
    assert payload["discover_only"] is True
    assert payload["turns"] == 0
    assert [item["id"] for item in payload["advertised_modes"]] == [
        "default",
        "acceptEdits",
        "plan",
        "yolo",
    ]
    assert payload["advertised_modes"][0]["description"] == "Mock default mode"
    diff = {item["mode"]: item for item in payload["diff"]}
    assert diff["plan"] == {
        "mode": "plan",
        "status": "advertised-missing",
        "description": "Mock planning mode",
        "current": None,
        "proposed": None,
    }
    assert diff["legacy"] == {
        "mode": "legacy",
        "status": "entry-missing",
        "description": None,
        "current": {
            "grants": "all",
            "delegates": False,
            "escalates": False,
        },
        "proposed": None,
    }
    text_result = _invoke("probe", "mock", "--discover")
    assert "+ plan — Mock planning mode" in text_result.stdout
    assert "- legacy" in text_result.stdout
    assert "Background task failed" not in result.stderr
    assert not turns_file.exists()


def test_probe_without_discover_is_a_usage_error_naming_the_flag(state_root: Path) -> None:
    """Measurement is not in this release, so a bare probe must fail visibly.

    An empty report, or a discovery report handed to a caller who asked to be
    told what a mode permits, is the failure this command exists to prevent.
    """
    turns_file = state_root / "turns.ndjson"
    _install(state_root, _modes(), turns_file)

    result = _invoke("probe", "mock")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "--discover" in result.stderr
    assert result.stdout == ""
    # It fails before reaching the adapter: no session, no turns, no side effects.
    assert not turns_file.exists()


def test_probe_without_discover_does_not_answer_in_json_either(state_root: Path) -> None:
    _install(state_root, _modes())

    result = _invoke("probe", "mock", "--json")

    assert result.exit_code == vocab.EXIT_USAGE
    assert result.stdout == ""


def test_discover_text_and_json_report_the_same_catalogue(state_root: Path) -> None:
    _install(state_root, _modes())

    json_result = _invoke("probe", "mock", "--discover", "--json")
    text_result = _invoke("probe", "mock", "--discover")

    payload = json.loads(json_result.stdout)
    for mode in payload["advertised_modes"]:
        assert str(mode["id"]) in text_result.stdout
    assert f"{len(payload['advertised_modes'])} advertised mode(s); 0 turn(s)" in text_result.stdout


def test_probe_help_documents_discovery_and_the_missing_measurement() -> None:
    result = _invoke("probe", "--help")

    assert result.exit_code == vocab.EXIT_OK
    assert "opens a session" in result.stdout
    assert "zero turns" in result.stdout
    assert "--discover" in result.stdout
    assert "not in this release" in result.stdout
    assert "POSIX shell commands" in result.stdout
    # The measurement vocabulary must be gone: it described behaviour that no
    # longer exists, and help that promises it is worse than help that omits it.
    assert "four turns per advertised mode" not in result.stdout
    assert "verdict" not in result.stdout.lower()


def test_probe_refuses_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe_engine.sys, "platform", "win32")

    with pytest.raises(probe_engine.ProbeError, match="POSIX shell.*measure the shell"):
        probe_engine.run("mock")
