"""Executable evidence for the CLI Design Standard claim.

The command set and its input descriptors come from ``acpc schema`` and the
Click tree.  The few scenario helpers below create state needed to exercise a
published command; they are never used to decide which commands exist.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import pty
import random
import re
import select
import signal
import subprocess
import sys
import threading
import time
import warnings
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any, NoReturn

import click
import pytest
from click.testing import CliRunner

import acpc.cli as cli_module
from acpc import daemon_client, errors, proc, runner, sessions, transcript, vocab
from acpc.cli import main

MOCK_AGENT_SCRIPT = str(Path(__file__).with_name("mock_agent.py"))
MOCK_ENTRY = f'''
name = "Mock Agent"
command = "{sys.executable} {MOCK_AGENT_SCRIPT}"
install_command = "true"
home = "~/.mock"
home_env = "MOCK_HOME"

[modes]
default = {{ grants = "read", delegates = true }}
'''

STANDARD_SNAPSHOT = Path(__file__).with_name("fixtures") / "cli-design-standard.md"
STANDARD_METADATA = STANDARD_SNAPSHOT.with_suffix(".meta.json")
EXPECTED_TOOL_VERSION = "0.7.1"
EXPECTED_STANDARD_NAME = "cli-design-standard"
EXPECTED_STANDARD_VERSION = "0.1.0-draft.7"
EXPECTED_EXTENSIONS = ["managed"]
REQUIRED_STANDARD_CLAUSES = (
    (
        "The identifier MUST remain usable after the initiating process exits and, until expiry "
        "under a documented retention policy, MUST NOT resolve to a different entity."
    ),
    (
        "`--limit N` is the maximum number of records emitted from the selected position in the "
        "documented order; it does not select that position."
    ),
    (
        "`--follow`, an unbounded mode selected explicitly under I8c, removes the default window "
        "when offered."
    ),
)
EXPECTED_EXIT_DESCRIPTIONS = {
    "0": "Success, including an empty result: the turn ended normally, or the view rendered.",
    "1": (
        "Generic failure: the agent errored — a crash, a refusal, exhausted context, "
        "missing auth — or the command could not do what was asked."
    ),
    "2": (
        "Usage error: the call cannot be accepted in this form — bad flags, a mode that "
        "exceeds the policy, or a policy no declared mode satisfies."
    ),
    "4": (
        "Output budget exhausted: `log --follow` stopped because `--max-output` ran out "
        "before the session ended; the footer's cursor covers what was printed."
    ),
    "124": (
        "Timeout: `run --timeout` stopped waiting and left the session running; `wait` "
        "and `log --wait-new` do the same."
    ),
    "130": (
        "Cancelled by SIGINT or `acpc cancel`. Answer-printing commands mirror the session "
        "result, so `wait` on a cancelled session also exits 130."
    ),
    "141": "SIGPIPE: a downstream reader closed the pipe.",
    "143": "SIGTERM: the client detached from a daemon-owned session, or ended the turn.",
}
SCHEMA_KEYS = {"type", "enum", "properties", "required", "items"}
EXPECTED_REQUIRED_FIELDS = {
    "agents check": {"items", "has_more"},
    "agents create": {"name", "extends", "path", "changed"},
    "agents delete": {"name", "path", "changed"},
    "agents get": {"agent"},
    "agents list": {"items", "has_more"},
    "cancel": {"session_id", "status", "stop_reason", "changed"},
    "continue": {
        "status",
        "session_id",
        "created_at",
        "started_at",
        "finished_at",
        "paths",
        "truncated",
        "partial",
        "denied",
        "permissions_clamp",
        "changed",
    },
    "daemon status": {"items", "has_more"},
    "daemon stop": {"targets", "changed", "requires_confirmation"},
    "delete": {"session_id", "removed", "changed", "paths"},
    "install": {"agent", "ok", "returncode", "changed"},
    "list": {"items", "has_more"},
    "log": {"i", "ts", "type"},
    "probe": {
        "entry",
        "base_adapter",
        "discover_only",
        "turns",
        "current_mode",
        "advertised_modes",
        "mode_reports",
        "verdicts",
        "refusal_violations",
        "implied_modes",
        "unmeasured",
        "current_modes",
        "diff",
    },
    "prune": {"targets", "changed", "requires_confirmation"},
    "resolve": {"entry", "base_adapter", "command", "cwd", "env", "env_passthrough", "resolved"},
    "run": {
        "status",
        "session_id",
        "created_at",
        "started_at",
        "finished_at",
        "paths",
        "truncated",
        "partial",
        "denied",
        "permissions_clamp",
        "changed",
    },
    "skills get": {"name", "description", "path", "body"},
    "skills list": {"items", "has_more"},
    "status": {
        "session_id",
        "status",
        "pid",
        "turns",
        "entry",
        "base_adapter",
        "model",
        "name",
        "runtime_seconds",
        "idle_seconds",
        "tokens",
        "cost",
        "exit_code",
        "stop_reason",
        "failure",
        "paths",
        "created_at",
        "started_at",
        "finished_at",
    },
    "steer": {
        "status",
        "session_id",
        "created_at",
        "started_at",
        "finished_at",
        "paths",
        "truncated",
        "partial",
        "denied",
        "permissions_clamp",
        "changed",
    },
    "wait": {
        "status",
        "session_id",
        "created_at",
        "started_at",
        "finished_at",
        "stop_reason",
        "cost",
        "answer",
        "paths",
        "truncated",
        "partial",
        "denied",
        "permissions_clamp",
    },
}
# The commands that print an answer, and therefore share one result shape
# covering success and failure, one `partial` marker and one
# `output_description` stating when a failure still answers.
ANSWER_COMMANDS = frozenset({"run", "continue", "steer", "wait"})

EXPECTED_OUTPUT_ENUMS = {
    "agents list.output.items[].kind": {"adapter", "variant"},
    "cancel.output.status": {"running", "succeeded", "failed", "canceled", "unknown"},
    "continue.output.status": {"running", "succeeded", "failed", "canceled", "unknown"},
    "list.output.items[].status": {
        "starting",
        "running",
        "preparing",
        "succeeded",
        "failed",
        "canceled",
        "unknown",
    },
    "log.output.type": {"error", "msg", "permission", "state", "thought", "tool", "usage"},
    "probe.output.diff[].status": {"advertised-missing", "entry-missing"},
    "run.output.status": {
        "starting",
        "running",
        "succeeded",
        "failed",
        "canceled",
        "unknown",
    },
    "status.output.status": {
        "starting",
        "running",
        "preparing",
        "succeeded",
        "failed",
        "canceled",
        "unknown",
    },
    "steer.output.status": {"running", "succeeded", "failed", "canceled", "unknown"},
    "wait.output.status": {
        "starting",
        "running",
        "succeeded",
        "failed",
        "canceled",
        "unknown",
    },
}

EXPECTED_EFFECTS = {
    "agents check": "read_only",
    "agents create": "non_idempotent",
    "agents delete": "non_idempotent",
    "agents get": "read_only",
    "agents list": "read_only",
    "cancel": "idempotent",
    "continue": "non_idempotent",
    "daemon status": "read_only",
    "daemon stop": "idempotent",
    "delete": "non_idempotent",
    "install": "non_idempotent",
    "list": "read_only",
    "log": "read_only",
    "probe": "read_only",
    "prune": "non_idempotent",
    "resolve": "read_only",
    "run": "non_idempotent",
    "skills get": "read_only",
    "skills list": "read_only",
    "status": "read_only",
    "steer": "non_idempotent",
    "wait": "read_only",
}

MUTATING_ORACLE_NOTES = {
    "agents create": "file creation changes the registry and reports changed=true",
    "agents delete": "file deletion changes the registry and reports changed=true",
    "cancel": "finished-session cancellation is an observed idempotent no-op",
    "continue": "a follow-up changes the session transcript and reports changed=true",
    "daemon stop": "dry-run over an absent target is an observed idempotent no-op",
    "delete": "deletion clears session data and leaves a reservation marker",
    "install": "the external installer owns the transition, so changed=null is required",
    "prune": "dry-run is unchanged; mutation clears data and leaves a marker",
    "run": "starting a turn creates a session and reports changed=true",
    "steer": "steering a live turn changes the managed session and reports changed=true",
}

_SESSION_OUTPUT_PROPERTIES = frozenset(
    {
        "session_id",
        "status",
        "stop_reason",
        "paths",
        "cost",
        "answer",
        "truncated",
        "partial",
        "output_file",
        "denied",
        "permissions_clamp",
        "next",
        "resume",
        "created_at",
        "started_at",
        "finished_at",
        "changed",
    }
)
_PATHS_PROPERTIES = frozenset({"dir", "prompt", "transcript", "answer"})
_DENIAL_PROPERTIES = frozenset({"category", "count", "minimum_policy", "remedy", "target"})
_PERMISSIONS_CLAMP_PROPERTIES = frozenset({"requested", "ceiling", "effective"})
_RESOLUTION_PROPERTIES = frozenset({"model", "effort", "mode", "permissions", "home"})
_RESOLUTION_FIELD_PROPERTIES = frozenset(
    {"value", "source", "grants", "delegates", "escalates", "clamp"}
)
_RESOLUTION_FIELD_REQUIRED = frozenset({"value", "source"})
_RESOLUTION_CLAMP_REQUIRED = frozenset({"requested", "ceiling", "effective"})


def _session_output_oracle(
    required: set[str], *, include_changed: bool = True
) -> dict[str, tuple[frozenset[str], frozenset[str]]]:
    properties = (
        _SESSION_OUTPUT_PROPERTIES if include_changed else _SESSION_OUTPUT_PROPERTIES - {"changed"}
    )
    return {
        "output": (properties, frozenset(required)),
        "output.paths": (_PATHS_PROPERTIES, frozenset(_PATHS_PROPERTIES)),
        "output.denied[]": (_DENIAL_PROPERTIES, frozenset(_DENIAL_PROPERTIES - {"target"})),
        "output.permissions_clamp": (
            _PERMISSIONS_CLAMP_PROPERTIES,
            frozenset(_PERMISSIONS_CLAMP_PROPERTIES),
        ),
    }


EXPECTED_OUTPUT_ORACLES: dict[str, dict[str, tuple[frozenset[str], frozenset[str]]]] = {
    "agents check": {
        "output": (frozenset({"items", "has_more"}), frozenset({"items", "has_more"})),
        "output.items[]": (
            frozenset({"agent", "ok", "models", "error"}),
            frozenset({"agent", "ok"}),
        ),
    },
    "agents create": {
        "output": (
            frozenset({"name", "extends", "path", "changed"}),
            frozenset({"name", "extends", "path", "changed"}),
        )
    },
    "agents delete": {
        "output": (
            frozenset({"name", "path", "changed"}),
            frozenset({"name", "path", "changed"}),
        )
    },
    "agents get": {
        "output": (
            frozenset(
                {
                    "agent",
                    "base_adapter",
                    "description",
                    "resolved",
                    "env",
                    "env_passthrough",
                    "advertised",
                    "presets",
                    "models",
                    "commands",
                }
            ),
            frozenset({"agent"}),
        ),
        "output.resolved": (
            frozenset({"model", "effort", "mode", "permissions", "home"}),
            frozenset({"model", "effort", "mode", "permissions", "home"}),
        ),
        "output.resolved.model": (
            frozenset({"value", "source"}),
            frozenset({"value", "source"}),
        ),
        "output.resolved.effort": (
            frozenset({"value", "source"}),
            frozenset({"value", "source"}),
        ),
        "output.resolved.mode": (
            frozenset({"value", "source"}),
            frozenset({"value", "source"}),
        ),
        "output.resolved.permissions": (
            frozenset({"value", "source"}),
            frozenset({"value", "source"}),
        ),
        "output.resolved.home": (
            frozenset({"value", "source"}),
            frozenset({"value", "source"}),
        ),
        "output.advertised": (
            frozenset({"modes", "mode_specs", "models", "commands"}),
            frozenset({"modes", "mode_specs", "models", "commands"}),
        ),
        "output.commands[]": (
            frozenset({"name", "description"}),
            frozenset({"name", "description"}),
        ),
    },
    "agents list": {
        "output": (frozenset({"items", "has_more"}), frozenset({"items", "has_more"})),
        "output.items[]": (
            frozenset(
                {
                    "name",
                    "kind",
                    "display_name",
                    "status",
                    "base_adapter",
                    "model",
                    "effort",
                    "permissions",
                    "home",
                    "description",
                }
            ),
            frozenset({"name", "kind", "description"}),
        ),
    },
    "cancel": {
        "output": (
            frozenset({"session_id", "status", "stop_reason", "changed"}),
            frozenset({"session_id", "status", "stop_reason", "changed"}),
        )
    },
    "daemon status": {
        "output": (frozenset({"items", "has_more"}), frozenset({"items", "has_more"})),
        "output.items[]": (
            frozenset(
                {
                    "target",
                    "version",
                    "pid",
                    "uptime_seconds",
                    "log",
                    "sessions",
                    "preparing",
                    "restoring",
                    "max_concurrent",
                    "idle_seconds",
                }
            ),
            frozenset(
                {
                    "target",
                    "version",
                    "pid",
                    "uptime_seconds",
                    "log",
                    "sessions",
                    "preparing",
                    "restoring",
                    "max_concurrent",
                    "idle_seconds",
                }
            ),
        ),
    },
    "daemon stop": {
        "output": (
            frozenset({"targets", "changed", "requires_confirmation"}),
            frozenset({"targets", "changed", "requires_confirmation"}),
        )
    },
    "delete": {
        "output": (
            frozenset({"session_id", "removed", "changed", "paths"}),
            frozenset({"session_id", "removed", "changed", "paths"}),
        ),
        "output.paths": (_PATHS_PROPERTIES, _PATHS_PROPERTIES),
    },
    "install": {
        "output": (
            frozenset({"agent", "ok", "returncode", "changed"}),
            frozenset({"agent", "ok", "returncode", "changed"}),
        )
    },
    "list": {
        "output": (frozenset({"items", "has_more"}), frozenset({"items", "has_more"})),
        "output.items[]": (
            frozenset(
                {
                    "session_id",
                    "entry",
                    "model",
                    "status",
                    "name",
                    "prompt_snippet",
                    "runtime_seconds",
                    "idle_seconds",
                    "created_at",
                    "started_at",
                    "finished_at",
                }
            ),
            frozenset(
                {
                    "session_id",
                    "entry",
                    "model",
                    "status",
                    "name",
                    "prompt_snippet",
                    "runtime_seconds",
                    "idle_seconds",
                    "created_at",
                    "started_at",
                    "finished_at",
                }
            ),
        ),
    },
    "log": {
        "output": (
            frozenset(
                {
                    "i",
                    "ts",
                    "type",
                    "text",
                    "name",
                    "args_summary",
                    "status",
                    "duration_ms",
                    "kind",
                    "decision",
                    "auto",
                    "message",
                    "observation",
                    "next_step",
                    "adapter_log",
                    "adapter_log_tail",
                    "from",
                    "to",
                    "tokens",
                    "cost",
                }
            ),
            frozenset({"i", "ts", "type"}),
        )
    },
    "probe": {
        "output": (
            frozenset(
                {
                    "entry",
                    "base_adapter",
                    "discover_only",
                    "turns",
                    "current_mode",
                    "advertised_modes",
                    "mode_reports",
                    "verdicts",
                    "refusal_violations",
                    "implied_modes",
                    "unmeasured",
                    "current_modes",
                    "diff",
                }
            ),
            frozenset(
                {
                    "entry",
                    "base_adapter",
                    "discover_only",
                    "turns",
                    "current_mode",
                    "advertised_modes",
                    "mode_reports",
                    "verdicts",
                    "refusal_violations",
                    "implied_modes",
                    "unmeasured",
                    "current_modes",
                    "diff",
                }
            ),
        ),
        "output.advertised_modes[]": (
            frozenset({"id", "name", "description"}),
            frozenset({"id", "name", "description"}),
        ),
        "output.diff[]": (
            frozenset({"mode", "status", "description", "current", "proposed"}),
            frozenset({"mode", "status", "description", "current", "proposed"}),
        ),
        "output.diff[].current": (
            frozenset({"grants", "delegates", "escalates"}),
            frozenset({"grants", "delegates", "escalates"}),
        ),
        "output.diff[].proposed": (
            frozenset({"grants", "delegates", "escalates"}),
            frozenset({"grants", "delegates", "escalates"}),
        ),
    },
    "prune": {
        "output": (
            frozenset({"targets", "changed", "requires_confirmation"}),
            frozenset({"targets", "changed", "requires_confirmation"}),
        )
    },
    "resolve": {
        "output": (
            frozenset(
                {"entry", "base_adapter", "command", "cwd", "env", "env_passthrough", "resolved"}
            ),
            frozenset(
                {"entry", "base_adapter", "command", "cwd", "env", "env_passthrough", "resolved"}
            ),
        ),
        "output.resolved": (
            _RESOLUTION_PROPERTIES,
            _RESOLUTION_PROPERTIES,
        ),
        **{
            f"output.resolved.{field}": (
                _RESOLUTION_FIELD_PROPERTIES,
                _RESOLUTION_FIELD_REQUIRED,
            )
            for field in _RESOLUTION_PROPERTIES
        },
        **{
            f"output.resolved.{field}.clamp": (
                _PERMISSIONS_CLAMP_PROPERTIES,
                _RESOLUTION_CLAMP_REQUIRED,
            )
            for field in _RESOLUTION_PROPERTIES
        },
    },
    "skills get": {
        "output": (
            frozenset({"name", "description", "path", "body"}),
            frozenset({"name", "description", "path", "body"}),
        )
    },
    "skills list": {
        "output": (frozenset({"items", "has_more"}), frozenset({"items", "has_more"})),
        "output.items[]": (
            frozenset({"name", "description", "path"}),
            frozenset({"name", "description", "path"}),
        ),
    },
    "status": {
        "output": (
            frozenset(
                {
                    "session_id",
                    "status",
                    "pid",
                    "turns",
                    "entry",
                    "base_adapter",
                    "model",
                    "name",
                    "runtime_seconds",
                    "idle_seconds",
                    "tokens",
                    "cost",
                    "exit_code",
                    "stop_reason",
                    "failure",
                    "paths",
                    "created_at",
                    "started_at",
                    "finished_at",
                }
            ),
            frozenset(EXPECTED_REQUIRED_FIELDS["status"]),
        ),
        "output.paths": (_PATHS_PROPERTIES, _PATHS_PROPERTIES),
    },
}

for _name in ("continue", "run", "steer"):
    EXPECTED_OUTPUT_ORACLES[_name] = _session_output_oracle(EXPECTED_REQUIRED_FIELDS[_name])
EXPECTED_OUTPUT_ORACLES["wait"] = _session_output_oracle(
    EXPECTED_REQUIRED_FIELDS["wait"], include_changed=False
)

EXPECTED_EMPTY_OUTPUT_OBJECTS = {
    "agents get.output.env": "free-form environment values are intentionally untyped",
    "agents get.output.advertised.mode_specs": "adapter mode reports are vendor-defined",
    "agents get.output.advertised.commands[]": "adapter command details are vendor-defined",
    "agents get.output.presets": "adapter presets are vendor-defined",
    "probe.output.mode_reports": "probe reports are vendor-defined",
    "probe.output.verdicts": "probe verdicts are vendor-defined",
    "probe.output.refusal_violations[]": "violation records are vendor-defined",
    "probe.output.implied_modes": "probe mode facts are vendor-defined",
    "probe.output.unmeasured[]": "unmeasured records are vendor-defined",
    "probe.output.current_modes": "probe mode facts are vendor-defined",
    "resolve.output.env": "resolved environment values are vendor-defined",
}

COMMAND_ORACLE_NOTES = {
    name: "covered by the indexed success scenario and the independent output oracle"
    for name in EXPECTED_EFFECTS
}


@pytest.fixture(autouse=True)
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "state"
    agents = root / "agents"
    agents.mkdir(parents=True)
    (agents / "mock.toml").write_text(MOCK_ENTRY, encoding="utf-8")
    monkeypatch.setenv("ACPC_HOME", str(root))
    return root


@pytest.fixture
def cli() -> CliRunner:
    return CliRunner()


def invoke(cli: CliRunner, *args: str, input_text: str | None = None):
    return cli.invoke(main, list(args), input=input_text, catch_exceptions=False)


def read_index(cli: CliRunner) -> dict[str, Any]:
    result = invoke(cli, "schema")
    assert result.exit_code == vocab.EXIT_OK, result.stderr
    return json.loads(result.stdout)


def read_detail(cli: CliRunner, name: str) -> dict[str, Any]:
    result = invoke(cli, "schema", *name.split())
    assert result.exit_code == vocab.EXIT_OK, result.stderr
    return json.loads(result.stdout)


def click_command(name: str) -> click.Command:
    command: click.Command = main
    for segment in name.split():
        assert isinstance(command, click.Group)
        command = command.commands[segment]
    return command


def accepted_options(name: str) -> list[click.Option]:
    return [
        parameter
        for parameter in click_command(name).params
        if isinstance(parameter, click.Option) and parameter.name != "help"
    ]


def _canonical_option(option: click.Option) -> str:
    spellings = [*option.opts, *option.secondary_opts]
    longs = [spelling for spelling in spellings if spelling.startswith("--")]
    return (longs[0] if longs else spellings[0]).lstrip("-")


def _descriptor_for(option: click.Option) -> dict[str, Any]:
    spellings = [*option.opts, *option.secondary_opts]
    canonical = _canonical_option(option)
    descriptor: dict[str, Any] = {
        "name": canonical,
        "type": "boolean" if option.is_flag else _click_type(option),
        "required": option.required,
    }
    if isinstance(option.default, (bool, int, float, str)):
        descriptor["default"] = option.default
    aliases = [spelling.lstrip("-") for spelling in spellings if spelling != f"--{canonical}"]
    if aliases:
        descriptor["aliases"] = aliases
    if isinstance(option.type, click.Choice):
        descriptor["enum"] = [str(choice) for choice in option.type.choices]
    if option.multiple:
        descriptor["repeatable"] = True
    return descriptor


def _click_type(option: click.Option) -> str:
    return {
        "integer": "integer",
        "integer range": "integer",
        "float": "number",
        "float range": "number",
        "boolean": "boolean",
    }.get(option.type.name, "string")


def _error(result: Any) -> dict[str, Any]:
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert lines, result.output
    return json.loads(lines[-1])["error"]


def _finished_session(state: str = "succeeded") -> str:
    meta = sessions.create_session(entry="mock", base_adapter="mock", prompt="fixture")
    sessions.mark_running(
        meta.session_id,
        pid=os.getpid(),
        process_start_time=proc.process_start_time(),
    )
    sessions.transition(meta.session_id, state, exit_code=0, stop_reason="test")
    return meta.session_id


class _SequenceRng(random.Random):
    """Return a fixed stream of characters so allocator collisions are deterministic."""

    def __init__(self, values: str) -> None:
        super().__init__(0)
        self._values = iter(values)

    def choice(self, seq: Any) -> Any:
        del seq
        return next(self._values)


def _active_session(*, target: str | None = None, pid: int | None = None) -> str:
    meta = sessions.create_session(
        entry="mock",
        base_adapter="mock",
        prompt="active fixture",
        target=target,
    )
    if pid is None:
        sessions.transition(meta.session_id, "canceled", exit_code=vocab.EXIT_CANCELLED)
        return meta.session_id
    sessions.mark_running(
        meta.session_id,
        pid=pid,
        process_start_time=proc.process_start_time(pid),
    )
    return meta.session_id


def _wait_for_state(session_id: str, state: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sessions.load(session_id).state == state:
            return
        time.sleep(0.05)
    pytest.fail(f"session {session_id} never became {state}")


def _background_session(cli: CliRunner, prompt: str = "slow:1 background") -> str:
    result = invoke(cli, "run", "mock", prompt, "--background", "--json", "--quiet")
    assert result.exit_code == vocab.EXIT_OK, result.stderr
    session_id = json.loads(result.stdout)["session_id"]
    _wait_for_state(session_id, "running")
    return session_id


def _log_fixture() -> str:
    session_id = _finished_session()
    events = transcript.Transcript(sessions.transcript_path(session_id))
    events.append("msg", text="schema conformance")
    return session_id


def _probe_fixture(root: Path) -> None:
    entry = MOCK_ENTRY.replace(
        '[modes]\ndefault = { grants = "read", delegates = true }',
        '[modes]\ndefault = { grants = "none", delegates = false }\nlegacy = '
        '{ grants = "all", delegates = false }',
    )
    (root / "agents" / "mock.toml").write_text(entry, encoding="utf-8")


def _format_selection(name: str, selection: str) -> list[str]:
    if selection == "default":
        return []
    if selection == "json":
        return ["--json"]
    if selection == "format":
        return ["--format", "ndjson" if name == "log" else "json"]
    return ["--format", selection]


def _quiet_if_supported(name: str) -> list[str]:
    return ["--quiet"] if any(option.name == "quiet" for option in accepted_options(name)) else []


def _success_call(
    cli: CliRunner,
    name: str,
    selection: str,
    label: str,
    root: Path,
    *,
    format_args: list[str] | None = None,
) -> Any:
    output_flags = _format_selection(name, selection) if format_args is None else format_args
    if selection == "plain":
        output_flags.extend(("--limit", "1"))
    quiet = _quiet_if_supported(name)
    if name == "agents check":
        # The format oracle needs the bounded collection shape, not a second
        # ACP discovery run.  `agents check mock` covers the real boundary.
        return invoke(cli, "agents", "check", "--limit", "0", *output_flags, *quiet)
    if name == "agents create":
        return invoke(
            cli,
            "agents",
            "create",
            f"variant-{label}",
            "--extends",
            "mock",
            *output_flags,
        )
    if name == "agents delete":
        invoke(cli, "agents", "create", f"delete-{label}", "--extends", "mock", "--json")
        return invoke(cli, "agents", "delete", f"delete-{label}", "--yes", *output_flags)
    if name == "agents get":
        return invoke(cli, "agents", "get", "mock", *output_flags)
    if name == "agents list":
        return invoke(cli, "agents", "list", *output_flags)
    if name == "cancel":
        session_id = sessions.create_session(
            entry="mock", base_adapter="mock", prompt="cancel fixture"
        ).session_id
        return invoke(cli, "cancel", session_id, *output_flags)
    if name == "continue":
        started = invoke(cli, "run", "mock", "echo:first", "--json", "--quiet")
        session_id = json.loads(started.stdout)["session_id"]
        return invoke(cli, "continue", session_id, "echo:next", *output_flags, *quiet)
    if name == "daemon status":
        return invoke(cli, "daemon", "status", *output_flags)
    if name == "daemon stop":
        return invoke(cli, "daemon", "stop", "never-started", *output_flags)
    if name == "delete":
        return invoke(cli, "delete", _finished_session(), "--yes", *output_flags)
    if name == "install":
        return invoke(cli, "install", "mock", "--yes", *output_flags)
    if name == "list":
        return invoke(cli, "list", *output_flags)
    if name == "log":
        return invoke(cli, "log", _log_fixture(), *output_flags, *quiet)
    if name == "probe":
        _probe_fixture(root)
        return invoke(cli, "probe", "mock", "--discover", *output_flags)
    if name == "prune":
        return invoke(cli, "prune", "--yes", *output_flags)
    if name == "resolve":
        return invoke(cli, "resolve", "mock", *output_flags)
    if name == "run":
        return invoke(cli, "run", "mock", "echo:conformance", *output_flags, *quiet)
    if name == "skills get":
        skills = json.loads(invoke(cli, "skills", "list", "--json").stdout)["items"]
        return invoke(cli, "skills", "get", skills[0]["name"], *output_flags)
    if name == "skills list":
        return invoke(cli, "skills", "list", *output_flags)
    if name == "status":
        return invoke(cli, "status", _finished_session(), *output_flags)
    if name == "steer":
        session_id = _background_session(cli, "slow:1 steer fixture")
        return invoke(cli, "steer", session_id, "echo:steered", *output_flags, *quiet)
    if name == "wait":
        session_id = _background_session(cli)
        return invoke(cli, "wait", session_id, *output_flags, *quiet)
    raise AssertionError(f"no scenario for schema command {name!r}")


def _assert_schema_value(schema: dict[str, Any], value: Any, path: str) -> None:
    if not schema:
        return
    assert set(schema) <= SCHEMA_KEYS, path
    if "enum" in schema:
        assert value in schema["enum"], (path, value, schema["enum"])
    declared_type = schema.get("type")
    allowed = declared_type if isinstance(declared_type, list) else [declared_type]
    if value is None:
        assert "null" in allowed, path
        return
    if "object" in allowed:
        assert isinstance(value, dict), path
        properties = schema.get("properties", {})
        assert set(schema.get("required", [])) <= set(value), path
        assert set(value) <= set(properties), (path, set(value) - set(properties))
        for key, item in value.items():
            _assert_schema_value(properties[key], item, f"{path}.{key}")
        return
    if "array" in allowed:
        assert isinstance(value, list), path
        for index, item in enumerate(value):
            _assert_schema_value(schema["items"], item, f"{path}[{index}]")
        return
    if "boolean" in allowed:
        assert isinstance(value, bool), path
    elif "integer" in allowed:
        assert isinstance(value, int) and not isinstance(value, bool), path
    elif "number" in allowed:
        assert isinstance(value, (int, float)) and not isinstance(value, bool), path
    elif "string" in allowed:
        assert isinstance(value, str), path
    else:
        raise AssertionError((path, schema))


def _assert_machine_output(detail: dict[str, Any], result: Any) -> None:
    assert result.exit_code == vocab.EXIT_OK, result.stderr
    if detail.get("stream"):
        lines = result.stdout.splitlines()
        assert all(line.strip() for line in lines), result.stdout
        for index, line in enumerate(lines):
            value = json.loads(line)
            _assert_schema_value(detail["output"], value, f"stdout[{index}]")
    else:
        value = json.loads(result.stdout)
        _assert_schema_value(detail["output"], value, "stdout")


def _snapshot_tree(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_R1a_read_only_agents_list_preserves_its_intended_state(
    cli: CliRunner, state_root: Path
) -> None:
    registry = state_root / "agents"
    before = _snapshot_tree(registry)
    result = invoke(cli, "agents", "list", "--json")
    assert result.exit_code == vocab.EXIT_OK, result.stderr
    after = _snapshot_tree(registry)
    assert before == after
    # Cache files, daemon logs and liveness metadata are incidental artifacts;
    # the registry is the intended state of this read-only command.


def test_R5a_agents_create_changed_matches_the_observed_transition(
    cli: CliRunner, state_root: Path
) -> None:
    registry = state_root / "agents"
    before = _snapshot_tree(registry)
    result = invoke(cli, "agents", "create", "observed", "--extends", "mock", "--json")
    assert result.exit_code == vocab.EXIT_OK, result.stderr
    after = _snapshot_tree(registry)
    payload = json.loads(result.stdout)
    assert payload["changed"] is (before != after)
    assert (registry / "observed.toml").is_file()


def test_R1_R5_every_mutating_command_has_a_semantic_oracle(
    cli: CliRunner, state_root: Path, live_daemon: None
) -> None:
    index = read_index(cli)
    mutating = {entry["name"] for entry in index["commands"] if entry["effects"] != "read_only"}
    assert set(MUTATING_ORACLE_NOTES) == mutating

    registry = state_root / "agents"
    before = _snapshot_tree(registry)
    created = invoke(cli, "agents", "create", "stage-r", "--extends", "mock", "--json")
    after = _snapshot_tree(registry)
    assert created.exit_code == vocab.EXIT_OK, created.stderr
    assert json.loads(created.stdout)["changed"] is (before != after)

    before = _snapshot_tree(registry)
    deleted = invoke(cli, "agents", "delete", "stage-r", "--yes", "--json")
    after = _snapshot_tree(registry)
    assert deleted.exit_code == vocab.EXIT_OK, deleted.stderr
    assert json.loads(deleted.stdout)["changed"] is (before != after)

    canceled_id = _finished_session()
    before = _snapshot_tree(sessions.session_dir(canceled_id))
    canceled = invoke(cli, "cancel", canceled_id, "--json")
    after = _snapshot_tree(sessions.session_dir(canceled_id))
    assert canceled.exit_code == vocab.EXIT_OK, canceled.stderr
    assert json.loads(canceled.stdout)["changed"] is False
    assert before == after

    started = invoke(cli, "run", "mock", "echo:continue-base", "--json", "--quiet")
    assert started.exit_code == vocab.EXIT_OK, started.stderr
    continued_id = json.loads(started.stdout)["session_id"]
    before = _snapshot_tree(sessions.session_dir(continued_id))
    continued = invoke(cli, "continue", continued_id, "echo:continued", "--json", "--quiet")
    after = _snapshot_tree(sessions.session_dir(continued_id))
    assert continued.exit_code == vocab.EXIT_OK, continued.stderr
    assert json.loads(continued.stdout)["changed"] is True
    assert before != after

    daemon_root = state_root / "daemon"
    before = _snapshot_tree(daemon_root)
    stopped = invoke(cli, "daemon", "stop", "never-started", "--dry-run", "--json")
    after = _snapshot_tree(daemon_root)
    assert stopped.exit_code == vocab.EXIT_OK, stopped.stderr
    assert json.loads(stopped.stdout)["changed"] is False
    assert before == after

    deleted_meta = sessions.create_session(
        entry="mock", base_adapter="mock", prompt="delete oracle", clock=lambda: 0.0
    )
    sessions.mark_running(
        deleted_meta.session_id,
        pid=os.getpid(),
        process_start_time=proc.process_start_time(),
        clock=lambda: 0.0,
    )
    sessions.transition(deleted_meta.session_id, "succeeded", exit_code=0, clock=lambda: 0.0)
    before = _snapshot_tree(sessions.session_dir(deleted_meta.session_id))
    deleted = invoke(cli, "delete", deleted_meta.session_id, "--yes", "--json")
    after = _snapshot_tree(sessions.session_dir(deleted_meta.session_id))
    assert deleted.exit_code == vocab.EXIT_OK, deleted.stderr
    assert json.loads(deleted.stdout)["changed"] is True
    assert before != after
    assert sessions.tombstone_path(deleted_meta.session_id).is_file()

    installed = invoke(cli, "install", "mock", "--yes", "--json")
    assert installed.exit_code == vocab.EXIT_OK, installed.stderr
    assert json.loads(installed.stdout)["changed"] is None

    pruned_meta = sessions.create_session(
        entry="mock", base_adapter="mock", prompt="prune oracle", clock=lambda: 0.0
    )
    sessions.mark_running(
        pruned_meta.session_id,
        pid=os.getpid(),
        process_start_time=proc.process_start_time(),
        clock=lambda: 0.0,
    )
    sessions.transition(pruned_meta.session_id, "succeeded", exit_code=0, clock=lambda: 0.0)
    before = _snapshot_tree(sessions.session_dir(pruned_meta.session_id))
    preview = invoke(cli, "prune", "--older-than", "0d", "--dry-run", "--json")
    assert preview.exit_code == vocab.EXIT_OK, preview.stderr
    assert json.loads(preview.stdout)["changed"] is False
    assert before == _snapshot_tree(sessions.session_dir(pruned_meta.session_id))
    pruned = invoke(cli, "prune", "--older-than", "0d", "--yes", "--json")
    after = _snapshot_tree(sessions.session_dir(pruned_meta.session_id))
    assert pruned.exit_code == vocab.EXIT_OK, pruned.stderr
    assert json.loads(pruned.stdout)["changed"] is True
    assert before != after
    assert sessions.tombstone_path(pruned_meta.session_id).is_file()

    run_before = _snapshot_tree(state_root / "sessions")
    run_result = invoke(cli, "run", "mock", "echo:run-oracle", "--json", "--quiet")
    run_after = _snapshot_tree(state_root / "sessions")
    assert run_result.exit_code == vocab.EXIT_OK, run_result.stderr
    assert json.loads(run_result.stdout)["changed"] is True
    assert run_before != run_after

    steer_id = _background_session(cli, "slow:1 steer oracle")
    steered = invoke(cli, "steer", steer_id, "echo:steered", "--json", "--quiet")
    assert steered.exit_code == vocab.EXIT_OK, steered.stderr
    assert json.loads(steered.stdout)["changed"] is True


def test_R2_R3_irreversible_mutations_keep_their_confirmation_gate(cli: CliRunner) -> None:
    deleted_meta = sessions.create_session(entry="mock", base_adapter="mock", prompt="gate")
    sessions.mark_running(
        deleted_meta.session_id,
        pid=os.getpid(),
        process_start_time=proc.process_start_time(),
    )
    sessions.transition(deleted_meta.session_id, "succeeded", exit_code=0)
    before = _snapshot_tree(sessions.session_dir(deleted_meta.session_id))
    delete_result = invoke(cli, "delete", deleted_meta.session_id, "--json")
    assert delete_result.exit_code == vocab.EXIT_AGENT_ERROR
    assert _error(delete_result)["kind"] == errors.CONFIRMATION_REQUIRED
    assert before == _snapshot_tree(sessions.session_dir(deleted_meta.session_id))

    pruned_meta = sessions.create_session(entry="mock", base_adapter="mock", prompt="gate prune")
    sessions.mark_running(
        pruned_meta.session_id,
        pid=os.getpid(),
        process_start_time=proc.process_start_time(),
        clock=lambda: 0.0,
    )
    sessions.transition(pruned_meta.session_id, "succeeded", exit_code=0, clock=lambda: 0.0)
    before = _snapshot_tree(sessions.session_dir(pruned_meta.session_id))
    prune_result = invoke(cli, "prune", "--older-than", "0d", "--json")
    assert prune_result.exit_code == vocab.EXIT_AGENT_ERROR
    assert _error(prune_result)["kind"] == errors.CONFIRMATION_REQUIRED
    assert before == _snapshot_tree(sessions.session_dir(pruned_meta.session_id))


def test_D1_D3b_help_and_SPEC_match_the_generated_command_set(cli: CliRunner) -> None:
    index = read_index(cli)
    names = [entry["name"] for entry in index["commands"]]
    assert names == sorted(names)
    assert len(names) == len(set(names))

    root_help = invoke(cli, "--help").stdout
    assert "dispatch coding agents over ACP" in root_help
    assert "acpc schema" in root_help
    assert "--json" in root_help
    spec = Path("SPEC.md").read_text(encoding="utf-8")
    section = re.search(r"^## Command surface\n(.*?)(?=^## |\Z)", spec, re.MULTILINE | re.DOTALL)
    assert section is not None
    rows = re.findall(
        r"^\| `([^`]+)` \| `(read_only|idempotent|non_idempotent)` \|$",
        section.group(1),
        re.MULTILINE,
    )
    assert dict(rows) == {entry["name"]: entry["effects"] for entry in index["commands"]}
    surface_blocks = re.findall(r"```text\n(.*?)```", section.group(1), re.DOTALL)

    def spec_syntax(name: str) -> str:
        for block in surface_blocks:
            lines = block.splitlines()
            for index, line in enumerate(lines):
                if not (line == name or line.startswith(f"{name} ")):
                    continue
                syntax = [line]
                for continuation in lines[index + 1 :]:
                    if continuation.startswith(("    ", "\t")):
                        syntax.append(continuation)
                    else:
                        break
                return "\n".join(syntax)
        raise AssertionError(f"SPEC has no syntax for {name}")

    global_spellings = {
        spelling.lstrip("-")
        for flag in index["global_flags"]
        for spelling in (f"--{flag['name']}", *flag.get("aliases", []))
    }
    for entry in index["commands"]:
        name = entry["name"]
        spec_flags = set(re.findall(r"--([a-z][a-z0-9-]*)", spec_syntax(name)))
        options = accepted_options(name)
        parser_flags = {_canonical_option(option) for option in options}
        aliases = {
            spelling.lstrip("-")
            for option in options
            for spelling in (*option.opts, *option.secondary_opts)
            if spelling.startswith("--") and spelling.lstrip("-") not in parser_flags
        }
        spec_flags -= aliases
        assert spec_flags | global_spellings == parser_flags | global_spellings, name

    for name in names:
        command = click_command(name)
        result = invoke(cli, *name.split(), "--help")
        assert result.exit_code == vocab.EXIT_OK, (name, result.stderr)
        assert "Usage:" in result.stdout
        assert (command.short_help or command.help or "").split()[0] in result.stdout
        for option in accepted_options(name):
            assert not option.hidden, (name, _canonical_option(option))
            assert any(
                spelling.startswith("--") and spelling in result.stdout for spelling in option.opts
            )

    list_help = invoke(cli, "list", "--help").stdout
    assert "Return at most N sessions" in list_help
    assert "has_more" in list_help


def test_D6b_parser_descriptors_and_D7a_global_flags_are_generated(cli: CliRunner) -> None:
    index = read_index(cli)
    global_flags = {flag["name"]: flag for flag in index["global_flags"]}
    for flag in index["global_flags"]:
        _assert_input_descriptor(flag, f"global_flags.{flag['name']}")
    per_command: dict[str, set[str]] = {}
    for entry in index["commands"]:
        name = entry["name"]
        detail = read_detail(cli, name)
        for argument in detail["args"]:
            _assert_input_descriptor(argument, f"{name}.args.{argument['name']}")
        for flag in detail["flags"]:
            _assert_input_descriptor(flag, f"{name}.flags.{flag['name']}")
        command = click_command(name)
        args = [
            parameter.name for parameter in command.params if isinstance(parameter, click.Argument)
        ]
        assert [argument["name"] for argument in detail["args"]] == args
        published = {flag["name"]: flag for flag in detail["flags"]}
        accepted = accepted_options(name)
        per_command[name] = {_canonical_option(option) for option in accepted}
        assert len(published) == len(accepted) - len(global_flags)
        for option in accepted:
            expected = _descriptor_for(option)
            if expected["name"] in global_flags:
                assert expected["name"] not in published
                continue
            assert expected["name"] in published, (name, expected["name"])
            for key, value in expected.items():
                assert published[expected["name"]].get(key) == value, (name, key)

    common = set.intersection(*per_command.values())
    uniform = {
        name
        for name in common
        if len(
            {
                repr(next(flag for flag in read_detail(cli, path)["flags"] if flag["name"] == name))
                if name not in global_flags
                else repr(global_flags[name])
                for path in per_command
            }
        )
        == 1
    }
    assert set(global_flags) == uniform
    assert global_flags


def test_D6d_routing_D7b_flat_entries_and_D7c_shape(cli: CliRunner) -> None:
    index = read_index(cli)
    assert set(index) == {
        "schema_version",
        "tool_version",
        "global_flags",
        "format_defaults",
        "exit_codes",
        "conformance",
        "commands",
    }
    assert isinstance(index["schema_version"], str)
    assert index["schema_version"].isdecimal()
    assert int(index["schema_version"]) > 0
    assert index["tool_version"] == EXPECTED_TOOL_VERSION
    version = invoke(cli, "--version")
    assert version.exit_code == 0
    assert version.stdout == f"{EXPECTED_TOOL_VERSION}\n"
    assert index["exit_codes"] == EXPECTED_EXIT_DESCRIPTIONS
    assert index["conformance"] == {
        "name": EXPECTED_STANDARD_NAME,
        "standard": EXPECTED_STANDARD_VERSION,
        "extensions": EXPECTED_EXTENSIONS,
    }
    names = [entry["name"] for entry in index["commands"]]
    assert set(names) == set(EXPECTED_EFFECTS)
    assert set(COMMAND_ORACLE_NOTES) == set(names)
    for entry in index["commands"]:
        assert set(entry) == {"name", "description", "effects"}
        assert entry["name"] and "  " not in entry["name"]
        assert entry["effects"] in {"read_only", "idempotent", "non_idempotent"}
        assert entry["effects"] == EXPECTED_EFFECTS[entry["name"]]
        assert entry["description"] == _descriptor_description(click_command(entry["name"]))
        detail = read_detail(cli, entry["name"])
        assert detail["name"] == entry["name"]
        assert detail["effects"] == entry["effects"]
        output = detail.get("output", {})
        if "next" in output.get("properties", {}):
            assert "next" not in output.get("required", [])
        assert set(output.get("required", [])) == EXPECTED_REQUIRED_FIELDS[entry["name"]]
        # D7: the result behavior a schema cannot state.  Only the commands
        # that return results on failure and carry foreground-only fields
        # declare it; every other command's schema already says everything.
        description = detail.get("output_description")
        if entry["name"] in ANSWER_COMMANDS:
            assert isinstance(description, str) and description, entry["name"]
        else:
            assert description is None, entry["name"]
        if entry["name"] in {"run", "continue", "steer"}:
            assert {"truncated", "output_file"} <= set(output["properties"])
        if entry["name"] in ANSWER_COMMANDS:
            assert "partial" in output["properties"] and "partial" in output["required"]

    prefixes = {parts[0] for parts in (name.split() for name in names) if len(parts) > 1}
    for prefix in prefixes:
        result = invoke(cli, "schema", prefix)
        assert result.exit_code == vocab.EXIT_USAGE
        message = _error(result)["message"]
        assert any(name.startswith(f"{prefix} ") for name in names)
        assert prefix in message
    unknown = invoke(cli, "schema", "__no_such_command__")
    assert unknown.exit_code == vocab.EXIT_USAGE
    assert _error(unknown)["kind"] == errors.INVALID_INPUT


def _check_schema_shape(schema: dict[str, Any], path: str) -> None:
    assert set(schema) <= SCHEMA_KEYS, path
    if not schema:
        return
    assert "type" in schema, path
    types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
    assert set(types) <= {"string", "integer", "number", "boolean", "array", "object", "null"}
    if "object" in types:
        assert "properties" in schema and "required" in schema, path
        assert len(schema["required"]) == len(set(schema["required"])), path
        assert set(schema["required"]) <= set(schema["properties"]), path
        for key, child in schema["properties"].items():
            _check_schema_shape(child, f"{path}.{key}")
    if "array" in types:
        assert "items" in schema, path
        _check_schema_shape(schema["items"], f"{path}[]")
    if "enum" in schema:
        assert isinstance(schema["enum"], list) and schema["enum"], path


def _schema_nodes(schema: dict[str, Any], path: str = "output") -> dict[str, dict[str, Any]]:
    nodes: dict[str, dict[str, Any]] = {}
    if not schema:
        return nodes
    types = schema.get("type", [])
    types = types if isinstance(types, list) else [types]
    if "object" in types and "properties" in schema:
        nodes[path] = schema
        for name, child in schema["properties"].items():
            nodes.update(_schema_nodes(child, f"{path}.{name}"))
    if "array" in types and "items" in schema:
        nodes.update(_schema_nodes(schema["items"], f"{path}[]"))
    return nodes


def _empty_schema_paths(schema: dict[str, Any], path: str = "output") -> set[str]:
    paths: set[str] = set()
    if not schema:
        paths.add(path)
        return paths
    types = schema.get("type", [])
    types = types if isinstance(types, list) else [types]
    if "object" in types:
        for name, child in schema.get("properties", {}).items():
            paths.update(_empty_schema_paths(child, f"{path}.{name}"))
    if "array" in types and "items" in schema:
        paths.update(_empty_schema_paths(schema["items"], f"{path}[]"))
    return paths


def _assert_output_oracle(name: str, detail: dict[str, Any]) -> None:
    output = detail["output"]
    actual_nodes = _schema_nodes(output)
    expected_nodes = EXPECTED_OUTPUT_ORACLES[name]
    assert set(actual_nodes) == set(expected_nodes), name
    for path, (properties, required) in expected_nodes.items():
        assert set(actual_nodes[path]["properties"]) == set(properties), (name, path)
        assert set(actual_nodes[path]["required"]) == set(required), (name, path)
    empty_paths = {f"{name}.{path}" for path in _empty_schema_paths(output)}
    expected_empty = {path for path in EXPECTED_EMPTY_OUTPUT_OBJECTS if path.startswith(f"{name}.")}
    assert empty_paths == expected_empty, name


def _descriptor_description(command: click.Command) -> str:
    text = command.short_help or command.help or ""
    return " ".join(text.split("\n\n", 1)[0].replace("``", "`").replace("\b", " ").split())


def _assert_input_descriptor(descriptor: dict[str, Any], path: str) -> None:
    assert {"name", "type", "required", "description"} <= set(descriptor), path
    assert isinstance(descriptor["name"], str) and descriptor["name"], path
    assert descriptor["type"] in {"string", "integer", "number", "boolean"}, path
    assert isinstance(descriptor["required"], bool), path
    assert isinstance(descriptor["description"], str) and descriptor["description"].strip(), path


@pytest.mark.timeout(120)
def test_D8_O4a_O4d_R1a_R1b_R5a_schema_and_success_matrix(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    state_root: Path,
    live_daemon: None,
) -> None:
    index = read_index(cli)
    for selection in ("json", "format"):
        for entry in index["commands"]:
            name = entry["name"]
            detail = read_detail(cli, name)
            _check_schema_shape(detail["output"], f"{name}.output")
            _assert_output_oracle(name, detail)
            assert detail["effects"] == entry["effects"]
            assert detail["effects"] in {"read_only", "idempotent", "non_idempotent"}
            accepts_yes = any("--yes" in option.opts for option in accepted_options(name))
            assert detail["confirm"] is accepts_yes
            if detail["effects"] == "read_only":
                assert "changed" not in detail["output"].get("required", [])
            else:
                assert "changed" in detail["output"].get("required", [])
            if detail.get("stream"):
                assert name == "log"
                assert detail["output"]["type"] == "object"
            result = _success_call(cli, name, selection, f"{selection}-{name}", state_root)
            try:
                _assert_machine_output(detail, result)
            except AssertionError as error:
                raise AssertionError(f"{name} ({selection}): {error}") from error
            if any(option.name == "quiet" for option in accepted_options(name)):
                assert result.stderr == "" or result.stderr.endswith("\n")
            assert detail["interactive"] is False
        monkeypatch.setenv("NO_INPUT", "1")


def test_O2a_O2b_default_format_is_exercised_for_every_indexed_command(
    cli: CliRunner,
    state_root: Path,
    live_daemon: None,
) -> None:
    index = read_index(cli)
    assert index["format_defaults"] == {"tty": "text", "non_tty": "json"}
    default_list = invoke(cli, "list")
    assert default_list.exit_code == vocab.EXIT_OK, default_list.stderr
    assert json.loads(default_list.stdout) == {"items": [], "has_more": False}
    for entry in index["commands"]:
        name = entry["name"]
        detail = read_detail(cli, name)
        result = _success_call(cli, name, "default", f"default-{name}", state_root)
        assert result.exit_code == vocab.EXIT_OK, (name, result.stderr)
        if detail.get("format_defaults", {}).get("non_tty") == "json":
            _assert_machine_output(detail, result)
        else:
            assert result.stdout or result.stderr, name


def _read_fd(fd: int) -> bytes:
    data = bytearray()
    try:
        while chunk := os.read(fd, 4096):
            data.extend(chunk)
    except OSError:
        pass
    finally:
        os.close(fd)
    return bytes(data)


def _run_with_stream_context(
    args: list[str], *, stdout_tty: bool, stderr_tty: bool
) -> tuple[int, bytes, bytes]:
    masters: list[int] = []
    slaves: list[int | None] = []
    targets: list[Any] = []
    for is_tty in (stdout_tty, stderr_tty):
        if is_tty:
            master, slave = pty.openpty()
            masters.append(master)
            slaves.append(slave)
            targets.append(slave)
        else:
            slaves.append(None)
            targets.append(subprocess.PIPE)
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            [sys.executable, "-c", "from acpc.cli import main; raise SystemExit(main())", *args],
            stdin=subprocess.DEVNULL,
            stdout=targets[0],
            stderr=targets[1],
            env=os.environ.copy(),
        )
        for slave in slaves:
            if slave is not None:
                os.close(slave)
        pipe_stdout, pipe_stderr = process.communicate(timeout=10)
        tty_output = [_read_fd(master) for master in masters]
        output = iter(tty_output)
        stdout = next(output) if stdout_tty else pipe_stdout
        stderr = next(output) if not stdout_tty and stderr_tty else pipe_stderr
        if stdout_tty and stderr_tty:
            stderr = next(output)
        return process.returncode, stdout or b"", stderr or b""
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        for slave in slaves:
            if slave is not None:
                with contextlib.suppress(OSError):
                    os.close(slave)
        for master in masters:
            with contextlib.suppress(OSError):
                os.close(master)


@pytest.mark.skipif(sys.platform == "win32", reason="PTY is not available")
def test_O1_stream_contexts_are_independent_for_a_machine_document() -> None:
    contexts = ((False, False), (True, False), (False, True), (True, True))
    for stdout_tty, stderr_tty in contexts:
        code, stdout, stderr = _run_with_stream_context(
            ["list", "--json"],
            stdout_tty=stdout_tty,
            stderr_tty=stderr_tty,
        )
        assert code == vocab.EXIT_OK, stderr
        assert json.loads(stdout) == {"items": [], "has_more": False}
        assert stderr == b""


@pytest.mark.timeout(60)
@pytest.mark.parametrize("no_input", [False, True], ids=["input-allowed", "no-input"])
def test_O1_O2b_O3a_F2a_F2c_machine_matrix(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    state_root: Path,
    no_input: bool,
    live_daemon: None,
) -> None:
    if no_input:
        monkeypatch.setenv("NO_INPUT", "1")
    else:
        monkeypatch.delenv("NO_INPUT", raising=False)
    index = read_index(cli)
    for entry in index["commands"]:
        name = entry["name"]
        detail = read_detail(cli, name)
        result = _success_call(cli, name, "json", f"matrix-{no_input}-{name}", state_root)
        _assert_machine_output(detail, result)
        assert "Traceback" not in result.stderr

    missing = invoke(cli, "run", "no-such-agent", "probe", "--json", "--quiet")
    assert missing.exit_code == vocab.EXIT_AGENT_ERROR
    assert missing.stdout == ""
    assert _error(missing)["kind"] == errors.NOT_FOUND
    assert json.loads([line for line in missing.stderr.splitlines() if line.strip()][-1])["error"]

    preformat = invoke(cli, "list", "--json", "--no-such-flag")
    assert preformat.exit_code == vocab.EXIT_USAGE
    assert preformat.stdout == ""
    assert _error(preformat)["kind"] == errors.INVALID_INPUT


ERROR_SCENARIOS: dict[str, tuple[str, ...] | None] = {
    "agents check": None,
    "agents create": ("agents", "create", "bad", "--extends", "missing", "--json"),
    "agents delete": ("agents", "delete", "missing", "--json"),
    "agents get": ("agents", "get", "missing", "--json"),
    "agents list": ("agents", "list", "--json", "--no-such-flag"),
    "cancel": ("cancel", "missing", "--json"),
    "continue": ("continue", "missing", "echo:x", "--json"),
    "daemon status": ("daemon", "status", "--json", "--format", "invalid"),
    "daemon stop": ("daemon", "stop", "--json", "--format", "invalid"),
    "delete": ("delete", "missing", "--json"),
    "install": ("install", "missing", "--json"),
    "list": ("list", "--json", "--no-such-flag"),
    "log": ("log", "missing", "--json", "--quiet"),
    "probe": ("probe", "missing", "--discover", "--json"),
    "prune": ("prune", "--json", "--older-than", "nonsense"),
    "resolve": ("resolve", "missing", "--json"),
    "run": ("run", "missing", "probe", "--json", "--quiet"),
    "skills get": ("skills", "get", "missing", "--json"),
    "skills list": ("skills", "list", "--json", "--no-such-flag"),
    "status": ("status", "missing", "--json"),
    "steer": ("steer", "missing", "echo:x", "--json"),
    "wait": ("wait", "missing", "--json", "--quiet"),
}
ERROR_SCENARIO_SKIPS = {
    "agents check": "requires a corrupt registry fixture and is covered by registry tests",
}
EXPECTED_ERROR_RESULTS = {
    "agents create": (errors.NOT_FOUND, vocab.EXIT_AGENT_ERROR),
    "agents delete": (errors.NOT_FOUND, vocab.EXIT_AGENT_ERROR),
    "agents get": (errors.NOT_FOUND, vocab.EXIT_AGENT_ERROR),
    "agents list": (errors.INVALID_INPUT, vocab.EXIT_USAGE),
    "cancel": (errors.NOT_FOUND, vocab.EXIT_AGENT_ERROR),
    "continue": (errors.NOT_FOUND, vocab.EXIT_AGENT_ERROR),
    "daemon status": (errors.INVALID_INPUT, vocab.EXIT_USAGE),
    "daemon stop": (errors.INVALID_INPUT, vocab.EXIT_USAGE),
    "delete": (errors.NOT_FOUND, vocab.EXIT_AGENT_ERROR),
    "install": (errors.NOT_FOUND, vocab.EXIT_AGENT_ERROR),
    "list": (errors.INVALID_INPUT, vocab.EXIT_USAGE),
    "log": (errors.NOT_FOUND, vocab.EXIT_AGENT_ERROR),
    "probe": (errors.NOT_FOUND, vocab.EXIT_AGENT_ERROR),
    "prune": (errors.INVALID_INPUT, vocab.EXIT_USAGE),
    "resolve": (errors.NOT_FOUND, vocab.EXIT_AGENT_ERROR),
    "run": (errors.NOT_FOUND, vocab.EXIT_AGENT_ERROR),
    "skills get": (errors.NOT_FOUND, vocab.EXIT_AGENT_ERROR),
    "skills list": (errors.INVALID_INPUT, vocab.EXIT_USAGE),
    "status": (errors.NOT_FOUND, vocab.EXIT_AGENT_ERROR),
    "steer": (errors.NOT_FOUND, vocab.EXIT_AGENT_ERROR),
    "wait": (errors.NOT_FOUND, vocab.EXIT_AGENT_ERROR),
}


def _assert_error_location(
    result: Any, *, expected_kind: str, expected_exit_code: int
) -> dict[str, Any]:
    assert result.exit_code == expected_exit_code
    assert result.stdout == ""
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert lines, result.stderr
    envelope = json.loads(lines[-1])
    assert set(envelope) == {"error"}
    assert envelope["error"]["message"] in result.stderr
    assert envelope["error"]["kind"] == expected_kind
    return envelope["error"]


def test_F2_error_matrix_is_explicit_for_every_command(cli: CliRunner) -> None:
    names = {entry["name"] for entry in read_index(cli)["commands"]}
    assert set(ERROR_SCENARIOS) == names
    assert set(EXPECTED_ERROR_RESULTS) == names - set(ERROR_SCENARIO_SKIPS)
    for name, args in ERROR_SCENARIOS.items():
        if args is None:
            continue
        result = invoke(cli, *args)
        kind, exit_code = EXPECTED_ERROR_RESULTS[name]
        _assert_error_location(result, expected_kind=kind, expected_exit_code=exit_code)


def test_F2_error_matrix_records_why_known_errors_are_not_scenarios() -> None:
    assert set(ERROR_SCENARIO_SKIPS) == {
        name for name, args in ERROR_SCENARIOS.items() if args is None
    }
    assert all(ERROR_SCENARIO_SKIPS.values())


def test_M1_wait_failure_reports_the_observed_terminal_meaning(cli: CliRunner) -> None:
    session_id = _finished_session("failed")

    result = invoke(cli, "wait", session_id, "--json", "--quiet")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    document = json.loads(result.stdout)
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    error = json.loads(lines[-1])["error"]
    assert error["kind"] == errors.OPERATION_FAILED
    assert error["context"] == {"session_id": session_id, "status": "failed"}
    # M1c: the status a failure result carries is the same observation the
    # error's context reports — one value, not two spellings of it.
    assert document["status"] == error["context"]["status"]
    assert document["session_id"] == session_id
    assert document["partial"] is False


def test_H5b_plain_alias_is_byte_identical_for_every_plain_collection(cli: CliRunner) -> None:
    for name, path in (
        ("agents list", ("agents", "list")),
        ("agents check", ("agents", "check")),
        ("list", ("list",)),
        ("daemon status", ("daemon", "status")),
        ("skills list", ("skills", "list")),
    ):
        limit = ["--limit", "1"]
        first = invoke(cli, *path, "--plain", *limit)
        second = invoke(cli, *path, "--format", "plain", *limit)
        assert first.exit_code == vocab.EXIT_OK, (name, first.stderr)
        assert second.exit_code == vocab.EXIT_OK, (name, second.stderr)
        assert first.stdout.encode() == second.stdout.encode(), name
        assert all(line and "--" not in line for line in first.stdout.splitlines()), name


def test_O7a_O7b_O7c_O7d_log_stream_is_framed_bounded_ordered_and_complete(
    cli: CliRunner,
) -> None:
    session_id = _finished_session()
    events = transcript.Transcript(sessions.transcript_path(session_id))
    for number in range(25):
        events.append("msg", text=f"event-{number}")

    all_result = invoke(cli, "log", session_id, "--json", "--since", "0", "--quiet")
    assert all_result.exit_code == vocab.EXIT_OK, all_result.stderr
    all_records = [json.loads(line) for line in all_result.stdout.splitlines()]
    assert len(all_records) > 20

    tail_result = invoke(cli, "log", session_id, "--json", "--tail", "2", "--quiet")
    assert tail_result.exit_code == vocab.EXIT_OK, tail_result.stderr
    assert [json.loads(line) for line in tail_result.stdout.splitlines()] == all_records[-2:]

    default_result = invoke(cli, "log", session_id, "--json", "--quiet")
    assert default_result.exit_code == vocab.EXIT_OK, default_result.stderr
    assert [json.loads(line) for line in default_result.stdout.splitlines()] == all_records[-20:]

    cursor = all_records[2]["i"]
    expected_after_cursor = [record for record in all_records if record["i"] > cursor]
    assert expected_after_cursor[:2] != expected_after_cursor[-2:]
    result = invoke(
        cli,
        "log",
        session_id,
        "--json",
        "--since",
        str(cursor),
        "--limit",
        "2",
        "--quiet",
    )
    assert result.exit_code == vocab.EXIT_OK, result.stderr
    framed = result.stdout.splitlines(keepends=True)
    assert len(framed) == 2
    assert all(line.endswith("\n") and line.strip() for line in framed)
    records = [json.loads(line) for line in framed]
    assert records == expected_after_cursor[:2]
    assert [record["i"] for record in records] == sorted(record["i"] for record in records)
    assert result.stderr == ""

    seen: list[dict[str, Any]] = []
    page_cursor = 0
    while True:
        page_result = invoke(
            cli,
            "log",
            session_id,
            "--json",
            "--since",
            str(page_cursor),
            "--limit",
            "2",
            "--quiet",
        )
        assert page_result.exit_code == vocab.EXIT_OK, page_result.stderr
        page_records = [json.loads(line) for line in page_result.stdout.splitlines()]
        if not page_records:
            break
        seen.extend(page_records)
        page_cursor = page_records[-1]["i"]
    assert seen == all_records

    detail = read_detail(cli, "log")
    flags = {flag["name"]: flag["description"] for flag in detail["flags"]}
    assert "last 20" in flags["limit"] and "--follow" in flags["limit"]
    assert "--tail" in flags["limit"]
    assert "last 20" in flags["tail"] and "--follow" in flags["tail"]
    assert "transcript order" in flags["tail"]
    assert "transcript-order events" in flags["follow"]
    assert "--tail" in flags["follow"]
    conflict = invoke(cli, "log", session_id, "--tail", "2", "--limit", "2", "--json", "--quiet")
    assert conflict.exit_code == vocab.EXIT_USAGE
    assert _error(conflict)["kind"] == errors.INVALID_INPUT

    followed = invoke(
        cli,
        "log",
        session_id,
        "--json",
        "--follow",
        "--since",
        "0",
        "--limit",
        "1",
        "--quiet",
    )
    assert followed.exit_code == vocab.EXIT_OK, followed.stderr
    assert len(followed.stdout.splitlines()) == 1

    followed_tail = invoke(
        cli,
        "log",
        session_id,
        "--json",
        "--tail",
        "3",
        "--follow",
        "--quiet",
    )
    assert followed_tail.exit_code == vocab.EXIT_OK, followed_tail.stderr
    assert [json.loads(line) for line in followed_tail.stdout.splitlines()] == all_records[-3:]


def test_F1e_O2f_exit_table_and_delegated_stdout_decision(cli: CliRunner) -> None:
    index = read_index(cli)
    assert set(index["exit_codes"]) == set(vocab.EXIT_DESCRIPTIONS)
    for entry in index["commands"]:
        detail = read_detail(cli, entry["name"])
        assert "exit_codes" not in detail
        assert "delegates_stdout" not in detail
    result = _success_call(cli, "run", "json", "delegated", Path(os.environ["ACPC_HOME"]))
    assert result.exit_code == vocab.EXIT_OK
    assert json.loads(result.stdout)["status"] == "succeeded"


def test_R7a_R7b_R7c_background_changes_wait_only_and_keeps_identifier(
    cli: CliRunner,
    live_daemon: None,
) -> None:
    detail = read_detail(cli, "run")
    assert "block by default" in detail["description"]
    started = invoke(
        cli,
        "run",
        "mock",
        "slow:1 accepted work",
        "--background",
        "--permissions",
        "read",
        "--json",
        "--quiet",
    )
    assert started.exit_code == vocab.EXIT_OK, started.stderr
    session_id = json.loads(started.stdout)["session_id"]
    assert sessions.read_meta(session_id).is_active
    waited = invoke(cli, "wait", session_id, "--json", "--quiet")
    assert waited.exit_code == vocab.EXIT_OK, waited.stderr
    assert json.loads(waited.stdout)["session_id"] == session_id
    assert sessions.read_meta(session_id).state == "succeeded"


def test_D5a_background_breadcrumb_is_an_executable_wait_vector(
    cli: CliRunner, live_daemon: None
) -> None:
    started = invoke(
        cli,
        "run",
        "mock",
        "slow:1 breadcrumb",
        "--background",
        "--permissions",
        "read",
        "--json",
        "--quiet",
    )
    assert started.exit_code == vocab.EXIT_OK, started.stderr
    payload = json.loads(started.stdout)
    assert payload["next"] == ["acpc", "wait", payload["session_id"]]

    waited = invoke(cli, *payload["next"][1:], "--json", "--quiet")
    assert waited.exit_code == vocab.EXIT_OK, waited.stderr
    assert json.loads(waited.stdout)["session_id"] == payload["session_id"]


def test_O8_broken_pipe_is_a_documented_pipe_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenStream:
        def write(self, _text: str) -> None:
            raise BrokenPipeError

        def flush(self) -> None:
            raise BrokenPipeError

    def leave() -> NoReturn:
        raise SystemExit(vocab.EXIT_SIGPIPE)

    monkeypatch.setattr(cli_module.sys, "stdout", BrokenStream())
    monkeypatch.setattr(cli_module, "_leave_on_broken_pipe", leave)

    with pytest.raises(SystemExit) as raised:
        cli_module._write_stdout("pipe")

    assert raised.value.code == vocab.EXIT_SIGPIPE


def test_O8_main_keeps_the_documented_pipe_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_write(_text: str) -> None:
        raise BrokenPipeError

    def leave() -> NoReturn:
        raise SystemExit(vocab.EXIT_SIGPIPE)

    monkeypatch.setattr(cli_module, "_write_stdout", broken_write)
    monkeypatch.setattr(cli_module, "_leave_on_broken_pipe", leave)

    with pytest.raises(SystemExit) as raised:
        main.main(args=("list",), standalone_mode=True)

    assert raised.value.code == vocab.EXIT_SIGPIPE


@pytest.mark.parametrize("cleanup", ["delete", "prune_explicit", "prune_bare"])
def test_R7b_deleted_identifier_stays_reserved_forever(
    cli: CliRunner,
    cleanup: str,
) -> None:
    old = sessions.create_session(
        entry="mock",
        base_adapter="mock",
        prompt="old tombstone",
        clock=lambda: 0.0,
        rng=_SequenceRng("a" * 4),
    )
    sessions.mark_running(
        old.session_id,
        pid=os.getpid(),
        process_start_time=proc.process_start_time(),
        clock=lambda: 0.0,
    )
    sessions.transition(old.session_id, "succeeded", exit_code=0, clock=lambda: 0.0)

    if cleanup == "delete":
        result = invoke(cli, "delete", old.session_id, "--yes", "--json")
    elif cleanup == "prune_explicit":
        result = invoke(cli, "prune", "--older-than", "0d", "--yes", "--json")
    else:
        result = invoke(cli, "prune", "--yes", "--json")
    assert result.exit_code == vocab.EXIT_OK, result.stderr
    assert sessions.tombstone_path(old.session_id).is_file()
    assert sessions.session_dir(old.session_id).is_dir()
    assert not sessions.meta_path(old.session_id).exists()

    sessions.prune_sessions(older_than=0.0, clock=lambda: 365 * 24 * 60 * 60)
    assert sessions.tombstone_path(old.session_id).is_file()
    sessions.prune_sessions(
        older_than=0.0,
        clock=lambda: 100.0 + 365 * 24 * 60 * 60,
    )
    with pytest.raises(sessions.SessionIdsExhausted):
        sessions.allocate_session_id(rng=_SequenceRng("a" * (4 * 64)))


def test_R7b_exhausted_allocation_does_not_offer_prune_hint(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    occupied = sessions.create_session(
        entry="mock",
        base_adapter="mock",
        prompt="occupied",
        rng=_SequenceRng("a" * 4),
    )
    assert occupied.session_id == "aaaa"
    monkeypatch.setattr(
        sessions.random,
        "SystemRandom",
        lambda: _SequenceRng("a" * (4 * 64)),
    )

    result = invoke(cli, "run", "mock", "echo:unavailable", "--json", "--quiet")

    assert result.exit_code == vocab.EXIT_AGENT_ERROR
    error = _error(result)
    assert error["kind"] == errors.UNAVAILABLE
    assert "prune" not in error["message"]
    assert "hint" not in error


def _enum_values(value: Any, path: str = "") -> list[tuple[str, list[Any]]]:
    found: list[tuple[str, list[Any]]] = []
    if isinstance(value, dict):
        if "enum" in value:
            found.append((path, list(value["enum"])))
        for key, child in value.get("properties", {}).items():
            found.extend(_enum_values(child, f"{path}.{key}"))
        if "items" in value:
            found.extend(_enum_values(value["items"], f"{path}[]"))
    return found


def _observe_flag_enums(
    cli: CliRunner, index: dict[str, Any], state_root: Path, observed: dict[str, set[Any]]
) -> None:
    for flag in index["global_flags"]:
        if "enum" in flag:
            for value in flag["enum"]:
                result = invoke(cli, "list", "--json", "--color", value)
                assert result.exit_code == vocab.EXIT_OK, result.stderr
                observed[f"flag:{flag['name']}"] = observed.get(f"flag:{flag['name']}", set()) | {
                    value
                }
    for entry in index["commands"]:
        name = entry["name"]
        detail = read_detail(cli, name)
        for flag in detail["flags"]:
            if "enum" not in flag:
                continue
            key = f"{name}.flag:{flag['name']}"
            for value in flag["enum"]:
                if flag["name"] == "format":
                    result = _success_call(
                        cli,
                        name,
                        value,
                        f"enum-{name}-{value}",
                        state_root,
                        format_args=["--format", value],
                    )
                elif flag["name"] == "permissions":
                    safe_value = str(value).replace("-", "_")
                    args = [
                        "agents",
                        "create",
                        f"enum-{name.replace(' ', '-')}-{safe_value}",
                        "--extends",
                        "mock",
                        "--permissions",
                        value,
                        "--json",
                    ]
                    result = invoke(cli, *args)
                else:
                    continue
                assert result.exit_code == vocab.EXIT_OK, (key, value, result.stderr)
                observed[key] = observed.get(key, set()) | {value}


def _observe_session_statuses(
    cli: CliRunner,
    observed: dict[str, set[Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_cancel_with_daemon = cli_module._cancel_with_daemon
    real_wait_for_cancel = cli_module._wait_for_cancel
    for state in vocab.SESSION_STATES:
        session_id = sessions.create_session(
            entry="mock", base_adapter="mock", prompt=state
        ).session_id
        if state == "starting":
            pass
        elif state == "running":
            sessions.mark_running(
                session_id, pid=os.getpid(), process_start_time=proc.process_start_time()
            )
        elif state == "preparing":
            sessions.mark_running(
                session_id, pid=os.getpid(), process_start_time=proc.process_start_time()
            )
            path = sessions.meta_path(session_id)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["status"] = "preparing"
            path.write_text(json.dumps(payload), encoding="utf-8")
        elif state == "unknown":
            sessions.mark_running(session_id, pid=999999, process_start_time="gone")
        else:
            sessions.mark_running(
                session_id, pid=os.getpid(), process_start_time=proc.process_start_time()
            )
            sessions.transition(session_id, state, exit_code=0, stop_reason="test")
        result = invoke(cli, "status", session_id, "--json")
        assert result.exit_code == vocab.EXIT_OK
        status = json.loads(result.stdout)["status"]
        observed["status.output.status"] = observed.get("status.output.status", set()) | {status}
    listed = invoke(cli, "list", "--json")
    assert listed.exit_code == vocab.EXIT_OK, listed.stderr
    observed["list.output.items[].status"] = {
        item["status"] for item in json.loads(listed.stdout)["items"]
    }
    for state in ("succeeded", "failed", "canceled", "unknown"):
        session_id = _finished_session(state)
        result = invoke(cli, "cancel", session_id, "--json")
        assert result.exit_code == vocab.EXIT_OK, result.stderr
        observed["cancel.output.status"] = observed.get("cancel.output.status", set()) | {
            json.loads(result.stdout)["status"]
        }
    active = sessions.create_session(
        entry="mock", base_adapter="mock", prompt="cancel status", target="mock-target"
    )
    sessions.mark_running(
        active.session_id, pid=os.getpid(), process_start_time=proc.process_start_time()
    )

    async def accepted_cancel(*_args: Any, **_kwargs: Any) -> Any:
        return cli_module._DaemonCancelReply(accepted=True, turn_token=active.turns)

    monkeypatch.setattr(cli_module, "_cancel_with_daemon", accepted_cancel)
    monkeypatch.setattr(
        cli_module,
        "_wait_for_cancel",
        lambda *_args, **_kwargs: sessions.read_meta(active.session_id),
    )
    result = invoke(cli, "cancel", active.session_id, "--json")
    assert result.exit_code == vocab.EXIT_OK, result.stderr
    observed["cancel.output.status"] = observed.get("cancel.output.status", set()) | {
        json.loads(result.stdout)["status"]
    }
    monkeypatch.setattr(cli_module, "_cancel_with_daemon", real_cancel_with_daemon)
    monkeypatch.setattr(cli_module, "_wait_for_cancel", real_wait_for_cancel)


@contextlib.contextmanager
def _direct_child_route() -> Iterator[None]:
    """Force the direct-child route so no shared daemon owns the turn."""

    async def unavailable(target: str) -> daemon_client.DaemonUnavailable:
        return daemon_client.DaemonUnavailable(f"forced direct child ({target})")

    original = daemon_client.ensure_daemon
    daemon_client.ensure_daemon = unavailable
    try:
        yield
    finally:
        daemon_client.ensure_daemon = original


def _await_alias(alias: str, timeout: float = 30.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return sessions.resolve_selector(alias)
        except sessions.SessionError:
            time.sleep(0.05)
    pytest.fail(f"no session was named {alias!r}")


def _wait_for_meta(
    session_id: str,
    ready: Callable[[sessions.SessionMeta], bool],
    timeout: float = 30.0,
) -> sessions.SessionMeta:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        meta = sessions.load(session_id)
        if ready(meta):
            return meta
        time.sleep(0.05)
    pytest.fail(f"session {session_id} never reached the turn under test")


def _kill_turn_worker(
    cli: CliRunner,
    args: Sequence[str],
    session_id: Callable[[], str],
    ready: Callable[[sessions.SessionMeta], bool],
) -> Any:
    """Run a foreground call and kill the process hosting its turn mid-wait.

    The client observes the loss of its host and reports the `unknown`
    outcome, which is the only way that status reaches an answer document.
    `ready` selects the turn under test, so a follow-up call never kills the
    process that served the turn before it.
    """
    holder: dict[str, Any] = {}

    def call() -> None:
        try:
            holder["result"] = invoke(cli, *args)
        except BaseException as error:  # noqa: BLE001 - re-raised on this thread
            holder["error"] = error

    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    try:
        found = session_id()
        meta = _wait_for_meta(found, ready)
        pid = meta.pid
        assert pid is not None and pid != os.getpid(), found
        os.kill(pid, signal.SIGKILL)
    finally:
        thread.join(timeout=30)
    assert not thread.is_alive(), holder
    assert "error" not in holder, holder
    return holder["result"]


def _observe_work_statuses(
    cli: CliRunner, observed: dict[str, set[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Observe every `status` the four answer commands declare (O4c).

    Each member is produced by a real call: a foreground success, a refused
    turn, a `--cancel-after` cancellation, a background dispatch, a `--timeout`
    deadline that expires on the direct route, and a turn whose host process is
    killed.  The direct route is forced for the last two so the deadline can
    land before `run`'s worker records itself as running, and so the killed
    process is a per-call worker rather than a shared daemon.
    """

    def record(name: str, result: Any, *, exit_code: int | None = None) -> None:
        if exit_code is not None:
            assert result.exit_code == exit_code, (name, result.stdout, result.stderr)
        document = json.loads(result.stdout)
        observed.setdefault(f"{name}.output.status", set()).add(document["status"])

    def call(*args: str) -> Any:
        return invoke(cli, *args, "--json", "--quiet")

    def finished_base() -> str:
        result = call("run", "mock", "echo:enum base")
        assert result.exit_code == vocab.EXIT_OK, result.stderr
        return json.loads(result.stdout)["session_id"]

    base_id = finished_base()

    record("run", call("run", "mock", "echo:enum run"), exit_code=vocab.EXIT_OK)
    record("run", call("run", "mock", "fail enum run"), exit_code=vocab.EXIT_AGENT_ERROR)
    record("run", call("run", "mock", "chunkslow:5 enum run", "--cancel-after", "0.5"))
    record(
        "run",
        call("run", "mock", "slow:1 enum run", "--background", "--permissions", "read"),
        exit_code=vocab.EXIT_OK,
    )

    record("continue", call("continue", base_id, "echo:enum continue"), exit_code=vocab.EXIT_OK)
    record("continue", call("continue", base_id, "fail enum continue"))
    record(
        "continue", call("continue", base_id, "chunkslow:5 enum continue", "--cancel-after", "0.5")
    )
    record(
        "continue",
        call("continue", base_id, "slow:1 enum continue", "--background", "--permissions", "read"),
        exit_code=vocab.EXIT_OK,
    )

    def steer(prompt: str, *extra: str) -> Any:
        steer_id = _background_session(cli, "slow:30 enum steer base")
        return call("steer", steer_id, prompt, *extra)

    record("steer", steer("echo:enum steer"), exit_code=vocab.EXIT_OK)
    record("steer", steer("fail enum steer"))
    record("steer", steer("chunkslow:5 enum steer", "--cancel-after", "0.5"))

    # `--background` needs the daemon, so the sessions steered on the direct
    # route are opened before the route is forced.
    steer_timeout_id = _background_session(cli, "slow:30 enum steer base")
    steer_unknown_id = _background_session(cli, "slow:30 enum steer base")

    with _direct_child_route():
        record(
            "run",
            call("run", "mock", "slow:5 enum run", "--timeout", "0.001"),
            exit_code=vocab.EXIT_TIMEOUT,
        )
        record(
            "continue",
            call("continue", finished_base(), "slow:30 enum continue", "--timeout", "3"),
            exit_code=vocab.EXIT_TIMEOUT,
        )
        record(
            "steer",
            call("steer", steer_timeout_id, "slow:30 enum steer", "--timeout", "3"),
            exit_code=vocab.EXIT_TIMEOUT,
        )
        record(
            "run",
            _kill_turn_worker(
                cli,
                (
                    "run",
                    "mock",
                    "slow:30 enum run",
                    "--timeout",
                    "20",
                    "--name",
                    "enum-run-unknown",
                    "--json",
                    "--quiet",
                ),
                lambda: _await_alias("enum-run-unknown"),
                lambda meta: meta.state == "running",
            ),
            exit_code=vocab.EXIT_AGENT_ERROR,
        )
        unknown_base = finished_base()
        base_turn = sessions.read_meta(unknown_base).turns
        record(
            "continue",
            _kill_turn_worker(
                cli,
                (
                    "continue",
                    unknown_base,
                    "slow:30 enum continue",
                    "--timeout",
                    "20",
                    "--json",
                    "--quiet",
                ),
                lambda: unknown_base,
                lambda meta: meta.turns > base_turn and meta.state == "running",
            ),
            exit_code=vocab.EXIT_AGENT_ERROR,
        )
        steer_turn = sessions.read_meta(steer_unknown_id).turns
        record(
            "steer",
            _kill_turn_worker(
                cli,
                (
                    "steer",
                    steer_unknown_id,
                    "slow:30 enum steer",
                    "--timeout",
                    "20",
                    "--json",
                    "--quiet",
                ),
                lambda: steer_unknown_id,
                lambda meta: meta.turns > steer_turn and meta.state == "running",
            ),
            exit_code=vocab.EXIT_AGENT_ERROR,
        )

    waited_id = _background_session(cli, "slow:1 enum wait")
    record("wait", call("wait", waited_id), exit_code=vocab.EXIT_OK)
    record("wait", call("wait", _finished_session("failed")), exit_code=vocab.EXIT_AGENT_ERROR)
    record("wait", call("wait", _finished_session("canceled")), exit_code=vocab.EXIT_CANCELLED)
    record("wait", call("wait", _finished_session("unknown")), exit_code=vocab.EXIT_AGENT_ERROR)
    record(
        "wait",
        call("wait", _active_session(pid=os.getpid()), "--timeout", "0"),
        exit_code=vocab.EXIT_TIMEOUT,
    )
    starting = sessions.create_session(entry="mock", base_adapter="mock", prompt="enum starting")
    record(
        "wait", call("wait", starting.session_id, "--timeout", "0"), exit_code=vocab.EXIT_TIMEOUT
    )


