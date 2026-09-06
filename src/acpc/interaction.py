"""What acpc may ask the caller, and the gate in front of a mutation.

One definition of "a person is at the other end": stdin is a terminal, the
selected stdout format is readable by a person, and `NO_INPUT` is empty or
unset.  Every gate reads it from here, so the answer cannot differ between
two commands.

Both terminal checks are single functions on purpose — a test replaces one
and gets the whole tool's answer, rather than patching whichever module it
happened to reach.  This module deliberately knows nothing about `cli`: the
question "may acpc ask?" belongs below the command layer, where the session
store and the runner can also ask it.
"""

import os
import sys

from acpc import errors
from acpc.errors import AcpcError

# The caller says nothing may be asked of it, whatever the terminals suggest.
NO_INPUT = "NO_INPUT"


def stdin_is_tty() -> bool:
    """Whether a person can answer; one seam, so tests can move it."""
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def stdout_is_tty() -> bool:
    """Whether a person is reading the result; one seam, as above."""
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def no_input() -> bool:
    """Whether `NO_INPUT` forbids asking."""
    return os.environ.get(NO_INPUT, "") != ""


def interactive_context() -> bool:
    """Whether acpc may put a question to whoever made this call.

    A machine-readable stdout settles it on its own: a caller parsing JSON
    has no way to answer, and a prompt it cannot see would hang it.
    """
    return stdin_is_tty() and not errors.machine_format_selected() and not no_input()


def ask_yes_no(question: str, *, default: bool) -> bool:
    """Ask on `/dev/tty` — never on stdin, which may carry the prompt.

    The terminal is opened twice, once per direction: a single "r+" handle
    raises `io.UnsupportedOperation: File or stream is not seekable`, which
    is an OSError and would be caught below as a silent denial.

    End of input is not an answer.  A closed terminal, a killed reader, a
    heredoc that ran out — none of them agreed to anything, so each is a no
    regardless of `default`, which only covers a bare Enter.
    """
    try:
        with (
            open("/dev/tty", "w", encoding="utf-8") as ask,
            open("/dev/tty", encoding="utf-8") as answer,
        ):
            ask.write(question)
            ask.flush()
            line = answer.readline()
    except OSError:
        return False
    if line == "":
        return False
    reply = line.strip().lower()
    if not reply:
        return default
    return reply in {"y", "yes"}


def require_confirmation(
    approved: bool,
    *,
    message: str,
    hint: str,
    prompt: str | None = None,
    default: bool = True,
) -> None:
    """Let a confirmed call through; stop an unconfirmed one before its effect.

    `--yes` confirms by itself.  A command that also offers a prompt supplies
    one, and it is put only in an interactive context; a declined prompt and a
    missing flag are the same failure, because in both the call was never
    agreed to.
    """
    if approved:
        return
    if prompt is not None and interactive_context() and ask_yes_no(prompt, default=default):
        return
    raise AcpcError(message, kind=errors.CONFIRMATION_REQUIRED, hint=hint)
