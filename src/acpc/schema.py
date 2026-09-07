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
from typing import Any

import click

from acpc import __version__, effects, vocab

# Introspection format version.  It changes when a required field is added, a
# field is removed, or a field's type or meaning changes — not when an
# optional field appears.
SCHEMA_VERSION = "1"

# The first argument reserved for introspection; it is not a command entry.
COMMAND_NAME = "schema"

# The standard this surface is generated against.
STANDARD_NAME = "cli-design-standard"
STANDARD_VERSION = "0.1.0-draft.5"

# Default output format per context.  acpc renders text in both today; a
# command that has a machine shape offers it behind its own `--json`.
FORMAT_DEFAULTS: dict[str, str] = {"tty": "text", "non_tty": "json"}

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
            "in the machine envelope either way."
        ),
        "type": "boolean",
        "required": False,
        "default": False,
    },
    {
        "name": "format",
        "description": (
            "Select the command's human or machine representation; accepted values depend on "
            "the command, and --json is its machine alias."
        ),
        "type": "string",
        "required": False,
    },
    {
        "name": "color",
        "description": "Color policy for human output: auto, always or never.",
        "type": "string",
        "required": False,
        "default": "auto",
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


class SchemaError(Exception):
    """A command cannot be published: a parameter has no declared contract."""


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
    if isinstance(parameter, click.Option) and parameter.multiple:
        descriptor["repeatable"] = True
    if parameter.name in getattr(command, _STDIN, frozenset()):
        descriptor["accepts_stdin"] = True
    return descriptor


def _argument_source(command: click.Command) -> click.Command:
    """Where a command's positional arguments are actually parsed.

    `acpc agents <name>` and `acpc skills <name>` are parsed by a hidden
    command the group hands an unrecognized first word to.  The group and
    that command declare the same flags, so the group's entry borrows its
    arguments and they become optional: the group runs bare as well.
    """
    source = getattr(command, "view_command", None)
    return source if isinstance(source, click.Command) else command


def _args(command: click.Command) -> list[dict[str, Any]]:
    source = _argument_source(command)
    borrowed = source is not command
    return [
        _descriptor(parameter, source, required=False if borrowed else None)
        for parameter in source.params
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
            if command.invoke_without_command:
                yield " ".join(path), command
            yield from _walk(command, path)
        else:
            yield " ".join(path), command


def commands(root: click.Group) -> dict[str, click.Command]:
    """Every command entry by full path, in Unicode code point order."""
    return dict(sorted(_walk(root), key=lambda item: item[0]))


def detail(name: str, command: click.Command) -> dict[str, Any]:
    """The D8 document for one command.

    `output` is not published yet: the JSON success shapes have not been
    settled, and a schema that guessed them would be the drift this module
    exists to prevent.
    """
    document: dict[str, Any] = {
        "name": name,
        "description": _short_description(command),
        "args": _args(command),
        "flags": _flags(command),
        "effects": _effects_of(command),
        "confirm": _accepts_confirmation(command),
        "interactive": INTERACTIVE,
    }
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
            "extensions": [],
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
    # caller wrote `schema "agents init"` where the path is `schema agents
    # init`.  Accepting it would make two spellings of the same path, and
    # only one of them survives being built from a `name` split on spaces.
    command = None if any(" " in segment for segment in path) else known.get(name)
    if command is None:
        raise KeyError(name)
    return detail(name, command)
