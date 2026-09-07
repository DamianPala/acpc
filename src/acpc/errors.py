"""Structured failures: one envelope, one kind, one place it is written.

Every failure the CLI reports is an `AcpcError`.  It carries a stable `kind`
that a caller can branch on, the human message acpc has always printed, and
the optional recovery fields.  The top level renders it exactly once, on
stderr, as the last non-empty line — never on stdout, which belongs to the
command's own result.

This module deliberately knows nothing about `cli`: `sessions`, `registry` and
`runner` have to be able to raise a classified failure without importing the
command layer.
"""

import json
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal, TypeVar

import click

from acpc import vocab

# Kinds with a meaning shared across tools.  A caller that knows one of these
# knows what it means here, so acpc never reuses one for something else.
INVALID_INPUT = "invalid_input"
NOT_FOUND = "not_found"
CONFLICT = "conflict"
PERMISSION_DENIED = "permission_denied"
UNAUTHENTICATED = "unauthenticated"
TIMEOUT = "timeout"
UNAVAILABLE = "unavailable"
OUTCOME_UNKNOWN = "outcome_unknown"
INTERRUPTED = "interrupted"
CURSOR_UNAVAILABLE = "cursor_unavailable"
CONFIRMATION_REQUIRED = "confirmation_required"
OPERATION_FAILED = "operation_failed"
PRECONDITION_FAILED = "precondition_failed"

# Kinds acpc defines because no shared kind carries the distinction.
#
# `agent_error`: an external program acpc invoked reported its own failure —
# an `install_command` returning non-zero, an adapter answering with an error
# acpc has no better name for.  acpc reached it and it answered, so
# `unavailable` would be a lie, and nothing acpc manages is in conflict.  A
# turn that ends badly is not this: the operation ran to an end acpc did not
# ask for, which is `operation_failed`.
#
# `corrupt_state`: state acpc owns exists on disk and cannot be trusted —
# `meta.json` or an agent entry is unreadable or holds a value outside its
# vocabulary.  The target was found, the call was valid, and re-running
# changes nothing, so `not_found`, `invalid_input` and `unavailable` all
# misdescribe it.
#
# `not_supported`: the target exists and the call is well formed, but this
# operation is not offered for it — `install` on an entry that carries no
# trusted installer.  No flag overrides it and no wait changes it, which is
# what separates it from `precondition_failed` and `unavailable`.
AGENT_ERROR = "agent_error"
CORRUPT_STATE = "corrupt_state"
NOT_SUPPORTED = "not_supported"

KINDS = (
    INVALID_INPUT,
    NOT_FOUND,
    CONFLICT,
    PERMISSION_DENIED,
    UNAUTHENTICATED,
    TIMEOUT,
    UNAVAILABLE,
    OUTCOME_UNKNOWN,
    INTERRUPTED,
    CURSOR_UNAVAILABLE,
    CONFIRMATION_REQUIRED,
    OPERATION_FAILED,
    PRECONDITION_FAILED,
    AGENT_ERROR,
    CORRUPT_STATE,
    NOT_SUPPORTED,
)

# Who can act on the failure, when acpc knows.
Action = Literal["agent", "user", "none"]


