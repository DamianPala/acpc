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
    _ps_fields,
    _PsUnavailableError,
    classify_process_identity,
    is_process_alive,
    kill_process_tree,
    process_cmdline,
    process_group_kwargs,
    process_identity_supported,
    process_liveness,
    process_start_time,
)


def _lstart_under(locale_name: str) -> str:
    """`ps`'s `lstart` for this very process, rendered under `locale_name`."""
    completed = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(os.getpid())],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "LC_ALL": locale_name},
    )
    return completed.stdout.strip()


_NON_C_LOCALE_CANDIDATES = ("de_DE.UTF-8", "fr_FR.UTF-8", "pl_PL.UTF-8")


def _non_c_locale() -> str | None:
    """Return a common installed locale that renders `lstart` differently from C."""
    under_c = _lstart_under("C")
    for name in _NON_C_LOCALE_CANDIDATES:
        if _lstart_under(name) != under_c:
            return name
    return None


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


class TestPsFields:
    """`_ps_fields` is the shared foundation of the darwin backend, exercised
    directly here since Linux's procps `ps` accepts the same column spelling."""

    def test_live_process_returns_stat_and_lstart_and_separate_command(self) -> None:
        process = subprocess.Popen(["sleep", "30"])
        try:
            fields = _ps_fields(process.pid, "stat", "lstart")
            assert fields is not None
            assert len(fields) == 2
            stat, lstart = fields
            assert stat
            assert lstart
            command_fields = _ps_fields(process.pid, "command")
            assert command_fields is not None
            assert "sleep" in command_fields[0]
        finally:
            process.kill()
            process.wait()

    def test_dead_pid_returns_none(self) -> None:
        process = subprocess.Popen(["sleep", "0"])
        process.wait()
        assert _ps_fields(process.pid, "stat") is None

    def test_missing_ps_binary_raises_the_private_unavailable_error(self, tmp_path: Path) -> None:
        """`ps` failing to run is an infrastructure failure, not a dead pid: it must
        be distinguishable from "the pid is gone" so callers don't conflate the two
        (see `TestDarwinLiveness::test_ps_unavailable_is_unverifiable_not_dead`)."""
        process = subprocess.Popen(["sleep", "30"])
        try:
            with (
                patch.dict(os.environ, {"PATH": str(tmp_path)}),
                pytest.raises(_PsUnavailableError),
            ):
                _ps_fields(process.pid, "stat")
        finally:
            process.kill()
            process.wait()


class TestDarwinProcessIdentity:
    """Runs the darwin backend on Linux's ps: `sys.platform` is patched to
    ``"darwin"`` while the real `ps` underneath is procps, not BSD `ps`, but
    both accept the same `-o <col>=` spelling for the columns used here."""

    def test_identity_supported_on_darwin(self) -> None:
        with patch.object(sys, "platform", "darwin"):
            assert process_identity_supported() is True

    def test_start_time_of_a_live_process_is_stable(self) -> None:
        process = subprocess.Popen(["sleep", "30"])
        try:
            with patch.object(sys, "platform", "darwin"):
                token = process_start_time(process.pid)
                assert token
                assert process_start_time(process.pid) == token
        finally:
            process.kill()
            process.wait()

    def test_start_time_of_a_dead_pid_is_none(self) -> None:
        process = subprocess.Popen(["sleep", "0"])
        process.wait()
        with patch.object(sys, "platform", "darwin"):
            assert process_start_time(process.pid) is None

    def test_start_time_token_does_not_change_with_the_caller_s_locale(self) -> None:
        """`ps`'s `lstart` is `strftime(..., "%c", ...)`, which depends on `LC_TIME`.

        The token is written by one process and compared by another later
        (`sessions.py:971`/`:1054` write it, `sessions.py:666` reads it back), so if
        `_ps_fields` didn't pin the locale it renders in, a daemon started under one
        `LC_ALL` and a caller reading under another would see a live process as dead.
        """
        other_locale = _non_c_locale()
        if other_locale is None:
            candidates = ", ".join(_NON_C_LOCALE_CANDIDATES)
            pytest.skip(
                f"none of the candidate locales rendered lstart differently from C: {candidates}"
            )
        process = subprocess.Popen(["sleep", "30"])
        try:
            with patch.object(sys, "platform", "darwin"):
                with patch.dict(os.environ, {"LC_ALL": other_locale}):
                    token_under_other_locale = process_start_time(process.pid)
                with patch.dict(os.environ, {"LC_ALL": "C"}):
                    token_under_c = process_start_time(process.pid)
            assert token_under_other_locale is not None
            assert token_under_other_locale == token_under_c
        finally:
            process.kill()
            process.wait()

    def test_cmdline_of_a_live_process_contains_the_command(self) -> None:
        process = subprocess.Popen(["sleep", "30"])
        try:
            with patch.object(sys, "platform", "darwin"):
                command_line = process_cmdline(process.pid)
            assert command_line is not None
            assert "sleep" in command_line
        finally:
            process.kill()
            process.wait()