def _observe_log_types(cli: CliRunner, observed: dict[str, set[Any]]) -> None:
    log_id = sessions.create_session(entry="mock", base_adapter="mock", prompt="events").session_id
    events = transcript.Transcript(sessions.transcript_path(log_id))
    event_fields = {
        "msg": {"text": "msg"},
        "thought": {"text": "thought"},
        "tool": {"name": "tool", "args_summary": "", "status": "done", "duration_ms": 0},
        "permission": {"kind": "read", "decision": "allowed"},
        "error": {"message": "error"},
        "state": {"from": "running", "to": "succeeded"},
        "usage": {"tokens": 0, "cost": 0.0},
    }
    for event_type in sorted(transcript.EVENT_TYPES):
        events.append(event_type, **event_fields[event_type])
    log_result = invoke(cli, "log", log_id, "--json")
    assert log_result.exit_code == vocab.EXIT_OK
    observed["log.output.type"] = {
        json.loads(line)["type"] for line in log_result.stdout.splitlines()
    }


def _observe_discovery_enums(
    cli: CliRunner, state_root: Path, observed: dict[str, set[Any]]
) -> None:
    _probe_fixture(state_root)
    probe_result = invoke(cli, "probe", "mock", "--discover", "--json")
    assert probe_result.exit_code == vocab.EXIT_OK, probe_result.stderr
    observed["probe.output.diff[].status"] = {
        item["status"] for item in json.loads(probe_result.stdout)["diff"]
    }
    agents_result = invoke(cli, "agents", "list", "--json")
    assert agents_result.exit_code == vocab.EXIT_OK, agents_result.stderr
    observed["agents list.output.items[].kind"] = {
        item["kind"] for item in json.loads(agents_result.stdout)["items"]
    }


