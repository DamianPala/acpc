"""Session state management for acpc.

Manages local state for running sessions and last-session tracking.
Atomic writes via tempfile+rename. PID verification for stale cleanup.
"""

import contextlib
import json
import os
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from platformdirs import user_state_dir


def state_dir() -> Path:
    """Return the current acpc state directory.

    Resolve the environment on every call so daemon tests and separate acpc
    processes can use isolated state without reloading this module.
    """
    configured_dir = os.environ.get("ACPC_STATE_DIR")
    if configured_dir:
        return Path(configured_dir)
    return Path(user_state_dir("acpc"))


def _sessions_file() -> Path:
    return state_dir() / "sessions.json"


def _last_dir() -> Path:
    return state_dir() / "last"


def _session_metadata_file() -> Path:
    return state_dir() / "session_metadata.json"


def run_dir() -> Path:
    """Return the directory for daemon sockets and lock files."""
    return state_dir() / "run"


def log_dir() -> Path:
    """Return the directory for daemon logs."""
    return state_dir() / "log"


def process_start_time(pid: int | None = None) -> str | None:
    """Return the kernel process-start token used for PID-reuse checks."""
    if sys.platform != "linux":
        return None
    process_id = os.getpid() if pid is None else pid
    try:
        stat = Path(f"/proc/{process_id}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    try:
        return stat.rsplit(")", 1)[1].split()[19]
    except (IndexError, ValueError):
        return None


def process_cmdline(pid: int | None = None) -> list[str] | None:
    """Return the process command line from procfs, if available."""
    if sys.platform != "linux":
        return None
    process_id = os.getpid() if pid is None else pid
    try:
        data = Path(f"/proc/{process_id}/cmdline").read_bytes()
    except (FileNotFoundError, OSError):
        return None
    return [part.decode("utf-8", errors="surrogateescape") for part in data.split(b"\0") if part]


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


def _read_last_session_record(path: Path) -> tuple[str, str | None] | None:
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        try:
            session_id = path.read_text().strip()
        except (FileNotFoundError, OSError):
            return None
        return (session_id, None) if session_id else None
    if not isinstance(data, dict):
        return None
    session_id = data.get("session_id")
    cwd = data.get("cwd")
    if not isinstance(session_id, str) or not session_id:
        return None
    return session_id, cwd if isinstance(cwd, str) else None


def _load_session_metadata() -> dict[str, dict[str, str]]:
    try:
        data = json.loads(_session_metadata_file().read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    result: dict[str, dict[str, str]] = {}
    for agent, sessions in data.items():
        if not isinstance(agent, str) or not isinstance(sessions, dict):
            continue
        result[agent] = {
            session_id: cwd
            for session_id, cwd in sessions.items()
            if isinstance(session_id, str) and isinstance(cwd, str)
        }
    return result


def _write_last_session(path: Path, session_id: str, cwd: str | None) -> None:
    if cwd is None:
        path.write_text(session_id)
    else:
        path.write_text(json.dumps({"session_id": session_id, "cwd": cwd}))


def save_last_session(agent: str, session_id: str, cwd: str | None = None) -> None:
    """Save last session ID for agent, scoped by PPID.

    Creates {last_dir}/{agent}.{PPID} and {agent}.default as fallback.
    """
    last = _last_dir()
    last.mkdir(parents=True, exist_ok=True)
    ppid = os.getppid()

    ppid_file = last / f"{agent}.{ppid}"
    default_file = last / f"{agent}.default"

    if cwd is None:
        previous = _read_last_session_record(ppid_file) or _read_last_session_record(default_file)
        if previous is not None and previous[0] == session_id:
            cwd = previous[1]
    _write_last_session(ppid_file, session_id, cwd)
    _write_last_session(default_file, session_id, cwd)

    metadata = _load_session_metadata()
    metadata.setdefault(agent, {})[session_id] = cwd or metadata.get(agent, {}).get(session_id, "")
    if not metadata[agent][session_id]:
        metadata[agent].pop(session_id, None)
    _atomic_write(_session_metadata_file(), metadata)


def load_last_session_record(agent: str) -> tuple[str, str | None] | None:
    """Load the last session ID and its recorded working directory."""
    last = _last_dir()
    ppid = os.getppid()
    for name in (f"{agent}.{ppid}", f"{agent}.default"):
        record = _read_last_session_record(last / name)
        if record is not None:
            return record
    return None


def load_session_cwd(agent: str, session_id: str) -> str | None:
    """Return persisted cwd metadata for one local session reference."""
    return _load_session_metadata().get(agent, {}).get(session_id)


def evict_session_metadata(agent: str, session_id: str) -> None:
    """Remove a dead session's local reference and cwd metadata."""
    metadata = _load_session_metadata()
    sessions = metadata.get(agent)
    if sessions is not None:
        sessions.pop(session_id, None)
        if not sessions:
            metadata.pop(agent, None)
        _atomic_write(_session_metadata_file(), metadata)
    last = _last_dir()
    for path in (last / f"{agent}.{os.getppid()}", last / f"{agent}.default"):
        record = _read_last_session_record(path)
        if record is not None and record[0] == session_id:
            with contextlib.suppress(FileNotFoundError, OSError):
                path.unlink()


def load_last_session(agent: str) -> str | None:
    """Load last session ID for agent.

    Try {agent}.{PPID} first, fall back to {agent}.default.
    Return None if neither exists.
    """
    record = load_last_session_record(agent)
    return record[0] if record is not None else None


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
