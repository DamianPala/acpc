"""Tests for the state-root layout and atomic write primitive in acpc.paths."""

import json
import stat
from pathlib import Path

import pytest

from acpc.paths import (
    acpc_home,
    atomic_write,
    daemon_dir,
    ensure_private_dir,
    session_dir,
    sessions_dir,
)


def test_acpc_home_defaults_to_dot_acpc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ACPC_HOME", raising=False)
    assert acpc_home() == Path.home() / ".acpc"


def test_acpc_home_env_override_is_resolved_per_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "one"))
    assert acpc_home() == tmp_path / "one"
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "two"))
    assert acpc_home() == tmp_path / "two"
    assert sessions_dir() == tmp_path / "two" / "sessions"
    assert daemon_dir() == tmp_path / "two" / "daemon"
    assert session_dir("x7k2") == tmp_path / "two" / "sessions" / "x7k2"


def test_acpc_home_expands_tilde(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACPC_HOME", "~/custom-acpc")
    assert acpc_home() == Path.home() / "custom-acpc"


def test_ensure_private_dir_creates_owner_only_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "root"))
    created = ensure_private_dir(sessions_dir() / "ab12")

    assert created.is_dir()
    for directory in (created, sessions_dir(), acpc_home()):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_ensure_private_dir_does_not_chmod_outside_the_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "root"))
    outside = tmp_path / "elsewhere" / "leaf"
    tmp_path.chmod(0o755)

    ensure_private_dir(outside)

    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o755


class TestAtomicWrite:
    def test_writes_json_owner_only(self, tmp_path: Path) -> None:
        path = tmp_path / "meta.json"
        atomic_write(path, {"state": "done"})
        assert json.loads(path.read_text()) == {"state": "done"}
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_writes_text(self, tmp_path: Path) -> None:
        path = tmp_path / "answer.md"
        atomic_write(path, "## Answer\n")
        assert path.read_text() == "## Answer\n"

    def test_replace_leaves_no_temp_files(self, tmp_path: Path) -> None:
        path = tmp_path / "meta.json"
        atomic_write(path, {"v": 1})
        atomic_write(path, {"v": 2})
        assert json.loads(path.read_text()) == {"v": 2}
        assert [p.name for p in tmp_path.iterdir()] == ["meta.json"]

    def test_exclusive_fails_on_existing_path(self, tmp_path: Path) -> None:
        path = tmp_path / "marker"
        atomic_write(path, "first", exclusive=True)
        with pytest.raises(FileExistsError):
            atomic_write(path, "second", exclusive=True)
        assert path.read_text() == "first"
        assert [p.name for p in tmp_path.iterdir()] == ["marker"]

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        path = tmp_path / "sessions" / "x7k2" / "meta.json"
        atomic_write(path, {"state": "starting"})
        assert path.exists()