def _assert_reachable_output_enums(
    cli: CliRunner, index: dict[str, Any], observed: dict[str, set[Any]]
) -> None:
    declared: dict[str, set[Any]] = {}
    for entry in index["commands"]:
        detail = read_detail(cli, entry["name"])
        declared.update(
            {
                path: set(values)
                for path, values in _enum_values(
                    detail.get("output", {}), entry["name"] + ".output"
                )
            }
        )
    assert declared == EXPECTED_OUTPUT_ENUMS
    for path, values in declared.items():
        assert observed.get(path, set()) == values, (path, values, observed.get(path))


@pytest.mark.timeout(120)
def test_O4c_declared_enums_have_reachable_values(
    cli: CliRunner,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    live_daemon: None,
) -> None:
    index = read_index(cli)
    observed: dict[str, set[Any]] = {}
    _observe_flag_enums(cli, index, state_root, observed)
    _observe_session_statuses(cli, observed, monkeypatch)
    _observe_work_statuses(cli, observed, monkeypatch)
    _observe_log_types(cli, observed)
    _observe_discovery_enums(cli, state_root, observed)
    _assert_reachable_output_enums(cli, index, observed)


def _start_daemon_pair(cli: CliRunner) -> tuple[str, str, int]:
    first = _background_session(cli, "slow:30 cancel victim")
    second = _background_session(cli, "slow:30 cancel sibling")
    first_meta = sessions.load(first)
    second_meta = sessions.load(second)
    assert first_meta.pid is not None and first_meta.pid == second_meta.pid
    return first, second, first_meta.pid


