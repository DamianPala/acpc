"""Client side of the daemon protocol.

SPEC.md `daemon`: the daemon is a performance cache that keeps adapters warm.
It is never required — "if the daemon cannot start at all (restricted
sandboxes), `run` spawns the adapter as a direct child — visibly".

This slice ships the seam, not the daemon. `ensure_daemon` reports the daemon
as unavailable with a reason, which is exactly the contract the runner's
routing already handles: try the daemon, fold a visible note into the stderr
summary, run the adapter as a direct child. A later slice replaces the body
of `ensure_daemon` without touching `runner.py`.
"""

from dataclasses import dataclass
from typing import Protocol

from acpc.transcript import Transcript


@dataclass(frozen=True, slots=True)
class DaemonUnavailable:
    """Why this call could not use the daemon; `reason` reaches the summary."""

    reason: str


class DaemonConnection(Protocol):
    """What the runner needs from a live daemon connection.

    Pinned here so the routing code in `runner.py` is final: the daemon slice
    supplies an implementation, the runner is not touched again.
    """

    async def run_turn(
        self,
        session_id: str,
        prompt: str,
        *,
        transcript: Transcript,
    ) -> str:
        """Run one turn on the warm adapter and return its ACP stop reason."""
        ...

    async def cancel(self, session_id: str) -> None:
        """Ask the daemon to cancel a session's in-flight turn."""
        ...

    async def close(self) -> None:
        """Release this client's handle on the daemon."""
        ...


async def ensure_daemon(target: str) -> DaemonConnection | DaemonUnavailable:
    """Connect to the daemon serving `target`, starting it if needed.

    Returns a live connection, or `DaemonUnavailable` naming why the direct
    path has to be used instead.
    """
    return DaemonUnavailable(f"no daemon support in this build (target {target})")
