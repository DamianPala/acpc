"""The command surface, published as JSON and generated from the parser.

`acpc schema` answers two questions: which commands exist, and how one of
them is called.  Both answers are built by walking the Click tree that runs
the tool, never from a table written beside it.  A table drifts — a flag is
added, the table is not touched, and the tool publishes something false —
while a generator cannot: the parser is the only source it has.

Click carries most of what a descriptor needs (names, types, choices,
defaults, whether a value is required) but not all of it: a positional
argument has no help text at all, and nothing in Click says that `-` on this
argument means stdin.  Those facts are declared on the command with the
decorators below, next to the parameter they describe, for the same reason
`effects` sits on the command rather than in a registry.

Nothing here imports `cli`.  The generator takes a Click group as an
argument, so the whole surface can be produced and checked without running a
command or touching a session directory.
"""

from collections.abc import Callable, Iterator, Mapping, Sequence
from copy import deepcopy
from typing import Any

import click

from acpc import __version__, effects, transcript, vocab

# Introspection format version.  It changes when a required field is added, a
# field is removed, or a field's type or meaning changes — not when an
# optional field appears.
SCHEMA_VERSION = "1"

# The first argument reserved for introspection; it is not a command entry.
COMMAND_NAME = "schema"

# The standard this surface is generated against.
STANDARD_NAME = "cli-design-standard"
STANDARD_VERSION = "0.2.0-draft.11"

# Default output format per context.  acpc renders text in both today; a
# command that has a machine shape offers it behind its own `--json`.
FORMAT_DEFAULTS: dict[str, str] = {"tty": "text", "non_tty": "json"}