def _assert_process_alive(pid: int) -> None:
    assert proc.process_liveness(pid, proc.process_start_time(pid)) == "verified"


def _stop_session_daemon(session_id: str, connect: Any) -> None:
    resolution = runner.resolution_from_session(sessions.load(session_id))

    async def stop() -> None:
        daemon = await connect(runner.call_target(resolution))
        if daemon is None:
            return
        try:
            await daemon.stop()
        finally:
            await daemon.close()

    asyncio.run(stop())


@pytest.mark.parametrize(
    "layout",
    [
        "accepted_pending",
        "rpc_unconfirmed",
        "terminal_before_read",
        "terminal_between_rpc",
        "preparing_stale",
        "cmdline_unknown",
    ],
)
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal semantics")
def test_M2_cancel_races_are_forced_and_preserve_the_daemon_and_sibling(
    cli: CliRunner,
    live_daemon: None,
    monkeypatch: pytest.MonkeyPatch,
    layout: str,
) -> None:
    victim, sibling, daemon_pid = _start_daemon_pair(cli)
    real_connect = daemon_client.connect

    try:
        if layout == "accepted_pending":
            result = invoke(cli, "cancel", victim, "--json")
            assert result.exit_code == vocab.EXIT_OK, result.stderr
            assert json.loads(result.stdout)["changed"] is True
            assert json.loads(result.stdout)["status"] == "canceled"
        elif layout == "rpc_unconfirmed":
            os.kill(daemon_pid, signal.SIGSTOP)
            try:
                monkeypatch.setattr(cli_module.runner, "CANCEL_ACK_TIMEOUT", 0.05)
                result = invoke(cli, "cancel", victim, "--json")
            finally:
                os.kill(daemon_pid, signal.SIGCONT)
            assert result.exit_code == vocab.EXIT_AGENT_ERROR
            error = _error(result)
            assert error["kind"] == errors.OUTCOME_UNKNOWN
            assert error["context"] == {"session_id": victim, "status": "running"}
            assert sessions.read_meta(victim).state == "running"
        elif layout == "terminal_before_read":
            sessions.transition(victim, "succeeded", exit_code=0, stop_reason="race")

            async def no_preparation(_targets: Any) -> set[str]:
                return set()

            monkeypatch.setattr(cli_module, "_collect_preparing_sessions", no_preparation)
            result = invoke(cli, "cancel", victim, "--json")
            assert result.exit_code == vocab.EXIT_OK
            assert json.loads(result.stdout) == {
                "session_id": victim,
                "status": "succeeded",
                "stop_reason": "race",
                "changed": False,
            }
        elif layout == "terminal_between_rpc":
            result = invoke(cli, "cancel", victim, "--json")
            assert result.exit_code == vocab.EXIT_OK, result.stderr
            payload = json.loads(result.stdout)
            assert payload["status"] == "canceled"
            assert payload["changed"] is True
        elif layout == "preparing_stale":
            sessions.transition(victim, "succeeded", exit_code=0, stop_reason="old turn")

            async def preparing(_targets: Any) -> set[str]:
                return {victim}

            monkeypatch.setattr(cli_module, "_collect_preparing_sessions", preparing)
            result = invoke(cli, "cancel", victim, "--json")
            assert result.exit_code == vocab.EXIT_OK
            payload = json.loads(result.stdout)
            assert payload["status"] == "succeeded"
            assert payload["changed"] is True
            assert sessions.read_meta(victim).state == "succeeded"
        else:

            async def no_connection(_target: str) -> None:
                return None

            monkeypatch.setattr(daemon_client, "connect", no_connection)
            monkeypatch.setattr(cli_module.proc, "process_cmdline", lambda _pid: None)
            result = invoke(cli, "cancel", victim, "--json")
            assert result.exit_code == vocab.EXIT_AGENT_ERROR
            error = _error(result)
            assert error["kind"] == errors.OUTCOME_UNKNOWN
            assert error["context"] == {"session_id": victim, "status": "running"}
            assert sessions.read_meta(victim).state == "running"

        _assert_process_alive(daemon_pid)
        assert sessions.read_meta(sibling).is_active
    finally:
        _stop_session_daemon(victim, real_connect)


