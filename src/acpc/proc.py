"""Process identity, liveness, and process-tree teardown primitives.

Harvested from the 0.3.0.dev1 implementation (sessions.py + runner.py), where
these survived real orphan-detection and PID-reuse incidents. SPEC.md leans on
them for *Session states*: "liveness is verified wherever state is read", with
the kernel start-time token guarding against PID reuse, and pidfd-based group
signalling guarding the kill path on Linux.
"""

import contextlib
import errno
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal

ProcessIdentity = Literal["verified", "unverifiable", "unsupported"]
ProcessLiveness = Literal["dead", "verified", "unverifiable"]
ProcessTreeResult = Literal["signalled", "already_gone", "refused"]

_PIDFD_SIGNAL_PROCESS_GROUP = 1 << 2


class ProcessIdentityError(RuntimeError):
    """Raised when a recorded process identity cannot be re-established."""


def process_identity_supported() -> bool:
    """Return whether this platform exposes the kernel token used for PID checks."""
    return sys.platform == "linux"


def process_start_time(pid: int | None = None) -> str | None:
    """Return the kernel process-start token used for PID-reuse checks."""
    if not process_identity_supported():
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


def process_liveness(pid: int, process_token: str | None = None) -> ProcessLiveness:
    """Return liveness, verified against the saved kernel identity when possible."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        pass
    if process_token is None:
        return "unverifiable"
    current_token = process_start_time(pid)
    if current_token is None:
        return "unverifiable"
    return "verified" if current_token == process_token else "dead"


def is_process_alive(pid: int, process_token: str | None = None) -> bool:
    """Check PID liveness, and its kernel identity when a token is available."""
    return process_liveness(pid, process_token) != "dead"


def classify_process_identity(process_token: str | None) -> ProcessIdentity:
    if process_token is not None:
        return "verified"
    return "unverifiable" if process_identity_supported() else "unsupported"


def process_group_kwargs() -> dict[str, Any]:
    """Platform-specific kwargs to spawn an adapter in its own process group.

    Linux/macOS: start_new_session=True (setsid; the adapter becomes PGID
    leader). Windows: CREATE_NEW_PROCESS_GROUP flag.
    """
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _check_process_identity(pid: int, expected_process_start_time: str) -> None:
    current_process_start_time = process_start_time(pid)
    if current_process_start_time != expected_process_start_time:
        raise ProcessIdentityError(
            f"process {pid} no longer has the recorded identity {expected_process_start_time!r}"
        )


def _kill_process_group_with_pidfd(
    pid: int, expected_process_start_time: str
) -> ProcessTreeResult | None:
    """Signal an identity-checked process group through a Linux pidfd."""
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if pidfd_open is None or pidfd_send_signal is None:
        return None

    try:
        pidfd = pidfd_open(pid)
    except OSError as error:
        if error.errno in {errno.ENOSYS, errno.ENODEV, errno.EINVAL}:
            return None
        if error.errno == errno.ESRCH:
            return "already_gone"
        return "refused"

    try:
        if os.getpgid(pid) != pid:
            raise ProcessIdentityError(f"process {pid} is not its own process-group leader")
        _check_process_identity(pid, expected_process_start_time)
        try:
            pidfd_send_signal(
                pidfd,
                signal.SIGKILL,
                None,
                _PIDFD_SIGNAL_PROCESS_GROUP,
            )
        except OSError as error:
            if error.errno != errno.EINVAL:
                return "already_gone" if error.errno == errno.ESRCH else "refused"
            return None
        return "signalled"
    finally:
        with contextlib.suppress(OSError):
            os.close(pidfd)


def _os_error_result(error: OSError) -> ProcessTreeResult:
    """Map a process-group syscall failure to the public stop result."""
    if isinstance(error, ProcessLookupError) or error.errno == errno.ESRCH:
        return "already_gone"
    return "refused"


def kill_process_tree(
    pid: int,
    expected_process_start_time: str | None = None,
    *,
    process_group_id: int | None = None,
) -> ProcessTreeResult:
    """Kill an adapter and all its children (cross-platform).

    Linux/macOS: killpg sends SIGKILL to the entire process group.
    Windows: taskkill /T recursively kills the process tree.

    When an expected Linux process-start token is supplied, identity is checked
    here immediately before signalling rather than trusted from the caller.

    The result distinguishes a successful signal, a process that was already
    gone, and a refusal to signal. ``process_group_id`` is used by the spawn
    teardown after a group leader has exited; the group ID is captured while
    that leader is known to be the group leader.
    """
    if sys.platform == "win32":
        try:
            completed = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                check=False,
            )
        except OSError as error:
            return _os_error_result(error)
        if completed.returncode == 0:
            return "signalled"
        # taskkill uses ERRORLEVEL 128 when the requested PID no longer exists.
        return "already_gone" if completed.returncode == 128 else "refused"

    try:
        if expected_process_start_time is not None and sys.platform == "linux":
            pidfd_result = _kill_process_group_with_pidfd(pid, expected_process_start_time)
            if pidfd_result is not None:
                return pidfd_result

        group_id = process_group_id
        if group_id is None:
            group_id = os.getpgid(pid)
            if group_id != pid:
                return "refused"

        if expected_process_start_time is not None and sys.platform == "linux":
            _check_process_identity(pid, expected_process_start_time)
            # This check cannot be atomic with killpg: if the PID exits and is
            # reused afterward, killpg can kill an unrelated group owned by the user.
        try:
            os.killpg(group_id, signal.SIGKILL)
        except OSError as error:
            return _os_error_result(error)
    except ProcessIdentityError:
        return "refused"
    except OSError as error:
        return _os_error_result(error)
    return "signalled"