def _object(properties: dict[str, Any], required: Sequence[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(required)}


def _array(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}


_STRING = {"type": "string"}
_INTEGER = {"type": "integer"}
_NUMBER = {"type": "number"}
_BOOLEAN = {"type": "boolean"}
_NULLABLE_STRING = {"type": ["string", "null"]}
_NULLABLE_NUMBER = {"type": ["number", "null"]}
_NULLABLE_INTEGER = {"type": ["integer", "null"]}
_SESSION_STATUS = {"type": "string", "enum": list(vocab.SESSION_STATES)}
# `status` in an answer result.  One schema covers the success and the failure
# documents of a command (O4a), so the enum lists every state a result can
# carry: the finished ones plus `running` when a `--timeout` deadline expires
# or a SIGTERM detaches the client before the turn ends. A `starting` record
# has no observed turn and produces no answer document.
# `preparing` is a daemon turn phase that is never written to `meta.json`, so
# no result document can report it. `waiting` is excluded too: a result
# document is only ever built once `execute_turn` returns, and it returns
# either with the turn finished or with no document at all (a `--timeout`
# deadline, O4c reachability would need a SIGTERM to land in the narrow
# window where the on-disk session is `waiting`, which no path in this
# codebase can drive deterministically) — `status` and `wait --timeout` are
# what report `waiting` (SPEC.md `run`, `wait`).
_ANSWER_STATUS = {
    "type": "string",
    "enum": [
        state for state in vocab.SESSION_STATES if state not in {"preparing", "starting", "waiting"}
    ],
}
# A follow-up turn rotates a finished session straight to `running` under the
# session lock, so `continue` and `steer` can observe `running` but never
# `starting`; listing it would name a value the command cannot return (O4c).
_FOLLOW_UP_STATUS = {
    "type": "string",
    "enum": list(_ANSWER_STATUS["enum"]),
}
# `wait` always describes a turn it observed end (V5a): a client deadline
# returns no document at all, so the only statuses a `wait` result can carry
# are the terminal ones.
_WAIT_STATUS = {
    "type": "string",
    "enum": [state for state in vocab.SESSION_STATES if state in vocab.FINISHED_STATES],
}
# `steer` keeps its pre-slice result contract: only the states it could
# publish after redirecting a live turn, without the new partial field.
_STEER_STATUS = {"type": "string", "enum": ["running", "succeeded"]}
_CANCEL_STATUS = {
    "type": "string",
    "enum": ["running", "succeeded", "failed", "canceled", "unknown"],
}

_PATHS = _object(
    {name: _STRING for name in ("dir", "prompt", "transcript", "answer")},
    ("dir", "prompt", "transcript", "answer"),
)
_DENIAL = _object(
    {
        "category": _STRING,
        "count": _INTEGER,
        "minimum_policy": _STRING,
        "remedy": _STRING,
        "target": _STRING,
    },
    ("category", "count", "minimum_policy", "remedy"),
)
_PERMISSIONS_CLAMP = _object(
    {name: _STRING for name in ("requested", "ceiling", "effective")},
    ("requested", "ceiling", "effective"),
)
# The session-capability object (V1c): one shape shared by `run`, `continue`,
# `steer`, `wait` and `status`, never redeclared per command.
_CAPABILITIES = _object(
    {
        "steer_mode": {"type": "string", "enum": ["in-place", "cancel-then-start"]},
        "continue_without_message": _BOOLEAN,
    },
    ("steer_mode", "continue_without_message"),
)
# `limit` on `status` and, optionally, on a `run`/`continue`/`wait` result: the
# usage-limit record SPEC.md `status` describes, `null` unless one touched the
# current turn.
#
# `source` is deliberately a plain string, not a closed enum: unlike `status`
# or `stop_reason`, every value it can take (`error_kind`, `rate_limit_info`,
# `text`) would have to be independently reachable through every command that
# carries `limit` for O4c, for a diagnostic field no caller branches on.
_LIMIT = _object(
    {
        "reason": _STRING,
        "resume_at": _NULLABLE_STRING,
        "auto_continue": _BOOLEAN,
        "source": _STRING,
    },
    ("reason", "resume_at", "auto_continue", "source"),
)
_NULLABLE_LIMIT = {
    "type": ["object", "null"],
    "properties": _LIMIT["properties"],
    "required": _LIMIT["required"],
}

_SESSION_RESULT_PROPERTIES = {
    "session_id": _STRING,
    "turn": _INTEGER,
    "status": _SESSION_STATUS,
    "stop_reason": _NULLABLE_STRING,
    "tokens": _NULLABLE_INTEGER,
    "paths": _PATHS,
    "cost": _NULLABLE_NUMBER,
    "answer": _STRING,
    "truncated": _BOOLEAN,
    "partial": _BOOLEAN,
    "output_file": _STRING,
    "denied": _array(_DENIAL),
    "capabilities": _CAPABILITIES,
    "permissions_clamp": {
        "type": ["object", "null"],
        "properties": _PERMISSIONS_CLAMP["properties"],
        "required": _PERMISSIONS_CLAMP["required"],
    },
    "next": _array(_STRING),
    "resume": _STRING,
    "created_at": _NULLABLE_STRING,
    "started_at": _NULLABLE_STRING,
    "finished_at": _NULLABLE_STRING,
    "changed": _BOOLEAN,
    "limit": _NULLABLE_LIMIT,
}

_RESOLUTION_CLAMP = _object(
    {name: _STRING for name in ("requested", "ceiling", "effective")},
    ("requested", "ceiling", "effective"),
)
_RESOLUTION_FIELD = _object(
    {
        "value": _NULLABLE_STRING,
        "source": _STRING,
        "grants": _STRING,
        "delegates": _BOOLEAN,
        "escalates": _BOOLEAN,
        "clamp": _RESOLUTION_CLAMP,
    },
    ("value", "source"),
)
_RESOLUTION = _object(
    {
        "entry": _STRING,
        "base_adapter": _STRING,
        "command": _STRING,
        "cwd": _STRING,
        "env": {},
        "env_passthrough": _array(_STRING),
        "resolved": _object(
            {
                name: _RESOLUTION_FIELD
                for name in ("model", "effort", "mode", "permissions", "home")
            },
            ("model", "effort", "mode", "permissions", "home"),
        ),
    },
    (
        "entry",
        "base_adapter",
        "command",
        "cwd",
        "env",
        "env_passthrough",
        "resolved",
    ),
)


def _session_result_schema(
    *,
    changed: bool,
    foreground_only: bool = False,
    status: dict[str, Any] = _ANSWER_STATUS,
    include_partial: bool = True,
) -> dict[str, Any]:
    properties = dict(_SESSION_RESULT_PROPERTIES)
    properties["status"] = status
    if not include_partial:
        properties.pop("partial")
    if not changed:
        properties.pop("changed")
    required = ["status", "session_id", "turn", "created_at", "started_at", "finished_at"]
    if foreground_only:
        required += ["stop_reason", "tokens", "cost", "answer"]
    required += ["paths", "truncated"]
    if include_partial:
        required.append("partial")
    required += ["denied", "permissions_clamp", "capabilities"]
    if changed:
        required.append("changed")
    return _object(properties, required)


_STEER_CORRECTION = _object(
    {
        "steer_mode": _STRING,
        "target_turn": _INTEGER,
        "target_status": _STRING,
        "message_state": _STRING,
    },
    ("steer_mode", "target_turn", "target_status", "message_state"),
)
# `steer` carries `correction_result` on top of the shared session result;
# `turn` is the same field every answer result carries, holding the turn to
# observe next, and `capabilities` belongs to the shared result shape too.
_STEER_RESULT_PROPERTIES = {
    "capabilities": _CAPABILITIES,
    "correction_result": _STEER_CORRECTION,
}


def _steer_schema() -> dict[str, Any]:
    schema = _session_result_schema(changed=True, status=_STEER_STATUS, include_partial=False)
    schema["properties"].update(_STEER_RESULT_PROPERTIES)
    schema["required"] = [
        *schema["required"],
        *[name for name in _STEER_RESULT_PROPERTIES if name not in schema["required"]],
    ]
    return schema


_AGENT_ITEM = _object(
    {
        "name": _STRING,
        "kind": {"type": "string", "enum": ["adapter", "variant"]},
        "display_name": _STRING,
        "status": _STRING,
        "base_adapter": _STRING,
        "model": _NULLABLE_STRING,
        "effort": _NULLABLE_STRING,
        "permissions": _NULLABLE_STRING,
        "home": _NULLABLE_STRING,
        "description": _NULLABLE_STRING,
    },
    ("name", "kind", "description"),
)
_AGENTS_LIST = _object(
    {"items": _array(_AGENT_ITEM), "has_more": _BOOLEAN},
    ("items", "has_more"),
)
_CHECK_ITEM = _object(
    {
        "agent": _STRING,
        "ok": _BOOLEAN,
        "models": _INTEGER,
        "error": _STRING,
    },
    ("agent", "ok"),
)
_AGENTS_CHECK = _object(
    {"items": _array(_CHECK_ITEM), "has_more": _BOOLEAN},
    ("items", "has_more"),
)
_RESOLVED_FIELD = _object({"value": _NULLABLE_STRING, "source": _STRING}, ("value", "source"))
_AGENTS_GET = _object(
    {
        "agent": _STRING,
        "base_adapter": _STRING,
        "description": _NULLABLE_STRING,
        "resolved": _object(
            {name: _RESOLVED_FIELD for name in ("model", "effort", "mode", "permissions", "home")},
            ("model", "effort", "mode", "permissions", "home"),
        ),
        "env": {},
        "env_passthrough": _array(_STRING),
        "advertised": _object(
            {
                "modes": _array(_STRING),
                "mode_specs": {},
                "models": _array(_STRING),
                "commands": _array({}),
            },
            ("modes", "mode_specs", "models", "commands"),
        ),
        "presets": {},
        "models": _array(_STRING),
        "commands": _array(
            _object(
                {"name": _STRING, "description": _NULLABLE_STRING},
                ("name", "description"),
            )
        ),
    },
    ("agent",),
)
_DAEMON_ITEM = _object(
    {
        "target": _STRING,
        "version": _STRING,
        "pid": _INTEGER,
        "uptime_seconds": _NUMBER,
        "log": _STRING,
        "sessions": _array(_STRING),
        "preparing": _array(_STRING),
        "restoring": _array(_STRING),
        "max_concurrent": _INTEGER,
        "idle_seconds": _NULLABLE_NUMBER,
    },
    (
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
    ),
)
_DAEMON_STATUS = _object(
    {"items": _array(_DAEMON_ITEM), "has_more": _BOOLEAN},
    ("items", "has_more"),
)
_TARGET_MUTATION = _object(
    {
        "targets": _array(_STRING),
        "changed": _BOOLEAN,
        "requires_confirmation": _BOOLEAN,
    },
    ("targets", "changed", "requires_confirmation"),
)
_STATUS_LIST_ITEM = _object(
    {
        "session_id": _STRING,
        "entry": _STRING,
        "model": _NULLABLE_STRING,
        "status": _SESSION_STATUS,
        "name": _NULLABLE_STRING,
        "prompt_snippet": _STRING,
        "runtime_seconds": _NUMBER,
        "idle_seconds": _NULLABLE_NUMBER,
        "created_at": _NULLABLE_STRING,
        "started_at": _NULLABLE_STRING,
        "finished_at": _NULLABLE_STRING,
    },
    (
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
    ),
)
# R2d's non-interactive inspection of the policy applied to accepted work:
# `policy` and `source` share the provenance recorded on the session's stored
# permission resolution, `mode` is the adapter mode serving it, and `clamp`
# reuses the same shape `permissions_clamp` does.
_STATUS_PERMISSIONS = _object(
    {
        "policy": _STRING,
        "mode": _NULLABLE_STRING,
        "source": _STRING,
        "clamp": {
            "type": ["object", "null"],
            "properties": _PERMISSIONS_CLAMP["properties"],
            "required": _PERMISSIONS_CLAMP["required"],
        },
    },
    ("policy", "mode", "source", "clamp"),
)
_STATUS_DETAIL = _object(
    {
        "session_id": _STRING,
        "status": _SESSION_STATUS,
        "pid": _NULLABLE_INTEGER,
        "turns": _INTEGER,
        "entry": _STRING,
        "base_adapter": _STRING,
        "model": _NULLABLE_STRING,
        "name": _NULLABLE_STRING,
        "runtime_seconds": _NUMBER,
        "idle_seconds": _NULLABLE_NUMBER,
        "tokens": _NULLABLE_INTEGER,
        "cost": _NULLABLE_NUMBER,
        "exit_code": _NULLABLE_INTEGER,
        "stop_reason": _NULLABLE_STRING,
        "failure": _NULLABLE_STRING,
        "capabilities": _CAPABILITIES,
        "limit": _NULLABLE_LIMIT,
        # V4c: acpc cannot observe whether a forwarded correction is still
        # pending inside the adapter, so this is always `null` rather than a
        # count acpc never has grounds to report.
        "pending_corrections": {"type": ["integer", "null"]},
        "permissions": _STATUS_PERMISSIONS,
        "paths": _PATHS,
        "created_at": _NULLABLE_STRING,
        "started_at": _NULLABLE_STRING,
        "finished_at": _NULLABLE_STRING,
    },
    (
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
        "capabilities",
        "limit",
        "pending_corrections",
        "permissions",
        "paths",
        "created_at",
        "started_at",
        "finished_at",
    ),
)
_STATUS = _object(
    {"items": _array(_STATUS_LIST_ITEM), "has_more": _BOOLEAN},
    ("items", "has_more"),
)
_LOG_EVENT = _object(
    {
        "i": _INTEGER,
        "ts": _STRING,
        "type": {"type": "string", "enum": sorted(transcript.EVENT_TYPES)},
        "text": _STRING,
        "name": _STRING,
        "args_summary": _STRING,
        "status": _STRING,
        "duration_ms": _INTEGER,
        "kind": _STRING,
        "decision": _STRING,
        "auto": _BOOLEAN,
        "mode": _STRING,
        "outcome": _STRING,
        "message": _STRING,
        "observation": _STRING,
        "next_step": _STRING,
        "adapter_log": _STRING,
        "adapter_log_tail": _STRING,
        "from": _STRING,
        "to": _STRING,
        "tokens": _NULLABLE_INTEGER,
        "cost": _NULLABLE_NUMBER,
        "reason": _STRING,
        "resume_at": _NULLABLE_STRING,
        "action": _STRING,
        "source": _STRING,
        "detail": _STRING,
    },
    ("i", "ts", "type"),
)
_MODE_FACTS = _object(
    {"grants": _STRING, "delegates": _BOOLEAN, "escalates": _BOOLEAN},
    ("grants", "delegates", "escalates"),
)
_PROBE = _object(
    {
        "entry": _STRING,
        "base_adapter": _STRING,
        "discover_only": _BOOLEAN,
        "turns": _INTEGER,
        "current_mode": _NULLABLE_STRING,
        "advertised_modes": _array(
            _object(
                {"id": _STRING, "name": _STRING, "description": _NULLABLE_STRING},
                ("id", "name", "description"),
            )
        ),
        "mode_reports": {},
        "verdicts": {},
        "refusal_violations": _array({}),
        "implied_modes": {},
        "unmeasured": _array({}),
        "current_modes": {},
        "diff": _array(
            _object(
                {
                    "mode": _STRING,
                    "status": {"type": "string", "enum": ["advertised-missing", "entry-missing"]},
                    "description": _NULLABLE_STRING,
                    "current": {
                        "type": ["object", "null"],
                        "properties": _MODE_FACTS["properties"],
                        "required": _MODE_FACTS["required"],
                    },
                    "proposed": {
                        "type": ["object", "null"],
                        "properties": _MODE_FACTS["properties"],
                        "required": _MODE_FACTS["required"],
                    },
                },
                ("mode", "status", "description", "current", "proposed"),
            )
        ),
    },
    (
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
    ),
)

_OUTPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "agents check": _AGENTS_CHECK,
    "agents create": _object(
        {"name": _STRING, "extends": _STRING, "path": _STRING, "changed": _BOOLEAN},
        ("name", "extends", "path", "changed"),
    ),
    "agents delete": _object(
        {"name": _STRING, "path": _STRING, "changed": _BOOLEAN},
        ("name", "path", "changed"),
    ),
    "agents get": _AGENTS_GET,
    "agents list": _AGENTS_LIST,
    "cancel": _object(
        {
            "session_id": _STRING,
            "status": _CANCEL_STATUS,
            "stop_reason": _NULLABLE_STRING,
            "changed": _BOOLEAN,
        },
        ("session_id", "status", "stop_reason", "changed"),
    ),
    "continue": _session_result_schema(changed=True, status=_FOLLOW_UP_STATUS),
    "daemon status": _DAEMON_STATUS,
    "daemon stop": _TARGET_MUTATION,
    "delete": _object(
        {"session_id": _STRING, "removed": _BOOLEAN, "changed": _BOOLEAN, "paths": _PATHS},
        ("session_id", "removed", "changed", "paths"),
    ),
    "install": _object(
        {
            "agent": _STRING,
            "ok": _BOOLEAN,
            "returncode": _INTEGER,
            "changed": {"type": ["boolean", "null"]},
        },
        ("agent", "ok", "returncode", "changed"),
    ),
    "log": _LOG_EVENT,
    "probe": _PROBE,
    "prune": _TARGET_MUTATION,
    "resolve": _RESOLUTION,
    "run": _session_result_schema(changed=True),
    "skills get": _object(
        {"name": _STRING, "description": _STRING, "path": _STRING, "body": _STRING},
        ("name", "description", "path", "body"),
    ),
    "skills list": _object(
        {
            "items": _array(
                _object(
                    {"name": _STRING, "description": _STRING, "path": _STRING},
                    ("name", "description", "path"),
                )
            ),
            "has_more": _BOOLEAN,
        },
        ("items", "has_more"),
    ),
    "list": _STATUS,
    "status": _STATUS_DETAIL,
    "steer": _steer_schema(),
    "wait": _session_result_schema(changed=False, foreground_only=True, status=_WAIT_STATUS),
}

