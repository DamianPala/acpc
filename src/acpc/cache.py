"""Advertised adapter data under ``cache/<adapter>/``.

Adapters announce their modes, models and commands after ``session/new``.
This module keeps that data separate from registry definitions: variants use
their parent adapter's cache, and a failed cache write never changes the
result of a turn that already completed.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import shutil
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from acp import PROTOCOL_VERSION, RequestError

from acpc import paths
from acpc.client import AcpcClient
from acpc.permissions import ModeSelectionError, PermissionLevel, select_mode
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
    force: bool = False,
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
            "efforts": {**previous_data.get("efforts", {}), **advertised.get("efforts", {})},
        }
        if (
            not force
            and previous is not None
            and _without_observed_at(merged) == _without_observed_at(previous.advertised)
        ):
            return

        cache_root = paths.ensure_private_dir(_cache_path(agent))
        payload = {"cached_at": _resolve_clock(clock)(), "advertised": merged}
        paths.atomic_write(cache_root / _CACHE_FILE, payload)

        paths.atomic_write(commands_path(agent), _commands_markdown(merged["commands"]))
    except Exception:  # noqa: BLE001
        return


def _commands_markdown(commands: Any) -> str:
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
    return "\n".join(lines)


def _without_observed_at(advertised: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **advertised,
        "efforts": {
            key: {field: value for field, value in row.items() if field != "observed_at"}
            for key, row in advertised.get("efforts", {}).items()
        },
    }


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
                "efforts": _effort_rows(advertised.get("efforts")),
            },
            cached_at=float(cached_at),
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _effort_rows(value: Any) -> dict[str, Any]:
    """Keep the well-formed rows; a damaged one must not break readers or later merges."""
    if not isinstance(value, Mapping):
        return {}
    return {
        key: dict(row)
        for key, row in value.items()
        if isinstance(row, Mapping)
        and isinstance(row.get("levels"), list)
        and all(isinstance(level, str) for level in row["levels"])
        and (row.get("current", 0) is None or isinstance(row.get("current"), str))
        and type(row.get("observed_at")) in (int, float)
    }


# 9999-12-30 UTC: any local zone can still render it.
_LAST_RESET = 253_402_128_000


def _home_key(home: str | None) -> str:
    return "" if home is None else str(Path(home).expanduser())


def _finite(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _valid_window(value: Any) -> bool:
    # JSON admits NaN, Infinity and any integer; renderers must never raise on a stored row.
    return value is None or (
        isinstance(value, Mapping)
        and _finite(value.get("utilization"))
        and type(value.get("resets_at")) is int
        and 0 <= value["resets_at"] < _LAST_RESET
    )


def _windows_row(value: Any) -> dict[str, Any] | None:
    if (
        not isinstance(value, Mapping)
        or not _finite(value.get("observed_at"))
        or not isinstance(value.get("session_id"), str)
    ):
        return None
    for name in ("five_hour", "seven_day"):
        if name not in value:
            return None
        window = value[name]
        if not _valid_window(window):
            return None
    return dict(value)


def _read_window_homes(agent: str) -> dict[str, Any]:
    try:
        payload = json.loads((_cache_path(agent) / "limits.json").read_text(encoding="utf-8"))
        homes = payload.get("homes") if isinstance(payload, Mapping) else None
        return dict(homes) if isinstance(homes, Mapping) else {}
    except (OSError, TypeError, ValueError):
        return {}


def refresh_windows(
    agent: str,
    home: str | None,
    windows: Mapping[str, Any] | None,
    *,
    session_id: str,
    clock: Clock | None = None,
) -> None:
    """Publish a whole observation without letting auxiliary state fail a turn.

    The client's observed_at is authoritative; publication never restamps it.
    """
    try:
        if windows is None:
            return
        row = _windows_row({**windows, "session_id": session_id})
        if row is None:
            return
        homes = _read_window_homes(agent)
        key = _home_key(home)
        previous = _windows_row(homes.get(key))
        if previous is not None and previous["observed_at"] > row["observed_at"]:
            return
        homes[key] = row
        root = paths.ensure_private_dir(_cache_path(agent))
        paths.atomic_write(root / "limits.json", {"homes": homes})
    except Exception:  # noqa: BLE001
        return


def read_windows(agent: str, home: str | None) -> dict[str, Any] | None:
    """Read only this vendor home's observation; damaged rows are cache misses."""
    try:
        return _windows_row(_read_window_homes(agent).get(_home_key(home)))
    except (OSError, TypeError, ValueError):
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
    from acpc import runner as runner_module

    try:
        command, args = runner_module.adapter_command(resolution)
    except Exception as error:  # noqa: BLE001
        raise ProbeError(str(error)) from None
    if not command or shutil.which(command) is None:
        raise ProbeError(resolution.entry.missing_binary_error())

    with tempfile.TemporaryDirectory(prefix="acpc-probe-") as temporary:
        transcript = Transcript(Path(temporary) / "transcript.ndjson")
        client = AcpcClient(
            transcript,
            PermissionLevel.READ,
            modes={},
            effort_config_id=resolution.entry.effort_config_id,
        )
        try:
            async with spawn_adapter(
                client,
                command,
                *args,
                env=resolution.adapter_environment,
                drain_stderr=False,
            ) as (connection, _process):
                await connection.initialize(protocol_version=PROTOCOL_VERSION)
                session = await connection.new_session(cwd=str(Path.cwd()), mcp_servers=[])
                client.capture_advertised(session)
                # Apply the resolved options exactly as a run would: a config
                # the adapter rejects (wrong effort id, unknown model) must
                # fail the check, not first surface on a paid run.  Imported
                # lazily — runner imports this module at load time.
                if resolution.entry.modes and (
                    resolution.mode is None or resolution.mode_spec is None
                ):
                    try:
                        mode, spec = select_mode(resolution.entry.modes, "all")
                    except ModeSelectionError as error:
                        raise ProbeError(
                            f"{resolution.entry.entry}: cannot probe without a declared mode: "
                            f"{error}"
                        ) from None
                    resolution = replace(
                        resolution,
                        mode=mode,
                        mode_spec=spec,
                        permissions="all",
                    )

                if resolution.mode is not None:
                    await runner_module.apply_call_options(
                        connection,
                        session.session_id,
                        runner_module.TurnRequest(resolution=resolution, prompt=""),
                        observer=client,
                    )
                await _probe_models(connection, session.session_id, resolution, client)
                deadline = time.monotonic() + _PROBE_UPDATE_WAIT
                while not client.advertised["commands"] and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
        except Exception as error:  # noqa: BLE001
            raise ProbeError(f"{resolution.entry.entry}: live probe failed: {error}") from None

        advertised = client.advertised
    refresh_advertised(resolution.entry.base_adapter, advertised, force=True)
    return advertised


async def _probe_models(
    connection: Any, session_id: str, resolution: CallResolution, client: AcpcClient
) -> None:
    from acpc import runner

    if resolution.entry.model_via == "set_model":
        # session/set_model answers without config options: a switch would record nothing.
        return
    candidates = [
        resolution.model,
        *client.advertised["models"],
        *(preset.model for preset in resolution.entry.presets.values()),
        *resolution.entry.effort_by_model,
    ]
    for model in dict.fromkeys(candidates):
        if not model:
            continue
        try:
            response = await connection.set_config_option(
                session_id=session_id, config_id="model", value=model
            )
            runner.observe_config_options(client, response, model)
        except RequestError:
            # Catalogs and presets can contain models unavailable to this account.
            continue
