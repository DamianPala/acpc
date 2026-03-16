"""Tests for acpc.sessions module."""

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from acpc.sessions import (
    RunningSession,
    _atomic_write,
    _is_process_alive,
    add_running,
    cleanup_last_sessions,
    get_running_by_agent,
    list_running,
    load_last_session,
    make_running_session,
    remove_running,
    save_last_session,
)


@pytest.fixture()
def state_dir(tmp_path: Path):
    """Patch STATE_DIR to use tmp_path."""
    with patch("acpc.sessions.STATE_DIR", tmp_path):
        yield tmp_path


def _make_session(
    session_id: str = "sess-1",
    agent: str = "codex",
    pid: int = 12345,
) -> RunningSession:
    return RunningSession(
        session_id=session_id,
        agent=agent,
        pid=pid,
        start_time=time.time(),
        cwd="/tmp/test",
        started="2026-03-16T10:00:00+00:00",
    )


class TestAddRunning:
    def test_creates_file_and_adds_entry(self, state_dir: Path) -> None:
        session = _make_session()
        add_running(session)

        sessions_file = state_dir / "sessions.json"
        assert sessions_file.exists()

        data = json.loads(sessions_file.read_text())
        assert "sess-1" in data
        assert data["sess-1"]["agent"] == "codex"
        assert data["sess-1"]["pid"] == 12345

    def test_adds_multiple_entries(self, state_dir: Path) -> None:
        add_running(_make_session("sess-1", "codex"))
        add_running(_make_session("sess-2", "claude"))

        data = json.loads((state_dir / "sessions.json").read_text())
        assert len(data) == 2
        assert "sess-1" in data
        assert "sess-2" in data


class TestRemoveRunning:
    def test_removes_entry(self, state_dir: Path) -> None:
        add_running(_make_session("sess-1"))
        add_running(_make_session("sess-2", "claude"))
        remove_running("sess-1")

        data = json.loads((state_dir / "sessions.json").read_text())
        assert "sess-1" not in data
        assert "sess-2" in data

    def test_removes_nonexistent_entry_silently(self, state_dir: Path) -> None:
        add_running(_make_session("sess-1"))
        remove_running("nonexistent")

        data = json.loads((state_dir / "sessions.json").read_text())
        assert "sess-1" in data


class TestListRunning:
    def test_returns_valid_entries(self, state_dir: Path) -> None:
        add_running(_make_session("sess-1", pid=os.getpid()))

        with patch("acpc.sessions._is_process_alive", return_value=True):
            result = list_running()

        assert "sess-1" in result
        assert result["sess-1"].agent == "codex"

    def test_cleans_stale_pids(self, state_dir: Path) -> None:
        add_running(_make_session("sess-1", pid=99999))

        with patch("acpc.sessions._is_process_alive", return_value=False):
            result = list_running()

        assert len(result) == 0
        data = json.loads((state_dir / "sessions.json").read_text())
        assert "sess-1" not in data

    def test_handles_corrupt_data(self, state_dir: Path) -> None:
        sessions_file = state_dir / "sessions.json"
        sessions_file.parent.mkdir(parents=True, exist_ok=True)
        sessions_file.write_text('{"bad": {"missing": "fields"}}')

        result = list_running()
        assert len(result) == 0


class TestGetRunningByAgent:
    def test_filters_by_agent(self, state_dir: Path) -> None:
        add_running(_make_session("sess-1", "codex", pid=1001))
        add_running(_make_session("sess-2", "claude", pid=1002))
        add_running(_make_session("sess-3", "codex", pid=1003))

        with patch("acpc.sessions._is_process_alive", return_value=True):
            result = get_running_by_agent("codex")

        assert len(result) == 2
        agents = {s.agent for s in result}
        assert agents == {"codex"}


class TestIsProcessAlive:
    def test_alive_process(self) -> None:
        assert _is_process_alive(os.getpid(), time.time()) is True

    def test_dead_process(self) -> None:
        with patch("os.kill", side_effect=ProcessLookupError):
            assert _is_process_alive(99999, time.time()) is False

    def test_permission_error_means_alive(self) -> None:
        with patch("os.kill", side_effect=PermissionError):
            assert _is_process_alive(1, time.time()) is True


class TestLastSession:
    def test_save_and_load_roundtrip(self, state_dir: Path) -> None:
        ppid = os.getppid()
        save_last_session("codex", "sess-42")

        result = load_last_session("codex")
        assert result == "sess-42"

        # Verify both files exist
        assert (state_dir / "last" / f"codex.{ppid}").exists()
        assert (state_dir / "last" / "codex.default").exists()

    def test_load_returns_none_when_no_file(self, state_dir: Path) -> None:
        result = load_last_session("nonexistent")
        assert result is None

    def test_load_falls_back_to_default(self, state_dir: Path) -> None:
        last = state_dir / "last"
        last.mkdir(parents=True, exist_ok=True)
        (last / "codex.default").write_text("sess-fallback")

        result = load_last_session("codex")
        assert result == "sess-fallback"

    def test_ppid_file_takes_priority(self, state_dir: Path) -> None:
        last = state_dir / "last"
        last.mkdir(parents=True, exist_ok=True)
        ppid = os.getppid()
        (last / f"codex.{ppid}").write_text("sess-ppid")
        (last / "codex.default").write_text("sess-default")

        result = load_last_session("codex")
        assert result == "sess-ppid"


class TestCleanupLastSessions:
    def test_removes_old_files(self, state_dir: Path) -> None:
        last = state_dir / "last"
        last.mkdir(parents=True, exist_ok=True)

        old_file = last / "codex.12345"
        old_file.write_text("old-session")

        new_file = last / "claude.67890"
        new_file.write_text("new-session")

        # Make old_file appear old (48h ago)
        old_time = time.time() - 48 * 3600
        os.utime(old_file, (old_time, old_time))

        cleanup_last_sessions(max_age_hours=24)

        assert not old_file.exists()
        assert new_file.exists()

    def test_handles_missing_dir(self, state_dir: Path) -> None:
        cleanup_last_sessions(max_age_hours=24)


class TestAtomicWrite:
    def test_writes_correct_content(self, tmp_path: Path) -> None:
        path = tmp_path / "test.json"
        data = {"key": "value", "num": 42}
        _atomic_write(path, data)

        assert path.exists()
        loaded = json.loads(path.read_text())
        assert loaded == data

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "deep" / "test.json"
        _atomic_write(path, {"ok": True})
        assert path.exists()

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        path = tmp_path / "test.json"
        _atomic_write(path, {"v": 1})
        _atomic_write(path, {"v": 2})

        loaded = json.loads(path.read_text())
        assert loaded == {"v": 2}


class TestMakeRunningSession:
    def test_creates_with_timestamp(self) -> None:
        rs = make_running_session(
            session_id="s1",
            agent="codex",
            pid=1234,
            cwd="/tmp",
        )
        assert rs.session_id == "s1"
        assert rs.agent == "codex"
        assert rs.pid == 1234
        assert rs.cwd == "/tmp"
        assert rs.start_time > 0
        assert "T" in rs.started  # ISO format
