"""Advertised adapter data under ``cache/<adapter>/``.

Adapters announce their modes, models and commands after ``session/new``.
This module keeps that data separate from registry definitions: variants use
their parent adapter's cache, and a failed cache write never changes the
result of a turn that already completed.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from acp import PROTOCOL_VERSION

from acpc import paths
from acpc.client import AcpcClient
from acpc.permissions import PermissionLevel
from acpc.spawn import spawn_adapter
from acpc.transcript import Transcript

if TYPE_CHECKING:
    from acpc.registry import CallResolution


_CACHE_FILE = "advertised.json"
_PROBE_UPDATE_WAIT = 0.2
_SENTENCE_END = re.compile(r"[.!?](?:\s|$)")


Clock = Callable[[], float]


def _resolve_clock(clock: Clock | None) -> Clock:
    """Every time-dependent entry point takes a `clock`; this is its default."""
    return time.time if clock is None else clock


class ProbeError(RuntimeError):
    """A live adapter probe could not launch or authenticate."""


@dataclass(frozen=True, slots=True)
class CachedAdvertised:
    """The data and publication time read from one adapter cache."""

    advertised: dict[str, Any]
    cached_at: float


def _cache_path(agent: str) -> Path:
    return paths.cache_dir() / agent


def _data_path(agent: str) -> Path:
    return _cache_path(agent) / _CACHE_FILE


def commands_path(agent: str) -> Path:
    """Return the full-description file named by the commands footer."""
    return _cache_path(agent) / "commands.md"


def refresh_advertised(
    agent: str,
    advertised: Mapping[str, Any],
    *,
    clock: Clock | None = None,
) -> None:
    """Atomically publish an adapter's advertised modes, models and commands.

    This is called on the happy path of every turn.  The broad exception
    boundary is intentional: cache state is auxiliary and must not turn a
    successful adapter answer into a failed session.
    """
    try:
        previous = read_advertised(agent)
        previous_data = previous.advertised if previous is not None else {}

        def catalog(name: str) -> list[Any]:
            incoming = advertised.get(name, [])
            if isinstance(incoming, list) and incoming:
                return list(incoming)
            saved = previous_data.get(name, [])
            return list(saved) if isinstance(saved, list) else []

        merged = {
            "modes": catalog("modes"),
            "models": catalog("models"),
            "commands": catalog("commands"),
        }
        if previous is not None and merged == previous.advertised:
            return

        cache_root = paths.ensure_private_dir(_cache_path(agent))
        payload = {"cached_at": _resolve_clock(clock)(), "advertised": merged}
        paths.atomic_write(cache_root / _CACHE_FILE, payload)

        commands = payload["advertised"]["commands"]
        lines: list[str] = []
        if isinstance(commands, list):
            for command in commands:
                if not isinstance(command, Mapping):
                    continue
                name = command.get("name", "")
                description = command.get("description", "")
                if not isinstance(name, str) or not isinstance(description, str):
                    continue
                lines.extend((f"# /{name.lstrip('/')}", "", description, ""))
        paths.atomic_write(commands_path(agent), "\n".join(lines))
    except Exception:  # noqa: BLE001
        return


def read_advertised(agent: str) -> CachedAdvertised | None:
    """Read one adapter cache, treating missing or damaged data as a miss."""
    try:
        value = json.loads(_data_path(agent).read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            return None
        cached_at = value.get("cached_at")
        advertised = value.get("advertised")
        if (
            isinstance(cached_at, bool)
            or not isinstance(cached_at, (int, float))
            or not isinstance(advertised, Mapping)
        ):
            return None
        return CachedAdvertised(
            advertised={
                "modes": list(advertised.get("modes", [])),
                "models": list(advertised.get("models", [])),
                "commands": list(advertised.get("commands", [])),
            },
            cached_at=float(cached_at),
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def cache_age(cached_at: float, *, clock: Clock | None = None) -> str:
    """Format a human-readable cache age; the clock is injectable for tests."""
    seconds = max(0, int(_resolve_clock(clock)() - cached_at))
    if seconds < 60:
        return "now"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def first_sentence(description: str) -> str:
    """Return a command description's first sentence."""
    match = _SENTENCE_END.search(description)
    if match is None:
        return description
    return description[: match.end()].strip()


async def probe_advertised(resolution: CallResolution) -> dict[str, Any]:
    """Launch an adapter, initialize it, and create a session for a live probe."""
    try:
        command = resolution.entry.command_args
    except Exception as error:  # noqa: BLE001
        raise ProbeError(str(error)) from None
    if not command or shutil.which(command[0]) is None:
        raise ProbeError(
            f"{resolution.entry.entry}: '{command[0] if command else ''}' is not installed — "
            f"run 'acpc install {resolution.entry.base_adapter}'"
        )

    with tempfile.TemporaryDirectory(prefix="acpc-probe-") as temporary:
        transcript = Transcript(Path(temporary) / "transcript.ndjson")
        client = AcpcClient(
            transcript,
            PermissionLevel.READ,
            bypass_modes=resolution.entry.bypass_modes,
        )
        try:
            async with spawn_adapter(
                client,
                command[0],
                *command[1:],
                env=resolution.adapter_environment,
                drain_stderr=False,
            ) as (connection, _process):
                await connection.initialize(protocol_version=PROTOCOL_VERSION)
                session = await connection.new_session(cwd=str(Path.cwd()), mcp_servers=[])
                client.capture_advertised(session)
                deadline = time.monotonic() + _PROBE_UPDATE_WAIT
                while not client.advertised["commands"] and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
        except Exception as error:  # noqa: BLE001
            raise ProbeError(f"{resolution.entry.entry}: live probe failed: {error}") from None

        advertised = client.advertised
    refresh_advertised(resolution.entry.base_adapter, advertised)
    return advertised
