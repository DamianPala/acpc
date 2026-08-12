"""Read an adapter's advertised permission modes and diff them against the entry.

Measuring what a mode actually *permits* is not part of this release: that needs
evidence read off disk after real turns, and the attribution engine that decided
those verdicts is deferred to 0.7 (see the backlog). What remains here answers
the cheap question honestly — what the adapter advertises, and how that differs
from the `[modes]` table the entry records.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from dataclasses import dataclass
from typing import Any

from acp import PROTOCOL_VERSION, RequestError
from acp.schema import (
    CreateTerminalResponse,
    KillTerminalResponse,
    PermissionOption,
    ReadTextFileResponse,
    ReleaseTerminalResponse,
    RequestPermissionResponse,
    TerminalOutputResponse,
    WaitForTerminalExitResponse,
    WriteTextFileResponse,
)

from acpc import paths, runner
from acpc.registry import AgentRegistry, ModeSpec, RegistryError, ResolvedEntry
from acpc.spawn import spawn_adapter

_WINDOWS_UNSUPPORTED_REASON = (
    "probe commands are POSIX shell commands and would measure the shell rather than the sandbox"
)


class ProbeError(RuntimeError):
    """The adapter could not be read."""


class _DiscoveryClient:
    """ACP client for a session that runs no turns.

    Discovery opens a session, reads its mode catalogue and releases it, so none
    of these callbacks should ever fire. They answer `method_not_found` rather
    than recording anything: there is no turn for their evidence to belong to.
    """

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        del session_id, update, kwargs

    async def request_permission(
        self,
        session_id: str,
        tool_call: Any,
        options: list[PermissionOption],
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        del session_id, tool_call, options, kwargs
        raise RequestError.method_not_found("session/request_permission")

    async def read_text_file(
        self, path: str, session_id: str, **kwargs: Any
    ) -> ReadTextFileResponse:
        del path, session_id, kwargs
        raise RequestError.method_not_found("fs/read_text_file")

    async def write_text_file(
        self, path: str, content: str, session_id: str, **kwargs: Any
    ) -> WriteTextFileResponse:
        del path, content, session_id, kwargs
        raise RequestError.method_not_found("fs/write_text_file")

    async def create_terminal(
        self,
        session_id: str,
        command: str,
        args: list[str] | None = None,
        env: list[Any] | None = None,
        cwd: str | None = None,
        output_byte_limit: int | None = None,
        **kwargs: Any,
    ) -> CreateTerminalResponse:
        del session_id, command, args, env, cwd, output_byte_limit, kwargs
        raise RequestError.method_not_found("terminal/create")

    async def terminal_output(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> TerminalOutputResponse:
        del session_id, terminal_id, kwargs
        raise RequestError.method_not_found("terminal/output")

    async def release_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> ReleaseTerminalResponse:
        del session_id, terminal_id, kwargs
        raise RequestError.method_not_found("terminal/release")

    async def wait_for_terminal_exit(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> WaitForTerminalExitResponse:
        del session_id, terminal_id, kwargs
        raise RequestError.method_not_found("terminal/wait_for_exit")

    async def kill_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> KillTerminalResponse:
        del session_id, terminal_id, kwargs
        raise RequestError.method_not_found("terminal/kill")

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        del method, params
        return {}

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        del method, params


def _mode_payload(modes: dict[str, ModeSpec] | Any) -> dict[str, dict[str, Any]]:
    return {
        name: {
            "grants": spec.grants,
            "delegates": spec.delegates,
            "escalates": spec.escalates,
        }
        for name, spec in modes.items()
    }


def _available_modes(session: Any) -> tuple[list[dict[str, str | None]], str | None]:
    state = getattr(session, "modes", None)
    if state is None:
        return [], None
    available = []
    for mode in getattr(state, "available_modes", []):
        available.append(
            {
                "id": str(mode.id),
                "name": str(mode.name),
                "description": getattr(mode, "description", None),
            }
        )
    return available, getattr(state, "current_mode_id", None)


def _discovery_diff(
    current: dict[str, dict[str, Any]], advertised: list[dict[str, str | None]]
) -> list[dict[str, Any]]:
    """Report both sides of the discovery comparison without inventing facts."""
    diff = []
    advertised_ids = {item["id"] for item in advertised}
    for item in advertised:
        mode = item["id"]
        if mode is not None and mode not in current:
            diff.append(
                {
                    "mode": mode,
                    "status": "advertised-missing",
                    "description": item["description"],
                    "current": None,
                    "proposed": None,
                }
            )
    for mode, facts in current.items():
        if mode not in advertised_ids:
            diff.append(
                {
                    "mode": mode,
                    "status": "entry-missing",
                    "description": None,
                    "current": facts,
                    "proposed": None,
                }
            )
    return diff


@dataclass(slots=True)
class ProbeReport:
    entry: ResolvedEntry
    advertised_modes: list[dict[str, str | None]]
    current_mode: str | None

    def payload(self) -> dict[str, Any]:
        current = _mode_payload(self.entry.modes)
        return {
            "entry": self.entry.entry,
            "base_adapter": self.entry.base_adapter,
            "discover_only": True,
            "turns": 0,
            "current_mode": self.current_mode,
            "advertised_modes": self.advertised_modes,
            "mode_reports": {},
            "verdicts": {},
            "refusal_violations": [],
            "implied_modes": {},
            "unmeasured": [],
            "current_modes": current,
            "diff": _discovery_diff(current, self.advertised_modes),
        }

    def text(self) -> str:
        payload = self.payload()
        lines = [
            f"entry        {self.entry.entry} ({self.entry.base_adapter})",
            f"discovery    {len(self.advertised_modes)} advertised mode(s); 0 turn(s)",
        ]
        if self.current_mode is not None:
            lines.append(f"current mode {self.current_mode}")
        lines.append("")
        lines.append("Advertised modes")
        for mode in self.advertised_modes:
            description = f" — {mode['description']}" if mode["description"] else ""
            lines.append(f"  {mode['id']}{description}")
        advertised_missing = [
            item for item in payload["diff"] if item["status"] == "advertised-missing"
        ]
        if advertised_missing:
            lines.append("")
            lines.append("Advertised modes absent from current [modes]")
            for item in advertised_missing:
                description = f" — {item['description']}" if item.get("description") else ""
                lines.append(f"  + {item['mode']}{description}")
        entry_missing = [item for item in payload["diff"] if item["status"] == "entry-missing"]
        if entry_missing:
            lines.append("")
            if not self.advertised_modes and self.entry.modes:
                lines.append(
                    "Entry modes absent from adapter catalogue "
                    "(empty discovery — table is operator-declared; do not strip it)"
                )
            else:
                lines.append("Entry modes absent from adapter catalogue")
            for item in entry_missing:
                lines.append(f"  - {item['mode']}")
        if not self.advertised_modes and self.entry.modes:
            lines.append("")
            lines.append(
                "note: adapter advertised no ACP modes; entry [modes] is Path B "
                "(docs/assumed). A one-sided diff is expected."
            )
        return "\n".join(lines) + "\n"


def _supports_session_close(initialize_response: Any) -> bool:
    capabilities = getattr(initialize_response, "agent_capabilities", None)
    session_capabilities = getattr(capabilities, "session_capabilities", None)
    return getattr(session_capabilities, "close", None) is not None


async def _run_async(entry: ResolvedEntry) -> ProbeReport:
    resolution = entry.resolve_call()
    command, args = runner.adapter_command(resolution)
    root = paths.ensure_private_dir(paths.acpc_home() / "probes")
    async with spawn_adapter(
        _DiscoveryClient(),
        command,
        *args,
        env=resolution.adapter_environment,
        cwd=str(root),
        drain_stderr=True,
    ) as (conn, _process):
        initialize_response = await conn.initialize(protocol_version=PROTOCOL_VERSION)
        session = await conn.new_session(cwd=str(root), mcp_servers=[])
        advertised, current_mode = _available_modes(session)
        if _supports_session_close(initialize_response):
            with contextlib.suppress(Exception):
                await conn.close_session(session_id=session.session_id)
        return ProbeReport(entry, advertised, current_mode)


def run(entry_name: str) -> ProbeReport:
    """Read one registry entry's advertised modes without using the daemon."""
    if sys.platform == "win32":
        raise ProbeError(_WINDOWS_UNSUPPORTED_REASON + "; probe is unavailable on Windows")
    try:
        entry = AgentRegistry().resolve(entry_name)
        return asyncio.run(_run_async(entry))
    except RegistryError:
        raise
    except (runner.RunnerError, OSError, RequestError) as error:
        raise ProbeError(str(error)) from None


def render_json(report: ProbeReport) -> str:
    """Serialize a report as one JSON document."""
    return json.dumps(report.payload(), ensure_ascii=False)
