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


class _PsUnavailableError(Exception):
    """Raised internally when ``ps`` itself could not be run.

    This is an infrastructure failure (missing binary, or a transient
    ``fork``/``posix_spawn`` failure such as ``EAGAIN`` under
    ``RLIMIT_NPROC``), not evidence that the pid is gone. Never leaves
    ``proc.py``: every caller either maps it to ``None``/``unverifiable``
    (the same outcome Linux's ``except OSError`` produces when ``/proc``
    can't be read) or lets it fall through to a ``None`` fields result,
    depending on how much that caller cares about the distinction.
    """


def process_identity_supported() -> bool:
    """Return whether this platform exposes the kernel token used for PID checks."""
    return sys.platform in ("linux", "darwin")


def _ps_fields(pid: int, *columns: str) -> list[str] | None:
    """Return values for one supported `ps -o` read.

    A single column keeps its complete value, including spaces in `lstart`
    or `command`. The combined `stat,lstart` shape splits the one-token
    `stat` from the remaining start-time value. `command` stays in a separate
    invocation and is its final column: on macOS, BSD `ps`'s `command` column
    (`adv_cmds/ps/print.c`, `p_command_and_or_args`) only prints the full,
    unabbreviated command when it is last. Combined with another column, it is
    truncated to its 16-character keyword width.
    BSD `ps` (macOS) and procps `ps` (Linux) both accept these spellings,
    which lets tests exercise the darwin path on Linux. `LC_ALL=C` pins the
    locale used for `lstart`, whose token is compared byte for byte later.

    Returns ``None`` when the pid is gone (``ps`` exits non-zero, prints
    nothing, or a column comes back short). Raises `_PsUnavailableError`
    when ``ps`` itself could not be run at all; callers decide how to map
    that.
    """
    if columns == ("stat", "lstart"):
        column_argument = "stat=,lstart="
    elif len(columns) == 1:
        column_argument = f"{columns[0]}="
    else:
        raise ValueError(f"unsupported ps columns: {columns!r}")
    try:
        completed = subprocess.run(
            ["ps", "-o", column_argument, "-p", str(pid)],
            capture_output=True,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except OSError as error:
        raise _PsUnavailableError(str(error)) from error
    if completed.returncode != 0:
        return None
    value = completed.stdout.decode("utf-8", errors="surrogateescape").strip()
    if not value:
        return None
    if columns == ("stat", "lstart"):
        fields = value.split(None, 1)
        if len(fields) != 2:
            return None
        return [fields[0], " ".join(fields[1].split())]
    return [value]


def process_start_time(pid: int | None = None) -> str | None:
    """Return the kernel process-start token used for PID-reuse checks.

    On macOS this is BSD `ps`'s ``lstart`` (e.g. ``Mon Sep 21 09:45:08
    2026``), which only has second resolution; that is enough for the
    reuse guard, but two processes started within the same second cannot be
    told apart by this token alone.
    """
    if not process_identity_supported():
        return None
    process_id = os.getpid() if pid is None else pid
    if sys.platform == "darwin":
        try:
            fields = _ps_fields(process_id, "lstart")
        except _PsUnavailableError:
            # Infrastructure failure, not proof the pid is gone. Absorbed to
            # `None` here (rather than propagated): every caller of
            # `process_start_time` already treats a `None` token as "cannot
            # verify" and refuses to act on it (`_check_process_identity`
            # refuses to kill; `_verified_or_dead` reports `unverifiable`),
            # which is the same safe outcome Linux reaches when `/proc`
            # can't be read.
            return None
        if fields is None:
            return None
        return " ".join(fields[0].split())
    try:
        stat = Path(f"/proc/{process_id}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    try:
        return stat.rsplit(")", 1)[1].split()[19]
    except (IndexError, ValueError):
        return None


def process_cmdline(pid: int | None = None) -> list[str] | None:
    """Return the process command line, if available.

    On macOS this is BSD `ps`'s ``command`` column split on whitespace,
    which loses argument boundaries (an argument containing a space is
    indistinguishable from two arguments); callers only test list
    membership of single tokens, so that is enough.
    """
    process_id = os.getpid() if pid is None else pid
    if sys.platform == "darwin":
        try:
            fields = _ps_fields(process_id, "command")
        except _PsUnavailableError:
            # Same as the Linux branch below on an OSError: an infrastructure
            # failure reads as "cannot tell", not as a specific command line.
            return None
        if fields is None:
            return None
        return fields[0].split()
    if sys.platform != "linux":
        return None
    try:
        data = Path(f"/proc/{process_id}/cmdline").read_bytes()
    except (FileNotFoundError, OSError):
        return None
    return [part.decode("utf-8", errors="surrogateescape") for part in data.split(b"\0") if part]


def _verified_or_dead(current_token: str | None, process_token: str | None) -> ProcessLiveness:
    """Shared token-comparison tail: the last step of every liveness branch."""
    if process_token is None or current_token is None:
        return "unverifiable"
    return "verified" if current_token == process_token else "dead"


def process_liveness(pid: int, process_token: str | None = None) -> ProcessLiveness:
    """Return liveness, verified against the saved kernel identity when possible."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        pass
    if sys.platform == "linux":
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except FileNotFoundError:
            return "dead"
        except OSError:
            return "unverifiable"
        try:
            fields = stat.rsplit(")", 1)[1].split()
            state = fields[0]
        except (IndexError, ValueError):
            return "unverifiable"
        if state == "Z":
            return "dead"
        current_token = fields[19] if len(fields) > 19 else None
        return _verified_or_dead(current_token, process_token)
    if sys.platform == "darwin":
        columns = ("stat", "lstart") if process_token is not None else ("stat",)
        try:
            stat_fields = _ps_fields(pid, *columns)
        except _PsUnavailableError:
            # `ps` itself failed to run: an infrastructure failure, not proof
            # the pid is gone. Mirrors Linux's `except OSError: return
            # "unverifiable"` a few lines up -- `"dead"` here would make
            # `sessions.py` persist a live session as permanently `unknown`.
            return "unverifiable"
        if stat_fields is None:
            return "dead"
        if stat_fields[0].startswith("Z"):
            return "dead"
        if process_token is None:
            return "unverifiable"
        return _verified_or_dead(stat_fields[1], process_token)
    return _verified_or_dead(process_start_time(pid), process_token)


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

    When an expected process-start token is supplied, identity is checked here
    immediately before signalling rather than trusted from the caller (Linux
    and macOS only; Windows has no equivalent kernel token to check against).

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

        if expected_process_start_time is not None and sys.platform in ("linux", "darwin"):
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
