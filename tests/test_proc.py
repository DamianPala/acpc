"""Tests for acpc.proc process identity, liveness, and tree-kill primitives."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from acpc.proc import (
    classify_process_identity,
    is_process_alive,
    kill_process_tree,
    process_group_kwargs,
    process_liveness,
    process_start_time,
)


class TestProcessGroupKwargs:
    def test_unix_uses_start_new_session(self) -> None:
        with patch.object(sys, "platform", "linux"):
            assert process_group_kwargs() == {"start_new_session": True}

    def test_windows_uses_create_new_process_group(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, create=True),
        ):
            assert process_group_kwargs() == {"creationflags": 0x00000200}

    def test_darwin_uses_start_new_session(self) -> None:
        with patch.object(sys, "platform", "darwin"):
            assert process_group_kwargs() == {"start_new_session": True}


class TestProcessIdentity:
    def test_own_process_has_a_start_token_on_linux(self) -> None:
        if sys.platform != "linux":
            pytest.skip("Linux /proc process-start tokens are unavailable")
        token = process_start_time()
        assert token is not None
        assert token.isdigit()

    def test_classify(self) -> None:
        assert classify_process_identity("12345") == "verified"
        if sys.platform == "linux":
            assert classify_process_identity(None) == "unverifiable"

    def test_liveness_of_a_dead_pid_is_dead(self) -> None:
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        process.wait()
        assert process_liveness(process.pid) == "dead"
        assert not is_process_alive(process.pid)

    def test_liveness_with_matching_token_is_verified(self) -> None:
        if sys.platform != "linux":
            pytest.skip("Linux /proc process-start tokens are unavailable")
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            token = process_start_time(process.pid)
            assert token is not None
            assert process_liveness(process.pid, token) == "verified"
            assert process_liveness(process.pid, "not-the-token") == "dead"
        finally:
            process.kill()
            process.wait()

    @pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc zombie state")
    def test_liveness_of_a_zombie_without_a_token_is_dead(self) -> None:
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        try:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                try:
                    stat = Path(f"/proc/{process.pid}/stat").read_text(encoding="utf-8")
                    state = stat.rsplit(")", 1)[1].split()[0]
                except FileNotFoundError:
                    state = "gone"
                if state == "Z":
                    assert process_liveness(process.pid) == "dead"
                    return
                time.sleep(0.01)
            pytest.fail("child did not exit while its parent kept it unreaped")
        finally:
            process.wait()

    @pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc access semantics")
    def test_liveness_stays_unverifiable_when_proc_stat_is_inaccessible(self) -> None:
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            with patch("acpc.proc.Path.read_text", side_effect=PermissionError("denied")):
                assert process_liveness(process.pid, "recorded-token") == "unverifiable"
        finally:
            process.kill()
            process.wait()


class TestProcessIdentitySignal:
    def test_real_process_token_refuses_mismatched_live_pid(self) -> None:
        """Prove the real-token fallback when PID wraparound is unavailable.

        The two processes stay alive while the second is checked, so this
        exercises the refusal path with real /proc tokens rather than mocks.
        """
        if sys.platform != "linux":
            pytest.skip("Linux /proc process-start tokens are unavailable")

        processes: list[subprocess.Popen[bytes]] = []
        try:
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    start_new_session=True,
                )
            )

            recorded_process = processes[0]
            recorded_token = process_start_time(recorded_process.pid)
            if recorded_token is None:
                pytest.skip("/proc process-start tokens are unreadable")

            time.sleep(2 / os.sysconf("SC_CLK_TCK"))
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    start_new_session=True,
                )
            )
            live_process = processes[1]
            assert recorded_process.pid != live_process.pid
            live_token = process_start_time(live_process.pid)
            if live_token is None:
                pytest.skip("/proc process-start tokens are unreadable")

            assert recorded_token != live_token
            assert recorded_process.poll() is None
            assert live_process.poll() is None

            result = kill_process_tree(
                live_process.pid,
                expected_process_start_time=recorded_token,
            )

            assert result == "refused"
            assert live_process.poll() is None
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
            for process in processes:
                process.wait()

    def test_refuses_when_identity_changes_before_signal(self) -> None:
        with (
            patch("acpc.proc.sys.platform", "linux"),
            patch("acpc.proc.os.pidfd_open", return_value=12, create=True),
            patch("acpc.proc.os.close"),
            patch("acpc.proc.os.getpgid", return_value=4242),
            patch("acpc.proc.process_start_time", return_value="new-token"),
            patch("acpc.proc.signal.pidfd_send_signal", create=True) as send_signal,
            patch("acpc.proc.os.killpg") as killpg,
        ):
            result = kill_process_tree(4242, expected_process_start_time="old-token")

        assert result == "refused"
        send_signal.assert_not_called()
        killpg.assert_not_called()

    def test_pidfd_preserves_process_group_signalling(self) -> None:
        with (
            patch("acpc.proc.sys.platform", "linux"),
            patch("acpc.proc.os.pidfd_open", return_value=12, create=True),
            patch("acpc.proc.os.close"),
            patch("acpc.proc.os.getpgid", return_value=4242),
            patch("acpc.proc.process_start_time", return_value="token"),
            patch("acpc.proc.signal.pidfd_send_signal", create=True) as send_signal,
            patch("acpc.proc.os.killpg") as killpg,
        ):
            kill_process_tree(4242, expected_process_start_time="token")

        send_signal.assert_called_once_with(12, signal.SIGKILL, None, 1 << 2)
        killpg.assert_not_called()

    def test_fallback_checks_group_then_token_immediately_before_signal(self) -> None:
        """Pin the TOCTOU guard: re-verify identity between group lookup and kill.

        The patched functions are thin kernel wrappers, so the sequence asserted
        here is the process's observable interaction with the OS boundary (like
        frames on a wire), not acpc-internal call order. Re-reading the start
        token immediately before ``killpg`` is the property that prevents
        signalling a recycled PGID; no on-disk or return-value observation can
        detect its loss.
        """
        events: list[str] = []

        def getpgid(pid: int) -> int:
            del pid
            events.append("getpgid")
            return 4242

        def start_time(pid: int) -> str:
            del pid
            events.append("start_time")
            return "token"

        def killpg(pgid: int, sig: signal.Signals) -> None:
            del pgid, sig
            events.append("killpg")

        with (
            patch("acpc.proc.sys.platform", "linux"),
            patch("acpc.proc.os.pidfd_open", None, create=True),
            patch("acpc.proc.signal.pidfd_send_signal", None, create=True),
            patch("acpc.proc.os.getpgid", side_effect=getpgid),
            patch("acpc.proc.process_start_time", side_effect=start_time),
            patch("acpc.proc.os.killpg", side_effect=killpg),
        ):
            result = kill_process_tree(4242, expected_process_start_time="token")

        assert result == "signalled"
        assert events == ["getpgid", "start_time", "killpg"]

    def test_refuses_non_leader_in_pidfd_path(self) -> None:
        with (
            patch("acpc.proc.sys.platform", "linux"),
            patch("acpc.proc.os.pidfd_open", return_value=12, create=True),
            patch("acpc.proc.os.close"),
            patch("acpc.proc.os.getpgid", return_value=4241),
            patch("acpc.proc.process_start_time", return_value="token"),
            patch("acpc.proc.signal.pidfd_send_signal", create=True) as send_signal,
            patch("acpc.proc.os.killpg") as killpg,
        ):
            result = kill_process_tree(4242, expected_process_start_time="token")

        assert result == "refused"
        send_signal.assert_not_called()
        killpg.assert_not_called()

    @pytest.mark.parametrize(
        ("error", "expected"),
        [(ProcessLookupError(), "already_gone"), (PermissionError(), "refused")],
    )
    def test_fallback_reports_signal_outcome(self, error: OSError, expected: str) -> None:
        with (
            patch("acpc.proc.sys.platform", "linux"),
            patch("acpc.proc.os.pidfd_open", None, create=True),
            patch("acpc.proc.signal.pidfd_send_signal", None, create=True),
            patch("acpc.proc.os.getpgid", return_value=4242),
            patch("acpc.proc.process_start_time", return_value="token"),
            patch("acpc.proc.os.killpg", side_effect=error),
        ):
            result = kill_process_tree(4242, expected_process_start_time="token")

        assert result == expected

    def test_kill_real_process_group(self) -> None:
        if sys.platform == "win32":
            pytest.skip("POSIX process groups only")
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        try:
            token = process_start_time(process.pid)
            result = kill_process_tree(process.pid, expected_process_start_time=token)
            assert result == "signalled"
            process.wait(timeout=5)
            assert process.returncode != 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