# Flags accepted by *every* command entry, and therefore not repeated in any
# D8 document.  A flag belongs here if and only if it is in the intersection
# of the flags of every entry in `commands` — not because it reads as general
# purpose.  `--help` and `--version` are excluded by definition; `schema` and
# the group prefixes are not entries, so they do not narrow the intersection.
GLOBAL_FLAGS: list[dict[str, Any]] = [
    {
        "name": "json",
        "description": (
            "Emit this command's result as JSON on stdout instead of text; failures answer "
            "in the machine envelope either way. On `log`, mutually exclusive with `--prose`; "
            "when `--format` is available, cannot be combined with a different format value."
        ),
        "type": "boolean",
        "required": False,
        "default": False,
    },
    {
        "name": "color",
        "description": "Color policy for human output: auto, always or never.",
        "type": "string",
        "required": False,
        "default": "auto",
        "enum": ["auto", "always", "never"],
    },
]

_GLOBAL_FLAG_NAMES = frozenset(flag["name"] for flag in GLOBAL_FLAGS)

# No acpc command starts an interactive session: a permission prompt, the
# `install` prompt and the `--bg` policy question all ask for one missing
# input and then run the same non-interactive call.
INTERACTIVE = False

_DESCRIPTIONS = "_acpc_schema_descriptions"
_STDIN = "_acpc_schema_stdin"
_STREAM = "_acpc_schema_stream"
_FORMAT_DEFAULTS = "_acpc_schema_format_defaults"
_OUTPUT_DESCRIPTION = "_acpc_schema_output_description"
_DISPATCHES_WITHOUT_COMMAND = "_acpc_schema_dispatches_without_command"


