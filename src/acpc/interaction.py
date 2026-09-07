"""What acpc may ask the caller, and the gate in front of a mutation.

Two rules live here, and they answer two different questions.

`interactive_context` answers *may acpc ask at all*: stdin is a terminal, the
selected stdout format is readable by a person, and `NO_INPUT` is empty or
unset.  Nothing may put a question to the caller outside it.

`ask_by_default` answers the narrower *should acpc ask without being told to*,
and adds a terminal on stdout to the above.  Redirecting the result to a file
is a caller saying it wants to keep the output, not a caller volunteering to
sit through a permission dialogue for every tool call, so the default drops to
a policy that never stops.  Asking in a strict subset of the interactive
context is always allowed: the rule governs when a tool *may* ask, never when
it must.

Each stream is classified on its own.  Redirecting one never reclassifies
another: stdin decides whether anyone can answer, stdout decides the format,
stderr decides decoration.  Both terminal checks are single functions on
purpose — a test replaces one and gets the whole tool's answer, rather than
patching whichever module it happened to reach.  This module deliberately
knows nothing about `cli`: the question "may acpc ask?" belongs below the
command layer, where the session store and the runner can also ask it.

Silence and an absent terminal are also two different answers.  A caller that
was asked and said nothing agreed to nothing, and every form of that arrives
as `None`.  A caller that could not be asked at all — no controlling terminal,
which is every Windows console, where `/dev/tty` does not exist — was not
consulted, and `TerminalUnavailable` says so instead of passing for silence.
"""

import os
import sys
from collections.abc import Sequence

from acpc import errors
from acpc.errors import AcpcError

# The caller says nothing may be asked of it, whatever the terminals suggest.
NO_INPUT = "NO_INPUT"


class TerminalUnavailable(Exception):
    """No terminal could be opened, so the question was never put.

    Distinct from an unanswered question on purpose: a caller that stayed
    silent refused, while a caller that was never reachable did nothing at
    all.  Each command decides for itself what an unasked question means —
    a confirmation stays unconfirmed, a policy falls back to its floor.
    """


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


def ask_by_default() -> bool:
    """Whether acpc should ask when the caller named no policy.

    Narrower than `interactive_context` by one stream: a person watching the
    result scroll past can answer a dialogue, a person collecting it in a file
    was not offering to.
    """
    return interactive_context() and stdout_is_tty()


def why_not_interactive() -> str | None:
    """Why acpc may not ask, as a clause; `None` when it may.

    Every reason that holds is named, not just the first.  Reporting one of
    several sends the caller round the loop for each: it withdraws `--json`,
    calls again and meets the missing terminal that was there all along.  The
    reasons it cannot take back come first, because those decide the answer
    however the rest are changed.
    """
    reasons = []
    if not stdin_is_tty():
        reasons.append("needs a terminal to ask on")
    if errors.machine_format_selected():
        reasons.append("cannot be used with --json, which forces a non-interactive context")
    if no_input():
        reasons.append("cannot be used while NO_INPUT is set, which forbids every prompt")
    if not reasons:
        return None
    if len(reasons) == 1:
        return reasons[0]
    return f"{', '.join(reasons[:-1])} and {reasons[-1]}"


def _ask_on_tty(question: str) -> str | None:
    """Put `question` on `/dev/tty` and read one line back; `None` for silence.

    Never on stdin, which may carry the prompt.  The terminal is opened twice,
    once per direction: a single "r+" handle raises `io.UnsupportedOperation:
    File or stream is not seekable`, which is an OSError and would be caught
    here as a silent denial.

    End of input is not an answer.  A closed terminal, a killed reader, a
    heredoc that ran out — none of them agreed to anything, and each arrives
    here as `None` so no caller can mistake it for a choice.

    A terminal that will not open at all is `TerminalUnavailable`, which is a
    different fact: there was nobody to ask, rather than somebody who declined
    to answer.
    """
    try:
        with (
            open("/dev/tty", "w", encoding="utf-8") as ask,
            open("/dev/tty", encoding="utf-8") as answer,
        ):
            ask.write(question)
            ask.flush()
            line = answer.readline()
    except OSError as error:
        raise TerminalUnavailable(str(error)) from error
    if line == "":
        return None
    return line.strip()


def ask_yes_no(question: str, *, default: bool) -> bool:
    """Ask a yes/no question; anything but a yes is a no.

    `default` only covers a bare Enter — silence is a no regardless of it.
    An unreachable terminal is a no for the same reason: a confirmation that
    was never given cannot be read as one, whatever stopped it being asked.
    """
    try:
        reply = _ask_on_tty(question)
    except TerminalUnavailable:
        return False
    if reply is None:
        return False
    if not reply:
        return default
    return reply.lower() in {"y", "yes"}


def ask_choice(question: str, *, choices: Sequence[str], default: str) -> str | None:
    """Ask for one of `choices`; `None` when nothing was chosen.

    Silence and an answer outside `choices` are both `None`: neither is a
    selection, and guessing which one a typo meant would hand out a policy
    nobody named.  A bare Enter takes `default`, which the question shows.

    `TerminalUnavailable` propagates: what an unasked question means depends
    on what was being chosen, and only the caller knows that.
    """
    reply = _ask_on_tty(question)
    if reply is None:
        return None
    if not reply:
        return default
    return reply if reply in choices else None


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