class TestDarwinLiveness:
    """Same rationale as TestDarwinProcessIdentity: real processes, patched platform."""

    def test_live_process_with_token_uses_one_ps_call_and_matching_start_token(self) -> None:
        process = subprocess.Popen(["sleep", "30"])
        try:
            with patch.object(sys, "platform", "darwin"):
                token = process_start_time(process.pid)
                assert token is not None
                combined_fields = _ps_fields(process.pid, "stat", "lstart")
                assert combined_fields is not None
                assert combined_fields[1] == token
                with patch("acpc.proc.subprocess.run", wraps=subprocess.run) as run_spy:
                    assert process_liveness(process.pid, token) == "verified"
            assert run_spy.call_count == 1
            assert run_spy.call_args is not None
            assert run_spy.call_args.args[0][:3] == ["ps", "-o", "stat=,lstart="]
        finally:
            process.kill()
            process.wait()

    def test_live_process_with_a_different_token_is_dead(self) -> None:
        process = subprocess.Popen(["sleep", "30"])
        try:
            with patch.object(sys, "platform", "darwin"):
                assert process_liveness(process.pid, "not-the-token") == "dead"
        finally:
            process.kill()
            process.wait()

    def test_live_process_without_a_token_is_unverifiable(self) -> None:
        process = subprocess.Popen(["sleep", "30"])
        try:
            with (
                patch.object(sys, "platform", "darwin"),
                patch("acpc.proc.subprocess.run", wraps=subprocess.run) as run_spy,
            ):
                assert process_liveness(process.pid) == "unverifiable"
            assert run_spy.call_count == 1
            assert run_spy.call_args is not None
            assert run_spy.call_args.args[0][:3] == ["ps", "-o", "stat="]
        finally:
            process.kill()
            process.wait()

    def test_ps_unavailable_is_unverifiable_not_dead(self, tmp_path: Path) -> None:
        """`ps` failing to run must read as "cannot tell", not "dead".

        `sessions.py`'s `_verify_liveness` persists a `dead` verdict as a
        permanent `state = "unknown"` (`exit_code = EXIT_AGENT_ERROR`) -- so if a
        transient infrastructure failure (missing `ps`, or a `fork`/`posix_spawn`
        failure under `RLIMIT_NPROC`) mapped to `dead`, a healthy, live session
        would be marked dead forever. Linux's equivalent infrastructure failure
        (`OSError` reading `/proc/<pid>/stat`) already returns `unverifiable`;
        this pins the same contract for darwin.
        """
        process = subprocess.Popen(["sleep", "30"])
        try:
            with (
                patch.object(sys, "platform", "darwin"),
                patch.dict(os.environ, {"PATH": str(tmp_path)}),
            ):
                assert process_liveness(process.pid) == "unverifiable"
                assert process_liveness(process.pid, "some-token") == "unverifiable"
                assert process_start_time(process.pid) is None
        finally:
            process.kill()
            process.wait()

    def test_zombie_child_is_dead(self) -> None:
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        try:
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                # `ps`, not procfs: the branch this pins is the one macOS takes,
                # so the test itself has to be runnable there, where /proc is absent.
                state = subprocess.run(
                    ["ps", "-o", "stat=", "-p", str(process.pid)],
                    capture_output=True,
                    text=True,
                    check=False,
                ).stdout.strip()
                if state.startswith("Z"):
                    with patch.object(sys, "platform", "darwin"):
                        assert process_liveness(process.pid) == "dead"
                    return
                time.sleep(0.01)
            pytest.fail("child did not become a zombie while its parent kept it unreaped")
        finally:
            process.wait()

    def test_dead_pid_is_dead(self) -> None:
        process = subprocess.Popen(["sleep", "0"])
        process.wait()
        with patch.object(sys, "platform", "darwin"):
            assert process_liveness(process.pid) == "dead"
            # The pid vanishing between `kill 0` and `ps`: the `ps` read itself says gone.
            with patch("acpc.proc.os.kill"):
                assert process_liveness(process.pid) == "dead"
                assert process_liveness(process.pid, "some-token") == "dead"

    def test_combined_token_matches_start_time_when_ps_pads_the_day(self, tmp_path: Path) -> None:
        """BSD `ps` pads `stat` and renders `%c` with a space-padded day (`Sep  4`)."""
        fake_ps = tmp_path / "ps"
        fake_ps.write_text(
            '#!/bin/sh\ncase "$2" in\n'
            '  lstart=) echo "Thu Sep  4 09:45:08 2026    " ;;\n'
            '  *) echo "Ss   Thu Sep  4 09:45:08 2026    " ;;\nesac\n',
            encoding="utf-8",
        )
        fake_ps.chmod(0o755)
        with (
            patch.object(sys, "platform", "darwin"),
            patch.dict(os.environ, {"PATH": str(tmp_path)}),
        ):
            token = process_start_time(os.getpid())
            assert token == "Thu Sep 4 09:45:08 2026"
            assert process_liveness(os.getpid(), token) == "verified"


class TestDarwinKillProcessTree:
    """Same rationale as TestDarwinProcessIdentity: real process groups, patched platform."""

    def test_signals_a_correctly_identified_group_leader(self) -> None:
        process = subprocess.Popen(["sleep", "30"], start_new_session=True)
        try:
            with patch.object(sys, "platform", "darwin"):
                token = process_start_time(process.pid)
                assert token is not None
                result = kill_process_tree(process.pid, expected_process_start_time=token)
            assert result == "signalled"
            process.wait(timeout=5)
            assert process.returncode != 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_refuses_a_stale_token(self) -> None:
        process = subprocess.Popen(["sleep", "30"], start_new_session=True)
        try:
            with patch.object(sys, "platform", "darwin"):
                result = kill_process_tree(process.pid, expected_process_start_time="stale-token")
            assert result == "refused"
            assert process.poll() is None
        finally:
            process.kill()
            process.wait()
