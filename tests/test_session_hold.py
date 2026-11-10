"""Finished adapter sessions are held, released and restored through the CLI."""

import json
import os
import signal
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from acpc import ipc, sessions
from acpc.cli import main
from tests.test_daemon import MOCK_ENTRY

pytestmark = pytest.mark.timeout(120)


@pytest.fixture
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "s"
    (root / "agents").mkdir(parents=True)
    (root / "agents/mock.toml").write_text(MOCK_ENTRY, encoding="utf-8")
    monkeypatch.setenv("ACPC_HOME", str(root))
    monkeypatch.setenv("ACPC_MOCK_ADVERTISE_CLOSE", "1")
    monkeypatch.setenv("ACPC_MOCK_SESSION_METHOD_FILE", str(root / "methods"))
    return root


def call(*args: str) -> dict[str, Any]:
    result = CliRunner().invoke(main, [*args, "--json"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def status() -> dict[str, Any]:
    return call("daemon", "status")["items"][0]


def methods(root: Path) -> list[str]:
    path = root / "methods"
    return path.read_text().splitlines() if path.exists() else []


def configure(root: Path, hold: str, *, cap: int = 8) -> None:
    (root / "config.toml").write_text(
        f'session_hold = "{hold}"\ndaemon_max_concurrent = {cap}\n', encoding="utf-8"
    )


def eventually(predicate: Callable[[], Any], *, timeout: float = 12) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition did not arrive before deadline"
        time.sleep(0.05)


def finished(entry: str = "mock") -> str:
    return call("run", entry, "echo:first")["session_id"]


def close_line(session_id: str) -> str:
    return f"close:{sessions.read_meta(session_id).adapter_session_id}"


def test_zero_hold_closes_and_continue_restores(state_root: Path, live_daemon: None) -> None:
    configure(state_root, "0s")
    session_id = finished()
    eventually(lambda: close_line(session_id) in methods(state_root))
    assert status()["held"] == []
    result = call("continue", session_id, "echo:second")
    assert result["resume"]
    recorded = methods(state_root)
    assert recorded.index("load") > recorded.index(close_line(session_id))


def test_idle_sweep_releases_after_hold_without_stopping_daemon(
    state_root: Path, live_daemon: None
) -> None:
    configure(state_root, "2s")
    session_id = finished()
    first_status = status()
    assert first_status["held"] == [session_id]
    assert methods(state_root) == []
    eventually(lambda: close_line(session_id) in methods(state_root))
    assert status()["held"] == []
    assert status()["pid"] == first_status["pid"]


def test_warm_continue_restarts_hold_and_running_turn_is_never_held_or_closed(
    state_root: Path, live_daemon: None
) -> None:
    configure(state_root, "2s")
    session_id = finished()
    release = state_root / "release"
    call("continue", session_id, f"chunkhold:{release}", "--bg")
    eventually(lambda: status()["sessions"] == [session_id])
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        assert status()["held"] == []
        assert methods(state_root) == []
        time.sleep(0.1)
    release.touch()
    call("wait", session_id)
    assert status()["held"] == [session_id]
    assert methods(state_root) == []
    eventually(lambda: close_line(session_id) in methods(state_root))
    assert status()["held"] == []


def test_hold_cap_releases_oldest_finished_session(state_root: Path, live_daemon: None) -> None:
    configure(state_root, "5m", cap=2)
    first, second, third = finished(), finished(), finished()
    eventually(lambda: bool(methods(state_root)))
    assert methods(state_root) == [close_line(first)]
    assert status()["held"] == sorted([second, third])


@pytest.mark.parametrize("delay", ["2", "7"])
def test_continue_waits_for_close_or_its_timeout(
    state_root: Path, live_daemon: None, monkeypatch: pytest.MonkeyPatch, delay: str
) -> None:
    monkeypatch.setenv("ACPC_MOCK_CLOSE_DELAY", delay)
    configure(state_root, "0s")
    session_id = finished()
    adapter_id = sessions.read_meta(session_id).adapter_session_id
    eventually(lambda: f"closing:{adapter_id}" in methods(state_root))
    assert status()["held"] == []
    result = call("continue", session_id, "echo:after close")
    assert result["resume"]
    recorded = methods(state_root)
    assert "load" in recorded
    if delay == "2":
        assert recorded.index("load") > recorded.index(close_line(session_id))


def test_failed_close_drops_mapping_and_continue_restores(
    state_root: Path, live_daemon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPC_MOCK_CLOSE_FAIL", "1")
    configure(state_root, "0s")
    session_id = finished()
    eventually(lambda: close_line(session_id) in methods(state_root))
    assert status()["held"] == []
    assert call("continue", session_id, "echo:still works")["resume"]
    assert "load" in methods(state_root)


def test_without_close_capability_session_stays_warm_past_hold(
    state_root: Path, live_daemon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ACPC_MOCK_ADVERTISE_CLOSE")
    configure(state_root, "1s")
    session_id = finished()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        assert status()["held"] == [session_id]
        assert methods(state_root) == []
        time.sleep(0.1)
    call("continue", session_id, "echo:warm")
    assert methods(state_root) == []
    assert status()["held"] == [session_id]


def test_status_reports_descendant_processes_and_text(state_root: Path, live_daemon: None) -> None:
    session_id = finished()
    processes = status()["processes"]
    assert processes["count"] == 1
    assert processes["rss_bytes"] > 0
    text = CliRunner().invoke(main, ["daemon", "status", "--format", "text"])
    assert "1 held" in text.stdout
    assert "processes" in text.stdout and "MiB" in text.stdout
    assert status()["held"] == [session_id]


@pytest.mark.parametrize("ps_result", ["missing", "error", "invalid", "timeout"])
def test_status_survives_unavailable_process_table(
    state_root: Path, live_daemon: None, monkeypatch: pytest.MonkeyPatch, ps_result: str
) -> None:
    bin_dir = state_root / "bin"
    bin_dir.mkdir()
    if ps_result != "missing":
        commands = {
            "error": "exit 1",
            "invalid": "printf 'broken\\n'",
            "timeout": "exec /bin/sleep 3",
        }
        script = bin_dir / "ps"
        script.write_text(f"#!/bin/sh\n{commands[ps_result]}\n")
        script.chmod(0o700)
    monkeypatch.setenv("PATH", str(bin_dir))
    finished()
    assert status()["processes"] == {"count": None, "rss_bytes": None}
    text = CliRunner().invoke(main, ["daemon", "status", "--format", "text"])
    assert "· processes · MiB" in text.stdout


@pytest.mark.parametrize("entry", ["mock", "variant"])
def test_resolve_names_the_run_target_without_starting_a_daemon(
    state_root: Path, live_daemon: None, entry: str
) -> None:
    if entry == "variant":
        (state_root / "agents/variant.toml").write_text(
            f'extends = "mock"\nhome = "{state_root / "other-home"}"\n'
        )
    preview = call("resolve", entry)
    assert call("daemon", "status")["items"] == []
    finished(entry)
    assert preview["daemon_target"] == status()["target"]
    text = CliRunner().invoke(main, ["resolve", entry, "--format", "text"])
    assert f"daemon       {preview['daemon_target']}" in text.stdout


def test_status_sums_all_descendants_and_excludes_daemon_and_unrelated_processes(
    state_root: Path, live_daemon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = state_root / "bin"
    bin_dir.mkdir()
    script = bin_dir / "ps"
    script.write_text(
        "#!/bin/sh\n"
        "printf '900003 900002 16\\n900004 1 9999\\n'\n"
        "printf '%s 1 100000\\n' \"$PPID\"\n"
        "printf '900001 %s 4\\n900002 900001 8\\n' \"$PPID\"\n"
    )
    script.chmod(0o700)
    monkeypatch.setenv("PATH", str(bin_dir))
    finished()
    assert status()["processes"] == {"count": 3, "rss_bytes": 28 * 1024}


def terminal_turn(root: Path, entry: str, state: str) -> str:
    if state == "failed":
        result = CliRunner().invoke(main, ["run", entry, "crash-late:delivered", "--json"])
    else:
        session_id = call("run", entry, f"chunkhold:{root / 'release'}", "--bg")["session_id"]
        eventually(lambda: "holding" in sessions.transcript_path(session_id).read_text())
        call("cancel", session_id)
        result = CliRunner().invoke(main, ["wait", session_id, "--json"])
    assert result.exit_code != 0, result.output
    session_id = json.loads(result.stdout)["session_id"]
    assert sessions.read_meta(session_id).state == state
    return session_id


@pytest.mark.parametrize("state", ["failed", "canceled"])
@pytest.mark.parametrize("one_shot", [False, True])
def test_failed_and_canceled_turns_close_without_being_held(
    state_root: Path, live_daemon: None, state: str, one_shot: bool
) -> None:
    configure(state_root, "5m" if one_shot else "0s")
    if one_shot:
        (state_root / "agents/judge.toml").write_text('extends = "mock"\none_shot = true\n')
    session_id = terminal_turn(state_root, "judge" if one_shot else "mock", state)
    eventually(lambda: close_line(session_id) in methods(state_root))
    assert status()["held"] == []
    assert methods(state_root).count(close_line(session_id)) == 1


@pytest.mark.parametrize("flag", ["permissions", "effort"])
def test_resolve_target_tracks_target_changing_flags(
    state_root: Path, live_daemon: None, flag: str
) -> None:
    if flag == "effort":
        (state_root / "agents/mock.toml").write_text(
            MOCK_ENTRY.replace(
                'home_env = "MOCK_HOME"',
                'home_env = "MOCK_HOME"\neffort_via = "cli"\neffort_cli_flag = "--mock-effort"',
            )
        )
    default = call("resolve", "mock")["daemon_target"]
    flags = ["--permissions", "all"] if flag == "permissions" else ["--effort", "low"]
    preview = call("resolve", "mock", *flags)["daemon_target"]
    session_id = call("run", "mock", "echo:flag", *flags)["session_id"]
    assert preview == status()["target"] == sessions.read_meta(session_id).target
    assert preview != default


def test_adapter_death_discards_held_sessions_before_enforcing_cap(
    state_root: Path, live_daemon: None
) -> None:
    configure(state_root, "5m", cap=1)
    old = finished()
    daemon_pid = status()["pid"]
    children = Path(f"/proc/{daemon_pid}/task/{daemon_pid}/children")
    if not children.exists():
        pytest.skip("requires /proc/<pid>/children")
    adapter_pids = [int(pid) for pid in children.read_text().split()]
    assert len(adapter_pids) == 1
    os.kill(adapter_pids[0], signal.SIGKILL)
    eventually(lambda: not Path(f"/proc/{adapter_pids[0]}").exists())
    first = call("run", "mock", "echo:healed", "--bg")["session_id"]
    call("wait", first, "--timeout", "10s")
    assert status()["held"] == [first]
    assert old not in status()["held"]
    second = call("run", "mock", "echo:capacity", "--bg")["session_id"]
    call("wait", second, "--timeout", "10s")
    eventually(lambda: close_line(first) in methods(state_root))
    assert status()["held"] == [second]


def test_process_table_is_read_only_for_daemon_status(
    state_root: Path, live_daemon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = state_root / "bin"
    bin_dir.mkdir()
    trace = state_root / "ps-calls"
    script = bin_dir / "ps"
    # macOS uses per-pid ps for liveness too; trace only whole-table sampling.
    script.write_text(
        f"#!/bin/sh\nif [ \"${{1-}}\" = -A ]; then printf 'ps\\n' >> '{trace}'; fi\n"
        'exec /bin/ps "$@"\n'
    )
    script.chmod(0o700)
    monkeypatch.setenv("PATH", str(bin_dir))
    session_id = finished()
    call("status", session_id)
    target = sessions.read_meta(session_id).target
    assert target is not None
    with socket.socket(socket.AF_UNIX) as observer:
        observer.settimeout(10)
        observer.connect(str(ipc.socket_path_for_target(target)))
        observer.sendall(b'{"op":"status"}\n')
        with observer.makefile("rb") as stream:
            assert "processes" not in json.loads(stream.readline())
    assert not trace.exists()
    processes = status()["processes"]
    assert processes["count"] == 1 and processes["rss_bytes"] > 0
    assert trace.read_text().splitlines() == ["ps"]


def test_old_daemon_status_defaults_new_fields_at_the_socket_boundary(state_root: Path) -> None:
    target = "old-mock"
    directory = state_root / "daemon"
    directory.mkdir()
    (directory / f"{target}.lock").touch()
    reply = (
        b'{"ok":true,"target":"old-mock","version":"1.2.0","pid":123,'
        b'"uptime_seconds":1,"log":"/tmp/old-mock.log","sessions":[],'
        b'"preparing":[],"restoring":[],"max_concurrent":8}\n'
    )
    frames = []
    with socket.socket(socket.AF_UNIX) as endpoint:
        endpoint.bind(str(ipc.socket_path_for_target(target)))
        endpoint.listen()
        endpoint.settimeout(10)

        def serve() -> None:
            for _ in range(2):
                connection, _ = endpoint.accept()
                with connection, connection.makefile("rwb") as stream:
                    frames.append(json.loads(stream.readline()))
                    stream.write(reply)
                    stream.flush()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        payload = status()
        text = CliRunner().invoke(main, ["daemon", "status", "--format", "text"])
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert payload["held"] == []
    assert payload["processes"] == {"count": None, "rss_bytes": None}
    assert text.exit_code == 0 and "0 held" in text.stdout
    assert "· processes · MiB" in text.stdout
    assert frames == [{"op": "status", "processes": True}] * 2