class SchemaError(Exception):
    """A command cannot be published: a parameter has no declared contract."""


def _output_schema(name: str) -> dict[str, Any]:
    try:
        return deepcopy(_OUTPUT_SCHEMAS[name])
    except KeyError:
        raise SchemaError(f"{name}: no output schema") from None


def _parameter_names(command: click.Command) -> set[str]:
    return {parameter.name for parameter in command.params if parameter.name}


def describes[C: click.Command](**descriptions: str) -> Callable[[C], C]:
    """Give parameters of the decorated command their one-line contract.

    Positional arguments have no other place to carry one, and a flag whose
    Click help omits a value syntax, a bound or a resolution rule states it
    here instead of stretching the help line.
    """

    def apply(command: C) -> C:
        unknown = set(descriptions) - _parameter_names(command)
        if unknown:
            raise SchemaError(f"{command.name}: no such parameter: {', '.join(sorted(unknown))}")
        merged = {**getattr(command, _DESCRIPTIONS, {}), **descriptions}
        setattr(command, _DESCRIPTIONS, merged)
        return command

    return apply


def reads_stdin[C: click.Command](*names: str) -> Callable[[C], C]:
    """Mark the parameters on which `-` selects stdin instead of a file."""

    def apply(command: C) -> C:
        unknown = set(names) - _parameter_names(command)
        if unknown:
            raise SchemaError(f"{command.name}: no such parameter: {', '.join(sorted(unknown))}")
        setattr(command, _STDIN, frozenset(names) | getattr(command, _STDIN, frozenset()))
        return command

    return apply