class AcpcError(click.ClickException):
    """A failure with a machine-readable kind; the top level renders it.

    Subclasses fix `kind` and `exit_code`; a caller can still override either
    per raise when one site is more specific than the class it reuses.
    """

    exit_code = vocab.EXIT_AGENT_ERROR
    kind: str = OPERATION_FAILED

    def __init__(
        self,
        message: str,
        *,
        kind: str | None = None,
        retryable: bool | None = None,
        action: Action | None = None,
        hint: str | None = None,
        context: Mapping[str, Any] | None = None,
        exit_code: int | None = None,
    ) -> None:
        super().__init__(message)
        if kind is not None:
            self.kind = kind
        # Click declares `exit_code` as a class attribute, so a per-raise code
        # rides alongside it and `exit_status` is what the top level reads.
        self._exit_code = exit_code
        self.retryable = retryable
        self.action = action
        self.hint = hint
        self.context: dict[str, Any] | None = dict(context) if context is not None else None

    @property
    def exit_status(self) -> int:
        """The code the process leaves with: this raise's, or the class's."""
        return self.exit_code if self._exit_code is None else self._exit_code

    def format_message(self) -> str:
        return self.message

    def with_context(self, **values: Any) -> "AcpcError":
        """Add recovery values in place and return self, for `raise` sites.

        A command that learns the session id only after the failure was raised
        deeper down still has to report it (the id is how the caller reaches
        the work), so the envelope is completed on the way out.
        """
        merged = dict(self.context or {})
        merged.update(values)
        self.context = merged
        return self

    def document(self) -> dict[str, Any]:
        """The F3 document: exactly one top-level field, `error`.

        Unset optional fields are absent rather than null — a `null` claims
        acpc looked and found nothing, and it did not look.
        """
        error: dict[str, Any] = {"kind": self.kind, "message": self.message}
        if self.retryable is not None:
            error["retryable"] = self.retryable
        if self.action is not None:
            error["action"] = self.action
        if self.hint is not None:
            error["hint"] = self.hint
        if self.context:
            error["context"] = dict(self.context)
        return {"error": error}


class UsageProblem(AcpcError):
    """A usage error: one actionable line on stderr, exit 2."""

    exit_code = vocab.EXIT_USAGE
    kind = INVALID_INPUT


class AgentProblem(AcpcError):
    """An agent-side failure: one actionable line on stderr, exit 1."""

    exit_code = vocab.EXIT_AGENT_ERROR
    kind = AGENT_ERROR


def serialize(document: Mapping[str, Any]) -> str:
    """Render one JSON line, keeping non-ASCII characters as themselves."""
    return json.dumps(dict(document), ensure_ascii=False)


# Whether this invocation's stdout format is machine-readable.  Set from the
# raw arguments before Click parses anything, so a failure raised before the
# format was resolved still knows, then refined by the command's own `--json`.
_machine_format = False


def reset(args: Sequence[str]) -> None:
    """Start an invocation: read `--json` out of the raw arguments.

    The scan stops at a bare `--`: past it the word is a prompt, not a flag.
    Reading the arguments the entry point was handed rather than `sys.argv`
    keeps in-process callers (Click's test runner, `acpc` embedded in another
    process) reporting on their own call.
    """
    global _machine_format
    _machine_format = False
    for arg in args:
        if arg == "--":
            break
        if arg == "--json":
            _machine_format = True
            break


def note_machine_format(selected: bool) -> None:
    """Refine the scan with what the command actually parsed."""
    global _machine_format
    _machine_format = selected


def machine_format_selected() -> bool:
    return _machine_format


def stderr_is_tty() -> bool:
    """Whether a person is reading stderr; one seam, so tests can move it."""
    try:
        return sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False


def envelope_required() -> bool:
    """A machine reads this failure when stdout is JSON or stderr is piped."""
    return machine_format_selected() or not stderr_is_tty()


def emit(error: AcpcError) -> None:
    """Write the failure to stderr — the envelope, or the line for a person.

    Never both: the envelope already carries `message` and `hint`, and a
    duplicate line above it is noise in the stream a caller is parsing.
    """
    if envelope_required():
        click.echo(serialize(error.document()), err=True)
        return
    click.echo(f"Error: {error.format_message()}", err=True)
    if error.hint:
        click.echo(error.hint, err=True)


_FC = TypeVar("_FC", bound=Callable[..., Any])


def json_option(help_text: str) -> Callable[[_FC], _FC]:
    """Declare `--json` so parsing it also settles the failure format."""

    def note(_ctx: click.Context, _param: click.Parameter, value: bool) -> bool:
        note_machine_format(bool(value))
        return value

    return click.option("--json", "json_mode", is_flag=True, help=help_text, callback=note)
