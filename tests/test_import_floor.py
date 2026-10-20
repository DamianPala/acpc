"""The light CLI paths do not import ACP until they need an adapter connection."""

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

from acpc import sessions

ROOT = Path(__file__).resolve().parents[1]
MOCK_AGENT = Path(__file__).with_name("mock_agent.py")
IMPORTS = "sorted(m for m in sys.modules if m == 'acp' or m.startswith('acp.') or m == 'pydantic')"
FLOOR_MARKER = "\n__ACPC_IMPORT_FLOOR__"
CLI_SCRIPT = f"""
import json
import runpy
import sys

sys.argv = ["acpc", *sys.argv[1:]]
exit_code = 0
try:
    runpy.run_module("acpc", run_name="__main__")
except SystemExit as error:
    if error.code is None:
        exit_code = 0
    elif isinstance(error.code, int):
        exit_code = error.code
    else:
        print(error.code, file=sys.stderr)
        exit_code = 1
loaded = sorted(
    module for module in sys.modules
    if module == "acp" or module.startswith("acp.") or module == "pydantic"
)
sys.stdout.write({FLOOR_MARKER!r} + json.dumps(loaded) + "\\n")
raise SystemExit(exit_code)
"""


def _home(tmp_path: Path, *, live_mock: bool = False) -> Path:
    home = tmp_path / "acpc-home"
    agents = home / "agents"
    agents.mkdir(parents=True)
    command = f"{sys.executable} {MOCK_AGENT}" if live_mock else "true"
    (agents / "mock.toml").write_text(
        f'''name = "Mock Agent"
command = "{command}"
install_command = "true"
home = "~/.mock-import-floor"
home_env = "MOCK_HOME"
model = "mock-sonnet-5"
permissions = "read"

[modes]
default = {{ grants = "read", delegates = true }}
''',
        encoding="utf-8",
    )
    return home


def _environment(home: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["ACPC_HOME"] = str(home)
    environment.pop("ACPC_CEILING", None)
    return environment


def _run_cli(home: Path, *args: str) -> tuple[subprocess.CompletedProcess[str], list[str], str]:
    result = subprocess.run(
        [sys.executable, "-c", CLI_SCRIPT, *args],
        cwd=ROOT,
        env=_environment(home),
        capture_output=True,
        check=False,
        text=True,
    )
    body, marker, suffix = result.stdout.rpartition(FLOOR_MARKER)
    assert marker, f"CLI import marker missing; stdout={result.stdout!r} stderr={result.stderr!r}"
    loaded = json.loads(suffix)
    assert isinstance(loaded, list) and all(isinstance(module, str) for module in loaded)
    return result, loaded, body


def _project_version() -> str:
    data: dict[str, Any] = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def test_importing_cli_and_version_do_not_load_acp_or_pydantic(tmp_path: Path) -> None:
    home = _home(tmp_path)
    direct = subprocess.run(
        [sys.executable, "-c", f"import acpc.cli, sys; print({IMPORTS})"],
        cwd=ROOT,
        env=_environment(home),
        capture_output=True,
        check=False,
        text=True,
    )
    assert direct.returncode == 0, direct.stderr
    assert direct.stdout == "[]\n"

    version = subprocess.run(
        [sys.executable, "-m", "acpc", "--version"],
        cwd=ROOT,
        env=_environment(home),
        capture_output=True,
        check=False,
        text=True,
    )
    assert version.returncode == 0, version.stderr
    assert version.stdout == f"{_project_version()}\n"
    assert version.stderr == ""

    wrapped, loaded, output = _run_cli(home, "--version")
    assert wrapped.returncode == 0, wrapped.stderr
    assert loaded == []
    assert output == version.stdout


def test_read_only_commands_do_not_load_acp_or_pydantic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    monkeypatch.setenv("ACPC_HOME", str(home))
    meta = sessions.create_session(
        entry="mock",
        base_adapter="mock",
        prompt="import floor status",
        target="mock~import-floor",
    )

    for args in (
        ("list",),
        ("status", meta.session_id, "--json"),
        ("agents", "list", "--plain", "--limit", "5"),
        ("resolve", "mock"),
        ("schema",),
    ):
        result, loaded, _output = _run_cli(home, *args)
        assert result.returncode == 0, f"{args}: {result.stderr}"
        assert loaded == [], f"{args} imported {loaded}"


def test_agents_get_cache_hit_skips_acp_but_cache_miss_probes(tmp_path: Path) -> None:
    hit_home = _home(tmp_path / "hit", live_mock=True)
    advertised_file = hit_home / "cache" / "mock" / "advertised.json"
    advertised_file.parent.mkdir(parents=True)
    advertised_file.write_text(
        json.dumps(
            {
                "cached_at": 100.0,
                "advertised": {
                    "modes": ["default"],
                    "models": ["mock-sonnet-5"],
                    "commands": [],
                    "efforts": {},
                },
            }
        ),
        encoding="utf-8",
    )
    hit, hit_loaded, _ = _run_cli(hit_home, "agents", "get", "mock", "--json")
    assert hit.returncode == 0, hit.stderr
    assert hit_loaded == []

    miss_home = _home(tmp_path / "miss", live_mock=True)
    miss, miss_loaded, _ = _run_cli(miss_home, "agents", "get", "mock", "--json")
    assert miss.returncode == 0, miss.stderr
    assert "acp" in miss_loaded
    assert "pydantic" in miss_loaded