def emits_record_stream[C: click.Command](command: C) -> C:
    """Mark a command whose success is a stream of records, not one document."""
    setattr(command, _STREAM, True)
    return command


def dispatches_without_command[C: click.Group](command: C) -> C:
    """Mark a group whose callback performs useful work without a subcommand.

    Apply this to a Click group only when invoking that group without a
    subcommand executes its operation. Groups that only print help or explain
    a missing subcommand remain unmarked and are published as prefixes.
    """
    setattr(command, _DISPATCHES_WITHOUT_COMMAND, True)
    return command


def output_description[C: click.Command](text: str) -> Callable[[C], C]:
    """Declare the result behavior the output schema cannot express.

    The schema says which fields a result document has; it cannot say when a
    failure returns one, or which fields are present only in one situation.
    That is what this string is for, and a command that returns results on
    failure or has success-only presence requirements must carry it (D7).
    """

    def apply(command: C) -> C:
        setattr(command, _OUTPUT_DESCRIPTION, _one_line(text))
        return command

    return apply


def format_defaults[C: click.Command](**defaults: str) -> Callable[[C], C]:
    """Declare a command's exception to the tool-wide output defaults."""
    if set(defaults) != {"tty", "non_tty"}:
        raise SchemaError("format defaults must declare tty and non_tty")

    def apply(command: C) -> C:
        setattr(command, _FORMAT_DEFAULTS, dict(defaults))
        return command

    return apply


