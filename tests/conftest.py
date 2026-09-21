"""Shared test policy.

The daemon is a real detached process with a 30-minute idle TTL. A test that
happens to call `run` should not leave one of those behind, so the default here
is the direct path — the same path a restricted sandbox gets. Tests that are
actually about the daemon ask for the `live_daemon` fixture, which lets them
through and stops what they started.
"""

import asyncio
import os
import time
from collections.abc import Iterator

import pytest

from acpc import daemon_client, proc, runner


@pytest.fixture(autouse=True)
def no_daemon(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Route every turn to a direct child unless the test opts out."""
    if "live_daemon" in request.fixturenames:
        return

    async def unavailable(target: str) -> daemon_client.DaemonUnavailable:
        return daemon_client.DaemonUnavailable(f"no daemon in this test ({target})")

    monkeypatch.setattr(daemon_client, "ensure_daemon", unavailable)


@pytest.fixture
def live_daemon() -> Iterator[None]:
    """Allow real daemons, and stop every one this test started.

    The state root is captured on the way in and restored for the sweep:
    teardown order does not guarantee that `ACPC_HOME` is still pointing at
    this test's directory, and sweeping the wrong root would find nothing and
    leave real processes behind.
    """
    home = os.environ.get("ACPC_HOME")
    yield
    previous = os.environ.get("ACPC_HOME")
    if home is not None:
        os.environ["ACPC_HOME"] = home
    try:
        asyncio.run(_stop_all())
    finally:
        if previous is None:
            os.environ.pop("ACPC_HOME", None)
        else:
            os.environ["ACPC_HOME"] = previous


async def _stop_all() -> None:
    for target in runner.all_daemon_targets():
        daemon = await daemon_client.connect(target)
        if daemon is None:
            continue
        pid = (await daemon.status()).get("pid")
        try:
            await daemon.stop()
        finally:
            await daemon.close()
        if isinstance(pid, int):
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and proc.process_cmdline(pid):
                await asyncio.sleep(0.02)
            assert not proc.process_cmdline(pid), f"daemon {pid} did not exit after stop"