def _wait_for_ready(process: subprocess.Popen[bytes]) -> None:
    assert process.stderr is not None
    assert process.stderr.readline() == b"READY\n"


def _read_pty(fd: int, marker: bytes) -> bytes:
    data = bytearray()
    deadline = time.monotonic() + 5
    while marker not in data and time.monotonic() < deadline:
        remaining = max(0.0, deadline - time.monotonic())
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            break
        try:
            data.extend(os.read(fd, 4096))
        except OSError as error:
            raise AssertionError((bytes(data), error)) from error
    assert marker in data
    return bytes(data)


def _drain_pty(fd: int) -> bytes:
    data = bytearray()
    os.set_blocking(fd, False)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        remaining = max(0.0, deadline - time.monotonic())
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            break
        try:
            chunk = os.read(fd, 4096)
        except BlockingIOError:
            continue
        except OSError:
            break
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


@pytest.mark.timeout(60)
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal semantics")
def test_F2b_F5_wait_interrupts_in_pipe_and_pty_contexts(cli: CliRunner, live_daemon: None) -> None:
    session_id = _background_session(cli, "slow:5 wait interruption")
    ready_code = (
        "import sys\n"
        "import acpc.cli as cli\n"
        "original = cli._select_format\n"
        "def ready(format_name, json_mode, **kwargs):\n"
        "    sys.stderr.write('READY\\n')\n"
        "    sys.stderr.flush()\n"
        "    return original(format_name, json_mode, **kwargs)\n"
        "cli._select_format = ready\n"
        "raise SystemExit(cli.main())\n"
    )
    command = [
        sys.executable,
        "-c",
        ready_code,
        "wait",
        session_id,
        "--json",
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=os.environ.copy(),
    )
    try:
        _wait_for_ready(process)
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
    assert process.returncode == vocab.EXIT_CANCELLED
    assert stdout == b""
    assert json.loads(stderr.splitlines()[-1])["error"]["kind"] == errors.INTERRUPTED
    _wait_for_state(session_id, "succeeded", timeout=10)

    session_id = _background_session(cli, "slow:5 wait pty interruption")
    master, slave = pty.openpty()
    pty_process = subprocess.Popen(
        [*command[:-2], session_id, "--json"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=slave,
        start_new_session=True,
        env=os.environ.copy(),
    )
    try:
        os.close(slave)
        ready = _read_pty(master, b"READY")
        os.killpg(pty_process.pid, signal.SIGINT)
        stdout, _ = pty_process.communicate(timeout=10)
        stderr = ready + _drain_pty(master)
    finally:
        if pty_process.poll() is None:
            os.killpg(pty_process.pid, signal.SIGKILL)
            pty_process.wait(timeout=10)
        with contextlib.suppress(OSError):
            os.close(slave)
        with contextlib.suppress(OSError):
            os.close(master)
    assert pty_process.returncode == vocab.EXIT_CANCELLED
    assert stdout == b""
    assert json.loads(stderr.splitlines()[-1])["error"]["kind"] == errors.INTERRUPTED
    _wait_for_state(session_id, "succeeded", timeout=10)


def test_D7c_claim_is_bound_to_the_versioned_standard_snapshot(cli: CliRunner) -> None:
    snapshot = STANDARD_SNAPSHOT.read_bytes()
    snapshot_text = snapshot.decode()
    metadata = json.loads(STANDARD_METADATA.read_text(encoding="utf-8"))
    source_path = Path(metadata["source"])
    assert not source_path.is_absolute()
    assert metadata["source_repository"] == "https://github.com/DamianPala/haz-skills.git"
    assert metadata["content_sha256"] == hashlib.sha256(snapshot).hexdigest()
    version = re.search(r"^\*\*Version:\*\*\s+(\S+)$", snapshot_text, re.MULTILINE)
    assert version is not None
    assert version.group(1) == EXPECTED_STANDARD_VERSION
    assert read_index(cli)["conformance"]["standard"] == version.group(1)
    for clause in REQUIRED_STANDARD_CLAUSES:
        assert clause in snapshot_text

    checkout = os.environ.get("ACPC_STANDARD_CHECKOUT")
    if checkout is None:
        warnings.warn(
            "conformance claim is unverified against its external source; set "
            "ACPC_STANDARD_CHECKOUT to verify the repository snapshot",
            pytest.PytestWarning,
            stacklevel=2,
        )
        return
    checkout_path = Path(checkout)
    assert checkout_path.read_bytes() == snapshot
    source_repository = Path(
        os.environ.get("ACPC_STANDARD_SOURCE_REPOSITORY", checkout_path.parents[2])
    )
    source_blob = subprocess.run(
        [
            "git",
            "-C",
            str(source_repository),
            "show",
            f"{metadata['source_commit']}:{source_path.as_posix()}",
        ],
        check=False,
        capture_output=True,
    )
    assert source_blob.returncode == 0, source_blob.stderr.decode(errors="replace")
    assert source_blob.stdout == snapshot