# Click type names mapped onto the four types a descriptor may declare.
# Anything else — a duration, a path, a choice of strings — is a string whose
# accepted syntax belongs in its description.
_TYPES = {
    "integer": "integer",
    "integer range": "integer",
    "float": "number",
    "float range": "number",
    "boolean": "boolean",
}


def _type_of(parameter: click.Parameter) -> str:
    if isinstance(parameter, click.Option) and parameter.is_flag:
        return "boolean"
    return _TYPES.get(parameter.type.name, "string")


def _is_own_flag(parameter: click.Parameter) -> bool:
    """True for a flag the command itself defines.

    `--help` and `--version` are excluded: they consume no value, expose
    nothing to the command, and the standard requires them separately.
    """
    if not isinstance(parameter, click.Option):
        return False
    return not (parameter.is_eager and not parameter.expose_value)


def _name_and_aliases(option: click.Option) -> tuple[str, list[str]]:
    """The canonical name and the other spellings, all without hyphens."""
    spellings = [*option.opts, *option.secondary_opts]
    longs = [item for item in spellings if item.startswith("--")]
    canonical = longs[0] if longs else spellings[0]
    aliases = [item.lstrip("-") for item in spellings if item != canonical]
    return canonical.lstrip("-"), aliases


def _one_line(text: str) -> str:
    """Collapse a declared help string into the single line a descriptor needs.

    Docstrings are written for `--help`, so they carry RST literals and the
    `\\b` Click uses to hold a block's line breaks.  Both are rendering marks,
    not content, and a JSON reader has no use for either.
    """
    return " ".join(text.replace("``", "`").replace("\b", " ").split())


