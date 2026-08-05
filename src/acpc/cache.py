"""Advertised adapter data under `cache/<agent>/`.

SPEC.md `agents`: modes, models and slash commands are announced by the
adapter only after session creation, so they are cached and "refreshed on
every real run". The runner calls `refresh_advertised` after every successful
turn with the data the ACP client captured.

This slice ships the seam, not the cache. A later slice replaces the body;
the signature is fixed here so the runner's call site is final.
"""

from collections.abc import Mapping
from typing import Any


def refresh_advertised(agent: str, advertised: Mapping[str, Any]) -> None:
    """Record an adapter's advertised modes, models and commands.

    Called on the happy path of every turn, so it must never raise: a caching
    problem is not a reason to fail a turn that already produced an answer.
    """
