"""Behavioral tests for reaching and starting a target's daemon."""

import asyncio
import sys
from pathlib import Path

import pytest

from acpc import daemon_client, ipc, runner
from acpc.registry import AgentRegistry

MOCK_AGENT_SCRIPT = str(Path(__file__).with_name("mock_agent.py"))

MOCK_ENTRY = f"""
name = "Mock Agent"
command = "{sys.executable} {MOCK_AGENT_SCRIPT}"
install_command = "true"
home = "~/.mock"
home_env = "MOCK_HOME"
efforts = ["low", "medium", "high", "xhigh"]

[presets]
standard = {{ model = "mock-sonnet-5", effort = "high" }}
"""


@pytest.fixture(autouse=True)
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "s"
    agents = root / "agents"
    agents.mkdir(parents=True)
    (agents / "mock.toml").write_text(MOCK_ENTRY, encoding="utf-8")
    monkeypatch.setenv("ACPC_HOME", str(root))
    return root


def target_name() -> str:
    return runner.call_target(AgentRegistry().resolve_call("mock"))


def test_a_cold_target_gets_a_daemon_started_for_it(live_daemon: None) -> None:
    async def scenario() -> dict:
        routed = await daemon_client.ensure_daemon(target_name())
        assert not isinstance(routed, daemon_client.DaemonUnavailable)
        try:
            return await routed.status()
        finally:
            await routed.close()

    status = asyncio.run(scenario())

    assert status["ok"]
    assert status["pid"] > 0


def test_a_second_caller_reuses_the_running_daemon(live_daemon: None) -> None:
    async def scenario() -> tuple[int, int]:
        pids = []
        for _ in range(2):
            routed = await daemon_client.ensure_daemon(target_name())
            assert not isinstance(routed, daemon_client.DaemonUnavailable)
            try:
                pids.append((await routed.status())["pid"])
            finally:
                await routed.close()
        return pids[0], pids[1]

    first, second = asyncio.run(scenario())

    assert first == second


def test_concurrent_callers_start_exactly_one_daemon(live_daemon: None) -> None:
    """The start lock is what keeps a cold-start stampede from forking daemons."""

    async def scenario() -> set[int]:
        target = target_name()
        routed = await asyncio.gather(*(daemon_client.ensure_daemon(target) for _ in range(5)))
        pids: set[int] = set()
        for daemon in routed:
            assert not isinstance(daemon, daemon_client.DaemonUnavailable)
            try:
                pids.add((await daemon.status())["pid"])
            finally:
                await daemon.close()
        return pids

    assert len(asyncio.run(scenario())) == 1


def test_a_daemon_of_another_build_stands_down(live_daemon: None) -> None:
    """SPEC `daemon`: version skew self-heals on connect."""

    async def scenario() -> tuple[int, int]:
        target = target_name()
        first = await daemon_client.ensure_daemon(target)
        assert not isinstance(first, daemon_client.DaemonUnavailable)
        try:
            old_pid = (await first.status())["pid"]
        finally:
            await first.close()

        # Pose as a different build; the running daemon should give way.
        transport = ipc.UnixSocketTransport(target)
        conn = await transport.connect()
        poser = daemon_client._SocketDaemon(target, transport, conn)
        try:
            reply = await poser.call({"op": "hello", "version": "0.0.0-other"})
        finally:
            await poser.close()
        assert reply["restart"] is True

        second = await daemon_client.ensure_daemon(target)
        assert not isinstance(second, daemon_client.DaemonUnavailable)
        try:
            return old_pid, (await second.status())["pid"]
        finally:
            await second.close()

    old_pid, new_pid = asyncio.run(scenario())

    assert old_pid != new_pid


def test_an_unusable_socket_path_degrades_with_a_reason() -> None:
    """A restricted sandbox has to fall back, not raise."""
    routed = asyncio.run(daemon_client.ensure_daemon("has/a/slash"))

    assert isinstance(routed, daemon_client.DaemonUnavailable)
    assert routed.reason


def test_cancelling_a_target_with_no_daemon_is_not_an_error() -> None:
    assert asyncio.run(daemon_client.cancel_turn(target_name(), "abcd")) is False


def test_connect_reports_no_daemon_rather_than_raising() -> None:
    assert asyncio.run(daemon_client.connect(target_name())) is None