def _description(parameter: click.Parameter, command: click.Command) -> str:
    declared = getattr(command, _DESCRIPTIONS, {}).get(parameter.name)
    if declared:
        return _one_line(declared)
    if isinstance(parameter, click.Option) and parameter.help:
        return _one_line(parameter.help)
    raise SchemaError(
        f"{command.name}: parameter {parameter.name!r} has no description; "
        "declare one with @schema.describes"
    )


_MISSING = object()

# The only value types a descriptor may publish.
_SCALARS = (bool, int, float, str)


def _default_of(parameter: click.Parameter) -> Any:
    """The parser's built-in default, or `_MISSING` when it has none.

    A required input has no default.  A value Click leaves as `None` is the
    sentinel for "not provided", and a value resolved when the command runs —
    from configuration, the registry or the environment — is not a default
    either: both are omitted, and the description says where the value comes
    from instead.
    """
    if parameter.required:
        return _MISSING
    value = parameter.default
    if callable(value):
        return _MISSING
    if parameter.multiple or parameter.nargs == -1:
        if not isinstance(value, list | tuple) or not value:
            return _MISSING
        return [item for item in value if isinstance(item, _SCALARS)]
    # Anything that is not one of the four scalar types is a sentinel: `None`,
    # or the marker Click leaves on a parameter that declared no default.
    return value if isinstance(value, _SCALARS) else _MISSING


def _descriptor(
    parameter: click.Parameter, command: click.Command, *, required: bool | None = None
) -> dict[str, Any]:
    descriptor: dict[str, Any] = {}
    if isinstance(parameter, click.Option):
        name, aliases = _name_and_aliases(parameter)
    else:
        # Verbatim: an argument's name is the placeholder `--help` prints for
        # it, and the two have to read as the same thing.
        name, aliases = parameter.name or "", []
    descriptor["name"] = name
    descriptor["description"] = _description(parameter, command)
    descriptor["type"] = _type_of(parameter)
    descriptor["required"] = parameter.required if required is None else required
    default = _default_of(parameter)
    if default is not _MISSING:
        descriptor["default"] = default
    if isinstance(parameter.type, click.Choice):
        descriptor["enum"] = [str(choice) for choice in parameter.type.choices]
    if aliases:
        descriptor["aliases"] = aliases
    if isinstance(parameter, click.Argument) and parameter.nargs == -1:
        descriptor["variadic"] = True
    if parameter.name in getattr(command, _STDIN, frozenset()):
        descriptor["accepts_stdin"] = True
    return descriptor


def _args(command: click.Command) -> list[dict[str, Any]]:
    return [
        _descriptor(parameter, command)
        for parameter in command.params
        if isinstance(parameter, click.Argument)
    ]


def _flags(command: click.Command) -> list[dict[str, Any]]:
    """The command's own flags: D8 must not repeat the global ones."""
    descriptors = (
        _descriptor(parameter, command) for parameter in command.params if _is_own_flag(parameter)
    )
    return [flag for flag in descriptors if flag["name"] not in _GLOBAL_FLAG_NAMES]


def _short_description(command: click.Command) -> str:
    """The command's one-line purpose, from its declaration."""
    text = command.short_help or command.help or ""
    line = _one_line(text.split("\n\n", 1)[0])
    if not line:
        raise SchemaError(f"{command.name}: no description")
    return line


