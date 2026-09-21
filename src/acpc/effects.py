"""Effect classification: one value per command, declared at the command.

Every command says whether it changes state acpc manages, and whether a
repeat of a successful call would change it again.  The declaration sits on
the command object itself rather than in a table beside it, because a table
drifts: a new command is written, the table is not touched, and the tool
claims something false about it.

The value is read straight off the Click tree, without running anything, so
a generator that publishes the command surface can report it.  The
classification is conservative: a command whose repeat lacks the guarantee is
`non_idempotent` even when most of its calls would qualify.
"""

from collections.abc import Callable
from typing import TypeVar

import click

# The command does not change intended state acpc manages or targets.
READ_ONLY = "read_only"
# A repeat of a successful call, with the same inputs and no intervening
# change, succeeds without a second state transition.
IDEMPOTENT = "idempotent"
# Neither guarantee holds: a repeat may act again, or fail with a conflict.
NON_IDEMPOTENT = "non_idempotent"

VALUES = (READ_ONLY, IDEMPOTENT, NON_IDEMPOTENT)

# Written on the command object.  Private by name so nothing mistakes it for
# a Click attribute; `of` is the only reader.
_ATTRIBUTE = "_acpc_effects"

_C = TypeVar("_C", bound=click.Command)


def declare(value: str) -> Callable[[_C], _C]:
    """Classify the command this decorates; goes above its Click decorator.

    Decorators apply bottom up, so this one has to sit outermost to receive
    the `Command` that `@group.command(...)` built.  The group already holds
    that same object, so setting the attribute here is what the tree sees.
    """
    if value not in VALUES:
        raise ValueError(f"unknown effects value: {value!r}")

    def apply(command: _C) -> _C:
        setattr(command, _ATTRIBUTE, value)
        return command

    return apply


read_only = declare(READ_ONLY)
idempotent = declare(IDEMPOTENT)
non_idempotent = declare(NON_IDEMPOTENT)


def of(command: click.Command) -> str | None:
    """The value a command declared, or `None` when it declared none."""
    value = getattr(command, _ATTRIBUTE, None)
    return value if isinstance(value, str) else None
