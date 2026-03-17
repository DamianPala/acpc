"""Session state management for acpc.

Manages local state for running sessions and last-session tracking.
Atomic writes via tempfile+rename. PID verification for stale cleanup.
"""

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from platformdirs import user_state_dir

STATE_DIR = Path(user_state_dir("acpc"))


def _sessions_file() -> Path:
    return STATE_DIR / "sessions.json"


def _last_dir() -> Path:
    return STATE_DIR / "last"


@dataclass
class RunningSession:
    """A running agent session entry."""

    session_id: str
    agent: str
    pid: int
    start_time: float  # time.time() at process start
    cwd: str
    started: str  # ISO format timestamp


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    """Write JSON via tempfile in same directory + os.rename (atomic on same fs)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.rename(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_sessions() -> dict[str, dict[str, object]]:
    """Load sessions map from disk. Returns empty dict if file missing or corrupt."""
    try:
        with open(_sessions_file()) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data  # type: ignore[return-value]
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {}


def _is_process_alive(pid: int, start_time: float) -> bool:  # noqa: ARG001
    """Check if PID is alive.

    start_time is stored for future use (PID reuse detection via /proc),
    but v0.1 only checks os.kill(pid, 0).
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we can't signal it (different user).
        return True
    return True


def add_running(session: RunningSession) -> None:
    """Add a running session entry. Atomic write via tempfile+rename."""
    sessions = _load_sessions()
    sessions[session.session_id] = asdict(session)
    _atomic_write(_sessions_file(), sessions)


def remove_running(session_id: str) -> None:
    """Remove a running session entry. Atomic write."""
    sessions = _load_sessions()
    sessions.pop(session_id, None)
    _atomic_write(_sessions_file(), sessions)


def list_running() -> dict[str, RunningSession]:
    """List all running sessions. Verify each PID is alive and clean stale entries."""
    sessions = _load_sessions()
    alive: dict[str, RunningSession] = {}
    stale_ids: list[str] = []

    for sid, data in sessions.items():
        try:
            rs = RunningSession(**data)  # type: ignore[arg-type]
        except (TypeError, KeyError):
            stale_ids.append(sid)
            continue
        if _is_process_alive(rs.pid, rs.start_time):
            alive[sid] = rs
        else:
            stale_ids.append(sid)

    if stale_ids:
        for sid in stale_ids:
            sessions.pop(sid, None)
        _atomic_write(_sessions_file(), sessions)

    return alive


def get_running_by_agent(agent: str) -> list[RunningSession]:
    """Get running sessions for a specific agent."""
    return [s for s in list_running().values() if s.agent == agent]


# --- Last session tracking (per-PPID) ---


def save_last_session(agent: str, session_id: str) -> None:
    """Save last session ID for agent, scoped by PPID.

    Creates {last_dir}/{agent}.{PPID} and {agent}.default as fallback.
    """
    last = _last_dir()
    last.mkdir(parents=True, exist_ok=True)
    ppid = os.getppid()

    ppid_file = last / f"{agent}.{ppid}"
    default_file = last / f"{agent}.default"

    ppid_file.write_text(session_id)
    default_file.write_text(session_id)


def load_last_session(agent: str) -> str | None:
    """Load last session ID for agent.

    Try {agent}.{PPID} first, fall back to {agent}.default.
    Return None if neither exists.
    """
    last = _last_dir()
    ppid = os.getppid()

    ppid_file = last / f"{agent}.{ppid}"
    if ppid_file.exists():
        text = ppid_file.read_text().strip()
        if text:
            return text

    default_file = last / f"{agent}.default"
    if default_file.exists():
        text = default_file.read_text().strip()
        if text:
            return text

    return None


def cleanup_last_sessions(max_age_hours: int = 24) -> None:
    """Remove last-session files older than max_age_hours."""
    last = _last_dir()
    if not last.exists():
        return

    cutoff = time.time() - (max_age_hours * 3600)
    for entry in last.iterdir():
        if entry.is_file():
            try:
                if entry.stat().st_mtime < cutoff:
                    entry.unlink()
            except OSError:
                pass


def make_running_session(
    session_id: str,
    agent: str,
    pid: int,
    cwd: str,
) -> RunningSession:
    """Create a RunningSession with current timestamp."""
    return RunningSession(
        session_id=session_id,
        agent=agent,
        pid=pid,
        start_time=time.time(),
        cwd=cwd,
        started=datetime.now(timezone.utc).isoformat(),
    )