def _effects_of(command: click.Command) -> str:
    value = effects.of(command)
    if value is None:
        raise SchemaError(f"{command.name}: no effects declaration")
    return value


def _accepts_confirmation(command: click.Command) -> bool:
    """Whether any valid call of this command may require `--yes` (R3a)."""
    return any(
        isinstance(parameter, click.Option) and "--yes" in parameter.opts
        for parameter in command.params
    )


def _walk(group: click.Group, prefix: tuple[str, ...] = ()) -> Iterator[tuple[str, click.Command]]:
    """Every path the tool itself dispatches, as `path, command`.

    A group is an entry only when it does something on its own; a group that
    exists to hold subcommands dispatches nothing and is only a prefix.
    """
    for name, command in group.commands.items():
        path = (*prefix, name)
        if path == (COMMAND_NAME,):
            continue
        if isinstance(command, click.Group):
            # A group may use ``invoke_without_command`` to produce a usage
            # error or help.  Only a group explicitly marked as doing useful
            # work without a subcommand is itself a dispatchable command.
            if getattr(command, _DISPATCHES_WITHOUT_COMMAND, False):
                yield " ".join(path), command
            yield from _walk(command, path)
        else:
            yield " ".join(path), command


def commands(root: click.Group) -> dict[str, click.Command]:
    """Every command entry by full path, in Unicode code point order."""
    return dict(sorted(_walk(root), key=lambda item: item[0]))


def detail(name: str, command: click.Command) -> dict[str, Any]:
    """The D8 document for one command.

    The parser supplies invocation details; the command's success schema is a
    declared contract because Click has no way to infer it.
    """
    document: dict[str, Any] = {
        "name": name,
        "description": _short_description(command),
        "args": _args(command),
        "flags": _flags(command),
        "effects": _effects_of(command),
        "confirm": _accepts_confirmation(command),
        "interactive": INTERACTIVE,
        "output": _output_schema(name),
    }
    description = getattr(command, _OUTPUT_DESCRIPTION, None)
    if description is not None:
        document["output_description"] = description
    if getattr(command, _STREAM, False):
        document["stream"] = True
    defaults = getattr(command, _FORMAT_DEFAULTS, None)
    if defaults is not None:
        document["format_defaults"] = dict(defaults)
    return document


def index(root: click.Group) -> dict[str, Any]:
    """The D7 index: everything a caller needs to choose a command."""
    return {
        "schema_version": SCHEMA_VERSION,
        "tool_version": __version__,
        "global_flags": list(GLOBAL_FLAGS),
        "format_defaults": dict(FORMAT_DEFAULTS),
        "exit_codes": dict(vocab.EXIT_DESCRIPTIONS),
        "conformance": {
            "name": STANDARD_NAME,
            "standard": STANDARD_VERSION,
            "extensions": ["managed", "conversational"],
        },
        "commands": [
            {
                "name": name,
                "description": _short_description(command),
                "effects": _effects_of(command),
            }
            for name, command in commands(root).items()
        ],
    }


def nearest(known: Mapping[str, click.Command], path: Sequence[str]) -> list[str]:
    """Valid paths closest to one that does not exist.

    Closeness is the longest prefix of the requested path that any entry
    shares.  With no shared prefix at all, every path is equally near, which
    is the whole list — the answer a caller who mistyped the first word needs.
    """
    for length in range(len(path), -1, -1):
        prefix = list(path[:length])
        matches = [name for name in known if name.split(" ")[:length] == prefix]
        if matches:
            return matches
    return list(known)


def document(root: click.Group, path: Sequence[str]) -> dict[str, Any]:
    """The index for an empty path, or one command's detail.

    Raises `KeyError` with the nearest valid paths when the path is unknown;
    the caller turns that into the usage error the standard asks for.
    """
    if not path:
        return index(root)
    known = commands(root)
    name = " ".join(path)
    # A segment holding a space is one quoted word, not two segments: the
    # caller wrote `schema "agents create"` where the path is `schema agents
    # create`.  Accepting it would make two spellings of the same path, and
    # only one of them survives being built from a `name` split on spaces.
    command = None if any(" " in segment for segment in path) else known.get(name)
    if command is None:
        raise KeyError(name)
    return detail(name, command)
