"""CLI entry point.

SPEC.md *Command surface*. Verbs land slice by slice; this module owns flag
parsing, usage errors (exit 2), the TTY rules and the fixed exit codes, and
delegates everything else to the layer that owns it.
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, NoReturn

import click

from acpc import (
    __version__,
    cache,
    config,
    daemon_client,
    effects,
    errors,
    interaction,
    output,
    paths,
    proc,
    render,
    runner,
    schema,
    sessions,
    skills,
    transcript,
    vocab,
)
from acpc import probe as probe_engine
from acpc.errors import AcpcError, AgentProblem, UsageProblem
from acpc.permissions import ModeSelectionError, PermissionLevel, select_mode
from acpc.registry import (
    AgentNotFound,
    AgentRegistry,
    CallResolution,
    CorruptEntry,
    FieldSource,
    InstallNotSupported,
    RegistryError,
    ResolvedEntry,
)

# How every command that addresses one session names it.  One sentence,
# published on each of them, because a caller reading one command's schema
# should not have to find the rule somewhere else.
_SELECTOR_HELP = (
    "Session id, the `--name` alias given at dispatch, or `last` for the most recent "
    "session; `last` resolves only in an interactive context — stdin a terminal, no "
    "--json, NO_INPUT unset — so a script must name the session."
)

# The prompt argument's contract, shared by the verbs that take one: one
# source only, `-` for stdin, and the size acpc refuses beyond.
_PROMPT_HELP = (
    "The prompt, or `-` to read it from stdin; --prompt-file is the third source and "
    f"exactly one of the three may be given. At most {vocab.MAX_PROMPT_BYTES} bytes "
    f"({vocab.MAX_PROMPT_LABEL}) of UTF-8, "
    "refused before anything is created."
)

_PROMPT_LIMIT_HELP = f"{vocab.MAX_PROMPT_BYTES} bytes ({vocab.MAX_PROMPT_LABEL})"

# V1a: `continue`'s own prompt contract — the only verb where all three
# sources can be left out, and only after an interrupted turn.
_CONTINUE_PROMPT_HELP = (
    "The prompt, or `-` to read it from stdin; --prompt-file is the third source, and at "
    "most one of the three may be given. Omitted along with --prompt-file, continue picks "
    "up an interrupted turn — canceled, failed or unknown — with acpc's own continuation "
    f"instruction; a succeeded turn requires a message. At most {vocab.MAX_PROMPT_BYTES} "
    f"bytes ({vocab.MAX_PROMPT_LABEL}) of UTF-8, refused before anything is created."
)

# Values `TimeoutParamType` accepts, stated wherever one is taken: the type
# name alone ("duration") does not tell a caller what to write.
_DURATION_SYNTAX = (
    "seconds, such as 90, or a value suffixed s, m, h, d or w, such as 90s, 5m, 1h30m"
)

# I8b: `--timeout`'s own descriptor carries no `default`, so its `description`
# says the wait is unbounded by default (`run`, `continue` and `steer` share
# the same wording; `wait` also accepts 0 and drops the --cancel-after note).
_TIMEOUT_HELP = (
    f"Stop waiting after this duration ({_DURATION_SYNTAX}) and exit 124 with no result; the "
    "session keeps running. Unbounded by default. Use --cancel-after to bound the work itself."
)
_WAIT_TIMEOUT_HELP = (
    f"Stop waiting after this duration ({_DURATION_SYNTAX}, or 0) and exit 124 with no result; "
    "the session keeps running. Unbounded by default."
)

_OUTPUT_FILE_HELP = (
    "Write exactly what stdout would receive to a file; stdout stays empty, on success and "
    "on a failure that returns a result. A call that returns no result creates no file. A "
    "relative path resolves against the directory acpc was invoked from, and the file is "
    "overwritten. The session's full answer.md lives in its session directory and follows "
    "its retention and prune policy (90 days by default)."
)

_STEER_OUTPUT_FILE_HELP = (
    "Write exactly what stdout would receive to a file; on success stdout stays empty, and "
    "a failed machine-format turn writes an empty file. A relative path resolves against the "
    "directory acpc was invoked from, and the file is overwritten. The session's full "
    "answer.md lives in its session directory and follows its retention and prune policy "
    "(90 days by default)."
)

# V6a: on a non-terminal stdout, `text` prints a tagged document keeping a
# subset of these fields under `<metadata>` rather than the human layout;
# see SPEC's Text presentation for the field list and layout.
_TEXT_PRESENTATION_NOTE = (
    "The `text` format has two presentations selected by the stdout stream: a tagged "
    "document off a terminal, keeping a subset of this schema's fields under "
    "`<metadata>`, and a human layout on one; `--json` returns the complete document."
)
_JSON_CHOICE_HELP = (
    "Print the complete result as one JSON document instead of the text presentation; a "
    "choice for programmatic parsing, not a requirement for reading an answer."
)

# What the answer schema cannot express: when a failure still answers, and
# which fields only one kind of call carries (D7 `output_description`, O4d/O5a).
# V1b: every role that carries the session-capability object names its
# identifier, answer and capability fields here, consistently.
_ANSWER_OUTPUT_LEAD = (
    "Returns the answer result for a turn this call observed the end of — including a "
    "failed or canceled turn — and returns no result for a call that observed no turn, "
    "including one whose --timeout deadline expired (`context.status` can be `waiting` "
    "when a usage limit was holding the turn) or whose watch ended in a detach. "
    "`session_id` names the session and `capabilities` the session-capability object; "
    "`stop_reason`, `tokens`, `cost` and `answer` are present on every foreground result "
    "and omitted by `--background`. "
)
_ANSWER_OUTPUT_DESCRIPTION = _ANSWER_OUTPUT_LEAD + _TEXT_PRESENTATION_NOTE
# V1a: `continue`'s own addition — what a call with no message at all does.
_CONTINUE_OUTPUT_DESCRIPTION = (
    _ANSWER_OUTPUT_LEAD
    + "A call with no message at all — neither `PROMPT`, `-` nor `--prompt-file` — "
    "continues an interrupted turn (`canceled`, `failed` or `unknown`) with acpc's own "
    "continuation instruction in place of a caller-supplied prompt; a `succeeded` turn "
    "has nothing to continue and the call fails with `invalid_input` before anything is "
    "created. " + _TEXT_PRESENTATION_NOTE
)
_WAIT_OUTPUT_DESCRIPTION = (
    "Selects the session's current turn when the call starts and keeps observing that "
    "turn even if the session rotates to a newer one meanwhile; returns the answer "
    "result once that turn has ended — including a failed or canceled turn — and "
    "returns no result when a --timeout deadline expires first, with `context.status` "
    "naming the turn's status at the deadline, `waiting` included. `session_id` names "
    "the session, `capabilities` the session-capability object and `answer` the answer "
    "text. "
) + _TEXT_PRESENTATION_NOTE
_STEER_OUTPUT_DESCRIPTION = (
    "Returns the answer result for the turn the correction landed on, or the acceptance "
    "receipt under `--background`; `session_id` names the session, and `capabilities` and "
    "`correction_result` are always present, and `stop_reason`, `tokens`, `cost` and "
    "`answer` join them on every foreground result. "
) + _TEXT_PRESENTATION_NOTE
_STATUS_OUTPUT_DESCRIPTION = (
    "Follows the selector: reports the session's current turn at the time of the call, "
    "so a session that rotated to a newer turn since is reported as that newer turn. "
    "`session_id` names the session and `capabilities` the session-capability object. "
    "`limit` is `null` unless a usage limit touched that turn; its `source` is one of "
    "`error_kind`, `rate_limit_info` or `text`. Whether a forwarded correction is still "
    "pending inside the adapter is not observable to acpc, so `pending_corrections` is "
    "always `null` rather than a count. `permissions` is the inspection of the policy "
    "applied to this session's work — `policy`, `mode`, `source` and `clamp`, the last "
    "`null` unless the inherited ceiling narrowed the requested policy."
)

# The same file, said in full for the schema: `--help` has no room for it.
_OUTPUT_FILE_DESCRIPTION = (
    f"{_OUTPUT_FILE_HELP.rstrip('.')}. A leading `~` is expanded, and a missing parent "
    "directory is created."
)
_STEER_OUTPUT_FILE_DESCRIPTION = (
    f"{_STEER_OUTPUT_FILE_HELP.rstrip('.')}. A leading `~` is expanded, and a missing parent "
    "directory is created."
)

_FORMAT_OUTPUT_HELP = (
    "Select text or JSON output; absent, text on a TTY and JSON on non-TTY. "
    "--json cannot be combined with a different --format value."
)
_FORMAT_COLLECTION_HELP = (
    "Select text, JSON, or one-item-per-line output; absent, text on a TTY and JSON on non-TTY. "
    "--json cannot be combined with a different --format value."
)
_FORMAT_NATIVE_HELP = (
    "Select text or JSON output; absent, text on both TTY and non-TTY. "
    "--json cannot be combined with a different --format value."
)
_FORMAT_STREAM_HELP = (
    "Select text or NDJSON output; absent, text on both TTY and non-TTY. "
    "NDJSON is mutually exclusive with --prose."
)

_PERMISSION_CHOICES = (*vocab.PERMISSION_VALUES, *vocab.PERMISSION_ALIASES)
_WARNED_PERMISSION_ALIASES: set[str] = set()


def _json_option(help_text: str) -> Any:
    """Declare JSON output and publish its format conflict."""
    return errors.json_option(
        f"{help_text} --json cannot be combined with a different --format value."
    )


_COLOR_POLICY: str | None = None


def _record_color_policy(
    ctx: click.Context, parameter: click.Parameter, value: str | None
) -> str | None:
    del parameter
    global _COLOR_POLICY
    if (
        value is not None
        and ctx.get_parameter_source("color") is click.core.ParameterSource.COMMANDLINE
    ):
        _COLOR_POLICY = value
    return value


def _color_option() -> Any:
    """Accept the standard color policy without contaminating machine output."""
    return click.option(
        "--color",
        type=click.Choice(("auto", "always", "never")),
        default="auto",
        expose_value=False,
        callback=_record_color_policy,
        help="Color policy for human output; NO_COLOR and TERM=dumb disable color.",
    )


def _resolution_options(function: Callable[..., Any]) -> Callable[..., Any]:
    """Add the flags that select an agent call's resolved configuration."""
    decorators = (
        click.option(
            "--cwd",
            metavar="DIR",
            help=(
                "Working directory of the callee; absent, the directory acpc was invoked from. "
                "A relative path resolves against that directory."
            ),
        ),
        click.option(
            "--model",
            metavar="M",
            help=(
                "Model tier (fast/standard/max) or a raw model ID; absent, the entry's "
                "configured model."
            ),
        ),
        click.option(
            "--effort",
            metavar="E",
            help="Reasoning effort level; absent, the entry's configured effort.",
        ),
        click.option(
            "--permissions",
            type=click.Choice(_PERMISSION_CHOICES),
            metavar="P",
            help=(
                "\b\n"
                "Permission scale: none, read, edit, execute, all or ask; absent, ask when acpc "
                "could put the question — stdin and stdout both terminals, no --json, NO_INPUT "
                "unset — and read in every other case, and --background asks which policy to detach "
                "with. ask itself needs a terminal on stdin and refuses under --json, --background "
                "or a set NO_INPUT. execute permits read, edit and execute; write and prompt are "
                "deprecated aliases for execute and ask."
            ),
        ),
        click.option(
            "--mode",
            metavar="M",
            help=(
                "Vendor mode override; normally unnecessary because --permissions selects the "
                "mode. Refused when it grants more than the policy; values from agents get <name>."
            ),
        ),
        click.option(
            "--home",
            metavar="DIR",
            help="Vendor home override; absent, the entry's configured home.",
        ),
    )
    for decorate in reversed(decorators):
        function = decorate(function)
    return function


def _stdout_is_tty() -> bool:
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def color_policy(*, stdout_tty: bool | None = None) -> str:
    """Resolve color policy in the standard's explicit-to-context order."""
    if _COLOR_POLICY is not None:
        return _COLOR_POLICY
    if os.environ.get("NO_COLOR"):
        return "never"
    if os.environ.get("TERM") == "dumb":
        return "never"
    is_tty = _stdout_is_tty() if stdout_tty is None else stdout_tty
    return "always" if is_tty else "never"


def _select_format(
    format_name: str | None,
    json_mode: bool,
    *,
    stream: bool = False,
    native_text: bool = False,
    plain: bool = False,
) -> str:
    """Resolve explicit format flags and the stdout-dependent default."""
    machine_name = "ndjson" if stream else "json"
    if json_mode and format_name not in (None, machine_name):
        raise UsageProblem("--json and --format select different output formats")
    if plain and (json_mode or format_name not in (None, "plain")):
        raise UsageProblem("--plain cannot be combined with another output format")
    # Explicit format wins over the stream-derived default; native text is a
    # command contract, not a hidden override for a caller's --format choice.
    selected = "plain" if plain else format_name
    if selected is None:
        selected = "text" if native_text or _stdout_is_tty() else machine_name
    if json_mode:
        selected = machine_name
    errors.note_machine_format(selected in {"json", "ndjson"})
    return selected


def _select_presentation(selected_format: str) -> str:
    """Split `text` into V6a's two stdout presentations: tagged off a terminal.

    `--output-file` and `--json` never reach this: a machine format has no
    presentation to choose, and a file destination gets whatever stdout would
    have received, which is exactly this same call.
    """
    if selected_format != "text":
        return selected_format
    return "human" if _stdout_is_tty() else "tagged"


def _write_rendered_file(path: str | None, result: output.OutputResult) -> bool:
    if path is None:
        return False
    output.write_output_file(path, result.text)
    return True


def _emit_turn_result(
    result: output.OutputResult,
    *,
    output_file: str | None,
    json_mode: bool,
    emit_failure_result: bool = True,
    success: bool = True,
) -> None:
    """Write the result of a call that observed the turn end.

    Every caller reaches here with a result in hand, so the document is
    written whatever the exit code will be: a failed or canceled turn still
    answers with the session's observed result (O5a). A client deadline never
    reaches this function at all (V5a); a call that observed no turn never
    gets this far either, and writes nothing.
    """
    if not emit_failure_result and not success:
        if output_file is not None:
            _write_rendered_file(output_file, output.OutputResult("", False, 0))
        elif not json_mode:
            _write_stdout(result.text)
        return
    if output_file is not None:
        _write_rendered_file(output_file, result)
        return
    _write_stdout(result.text)


def _timeout_failure(
    session_id: str, *, turn: int, status: str | None, retryable: bool
) -> AcpcError:
    """The V5a/M1e timeout failure every observing command raises alike.

    No result document ever accompanies it: the deadline stops this client's
    wait, not the work, so it neither cancels nor observes the turn's end,
    even when a partial answer was already recorded — `log --tail` reads that
    instead (V5a). `retryable` is `true` only for `wait`, whose repetition
    re-observes the same turn; `run`, `continue` and `steer` would start new
    work, so theirs is `false`.
    """
    context: dict[str, Any] = {"session_id": session_id, "turn": turn, "status": status}
    if retryable:
        context["retry_after_ms"] = int(runner.WAIT_POLL_INTERVAL * 1000)
    return AcpcError(
        f"session {session_id} timed out while turn {turn} was still {status}",
        kind=errors.TIMEOUT,
        exit_code=vocab.EXIT_TIMEOUT,
        retryable=retryable,
        hint=f"Run: acpc log {session_id} --tail 20, then acpc status {session_id}",
        next=["acpc", "status", session_id],
        context=context,
    )


def _emit_wait_timeout(session_id: str, output_file: str | None, *, turn: int) -> NoReturn:
    """Fail on the client deadline; the deadline itself never emits a result.

    `--output-file` gets no file at all: the call it was going to receive
    from never happened, and SPEC's "a call that returns no result creates
    no file" rule applies to that flag the same way it does to stdout (V5a).
    """
    del output_file
    try:
        observed = sessions.read_meta(session_id)
    except (sessions.SessionError, OSError):
        raise AcpcError(
            f"session {session_id} timed out and its state could not be observed",
            kind=errors.OUTCOME_UNKNOWN,
            retryable=False,
            context={"session_id": session_id, "status": None},
            exit_code=vocab.EXIT_AGENT_ERROR,
        ) from None
    # SPEC `continue`: a deadline that expires before the follow-up rotated in
    # has not observed the new turn; the record still describes the previous
    # one, whose terminal state is not this turn's status.
    status = "starting" if observed.turns < turn else observed.state
    raise _timeout_failure(session_id, turn=turn, status=status, retryable=False)


def _not_found(message: str, *, hint: str | None = None) -> AcpcError:
    """A named target does not exist: exit 1, because the call was well formed.

    Exit 2 means the caller wrote the command wrong.  `acpc status q7x2` for a
    session that was pruned is spelled correctly and asks a fair question, so
    the answer is a plain failure carrying `not_found` for the caller to match.
    """
    return AcpcError(message, kind=errors.NOT_FOUND, hint=hint)


def _registry_problem(error: RegistryError) -> AcpcError:
    """Classify a registry failure: a missing entry is not a bad flag."""
    if isinstance(error, AgentNotFound):
        return _not_found(str(error), hint="Run: acpc agents list")
    if isinstance(error, CorruptEntry):
        # A file acpc reads, not a flag the caller typed: no rewriting of the
        # call fixes it, and the message already names the file to repair.
        return AcpcError(str(error), kind=errors.CORRUPT_STATE, action="user")
    if isinstance(error, InstallNotSupported):
        # The entry exists and the call is well formed; acpc simply has no
        # trusted installer to run for it, and no flag turns one up.
        return AcpcError(str(error), kind=errors.NOT_SUPPORTED, action="user")
    return UsageProblem(str(error))


def _probe_problem(error: Exception) -> AcpcError:
    """An adapter acpc had to reach did not launch or did not answer."""
    return AgentProblem(str(error), kind=errors.UNAVAILABLE)


def _runner_problem(error: runner.RunnerError) -> AcpcError:
    """Classify a turn that never started, by what stopped it."""
    if isinstance(error, runner.AdapterUnavailable):
        return AgentProblem(str(error), kind=errors.UNAVAILABLE)
    if error.kind is not None:
        # The owning daemon already classified this refusal; keep its answer.
        return AgentProblem(
            str(error),
            kind=error.kind,
            retryable=True if error.kind == errors.CONFLICT else None,
        )
    return AgentProblem(str(error))


def _session_problem(error: sessions.SessionError) -> AcpcError:
    """Classify a session-store failure by what the caller has to do next.

    The store raises one exception per situation, so the mapping lives here
    rather than at fifty `raise` sites: not found, occupied, damaged, or an
    argument the store will never accept.
    """
    if isinstance(error, sessions.SessionNotFound):
        return _not_found(str(error))
    if isinstance(error, sessions.CorruptSessionError):
        return AcpcError(str(error), kind=errors.CORRUPT_STATE)
    if isinstance(error, sessions.SessionIdsExhausted):
        # acpc's own id space is full.  Nothing about the call is wrong and no
        # repeat or cleanup frees an id because reservations are permanent.
        return AcpcError(
            str(error),
            kind=errors.UNAVAILABLE,
            action="user",
        )
    if isinstance(error, sessions.SessionNameTaken | sessions.SessionBusy):
        # The session is busy or bound; the same call works once it settles.
        return AcpcError(str(error), kind=errors.CONFLICT, retryable=True)
    if isinstance(error, sessions.SessionStateError):
        # A state conflict that will not clear on its own — an invariant the
        # session cannot satisfy.  `retryable` stays absent rather than false:
        # acpc is not asserting anything either way about a repeat.
        return AcpcError(str(error), kind=errors.CONFLICT)
    return UsageProblem(str(error))


def _follow_up_problem(error: Exception, session_id: str) -> AcpcError:
    """Classify a failure raised while preparing a follow-up turn.

    Preparation reads the session's own record back and hands it to the
    runner, so a runner complaint here is about stored state rather than
    about the call — which is what separates this from `_runner_problem`.
    Everything reported from here names the session, because the caller has
    to be able to reach the work either way.
    """
    if isinstance(error, AcpcError):
        problem = error
    elif isinstance(error, transcript.TranscriptError):
        problem = AcpcError(
            str(error),
            kind=errors.CORRUPT_STATE,
            hint=f"Run: acpc delete {session_id} --yes to remove the incompatible session state.",
        )
    elif isinstance(error, sessions.SessionError):
        problem = _session_problem(error)
    elif isinstance(error, runner.RunnerError):
        # Every value these calls validate was read back from `meta.json`.
        problem = AcpcError(str(error), kind=errors.CORRUPT_STATE)
    else:
        problem = AgentProblem(str(error), kind=errors.UNAVAILABLE)
    return problem.with_context(session_id=session_id)


class TimeoutParamType(click.ParamType):
    """Parse CLI timeout values as seconds or config-style durations."""

    name = "duration"
    _ERROR = "use seconds (90) or a suffixed value (90s, 5m, 1h, 1h30m)"

    def __init__(self, *, allow_zero: bool = False) -> None:
        self.allow_zero = allow_zero

    def convert(
        self,
        value: Any,
        param: click.Parameter | None,
        ctx: click.Context | None,
    ) -> float:
        text = str(value)
        if text.count(".") == 1 and text.replace(".", "", 1).isdecimal():
            seconds = float(text)
            if seconds == 0 and not self.allow_zero:
                self.fail(f"{text!r} is not a duration — {self._ERROR}", param, ctx)
            return seconds
        candidate = f"{text}s" if text.isdecimal() else text
        try:
            return config.parse_duration(candidate, allow_zero=self.allow_zero)
        except ValueError:
            self.fail(f"{text!r} is not a duration — {self._ERROR}", param, ctx)


_ROOT_HELP = """acpc — dispatch coding agents over ACP.

Usage:
  acpc COMMAND [ARGS] [OPTIONS]
  acpc --help
Root arguments: none.

Global flags:
__GLOBAL_FLAGS__

Short task: acpc run <agent> "Explain this code" --permissions execute
Long or uncertain task: acpc run <agent> "Run the tests" --background --json; acpc wait <id> --quiet block until done, prints the answer.
Checking on a run: acpc log <id> --tail 10 --follow --timeout 60
Steering a running session: acpc steer <id> "Stop editing; diagnose only"   in place when the adapter supports it; --steer-mode cancel-then-start to interrupt
Context care: log is condensed by default; use --prose for the full answer.
Maintenance and setup: delete, prune and bare daemon stop explain their gates; --dry-run previews.
  Truncated or huge answer? Read <dir>/answer.md selectively — always complete.
  SIGINT cancels the turn owned by this command. SIGTERM detaches work already taken over by the daemon.
  acpc continue <id> "Now fix what you found"
  acpc run <agent> - --permissions execute <<'PROMPT'
  Review the implementation and make the required edits.
  PROMPT

Command groups:
__COMMAND_GROUPS__

Commands:
__COMMANDS__
  Use `acpc <command> --help` for a command's full reference.

Machine-readable interface:
  acpc schema           the whole command surface as JSON
  acpc schema run       one command's arguments, flags, effects and gates
  --json on a command emits that command's own result as JSON.

Flag → ACP
  --mode         → session/set_mode
  --permissions  → session/set_mode + request_permission
                   none · read · edit · execute · all · ask
                   execute permits read, edit and execute
                   write and prompt are deprecated aliases for execute and ask
  --model        → session/new (model)
  --effort       → session/new (effort)"""

_ROOT_COMMAND_LABELS = {
    "status": "status <id>",
    "cancel": "cancel <id>",
    "delete": "delete <id> --yes",
    "prune": "prune --yes",
    "install": "install <agent>",
}

_ROOT_COMMAND_DESCRIPTIONS = {
    "status": "liveness-verified metadata for one session",
    "list": "running + the 20 most recent sessions (--limit N to change)",
    "cancel": "cancel a running session; it stays resumable with continue",
}


def _root_description(command: click.Command) -> str:
    text = command.short_help or command.help or ""
    return " ".join(text.split("\n\n", 1)[0].replace("\b", " ").split())


def _root_help_row(label: str, description: str) -> list[str]:
    wrapped = textwrap.wrap(description, width=78) or [""]
    rows = [f"  {label:<17} {wrapped[0]}"]
    rows.extend(f"  {'':17} {line}" for line in wrapped[1:])
    return rows


def _root_help(group: click.Group) -> str:
    index = schema.index(group)
    flag_rows: list[str] = []
    for flag in index["global_flags"]:
        flag_name = flag["name"]
        flag_description = flag["description"]
        if not isinstance(flag_name, str) or not isinstance(flag_description, str):
            raise TypeError("schema global flags must have string names and descriptions")
        default = json.dumps(flag["default"], ensure_ascii=False)
        description = f"{flag_description} [default: {default}]"
        flag_rows.extend(_root_help_row(f"--{flag_name}", description))

    group_rows: list[str] = []
    for name, command in sorted(group.commands.items()):
        if isinstance(command, click.Group):
            group_rows.extend(_root_help_row(name, _root_description(command)))

    descriptions = {entry["name"]: entry["description"] for entry in index["commands"]}
    command_rows: list[str] = []
    for name, description in descriptions.items():
        if not isinstance(name, str) or not isinstance(description, str):
            raise TypeError("schema commands must have string names and descriptions")
        label = _ROOT_COMMAND_LABELS.get(name, name)
        command_description = _ROOT_COMMAND_DESCRIPTIONS.get(name) or description
        command_rows.extend(_root_help_row(label, command_description))

    return (
        _ROOT_HELP.replace("__GLOBAL_FLAGS__", "\n".join(flag_rows))
        .replace("__COMMAND_GROUPS__", "\n".join(group_rows))
        .replace("__COMMANDS__", "\n".join(command_rows))
        + "\n"
    )


class _CheatSheetGroup(click.Group):
    """Use the compact first-contact page for the root command."""

    def get_help(self, ctx: click.Context) -> str:
        return _root_help(self)

    def main(self, *args: Any, **kwargs: Any) -> Any:
        """Report every failure once, in one place, in one shape.

        Click parses before any command runs, so a bad flag fails here rather
        than inside a command that could have known the output format.  The
        raw arguments are read first for exactly that reason: a caller that
        asked for JSON gets the failure as JSON even when nothing else ran.
        """
        # Unconditional: `_machine_format` is module state, so an in-process
        # caller that skips standalone mode would otherwise report this
        # invocation in the format the previous one selected.
        errors.reset(_invocation_args(args, kwargs))
        if not kwargs.get("standalone_mode", True):
            return super().main(*args, **kwargs)
        kwargs["standalone_mode"] = False
        try:
            return super().main(*args, **kwargs)
        except click.UsageError as error:
            command_path = error.ctx.command_path if error.ctx is not None else None
            _fail(_friendly_usage_problem(error.format_message(), command_path, error.exit_code))
        except AcpcError as error:
            _fail(error)
        except click.ClickException as error:
            error_context = getattr(error, "ctx", None)
            command_path = getattr(error_context, "command_path", None)
            _fail(_friendly_usage_problem(error.format_message(), command_path, error.exit_code))
        except (click.Abort, KeyboardInterrupt):
            # Click turns Ctrl-C into Abort; a stack trace here would say the
            # tool broke, when the caller simply stopped it.
            _fail(_interrupted())
        except BrokenPipeError:
            # The reader is gone, so there is no one to hand an envelope to;
            # SPEC's 141 is the whole answer.
            _leave_on_broken_pipe()
        except Exception as error:  # noqa: BLE001
            # Last resort.  Anything that reaches here is a failure no command
            # classified, and the caller still gets one object rather than a
            # stack trace: `SystemExit` is not an `Exception`, so a command
            # that already chose its exit code passes straight through.
            _fail(_unclassified_problem(error))


def _unclassified_problem(error: Exception) -> AcpcError:
    """The envelope for a failure that reached the top level unnamed.

    The message names the exception's type and nothing else.  acpc has no
    story about what went wrong here, and an exception's own text carries
    paths, arguments and internals that the rest of the tool never prints.

    A refusal from the filesystem is the one thing still worth naming: it is
    somebody's to fix rather than a defect, and `permission_denied` with
    `action: user` is what says so.
    """
    if isinstance(error, PermissionError):
        return AcpcError(
            f"acpc was refused access it needed ({type(error).__name__})",
            kind=errors.PERMISSION_DENIED,
            action="user",
            hint="Check the permissions on the acpc state root (ACPC_HOME).",
        )
    return AcpcError(
        f"acpc failed unexpectedly ({type(error).__name__})",
        kind=errors.OPERATION_FAILED,
        action="none",
    )


def _invocation_args(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Sequence[str]:
    """The argument list Click is about to parse, however it was handed over.

    The console script passes none and lets Click read `sys.argv`; in-process
    callers pass their own list, and reading theirs is what keeps a test or an
    embedded call from reporting on the arguments of the process around it.
    """
    given = kwargs.get("args", args[0] if args else None)
    if given is None:
        return sys.argv[1:]
    return [str(item) for item in given]


def _interrupted(**context: Any) -> AcpcError:
    """The failure a Ctrl-C produces, wherever it lands."""
    return AcpcError(
        "interrupted",
        kind=errors.INTERRUPTED,
        exit_code=vocab.EXIT_CANCELLED,
        context=context or None,
    )


def _wait_failure(
    error: BaseException, *, session_id: str, observed_status: str | None
) -> AcpcError:
    """Add M1b's recovery context to every failure after ``wait`` parsed."""
    if isinstance(error, (click.Abort, KeyboardInterrupt)):
        problem = _interrupted()
    elif isinstance(error, AcpcError):
        problem = error
    elif isinstance(error, sessions.SessionError):
        problem = _session_problem(error)
    elif isinstance(error, runner.RunnerError):
        problem = _runner_problem(error)
    elif isinstance(error, Exception):
        problem = _unclassified_problem(error)
    else:
        problem = AcpcError(f"acpc failed unexpectedly ({type(error).__name__})", action="none")
    return problem.with_context(session_id=session_id, status=observed_status)


def _fail(error: AcpcError) -> NoReturn:
    """Write the failure to stderr and leave with its code."""
    errors.emit(error)
    raise SystemExit(error.exit_status) from None


def _no_such_option(message: str, spelling: str) -> bool:
    return any(
        marker in message
        for marker in (
            f"No such option: {spelling}",
            f"No such option '{spelling}'",
            f'No such option "{spelling}"',
        )
    )


def _no_such_command(message: str, spelling: str) -> bool:
    return f"No such command '{spelling}'" in message or f'No such command "{spelling}"' in message


def _matching_option_hint(message: str, hints: Mapping[str, str]) -> str | None:
    if "No such option" not in message:
        return None
    for spelling, replacement in hints.items():
        if _no_such_option(message, spelling):
            return replacement
    return None


def _status_option_hint(message: str) -> str | None:
    hint = _matching_option_hint(
        message,
        {
            "--limit": "--limit belongs to: acpc list --limit N",
            "--plain": "--plain belongs to: acpc list --plain --limit N",
        },
    )
    if hint is not None:
        return hint
    if "Invalid value for '--format'" in message and "plain" in message:
        return "--format plain belongs to: acpc list --format plain --limit N"
    return None


def _continue_option_hint(message: str) -> str | None:
    hints = {
        flag: f"{flag} is a run-only flag — use acpc run; continue reuses stored settings"
        for flag in (
            "--model",
            "--effort",
            "--mode",
            "--cwd",
            "--home",
            "--name",
        )
    }
    hints["--resolve"] = "--resolve moved to: acpc resolve <agent>"
    hints["--dry-run"] = "--dry-run was removed from continue — use acpc resolve <agent>"
    return _matching_option_hint(message, hints)


def _friendly_option_hint(message: str, command_parts: list[str]) -> str | None:
    follow_hint = "--follow is a log flag; use: acpc log <id> --follow [--timeout S]"
    short_follow_hint = "-f is not accepted; use --follow: acpc log <id> --follow [--timeout S]"
    detach_hint = (
        "--detach is not an acpc flag — background dispatch is: "
        'acpc run <agent> "<prompt>" --background'
    )
    if command_parts[-1:] == ["agents"]:
        hint = _matching_option_hint(
            message,
            {
                "--json": "--json belongs to: acpc agents list --json",
                "--check": "--check moved to: acpc agents check [<name>]",
                "--models": "--models belongs to: acpc agents get <name> --models",
                "--commands": "--commands belongs to: acpc agents get <name> --commands",
                "--limit": "--limit belongs to: acpc agents list --limit N",
                "--plain": "--plain belongs to: acpc agents list --plain --limit N",
            },
        )
        if hint is not None:
            return hint
    if command_parts[-1:] == ["skills"]:
        hint = _matching_option_hint(
            message, {"--json": "--json belongs to: acpc skills list --json"}
        )
        if hint is not None:
            return hint
    if command_parts[-1:] == ["status"]:
        hint = _status_option_hint(message)
        if hint is not None:
            return hint
    if command_parts[-1:] == ["continue"]:
        hint = _continue_option_hint(message)
        if hint is not None:
            return hint
    if command_parts[-1:] == ["run"]:
        if _no_such_option(message, "--dry-run"):
            return "--dry-run was removed from run — use acpc resolve <agent>"
        if _no_such_option(message, "--resolve"):
            return "--resolve moved to: acpc resolve <agent>"
    aliases = {
        "--follow": follow_hint,
        "-f": short_follow_hint,
        "--detach": detach_hint,
        "-d": detach_hint,
        "-C": "-C is not an acpc flag — the working-directory flag is --cwd DIR",
        "--tail": "--tail belongs to: acpc log <id> --tail N",
        "-o": "-o was renamed to --output-file — use --output-file FILE",
        "--output": "--output was renamed to --output-file — use --output-file FILE",
    }
    return _matching_option_hint(message, aliases)


def _daemon_command_hint(message: str, command_parts: list[str]) -> str | None:
    daemon_group = command_parts[-1:] == ["daemon"]
    daemon_stop = command_parts[-2:] == ["daemon", "stop"]
    if daemon_stop and _no_such_option(message, "--all"):
        return "--all is not a daemon flag — bare acpc daemon stop already addresses every daemon"
    if daemon_group and "No such command" in message:
        for spelling in ("list", "ls", "ps"):
            if _no_such_command(message, spelling):
                return f"no such command '{spelling}' — the daemon view is: acpc daemon status"
        for spelling in ("start", "restart"):
            if _no_such_command(message, spelling):
                return (
                    f"no such command '{spelling}' — daemons start on first use; acpc daemon "
                    "stop <agent> and the next run is the restart"
                )
    return None


def _group_command_hint(message: str, command_parts: list[str]) -> str | None:
    if command_parts[-1:] == ["agents"] and "No such command" in message:
        if any(_no_such_command(message, spelling) for spelling in ("init", "new")):
            return "agents create is the variant-creation command"
        return "agents needs a subcommand; use `acpc agents list` or `acpc agents get <name>`"
    if command_parts[-1:] == ["skills"] and "No such command" in message:
        return "skills needs a subcommand; use `acpc skills list` or `acpc skills get <name>`"
    return None


def _legacy_command_hint(message: str, command_parts: list[str]) -> str | None:
    if len(command_parts) == 1 and "No such command" in message:
        old_commands = {
            "rm": "delete",
            "stop": "cancel",
        }
        for old, new in old_commands.items():
            if _no_such_command(message, old):
                return f"no such command '{old}' — use acpc {new}"
    if "No such command" in message and (_no_such_command(message, "logs")):
        return "no such command 'logs' — the viewing command is: acpc log <id>"
    return None


def _friendly_command_hint(message: str, command_parts: list[str]) -> str | None:
    for hint_function in (_daemon_command_hint, _group_command_hint, _legacy_command_hint):
        hint = hint_function(message, command_parts)
        if hint is not None:
            return hint
    return None


def _friendly_usage_message(message: str, *, command_path: str | None = None) -> str:
    """Replace known neighboring-tool spellings with their acpc equivalents."""
    command_parts = (command_path or "").split()
    if command_parts[-1:] == ["status"] and (
        "Missing argument" in message or "Missing parameter" in message
    ):
        return "status requires a session id"
    return (
        _friendly_option_hint(message, command_parts)
        or _friendly_command_hint(message, command_parts)
        or message
    )


_MISSING_ARGUMENT_RECOVERY = {
    "agents create": (
        "agents create requires NAME and --extends",
        "Run: acpc agents create NAME --extends AGENT",
    ),
    "agents get": ("agents get requires NAME", "Run: acpc agents get NAME"),
    "agents delete": (
        "agents delete requires NAME",
        "Run: acpc agents list, then repeat with NAME",
    ),
    "cancel": ("cancel requires a session selector", "Run: acpc cancel SESSION_ID"),
    "continue": ("continue requires a session selector", "Run: acpc continue SESSION_ID PROMPT"),
    "delete": ("delete requires a session selector", "Run: acpc delete SESSION_ID --yes"),
    "install": ("install requires AGENT", "Run: acpc agents list, then repeat with AGENT"),
    "log": ("log requires a session selector", "Run: acpc log SESSION_ID"),
    "probe": ("probe requires ENTRY", "Run: acpc probe ENTRY --discover"),
    "resolve": ("resolve requires AGENT", "Run: acpc resolve AGENT"),
    "run": ("run requires AGENT", "Run: acpc run AGENT PROMPT"),
    "status": ("status requires a session id", "Run: acpc list to choose a session id"),
    "steer": ("steer requires a session selector", "Run: acpc steer SESSION_ID INSTRUCTION"),
    "wait": ("wait requires a session selector", "Run: acpc wait SESSION_ID"),
    "skills get": ("skills get requires NAME", "Run: acpc skills list, then repeat with NAME"),
}


def _friendly_usage_problem(message: str, command_path: str | None, exit_code: int) -> UsageProblem:
    command_parts = (command_path or "").split()
    command_name = " ".join(command_parts[-2:])
    recovery = _MISSING_ARGUMENT_RECOVERY.get(command_name)
    if recovery is None:
        recovery = _MISSING_ARGUMENT_RECOVERY.get(command_parts[-1])
    if recovery is not None and (
        "Missing argument" in message
        or "Missing parameter" in message
        or "Missing option" in message
    ):
        friendly_message, hint = recovery
        return UsageProblem(friendly_message, hint=hint, exit_code=exit_code)
    return UsageProblem(
        _friendly_usage_message(message, command_path=command_path),
        exit_code=exit_code,
    )


@effects.read_only
@click.group(
    cls=_CheatSheetGroup,
    invoke_without_command=True,
    context_settings={"show_default": True},
)
@click.version_option(__version__, "-V", "--version", message="%(version)s")
@click.help_option("-h", "--help")
@click.pass_context
def main(ctx: click.Context) -> None:
    """acpc — dispatch coding agents over ACP."""
    # Fresh per invocation: in-process callers (tests) would otherwise inherit
    # the previous command's unterminated-stdout state.
    global _stdout_line_open, _COLOR_POLICY
    _stdout_line_open = False
    _COLOR_POLICY = None
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@effects.read_only
@schema.describes(
    path="Command path to describe, one word per segment; empty for the index.",
)
@main.command(name="schema")
@click.argument("path", nargs=-1)
@click.help_option("-h", "--help")
def schema_command(path: tuple[str, ...]) -> None:
    """Print the command surface as JSON: the index, or one command's detail.

    Bare, it lists every command with its description and effects. Followed by
    a command path it prints that command's arguments, flags, effects and
    confirmation gate. Path segments are separate words: ``acpc schema agents
    init``.

    It reads no configuration, contacts nothing and starts nothing, so it
    answers the same way on a machine acpc has never run on.

    Example: ``acpc schema run``
    """
    try:
        document = schema.document(main, path)
    except KeyError:
        known = schema.commands(main)
        joined = " ".join(path)
        if joined in known:
            # The path exists; it was quoted into one word. The shell strips
            # the quotes before acpc sees them, so the caller is shown the
            # difference the message is about: one argument, not two.
            words = len(joined.split(" "))
            raise UsageProblem(
                f"unknown schema path {joined!r} — that is one argument containing a space; "
                f"pass {words} arguments: acpc schema {joined}"
            ) from None
        listed = ", ".join(repr(name) for name in schema.nearest(known, path))
        raise UsageProblem(f"unknown schema path {joined!r} — nearest: {listed}") from None
    except schema.SchemaError as error:
        # A command was added without the contract the generator needs; the
        # exception text is the only thing that says which one and what is
        # missing, so it is the message.
        raise AcpcError(str(error), kind=errors.OPERATION_FAILED) from None
    _write_stdout(json.dumps(document, ensure_ascii=False, indent=2) + "\n")


def _oversized_prompt(source: str, size: int, *, exact: bool = True) -> UsageProblem:
    """The one failure every prompt source raises when it is too big.

    A stream is read one character past the limit and no further, so what it
    yields is a floor on the real size rather than a measurement of it.  The
    message says which of the two the number is, because a caller that trims
    to fit would otherwise trim against a size acpc never took.
    """
    measured = f"{size} bytes" if exact else f"at least {size} bytes"
    return UsageProblem(
        f"prompt from {source} is {measured}; the limit is "
        f"{vocab.MAX_PROMPT_BYTES} bytes ({vocab.MAX_PROMPT_LABEL})"
    )


def _checked_prompt(text: str, source: str, *, exact: bool = True) -> str:
    """Return the prompt, or refuse it before anything has been created."""
    size = len(text.encode("utf-8"))
    if size > vocab.MAX_PROMPT_BYTES:
        raise _oversized_prompt(source, size, exact=exact)
    return text


def _read_stdin_prompt() -> str:
    """Read the prompt from stdin without buffering more than the limit.

    A character is at least one UTF-8 byte, so a read capped one character
    past the limit either returns everything there was or returns something
    that is already over — either way the refusal is exact and the process
    never holds an unbounded stream.
    """
    return _checked_prompt(sys.stdin.read(vocab.MAX_PROMPT_BYTES + 1), "-", exact=False)


def _prompt_file_problem(prompt_file: str, error: OSError) -> AcpcError:
    """Classify a `--prompt-file` that could not be read.

    A path that is not there is the caller's own argument, so it stays a usage
    error.  A path that is there and refused is the filesystem's answer, and
    no rewriting of the call changes it.
    """
    message = f"--prompt-file {prompt_file}: {error.strerror}"
    if isinstance(error, PermissionError):
        return AcpcError(message, kind=errors.PERMISSION_DENIED, action="user")
    return UsageProblem(message)


def _read_prompt_file(prompt_file: str) -> str:
    """Read the prompt file without buffering more than the limit.

    `stat` stays as the cheap refusal that never opens the file, but it can
    only be trusted when it says "too big": a FIFO, a character device and a
    `/proc` entry all report zero bytes and would hand an unbounded stream to
    an unbounded read.  So the read is capped exactly the way stdin's is, and
    `--prompt-file <(...)` costs no more memory than the limit allows.
    """
    path = Path(prompt_file).expanduser()
    try:
        size = path.stat().st_size
    except OSError as error:
        raise _prompt_file_problem(prompt_file, error) from None
    if size > vocab.MAX_PROMPT_BYTES:
        raise _oversized_prompt("--prompt-file", size)
    try:
        with path.open(encoding="utf-8") as stream:
            text = stream.read(vocab.MAX_PROMPT_BYTES + 1)
    except OSError as error:
        raise _prompt_file_problem(prompt_file, error) from None
    return _checked_prompt(text, "--prompt-file", exact=False)


def _read_prompt(
    prompt_text: str | None,
    prompt_file: str | None,
    *,
    operation: str,
    hint: str,
) -> str:
    """Resolve the single prompt source, or fail naming the options.

    The size limit is enforced here, which is before any caller has created a
    session directory or started a daemon: an oversized prompt leaves nothing
    behind.
    """
    sources = [
        name
        for name, present in (
            ("a prompt argument", prompt_text is not None and prompt_text != "-"),
            ("-", prompt_text == "-"),
            ("--prompt-file", prompt_file is not None),
        )
        if present
    ]
    if len(sources) != 1:
        raise UsageProblem(
            f"{operation}: give exactly one prompt source: a prompt argument, - for stdin, "
            f"or --prompt-file (got {len(sources)})",
            hint=hint,
        )
    if prompt_text == "-":
        return _read_stdin_prompt()
    if prompt_file is not None:
        return _read_prompt_file(prompt_file)
    return _checked_prompt(prompt_text or "", "the prompt argument")


def _normalize_permission(value: str | None) -> str | None:
    """Normalize a CLI permission and emit one note per deprecated alias."""
    canonical = vocab.normalize_permission(value)
    if value in vocab.PERMISSION_ALIASES and value not in _WARNED_PERMISSION_ALIASES:
        _WARNED_PERMISSION_ALIASES.add(value)
        click.echo(
            f"--permissions {value} is deprecated; use --permissions {canonical}",
            err=True,
        )
    return canonical


def _warn_permission_alias(alias: str | None) -> None:
    """Warn when an agent entry supplies a deprecated permission alias."""
    _normalize_permission(alias)


def _warn_unlisted_effort_model(resolution: CallResolution) -> None:
    """Warn when the resolved model has no ``[effort_by_model]`` row."""
    model = resolution.model
    entry = resolution.entry
    if model is None or not entry.effort_by_model or model in entry.effort_by_model:
        return
    union = entry.derived_effort_union()
    if union:
        click.echo(
            f"-- effort: {model} has no [effort_by_model] row; "
            f"using adapter efforts {', '.join(union)}",
            err=True,
        )
        return
    click.echo(
        f"-- effort: {model} has no [effort_by_model] row; using the global effort scale",
        err=True,
    )


def _stored_permission_policy(meta: sessions.SessionMeta) -> str:
    """Read and normalize the permission policy from a validated session."""
    resolved = meta.resolution.get("resolved")
    if not isinstance(resolved, dict):
        raise AcpcError(
            f"session {meta.session_id} has no stored permission resolution",
            kind=errors.CORRUPT_STATE,
            context={"session_id": meta.session_id},
        )
    permissions = resolved.get("permissions")
    if not isinstance(permissions, dict):
        return "read"
    return vocab.normalize_permission(permissions.get("value")) or "read"


def _stored_mode_item(meta: sessions.SessionMeta) -> Mapping[str, Any] | None:
    """Return the persisted mode object, if this session has one."""
    resolved = meta.resolution.get("resolved")
    if not isinstance(resolved, Mapping):
        return None
    mode = resolved.get("mode")
    return mode if isinstance(mode, Mapping) else None


def _continue_selection(
    meta: sessions.SessionMeta,
    policy: str,
) -> CallResolution:
    """Select against the current registry for an explicit or legacy continuation."""
    try:
        stored = runner.resolution_from_session(meta)
        entry = AgentRegistry().resolve(meta.entry)
        stored_mode = _stored_mode_item(meta)
        source = stored_mode.get("source") if stored_mode is not None else None
        explicit_mode = stored.mode if stored.mode is not None and source != "selected" else None
        # Live [effort_by_model] must not reject a stored model/effort pair.
        # Mode/permissions re-read the current table; effort stays as persisted.
        resolution = replace(entry, effort_by_model={}).resolve_call(
            model=stored.model,
            effort=stored.effort,
            mode=explicit_mode,
            permissions=policy,
            home=stored.home,
        )
        if explicit_mode is None:
            provenance = dict(resolution.provenance)
            provenance["mode"] = FieldSource("unset")
            resolution = replace(resolution, mode=None, provenance=provenance)
        return _select_resolution(resolution)
    except RegistryError as error:
        raise _registry_problem(error) from None


def _updated_session_resolution(
    meta: sessions.SessionMeta,
    resolution: CallResolution,
    *,
    policy: str,
    policy_changed: bool,
) -> dict[str, Any]:
    """Replace only the resolved policy and mode facts in a session payload."""
    payload = deepcopy(meta.resolution)
    resolved = payload.get("resolved")
    if not isinstance(resolved, dict):
        raise AcpcError(
            f"session {meta.session_id} has no stored resolution object",
            kind=errors.CORRUPT_STATE,
            context={"session_id": meta.session_id},
        )
    current_mode = runner.resolution_payload(resolution, cwd=None)["resolved"]["mode"]
    resolved["mode"] = current_mode
    permission_source = "call flag" if policy_changed else "stored"
    permission_payload: dict[str, Any] = {"value": policy, "source": permission_source}
    if resolution.permissions_clamp is not None:
        requested, ceiling = resolution.permissions_clamp
        permission_payload["source"] = (
            f"{permission_source} (clamped from {requested} by inherited ceiling {ceiling})"
        )
        permission_payload["clamp"] = {
            "requested": requested,
            "ceiling": ceiling,
            "effective": policy,
        }
    resolved["permissions"] = permission_payload
    if policy_changed:
        payload.pop("permissions_source", None)
    # Rebuild adapter from the live selection so wire vias (model_via /
    # effort_via / effort_cli_flag) survive continue --permissions. A hard
    # allowlist used to drop those fields and force config_option defaults.
    payload["adapter"] = runner.session_resolution(resolution, cwd=None)["adapter"]
    return payload


# The source of a policy a person typed at the `--bg` prompt: neither `unset`
# (somebody did set it) nor `default` (acpc did not pick it).
PERMISSIONS_ANSWERED = "answered"
# What a `resolve` preview reports instead of putting that question itself.
PERMISSIONS_ASKED_AT_DISPATCH = "asked at dispatch"


def _resolve_permissions(
    explicit: str | None,
    resolution: CallResolution,
    *,
    background: bool = False,
    preview: bool = False,
) -> tuple[str | None, tuple[str, str] | None, str | None]:
    """Settle the permission policy against the streams this call was given.

    Two separate questions (see `interaction`): whether acpc may ask at all,
    which stdin and the stdout format decide, and whether it asks unprompted,
    which additionally wants a terminal on stdout.  Redirecting the result to
    a file lowers the default; it does not withdraw an explicit `ask`.

    `--bg` is the one case that neither answers.  Accepting the work instead
    of waiting for it may change only how long the command waits, never what
    the callee may do, so it asks for the policy up front and carries the
    answer into the session rather than quietly lowering the default.

    Returns the policy, any inherited-ceiling clamp, and a source label when
    the policy came from somewhere provenance cannot name.  Under `preview`
    the policy is `None` whenever it would have to be asked for: a preview
    dispatches nothing, so it reports that the value is chosen later instead
    of producing an answer the real call would go on to ask for again.
    """
    policy = explicit if explicit is not None else resolution.permissions
    source: str | None = None
    if policy is None:
        policy, source = _default_policy(background=background, preview=preview)
    if policy is None:
        return None, None, source
    policy, clamp = _clamp_inherited_ceiling(policy)
    if policy == "ask":
        cause = (
            "cannot be used with --bg/--background, which returns before a request could be answered"
            if background
            else interaction.why_not_interactive()
        )
        if cause is not None:
            raise UsageProblem(
                f"--permissions ask {cause}; pass --permissions none, read, edit, execute or all"
            )
    return policy, clamp, source


def _default_policy(*, background: bool, preview: bool) -> tuple[str | None, str | None]:
    """The policy for a caller that named none, and where it came from."""
    if not interaction.ask_by_default():
        return "read", None
    if not background:
        return "ask", None
    if preview:
        return None, PERMISSIONS_ASKED_AT_DISPATCH
    return _ask_background_policy()


def _ask_background_policy() -> tuple[str, str | None]:
    """Ask once, before dispatch, which policy the detached session runs under.

    `ask` is not on offer: nobody will be attached to answer, so accepting it
    here would promise a dialogue that can never happen.  Silence is not
    consent — an unanswered question fails the call by name rather than
    settling on `read` behind the caller's back.

    A terminal that will not open is the other case.  There the question was
    never put, so there is no silence to read as consent and nothing to fail
    for: the policy drops to the floor every caller without a terminal gets,
    and is reported as acpc's own default rather than as an answer.
    """
    choices = list(vocab.PERMISSION_VALUES[:-1])
    default = "read"
    try:
        chosen = interaction.ask_choice(
            "acpc: --bg/--background detaches this session, so nothing can answer a permission request.\n"
            f"acpc: policy for this session [{', '.join(choices)}] ({default}): ",
            choices=choices,
            default=default,
        )
    except interaction.TerminalUnavailable:
        return default, None
    if chosen is None:
        raise UsageProblem(
            "--bg/--background needs a permission policy chosen before it detaches; "
            "pass --permissions none, read, edit, execute or all"
        )
    return chosen, PERMISSIONS_ANSWERED


def _clamp_inherited_ceiling(policy: str) -> tuple[str, tuple[str, str] | None]:
    """Apply the numeric ceiling exported by the parent acpc session.

    The variable is a guardrail, not a boundary: a callee with a shell can
    unset ACPC_CEILING, so this is not a security control.
    """
    raw_ceiling = os.environ.get("ACPC_CEILING")
    if raw_ceiling is None:
        return policy, None
    numeric_policies = vocab.PERMISSION_VALUES[:-1]
    if raw_ceiling not in numeric_policies:
        supported = ", ".join(numeric_policies)
        raise UsageProblem(f"ACPC_CEILING={raw_ceiling!r} is invalid; expected one of {supported}")
    ceiling = raw_ceiling

    if policy == "ask":
        if ceiling != "all":
            supported = ", ".join(numeric_policies)
            raise UsageProblem(
                f"--permissions ask exceeds inherited ceiling {ceiling}; "
                f"available policies: {supported}",
                kind=errors.PERMISSION_DENIED,
            )
        effective = policy
    elif PermissionLevel(policy).rank > PermissionLevel(ceiling).rank:
        effective = ceiling
    else:
        effective = policy
    if effective == policy:
        return policy, None
    return effective, (policy, ceiling)


def _mode_list(entry: ResolvedEntry) -> str:
    """Render declared modes in their TOML order for a selection error."""
    if not entry.modes:
        return "none"
    return ", ".join(f"{name} (grants {spec.grants})" for name, spec in entry.modes.items())


def _mode_selection_error(
    resolution: CallResolution,
    error: ModeSelectionError,
) -> AcpcError:
    """Turn a policy/mode mismatch into an actionable CLI usage error."""
    entry = resolution.entry
    modes = _mode_list(entry)
    if error.explicit_mode is not None:
        mode = error.explicit_mode
        spec = entry.modes.get(mode)
        if spec is None:
            reason = (
                f"mode {mode} is not declared in {entry.base_adapter}'s [modes] table; "
                "a vendor-advertised mode omitted there is accepted only with "
                "--permissions all"
            )
        else:
            reason = (
                f"mode {mode} grants {spec.grants}, which exceeds permissions {error.policy}; "
                f"the lowest policy that admits it is {spec.grants}"
            )
        source = resolution.provenance.get("mode", FieldSource("unset"))
        if source.kind == "call":
            return UsageProblem(f"--mode {mode}: {reason}")
        where = f" ({source.path})" if source.path is not None else ""
        return UsageProblem(
            f"agent '{entry.entry}' resolves mode {mode}{where}: {reason}; "
            "edit the mode or permissions in the entry"
        )

    if error.policy == "ask":
        reason = (
            f"--permissions ask on {entry.entry} is not really asking anything: "
            "no mode grants at most read, so no permission request can reach acpc"
        )
    else:
        reason = f"no mode on {entry.entry} grants at most permissions {error.policy}"
    # No mode admits the requested policy: the call is spelled correctly and
    # the entry refuses it, which is a denial rather than a malformed flag.
    if not error.modes:
        return UsageProblem(f"{reason}; declared modes: {modes}", kind=errors.PERMISSION_DENIED)
    floor = min(
        (PermissionLevel(spec.grants) for spec in error.modes.values()),
        key=lambda level: level.rank,
    ).value
    reason = f"{reason} — the lowest policy {entry.entry} runs under is {floor}"
    return UsageProblem(
        f"{reason}; pass --permissions {floor}; declared modes: {modes}",
        kind=errors.PERMISSION_DENIED,
        hint=f"Run: acpc run {entry.entry} --permissions {floor}",
    )


def _select_resolution(resolution: CallResolution) -> CallResolution:
    """Attach the policy-selected mode and its measured facts to a resolution."""
    policy = resolution.permissions
    if policy is None:
        raise UsageProblem("mode selection requires a resolved permission policy")
    try:
        mode, spec = select_mode(resolution.entry.modes, policy, resolution.mode)
    except ModeSelectionError as error:
        raise _mode_selection_error(resolution, error) from None
    provenance = dict(resolution.provenance)
    if resolution.mode is None:
        provenance["mode"] = FieldSource("selected")
    return replace(resolution, mode=mode, mode_spec=spec, provenance=provenance)


def _tty_permission_prompt(kind: str, title: str) -> bool:
    """Ask the human on /dev/tty — never on stdin (SPEC *Output contract*).

    Denial is the default: an unanswered permission request must not widen
    what the callee may do.
    """
    return interaction.ask_yes_no(f"acpc: allow {kind}? {title} [y/N] ", default=False)


def _emit_resolution(payload: dict[str, Any], *, json_mode: bool) -> None:
    if json_mode:
        click.echo(json.dumps(payload, ensure_ascii=False))
        return
    lines = [
        f"entry        {payload['entry']} ({payload['base_adapter']})",
        f"command      {payload['command']}",
    ]
    for name, item in payload["resolved"].items():
        value = "·" if item["value"] is None else item["value"]
        if name == "mode" and item["value"] is not None and "delegates" in item:
            policy = payload["resolved"]["permissions"]["value"]
            reason = item["source"]
            if item["source"] == "selected":
                reason = f"selected for permissions {policy}"
            delegation = "acpc-delegated" if item["delegates"] else "vendor-decided"
            escalation = " · escalates" if item.get("escalates") else ""
            lines.append(f"{name:<12} {value} ({reason}) · {delegation}{escalation}")
        else:
            lines.append(f"{name:<12} {value} ({item['source']})")
    if payload["cwd"]:
        lines.append(f"cwd          {payload['cwd']}")
    if payload["env"]:
        declared = " · ".join(f"{key}={value}" for key, value in payload["env"].items())
        lines.append(f"env          {declared}")
    if payload["env_passthrough"]:
        lines.append(f"passthrough  {' · '.join(payload['env_passthrough'])}")
    click.echo("\n".join(lines))


# Whether the last stdout write left its final line unterminated.  Answers and
# transcript content need not end with a newline, and stdout must carry their
# exact bytes, so the stderr side compensates (see _echo_metadata).
_stdout_line_open = False


def _leave_on_broken_pipe() -> NoReturn:
    """Leave with SPEC's 141 without letting the shutdown flush raise again."""
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, sys.stdout.fileno())
    raise SystemExit(vocab.EXIT_SIGPIPE)


def _write_stdout(text: str) -> None:
    """Write the answer, turning a closed stdout into SPEC's exit 141."""
    global _stdout_line_open
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except BrokenPipeError:
        _leave_on_broken_pipe()
    if text:
        _stdout_line_open = not text.endswith("\n")


def _echo_metadata(line: str) -> None:
    """Write a `--` stderr line, opening a fresh line if stdout left none.

    SPEC *Output contract*: the `--` prefix separates metadata only at a line
    boundary.  When the answer ends without a trailing newline, a merged blob
    would glue the summary to its last line — the newline goes on stderr,
    never stdout, which stays byte-identical to `answer.md`.
    """
    global _stdout_line_open
    if _stdout_line_open:
        line = "\n" + line
        _stdout_line_open = False
    click.echo(line, err=True)


def _display_home(value: str | None) -> str:
    """Render a vendor home in the copy-pastable form used by ``agents``."""
    if value is None:
        return "·"
    expanded = Path(value).expanduser()
    try:
        relative = expanded.relative_to(Path.home())
    except ValueError:
        return value
    return "~" if not relative.parts else str(Path("~") / relative)


def _display_path(value: Path) -> str:
    try:
        relative = value.expanduser().relative_to(Path.home())
    except ValueError:
        return str(value)
    return "~" if not relative.parts else str(Path("~") / relative)


def _source_text(source: FieldSource | None) -> str:
    if source is None:
        return "unset"
    if source.kind == "adapter-default":
        return "adapter default"
    if source.kind == "default":
        return "default"
    if source.kind == "unset":
        return "unset"
    if source.kind == "call":
        return "call"
    return "entry"


def _local_variant_value(entry: ResolvedEntry, field: str) -> str | None:
    """Return a variant field only when that variant directly defines it."""
    source = entry.provenance.get(field)
    if source is None or source.kind != "entry" or source.path is None:
        return None
    if source.path.stem != entry.entry:
        return None
    value = getattr(entry, field)
    if value is None:
        return None
    return _display_home(value) if field == "home" else str(value)


# SPEC: the roster bounds a description so a long one cannot bloat the
# context of the agent reading it; the detail view and --json stay full.
_ROSTER_DESCRIPTION_LIMIT = 80


def _agent_row(entry: ResolvedEntry) -> tuple[str, ...]:
    status = entry.roster_install_status()
    description = _roster_description(
        entry.description, full_command=f"acpc agents get {entry.entry}"
    )
    return (entry.entry, entry.name, status, description)


def _variant_row(entry: ResolvedEntry) -> tuple[str, ...]:
    values = {
        field: _local_variant_value(entry, field)
        for field in ("model", "effort", "permissions", "home")
    }
    description = _roster_description(
        entry.description, full_command=f"acpc agents get {entry.entry}"
    )
    return (
        entry.entry,
        values["model"] or "·",
        values["effort"] or "·",
        values["permissions"] or "·",
        values["home"] or "·",
        description,
    )


def _skill_row(skill: skills.Skill) -> tuple[str, ...]:
    description = _roster_description(
        skill.description, full_command=f"acpc skills get {skill.name}"
    )
    return (skill.name, description)


def _roster_description(value: str | None, *, full_command: str) -> str:
    if value is None:
        return ""
    safe = render.safe_text(value)
    preview = render.snippet(safe, limit=_ROSTER_DESCRIPTION_LIMIT)
    if preview != safe:
        return f"{preview} (full: {full_command})"
    return preview


def _skill_payload(skill: skills.Skill, *, include_body: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": skill.name,
        "description": skill.description,
        "path": str(skill.path),
    }
    if include_body:
        payload["body"] = skill.body
    return payload


def _agent_list_payload(registry: AgentRegistry) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for adapter in registry.adapters:
        rows.append(
            {
                "name": adapter.entry,
                "kind": "adapter",
                "display_name": adapter.name,
                "status": adapter.install_status,
                "description": adapter.description,
            }
        )
        for variant in registry.variants:
            if variant.base_adapter != adapter.entry:
                continue
            rows.append(
                {
                    "name": variant.entry,
                    "kind": "variant",
                    "base_adapter": variant.base_adapter,
                    "model": _local_variant_value(variant, "model"),
                    "effort": _local_variant_value(variant, "effort"),
                    "permissions": _local_variant_value(variant, "permissions"),
                    "home": _local_variant_value(variant, "home"),
                    "description": variant.description,
                }
            )
    return output.collection_envelope(rows, has_more=False)


def _cache_footer(record: cache.CachedAdvertised | None) -> str:
    if record is None:
        return "-- cached never"
    age = cache.cache_age(record.cached_at)
    return "-- cached now" if age == "now" else f"-- cached {age} ago"


def _commands_footer(
    adapter: str, commands: list[Mapping[str, Any]], record: cache.CachedAdvertised | None
) -> str:
    age = "never" if record is None else cache.cache_age(record.cached_at)
    age_text = "now" if age == "now" else f"{age} ago"
    command_file = _display_path(cache.commands_path(adapter))
    return f"-- {len(commands)} commands (cached {age_text}) | full descriptions: {command_file}"


def _command_name(value: Mapping[str, Any]) -> str:
    name = value.get("name", "")
    return f"/{name.lstrip('/')}" if isinstance(name, str) else "/"


def _mode_name(value: Any) -> str:
    if isinstance(value, Mapping):
        candidate = value.get("id", value.get("name", ""))
    else:
        candidate = value
    return str(candidate)


def _mode_display(name: str, entry: ResolvedEntry) -> str:
    spec = entry.modes.get(name)
    if spec is None:
        return f"{name} (undeclared)"
    delegates = " · delegates" if spec.delegates else ""
    escalates = " · escalates" if spec.escalates else ""
    return f"{name} ({spec.grants}{delegates}{escalates})"


def _advertised_payload(record: cache.CachedAdvertised | None) -> dict[str, Any]:
    if record is None:
        return {"modes": [], "models": [], "commands": []}
    return record.advertised


def _ensure_cache(entry: ResolvedEntry) -> cache.CachedAdvertised:
    """Return an adapter cache, probing installed adapters on a miss."""
    record = cache.read_advertised(entry.base_adapter)
    if record is not None:
        return record
    if not entry.installed:
        raise cache.ProbeError(entry.missing_binary_error())
    advertised = asyncio.run(cache.probe_advertised(entry.resolve_call()))
    refreshed = cache.read_advertised(entry.base_adapter)
    return refreshed or cache.CachedAdvertised(advertised=advertised, cached_at=time.time())


def _render_advertised_detail(
    entry: ResolvedEntry, record: cache.CachedAdvertised | None
) -> tuple[str, dict[str, Any]]:
    advertised = _advertised_payload(record)
    modes = [_mode_name(item) for item in advertised.get("modes", [])]
    models = [str(item) for item in advertised.get("models", [])]
    commands = [item for item in advertised.get("commands", []) if isinstance(item, Mapping)]
    # Modes are never capped: this view is where legal --mode values come
    # from, and unlike models/commands there is no fuller view behind it.
    visible_modes = [_mode_display(mode, entry) for mode in modes]
    visible_models = models[:3] + (["…"] if len(models) > 3 else [])
    visible_commands = [_command_name(item) for item in commands[:3]]
    if len(commands) > 3:
        visible_commands.append("…")
    lines = [
        f"modes        {len(modes)} · {' · '.join(visible_modes) if visible_modes else '·'}",
        f"models       {len(models)} · {' · '.join(visible_models) if visible_models else '·'}",
        f"commands     {len(commands)} · {' · '.join(visible_commands) if visible_commands else '·'}",
        _cache_footer(record),
    ]
    payload = {
        "advertised": {
            "modes": modes,
            "mode_specs": {
                mode: (
                    {
                        "grants": entry.modes[mode].grants,
                        "delegates": entry.modes[mode].delegates,
                        "escalates": entry.modes[mode].escalates,
                    }
                    if mode in entry.modes
                    else None
                )
                for mode in modes
            },
            "models": models,
            "commands": [dict(item) for item in commands],
        }
    }
    return "\n".join(lines) + "\n", payload


def _render_entry_detail(
    registry: AgentRegistry, entry: ResolvedEntry
) -> tuple[str, dict[str, Any], str | None]:
    resolution = registry.resolve_call(entry.entry)
    lines: list[str] = []
    if entry.is_variant:
        lines.append(f"extends      {entry.extends}")
    else:
        lines.append(f"adapter      {entry.name} · {entry.install_status} · {entry.command_head}")
    if entry.description is not None:
        lines.append(f"description  {render.safe_text(entry.description)}")

    resolved_values = {
        "model": resolution.model,
        "effort": resolution.effort,
        "mode": resolution.mode,
        "permissions": resolution.permissions,
        "home": resolution.home,
    }
    for field, value in resolved_values.items():
        if field == "home":
            rendered = _display_home(value)
        elif field == "permissions" and value is None:
            rendered = "ask at a terminal, read otherwise"
        else:
            rendered = "·" if value is None else str(value)
        rendered_source = _source_text(resolution.provenance.get(field))
        lines.append(f"{field:<12} {rendered} ({rendered_source})")

    declared = [f"{key}={value}" for key, value in entry.env.items()]
    env_text = " · ".join(declared) if declared else "·"
    env_source = _source_text(entry.provenance.get("env"))
    if entry.env_passthrough:
        env_text += f" ({env_source}) · passthrough: {' · '.join(entry.env_passthrough)}"
    else:
        env_text += f" ({env_source})"
    lines.append(f"env          {env_text}")
    if not entry.is_variant:
        variants = [item.entry for item in registry.variants if item.base_adapter == entry.entry]
        lines.append(f"variants     {' · '.join(variants) if variants else 'none'}")

    payload = {
        "agent": entry.entry,
        "base_adapter": entry.base_adapter,
        "description": entry.description,
        "resolved": {
            field: {
                "value": value,
                "source": _source_text(resolution.provenance.get(field)),
            }
            for field, value in resolved_values.items()
        },
        "env": dict(entry.env),
        "env_passthrough": list(entry.env_passthrough),
    }
    if entry.is_variant:
        lines.append(f"-- modes/models/commands: acpc agents get {entry.base_adapter}")
        return "\n".join(lines) + "\n", payload, None
    return "\n".join(lines) + "\n", payload, entry.base_adapter


def _render_models(
    entry: ResolvedEntry, record: cache.CachedAdvertised | None
) -> tuple[str, dict[str, Any]]:
    advertised = _advertised_payload(record)
    lines: list[str] = []
    preset_rows = [
        (tier, preset.model, preset.effort or "·") for tier, preset in entry.presets.items()
    ]
    lines.extend(
        render.format_table(
            preset_rows,
            header=("tier", "model", "effort"),
            prefix="presets   ",
            continuation_prefix="          ",
            separator="  ",
        )
    )
    models = [str(item) for item in advertised.get("models", [])]
    lines.append("models    " + ("\n          ".join(models) if models else "·"))
    lines.append(_cache_footer(record))
    return "\n".join(lines) + "\n", {
        "agent": entry.entry,
        "presets": {
            tier: {"model": preset.model, "effort": preset.effort}
            for tier, preset in entry.presets.items()
        },
        "models": models,
    }


def _render_commands(
    entry: ResolvedEntry, record: cache.CachedAdvertised | None
) -> tuple[str, dict[str, Any]]:
    advertised = _advertised_payload(record)
    commands = [item for item in advertised.get("commands", []) if isinstance(item, Mapping)]
    rows: list[tuple[str, str]] = []
    for command in commands:
        description = command.get("description", "")
        text = cache.first_sentence(description) if isinstance(description, str) else ""
        if isinstance(description, str) and text != description:
            text += "…"
        rows.append((_command_name(command), text))
    lines = render.format_table(rows)
    lines.append(_commands_footer(entry.base_adapter, commands, record))
    return "\n".join(lines) + "\n", {
        "agent": entry.entry,
        "commands": [
            {"name": _command_name(item), "description": item.get("description", "")}
            for item in commands
        ],
    }


def _emit_json(payload: Mapping[str, Any]) -> None:
    _write_stdout(json.dumps(dict(payload), ensure_ascii=False) + "\n")


def _render_agent_roster(registry: AgentRegistry, items: list[dict[str, Any]], total: int) -> str:
    selected_names = {str(item["name"]) for item in items}
    adapter_lines = render.format_table(
        [_agent_row(adapter) for adapter in registry.adapters if adapter.entry in selected_names],
        separator="  ",
    )
    variants = [variant for variant in registry.variants if variant.entry in selected_names]
    variant_lines = (
        render.format_table(
            [_variant_row(variant) for variant in variants],
            header=("entry", "model", "effort", "permissions", "home", "description"),
            prefix="  ",
            separator="  ",
        )
        if variants
        else []
    )
    lines = [*adapter_lines, *variant_lines]
    if len(items) < total:
        lines.append(f"-- {len(items)} of {total} — use --limit to change")
    return "\n".join(lines) + ("\n" if lines else "")


def _run_agents_list(selected_format: str, limit: int, plain: bool) -> None:
    """Render the bounded adapter and variant collection."""
    try:
        registry = AgentRegistry()
        all_items = list(_agent_list_payload(registry)["items"])
        items = all_items[:limit]
        payload = output.collection_envelope(items, has_more=len(items) < len(all_items))
        if selected_format == "json":
            _emit_json(payload)
        elif selected_format == "plain":
            _write_stdout("".join(f"{item['name']}\n" for item in items))
        else:
            _write_stdout(_render_agent_roster(registry, items, len(all_items)))
    except cache.ProbeError as error:
        raise _probe_problem(error) from None
    except RegistryError as error:
        raise _registry_problem(error) from None


@effects.read_only
@main.group(name="agents", invoke_without_command=True)
@click.help_option("-h", "--help")
@click.pass_context
def agents_group(ctx: click.Context) -> None:
    """Manage adapter and variant entries with explicit list/get verbs."""
    if ctx.invoked_subcommand is None:
        raise UsageProblem("agents requires a subcommand", hint="Run: acpc agents list")


@effects.read_only
@schema.describes()
@agents_group.command(name="list")
@click.option(
    "--limit",
    type=click.IntRange(min=0),
    default=render.DEFAULT_STATUS_LIMIT,
    show_default=True,
    help="Return at most N agents in the collection; default 20.",
)
@click.option(
    "--plain",
    is_flag=True,
    help=(
        "Print one agent name per line; requires explicit --limit and cannot be combined "
        "with --json or a different --format."
    ),
)
@click.option(
    "--format",
    "format_name",
    type=click.Choice(("text", "json", "plain")),
    help=_FORMAT_COLLECTION_HELP,
)
@_color_option()
@_json_option("Emit this collection as JSON.")
@click.help_option("-h", "--help")
@click.pass_context
def agents_list_command(
    ctx: click.Context, limit: int, plain: bool, format_name: str | None, json_mode: bool
) -> None:
    """List adapters and variants.
    Entries are ordered by adapter name, ascending; each adapter is followed by its variants in
    name order, and the default window is the first 20 of that order.

    The collection is grouped by adapter so a variant is shown with its base adapter.
    """
    selected_format = _select_format(format_name, json_mode, plain=plain)
    if (
        selected_format == "plain"
        and ctx.get_parameter_source("limit") is not click.core.ParameterSource.COMMANDLINE
    ):
        raise UsageProblem("--plain requires an explicit --limit")
    _run_agents_list(selected_format, limit, plain)


def _emit_agent_models(registry: AgentRegistry, entry: ResolvedEntry, selected_format: str) -> None:
    _warn_unlisted_effort_model(registry.resolve_call(entry.entry))
    record = _ensure_cache(entry)
    text, payload = _render_models(entry, record)
    if selected_format == "json":
        _emit_json(payload)
        click.echo(_cache_footer(record), err=True)
    else:
        _write_stdout(text)


def _emit_agent_commands(entry: ResolvedEntry, selected_format: str) -> None:
    record = _ensure_cache(entry)
    text, payload = _render_commands(entry, record)
    if selected_format == "json":
        _emit_json(payload)
        click.echo(
            _commands_footer(entry.base_adapter, list(payload["commands"]), record),
            err=True,
        )
    else:
        _write_stdout(text)


def _emit_agent_detail(registry: AgentRegistry, entry: ResolvedEntry, selected_format: str) -> None:
    text, payload, _ = _render_entry_detail(registry, entry)
    if not entry.is_variant:
        record = _ensure_cache(entry)
        advertised_text, advertised_payload = _render_advertised_detail(entry, record)
        payload.update(advertised_payload)
        text += advertised_text
        if selected_format == "json":
            click.echo(_cache_footer(record), err=True)
    if selected_format == "json":
        _emit_json(payload)
    else:
        _write_stdout(text)


@effects.read_only
@schema.describes(name="Adapter or variant to render, as listed by `acpc agents list`.")
@agents_group.command(name="get")
@click.argument("name")
@click.option(
    "--models",
    is_flag=True,
    help="Show full advertised presets and models; mutually exclusive with --commands.",
)
@click.option(
    "--commands",
    is_flag=True,
    help="Show advertised slash commands; mutually exclusive with --models.",
)
@click.option(
    "--format",
    "format_name",
    type=click.Choice(("text", "json")),
    help=_FORMAT_OUTPUT_HELP,
)
@_color_option()
@_json_option("Emit this agent view as JSON.")
@click.help_option("-h", "--help")
def agents_get_command(
    name: str, models: bool, commands: bool, format_name: str | None, json_mode: bool
) -> None:
    """Show one adapter or variant, optionally with models or commands."""
    if models and commands:
        raise UsageProblem("--models and --commands are mutually exclusive")
    selected_format = _select_format(format_name, json_mode)
    try:
        registry = AgentRegistry()
        entry = registry.resolve(name)
        if models:
            _emit_agent_models(registry, entry, selected_format)
            return
        if commands:
            _emit_agent_commands(entry, selected_format)
            return
        _emit_agent_detail(registry, entry, selected_format)
    except cache.ProbeError as error:
        raise _probe_problem(error) from None
    except RegistryError as error:
        raise _registry_problem(error) from None


def _check_entries(registry: AgentRegistry, name: str | None) -> list[ResolvedEntry]:
    if name is None:
        return list(registry)
    return [registry.resolve(name)]


def _check_one(registry: AgentRegistry, entry: ResolvedEntry, timeout: float) -> dict[str, Any]:
    try:
        resolution = registry.resolve_call(entry.entry)
        _warn_unlisted_effort_model(resolution)
        advertised = asyncio.run(
            asyncio.wait_for(cache.probe_advertised(resolution), timeout=timeout)
        )
        return {"agent": entry.entry, "ok": True, "models": len(advertised["models"])}
    except (cache.ProbeError, TimeoutError) as error:
        message = "check timed out" if isinstance(error, TimeoutError) else str(error)
        return {"agent": entry.entry, "ok": False, "error": message}


def _agents_check(
    registry: AgentRegistry,
    name: str | None,
    *,
    selected_format: str,
    limit: int,
    timeout: float,
) -> None:
    entries = _check_entries(registry, name)[:limit]
    results: list[dict[str, Any]] = []
    for entry in entries:
        results.append(_check_one(registry, entry, timeout))
    if name is None:
        payload: dict[str, Any] = output.collection_envelope(
            results, has_more=len(results) < len(_check_entries(registry, None))
        )
    else:
        payload = output.collection_envelope(results, has_more=False)
    if selected_format == "json":
        _emit_json(payload)
    elif selected_format == "plain":
        _write_stdout("".join(f"{result['agent']}\n" for result in results))
    else:
        for result in results:
            if result["ok"]:
                _write_stdout(f"{result['agent']} ok\n")
            else:
                _write_stdout(f"{result['agent']} failed: {result['error']}\n")
    failed = [str(result["agent"]) for result in results if not result["ok"]]
    if failed:
        noun = "check" if len(failed) == 1 else "checks"
        click.echo(f"-- {len(failed)} {noun} failed: {', '.join(failed)}", err=True)


_CHECK_TIMEOUT_DEFAULT = "30s"


@effects.read_only
@schema.describes(
    name=(
        "Optional adapter or variant to check; absent, every registered adapter and variant is "
        "checked, including entries whose adapter is unavailable."
    ),
)
@agents_group.command(name="check")
@click.argument("name", required=False)
@click.option(
    "--limit",
    type=click.IntRange(min=0),
    default=render.DEFAULT_STATUS_LIMIT,
    show_default=True,
    help="Return at most N check results; default 20; only valid without NAME.",
)
@click.option(
    "--plain",
    is_flag=True,
    help=(
        "Print one checked agent name per line; only valid without NAME, requires an explicit "
        "--limit, and cannot be combined with --json or a different --format."
    ),
)
@click.option(
    "--timeout",
    type=TimeoutParamType(),
    default=_CHECK_TIMEOUT_DEFAULT,
    show_default=True,
    metavar="S",
    help=f"Bound each adapter connection check; default {_CHECK_TIMEOUT_DEFAULT}.",
)
@click.option(
    "--format",
    "format_name",
    type=click.Choice(("text", "json", "plain")),
    help=_FORMAT_COLLECTION_HELP,
)
@_color_option()
@_json_option("Emit check results as JSON.")
@click.help_option("-h", "--help")
@click.pass_context
def agents_check_command(
    ctx: click.Context,
    name: str | None,
    limit: int,
    plain: bool,
    timeout: float,
    format_name: str | None,
    json_mode: bool,
) -> None:
    """Check registered adapters and variants, then report advertised data.
    Entries are ordered by name, ascending, and the default window is the first 20 of that order.

    With NAME, check one registered entry, including one whose adapter is not
    installed. Without NAME, check every registered adapter and variant.
    NAME cannot be combined with ``--limit`` or ``--plain``.
    JSON output is always a collection; with NAME it contains one item.
    """
    selected_format = _select_format(format_name, json_mode, plain=plain)
    if name is not None:
        if ctx.get_parameter_source("limit") is click.core.ParameterSource.COMMANDLINE:
            raise UsageProblem("--limit is only supported when checking all agents")
        if selected_format == "plain":
            raise UsageProblem("--plain is only supported when checking all agents")
    elif (
        selected_format == "plain"
        and ctx.get_parameter_source("limit") is not click.core.ParameterSource.COMMANDLINE
    ):
        raise UsageProblem("--plain requires an explicit --limit")
    try:
        registry = AgentRegistry()
        _agents_check(
            registry,
            name,
            selected_format=selected_format,
            limit=limit,
            timeout=timeout,
        )
    except cache.ProbeError as error:
        raise _probe_problem(error) from None
    except RegistryError as error:
        raise _registry_problem(error) from None


@effects.read_only
@schema.describes(entry="Adapter or variant whose advertised modes are read.")
@main.command(name="probe")
@click.argument("entry")
@click.option(
    "--discover",
    is_flag=True,
    help="Read the advertised modes; opens and releases a session and sends zero turns.",
)
@click.option(
    "--format",
    "format_name",
    type=click.Choice(("text", "json")),
    help=_FORMAT_OUTPUT_HELP,
)
@_color_option()
@_json_option("Emit the report as JSON.")
@click.help_option("-h", "--help")
def probe_command(entry: str, discover: bool, format_name: str | None, json_mode: bool) -> None:
    """Read an adapter's advertised modes, without editing its registry entry.

    ``--discover`` opens a session, reads the advertised mode catalogue and releases it,
    sending zero turns.  Output is the catalogue and a diff against the entry's current
    ``[modes]`` table, stated from both sides.  Applying any of it is a separate, explicit act.

    Measuring what a mode actually permits is not in this release, so ``--discover`` is
    required.  On Windows probe refuses to run: its commands are POSIX shell commands and
    would measure the shell rather than the sandbox.

    Example: ``acpc probe claude --discover``
    """
    if not discover:
        raise UsageProblem(
            "probe needs --discover: reading the advertised mode catalogue is what this "
            "release measures. Measuring what a mode actually permits is not in it",
            hint="Run: acpc probe ENTRY --discover",
        )
    try:
        report = probe_engine.run(entry)
    except RegistryError as error:
        raise _registry_problem(error) from None
    except probe_engine.ProbeError as error:
        raise _probe_problem(error) from None
    selected_format = _select_format(format_name, json_mode)
    if selected_format == "json":
        _emit_json(report.payload())
    else:
        _write_stdout(report.text())


def _agent_entry_path(name: str) -> Path:
    """Map one entry name to its file, refusing anything that leaves the directory.

    Two layers, because neither alone is enough: the character check rejects a
    name that is really a path (``../config``, an absolute path, which
    ``Path.__truediv__`` would happily follow), and the resolved-parent check
    rejects what survives it — a symbolic link planted inside ``agents``.
    """
    if not name or "/" in name or "\\" in name or name in {".", ".."}:
        raise UsageProblem(f"invalid agent name '{name}'")
    agents_dir = paths.agents_dir()
    target = agents_dir / f"{name}.toml"
    if target.resolve().parent != agents_dir.resolve():
        raise UsageProblem(f"invalid agent name '{name}': it resolves outside {agents_dir}")
    return target


@effects.non_idempotent
@schema.describes(
    name=(
        "Name of the variant entry to create; an existing name is a conflict. One file name "
        "under `agents`: it may not be empty, contain `/` or `\\`, be `.` or `..`, or resolve "
        "outside that directory, and it may not be a subcommand of `acpc agents` (`list`, `get`, "
        "`create`, `delete`, `check`) — such an entry would be unreachable."
    ),
    model="Default model or preset for the variant; absent, the field is left out and the "
    "entry under --extends decides at run time.",
    effort="Default reasoning effort for the variant; absent, the field is left out and the "
    "entry under --extends decides at run time.",
    mode="Default operating mode for the variant; absent, the field is left out and the "
    "entry under --extends decides at run time.",
    permissions="Default permission policy for the variant: none, read, edit, execute, all "
    "or ask; write and prompt are deprecated aliases. Absent, the field is left out and the "
    "entry under --extends decides at run time.",
    home="Vendor home override for the variant; absent, the field is left out and the entry "
    "under --extends decides at run time.",
)
@agents_group.command(name="create")
@click.argument("name")
@click.option(
    "--extends",
    "parent",
    required=True,
    metavar="AGENT",
    help="Base agent entry to inherit settings from.",
)
@click.option("--model", metavar="M", help="Default model or preset for the variant.")
@click.option("--effort", metavar="E", help="Default reasoning effort for the variant.")
@click.option("--mode", metavar="MODE", help="Default operating mode for the variant.")
@click.option(
    "--permissions",
    type=click.Choice(_PERMISSION_CHOICES),
    metavar="P",
    help=(
        "Default policy: none, read, edit, execute, all or ask; write and prompt are "
        "deprecated aliases."
    ),
)
@click.option("--home", metavar="DIR", help="Vendor home override for the variant.")
@click.option(
    "--format",
    "format_name",
    type=click.Choice(("text", "json")),
    help=_FORMAT_OUTPUT_HELP,
)
@_json_option("Emit the created entry as JSON.")
@_color_option()
@click.help_option("-h", "--help")
def agents_create_command(
    name: str,
    parent: str,
    model: str | None,
    effort: str | None,
    mode: str | None,
    permissions: str | None,
    home: str | None,
    format_name: str | None,
    json_mode: bool,
) -> None:
    """Scaffold a variant entry.

    A strict create: an existing entry of that name is a conflict, never an
    overwrite. ``acpc agents delete`` removes what this wrote.

    Example: ``acpc agents create work --extends mock --permissions execute``
    """
    selected_format = _select_format(format_name, json_mode)
    if name in agents_group.commands:
        # `acpc agents get <name>` is the named-entry dispatch, so the entry
        # would be listed and never reachable. Refuse the name instead.
        raise UsageProblem(
            f"invalid agent name '{name}': it is an acpc agents subcommand, so the entry "
            f"would be unreachable — acpc agents {name} runs the subcommand"
        )
    permissions = _normalize_permission(permissions)
    try:
        registry = AgentRegistry()
        registry.resolve(parent)
        if effort is not None:
            registry.resolve_call(parent, model=model, effort=effort)
    except RegistryError as error:
        raise _registry_problem(error) from None
    target = _agent_entry_path(name)
    if target.exists():
        # A strict create whose target already exists: nothing is malformed,
        # the name is simply taken.
        raise AcpcError(
            f"agent entry already exists: {target}",
            kind=errors.CONFLICT,
            hint="Pick another name, or edit the entry in place.",
        )
    fields = [
        ("extends", parent),
        ("model", model),
        ("effort", effort),
        ("mode", mode),
        ("permissions", permissions),
        ("home", home),
    ]
    contents = (
        "\n".join(
            f"{key} = {json.dumps(value, ensure_ascii=False)}"
            for key, value in fields
            if value is not None
        )
        + "\n"
    )
    try:
        paths.ensure_private_dir(paths.agents_dir())
        paths.atomic_write(target, contents)
    except PermissionError as error:
        # A directory a person has to fix, not a call anyone can rewrite, so
        # the caller is told to stop rather than to try something else.
        raise AcpcError(
            f"cannot write {target}: {error.strerror}",
            kind=errors.PERMISSION_DENIED,
            action="user",
            hint=f"Make {paths.agents_dir()} writable, then run the command again.",
        ) from None
    except OSError as error:
        # A full disk, a read-only mount, a failing device: the machine could
        # not serve the write, which may or may not still be true later.
        raise AcpcError(
            f"cannot write {target}: {error.strerror}",
            kind=errors.UNAVAILABLE,
        ) from None
    payload = {"name": name, "extends": parent, "path": str(target), "changed": True}
    if selected_format == "json":
        _emit_json(payload)
    else:
        _write_stdout(f"created {target}\n")


@effects.non_idempotent
@schema.describes(
    name=(
        "Entry under $ACPC_HOME/agents to delete; entries acpc ships are refused. One file name "
        "under `agents`: it may not be empty, contain `/` or `\\`, be `.` or `..`, or resolve "
        "outside that directory."
    )
)
@agents_group.command(name="delete")
@click.argument("name")
@click.option("--yes", "-y", "assume_yes", is_flag=True, help="Delete without being asked.")
@click.option(
    "--format",
    "format_name",
    type=click.Choice(("text", "json")),
    help=_FORMAT_OUTPUT_HELP,
)
@_json_option("Emit the deleted entry as JSON.")
@_color_option()
@click.help_option("-h", "--help")
def agents_delete_command(
    name: str, assume_yes: bool, format_name: str | None, json_mode: bool
) -> None:
    """Delete one entry this machine owns, under ``$ACPC_HOME/agents``.

    The file is usually hand-written and acpc has no operation that restores it,
    so deletion requires confirmation. The shipped adapter itself is not this
    machine's, so a name with no file under ``agents`` is refused.

    Example: ``acpc agents delete work``
    """
    selected_format = _select_format(format_name, json_mode)
    target = _agent_entry_path(name)
    try:
        shipped = name in AgentRegistry().shipped_names
    except RegistryError as error:
        raise _registry_problem(error) from None
    if not target.exists():
        if shipped:
            raise _not_found(
                f"'{name}' is an adapter acpc ships and nothing under "
                f"{paths.agents_dir()} overrides it — only entries created on this "
                "machine can be deleted",
                hint="Run: acpc agents list",
            )
        raise _not_found(f"unknown agent entry '{name}'", hint="Run: acpc agents list")
    interaction.require_confirmation(
        assume_yes,
        message=f"agents delete {name}: deleting the local entry needs confirmation",
        hint=f"Run: acpc agents delete {name} --yes",
        prompt=f"Delete local agent entry {name}? [y/N] ",
        default=False,
    )
    try:
        target.unlink()
    except OSError as error:
        raise AcpcError(
            f"cannot delete {target}: {error}",
            kind=errors.OPERATION_FAILED,
        ) from None
    payload = {"name": name, "path": str(target), "changed": True}
    if selected_format == "json":
        _emit_json(payload)
    else:
        _write_stdout(f"deleted {target}\n")


def _run_skills_list(selected_format: str, limit: int) -> None:
    """Render the bounded bundled-skill collection."""
    bundled = skills.list_skills()
    items = [_skill_payload(skill, include_body=False) for skill in bundled[:limit]]
    payload = output.collection_envelope(items, has_more=len(items) < len(bundled))
    if selected_format == "json":
        _emit_json(payload)
    elif selected_format == "plain":
        _write_stdout("".join(f"{item['name']}\n" for item in items))
    else:
        rows = render.format_table(
            [_skill_row(skill) for skill in bundled[:limit]],
            header=("name", "description"),
        )
        if len(items) < len(bundled):
            rows.append(f"-- {len(items)} of {len(bundled)} — use --limit to change")
        _write_stdout("\n".join(rows) + "\n")


def _run_skill_get(name: str, selected_format: str) -> None:
    """Render one bundled skill and identify its source directory."""
    try:
        skill = skills.get_skill(name)
    except skills.SkillNotFoundError:
        raise _not_found(f"unknown skill {name!r}", hint="Run: acpc skills list") from None
    if selected_format == "json":
        _emit_json(_skill_payload(skill, include_body=True))
    else:
        _write_stdout(skill.body)
    _echo_metadata(f"-- skill {skill.name} | dir {skill.path}")


@effects.read_only
@main.group(name="skills", invoke_without_command=True)
@click.help_option("-h", "--help")
@click.pass_context
def skills_group(ctx: click.Context) -> None:
    """Browse bundled skills with explicit list and get verbs."""
    if ctx.invoked_subcommand is None:
        raise UsageProblem("skills requires a subcommand", hint="Run: acpc skills list")


@effects.read_only
@schema.describes()
@skills_group.command(name="list")
@click.option(
    "--limit",
    type=click.IntRange(min=0),
    default=render.DEFAULT_STATUS_LIMIT,
    show_default=True,
    help="Return at most N skills in the collection; default 20.",
)
@click.option(
    "--plain",
    is_flag=True,
    help=(
        "Print one skill name per line; requires explicit --limit and cannot be combined "
        "with --json or a different --format."
    ),
)
@click.option(
    "--format",
    "format_name",
    type=click.Choice(("text", "json", "plain")),
    help=_FORMAT_COLLECTION_HELP,
)
@_color_option()
@_json_option("Emit this collection as JSON.")
@click.help_option("-h", "--help")
@click.pass_context
def skills_list_command(
    ctx: click.Context, limit: int, plain: bool, format_name: str | None, json_mode: bool
) -> None:
    """List bundled skills.
    Entries are ordered by name, ascending, and the default window is the first 20 of that order.
    """
    selected_format = _select_format(format_name, json_mode, plain=plain)
    if (
        selected_format == "plain"
        and ctx.get_parameter_source("limit") is not click.core.ParameterSource.COMMANDLINE
    ):
        raise UsageProblem("--plain requires an explicit --limit")
    _run_skills_list(selected_format, limit)


@effects.read_only
@schema.format_defaults(tty="text", non_tty="text")
@schema.describes(name="Bundled skill to render, as listed by `acpc skills list`.")
@skills_group.command(name="get")
@click.argument("name")
@click.option(
    "--format",
    "format_name",
    type=click.Choice(("text", "json")),
    help=_FORMAT_NATIVE_HELP,
)
@_color_option()
@_json_option("Emit this skill as JSON.")
@click.help_option("-h", "--help")
def skills_get_command(name: str, format_name: str | None, json_mode: bool) -> None:
    """Print one bundled skill's body and source directory."""
    selected_format = _select_format(format_name, json_mode, native_text=True)
    _run_skill_get(name, selected_format)


@effects.non_idempotent
@schema.describes(agent="Registry entry whose adapter is installed; resolved as `run` resolves it.")
@main.command(name="install")
@click.argument("agent")
@click.option("--yes", "-y", "assume_yes", is_flag=True, help="Install without being asked.")
@click.option(
    "--format",
    "format_name",
    type=click.Choice(("text", "json")),
    help=_FORMAT_OUTPUT_HELP,
)
@_json_option("Emit the install result as JSON.")
@_color_option()
@click.help_option("-h", "--help")
def install_command(agent: str, assume_yes: bool, format_name: str | None, json_mode: bool) -> None:
    """Run an agent's install command from its registry entry.

    Resolves the agent like ``run`` does, runs its ``install_command`` and
    relays the installer's output; a failing installer exits 1. The installer
    does not expose whether it changed the target, so successful calls report
    ``changed: null`` rather than guessing from its exit code.
    The installer is the vendor's, and acpc has no matching uninstall, so it asks
    first: a person is asked on the terminal, and every other caller passes
    ``--yes``.

    Example: ``acpc install codex --yes``
    """
    selected_format = _select_format(format_name, json_mode)
    try:
        registry = AgentRegistry()
        registry.resolve(agent)
        registry.install_command(agent)
    except RegistryError as error:
        # Nothing was installed and nothing ran: this is the failure itself,
        # not a result, so it leaves as an envelope with no stdout behind it.
        raise _registry_problem(error).with_context(agent=agent) from None

    # Last, after every check that can refuse this call on its own: an unknown
    # agent, or one with no trusted installer, must fail as that rather than
    # ask to confirm an install that could never happen.
    interaction.require_confirmation(
        assume_yes,
        message=f"install {agent}: running its installer needs confirmation",
        hint=f"Run: acpc install {agent} --yes",
        prompt=f"Install {agent}? [Y/n] ",
        default=True,
    )

    try:

        def run_installer(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                args,
                cwd=kwargs.get("cwd"),
                env=kwargs.get("env"),
                capture_output=True,
                text=True,
                check=False,
            )

        result = registry.execute_install(agent, runner=run_installer)
    except RegistryError as error:
        # Nothing was installed and nothing ran: this is the failure itself,
        # not a result, so it leaves as an envelope with no stdout behind it.
        raise _registry_problem(error).with_context(agent=agent) from None
    except OSError as error:
        raise AgentProblem(
            f"install {agent} failed: {error}",
            kind=errors.UNAVAILABLE,
            context={"agent": agent},
        ) from None

    for stream in (getattr(result, "stdout", None), getattr(result, "stderr", None)):
        if stream:
            click.echo(stream.rstrip("\n"), err=True)
    return_code = getattr(result, "returncode", 1)
    # The installer ran and reported for itself, so its report is this
    # command's declared output and stays on stdout; the envelope on stderr
    # is what says the command failed.
    payload = {
        "agent": agent,
        "ok": return_code == 0,
        "returncode": return_code,
        "changed": None,
    }
    if selected_format == "json" and return_code == 0:
        _emit_json(payload)
    elif return_code == 0:
        _write_stdout(f"installed {agent}\n")
    if return_code != 0:
        raise AgentProblem(
            f"install {agent} failed (exit {return_code})",
            context={"agent": agent, "returncode": return_code},
        )


@dataclass(frozen=True, slots=True)
class _DaemonCancelReply:
    """The daemon's answer, including the turn generation it accepted."""

    accepted: bool
    turn_token: int | None
    stale: bool = False


@dataclass(frozen=True, slots=True)
class _CancelResult:
    """The observed state and whether this call caused a cancellation request.

    ``stale`` means the daemon held a newer turn than the one this call
    selected; ``deadline_passed`` means the selected turn was still the
    active one, and still running, once the wait ran out. Both leave
    ``changed`` accurate for `cancel`'s own report; `steer` reads them to
    tell "nothing left to redirect" from "the redirect cannot happen yet".
    """

    meta: sessions.SessionMeta
    changed: bool
    stale: bool = False
    deadline_passed: bool = False


async def _cancel_with_daemon(
    target: str, session_id: str, turn_token: int
) -> _DaemonCancelReply | daemon_client.DaemonUnavailable | None:
    """Request cancellation without allowing a dead daemon to hang ``cancel``."""
    try:
        reply = await asyncio.wait_for(
            daemon_client.cancel_turn(target, session_id, turn_token),
            timeout=runner.CANCEL_ACK_TIMEOUT,
        )
    except TimeoutError:
        return None
    except Exception:  # noqa: BLE001
        return None
    if isinstance(reply, daemon_client.DaemonUnavailable):
        return reply
    if not isinstance(reply, Mapping):
        return None
    reply_turn_token = reply.get("turn_token")
    return _DaemonCancelReply(
        accepted=reply.get("ok") is True,
        turn_token=reply_turn_token
        if isinstance(reply_turn_token, int) and not isinstance(reply_turn_token, bool)
        else None,
        stale=reply.get("stale") is True,
    )


class _CancelDeadlinePassed(Exception):
    """The selected turn was still active, and still running, at the deadline.

    Silently reporting it settled would be false for both callers: `cancel`
    needs to keep saying `running`/`changed: true` as it does today, and
    `steer` needs to fail loudly instead of treating it as finished.
    """

    def __init__(self, meta: sessions.SessionMeta) -> None:
        super().__init__(meta.session_id)
        self.meta = meta


def _wait_for_cancel(session_id: str, *, expected_turn: int | None = None) -> sessions.SessionMeta:
    """Give a daemon's cancellation time to finalize the session on disk.

    Raises `_CancelDeadlinePassed` when ``expected_turn`` is still the active
    turn and still running once the deadline passes, and `outcome_unknown`
    when a *different* turn is active and the wait never caught up to it.
    """
    deadline = time.monotonic() + runner.CANCEL_ACK_TIMEOUT
    while True:
        meta = sessions.load(session_id)
        current_generation = expected_turn is None or meta.turns == expected_turn
        if current_generation and not meta.is_active:
            return meta
        if time.monotonic() >= deadline:
            if current_generation:
                raise _CancelDeadlinePassed(meta)
            raise AcpcError(
                f"could not observe the canceled turn for session {session_id}",
                kind=errors.OUTCOME_UNKNOWN,
                context={"session_id": session_id, "status": None},
            )
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


def _await_cancel(session_id: str, expected_turn: int) -> _CancelResult:
    """Wait for a requested cancellation to settle, deadline included."""
    try:
        return _CancelResult(_wait_for_cancel(session_id, expected_turn=expected_turn), True)
    except _CancelDeadlinePassed as passed:
        return _CancelResult(passed.meta, True, deadline_passed=True)


def _turn_terminal_state(session_id: str, turn: int) -> str:
    """The terminal state the transcript recorded for one past turn.

    Turns are strictly sequential and each finished one contributes exactly
    one terminal `state` event (SPEC.md *State on disk*), so the turn-th such
    event in order is that turn's own ending — the newer turn a `stale` reply
    means is not read here.
    """
    events = transcript.Transcript(sessions.transcript_path(session_id)).read().events
    seen = 0
    for event in events:
        if event.get("type") == "state" and event.get("to") in vocab.FINISHED_STATES:
            seen += 1
            if seen == turn:
                return str(event["to"])
    return "unknown"


def _stale_cancel_result(meta: sessions.SessionMeta, expected_turn: int) -> _CancelResult:
    """Report the selected turn's own ending without touching a newer one."""
    state = _turn_terminal_state(meta.session_id, expected_turn)
    return _CancelResult(replace(meta, state=state), False, stale=True)


def _cancel_local_session(meta: sessions.SessionMeta) -> _CancelResult:
    if meta.pid is None:
        return _CancelResult(
            sessions.transition(
                meta.session_id,
                "canceled",
                exit_code=vocab.EXIT_CANCELLED,
                stop_reason="stopped by user",
            ),
            True,
        )
    command_line = proc.process_cmdline(meta.pid)
    if command_line is None:
        raise AcpcError(
            f"could not identify the process hosting session {meta.session_id}",
            kind=errors.OUTCOME_UNKNOWN,
            hint=f"Run: acpc status {meta.session_id}",
            context={"session_id": meta.session_id, "status": meta.state},
        )
    if "acpc.direct_worker" in command_line:
        if not proc.is_process_alive(meta.pid, meta.process_start_time):
            return _CancelResult(sessions.load(meta.session_id), False)
        try:
            os.kill(meta.pid, signal.SIGINT)
        except ProcessLookupError:
            return _CancelResult(sessions.load(meta.session_id), False)
        except OSError as error:
            raise AcpcError(
                f"could not cancel session {meta.session_id}: {error}",
                kind=errors.UNAVAILABLE,
                context={"session_id": meta.session_id},
            ) from None
        return _await_cancel(meta.session_id, meta.turns)
    if command_line is not None and "acpc.daemon" in command_line:
        # Never kill the shared daemon from a one-session cancel: its PID in
        # meta.json is not the target's worker and doing so destroys siblings.
        raise AcpcError(
            f"could not cancel session {meta.session_id}: its saved process is the daemon",
            kind=errors.OUTCOME_UNKNOWN,
            context={"session_id": meta.session_id, "status": meta.state},
        )
    result = proc.kill_process_tree(meta.pid, meta.process_start_time)
    if result == "already_gone":
        return _CancelResult(sessions.load(meta.session_id), False)
    if result == "refused":
        # The process is there and would not take the signal, so acpc did not
        # observe the cancellation it was asked for and must not report one.
        raise AcpcError(
            f"could not cancel session {meta.session_id}: refused to signal it",
            kind=errors.UNAVAILABLE,
            context={"session_id": meta.session_id},
        )
    return _await_cancel(meta.session_id, meta.turns)


def _cancel_session(meta: sessions.SessionMeta) -> _CancelResult:
    """Cancel an active session and return the state it settled into.

    SPEC `cancel`: graceful `session/cancel` with a bounded wait for the ack,
    torn down anyway if the callee will not wind down in time. `steer` puts
    the same cancel in front of a follow-up turn, so it lives here rather
    than inside `cancel`. The turn selected is the one active when this call
    started (``meta.turns``); a daemon that has since moved on to a newer one
    answers `stale` and neither turn is touched further here.
    """
    expected_turn = meta.turns
    if meta.target is not None:
        reply = asyncio.run(_cancel_with_daemon(meta.target, meta.session_id, expected_turn))
        if isinstance(reply, daemon_client.DaemonUnavailable):
            current = sessions.load(meta.session_id)
            if not current.is_active:
                return _CancelResult(current, False)
            return _cancel_local_session(current)
        if reply is not None and reply.stale:
            return _stale_cancel_result(meta, expected_turn)
        if reply is not None and reply.accepted:
            turn_token = reply.turn_token if reply.turn_token is not None else expected_turn
            return _await_cancel(meta.session_id, turn_token)
        current = sessions.load(meta.session_id)
        if not current.is_active:
            return _CancelResult(current, False)
        if reply is None:
            raise AcpcError(
                f"cancel request for session {meta.session_id} was not confirmed",
                kind=errors.OUTCOME_UNKNOWN,
                context={"session_id": meta.session_id, "status": current.state},
            )
        return _cancel_local_session(current)
    return _cancel_local_session(meta)


def _emit_maintenance_result(
    payload: Mapping[str, Any],
    *,
    selected_format: str,
    text: str | None,
    summary: str,
) -> None:
    """Print a maintenance command's payload, then its stderr summary.

    ``--format json`` prints ``payload``; otherwise ``text`` is written verbatim
    when given, and nothing otherwise (a command with no non-JSON receipt).
    ``summary`` is the stderr line's content after the ``-- `` prefix.
    """
    if selected_format == "json":
        _emit_json(payload)
    elif text is not None:
        _write_stdout(text)
    click.echo(f"-- {summary}", err=True)


@effects.non_idempotent
@schema.describes(selector=_SELECTOR_HELP)
@main.command(name="cancel")
@click.argument("selector")
@click.option(
    "--format",
    "format_name",
    type=click.Choice(("text", "json")),
    help=_FORMAT_OUTPUT_HELP,
)
@_json_option("Emit the result as JSON.")
@_color_option()
@click.help_option("-h", "--help")
def cancel_command(selector: str, format_name: str | None, json_mode: bool) -> None:
    """Cancel the running turn; the session stays usable with ``acpc continue``.
    A repeated ``cancel`` can select a newer turn.

    Selects the turn in flight when the call starts and sends ACP ``session/cancel``
    for that turn only, then waits up to 10s for the ack; past that the connection
    is torn down anyway. A turn that ended before the request landed is reported
    with the state it reached and ``changed: false``; a newer turn started in the
    meantime is left alone, which is what a repeated ``cancel`` then selects.
    During a daemon-owned continuation preparation it cancels the preparation and
    writes a no-prompt placeholder. Transcript, meta and the partial answer stay
    on disk for post-mortem. A finished session is a successful no-op that reports
    the state it found; an unknown id is ``not_found``.

    Example: ``acpc cancel q7x2``
    """
    selected_format = _select_format(format_name, json_mode)
    meta = _load_view_session(selector)
    was_active = meta.is_active
    if not was_active:
        meta = _status_view_meta(meta)
        was_active = meta.is_active
    if was_active:
        result = _cancel_session(meta)
        meta = result.meta
        changed = result.changed
    else:
        changed = False

    payload = {
        "session_id": meta.session_id,
        "status": vocab.normalize_session_state(meta.state),
        "stop_reason": meta.stop_reason,
        "changed": changed,
    }
    _emit_maintenance_result(
        payload,
        selected_format=selected_format,
        text=f"{meta.session_id} {meta.state}\n",
        summary=f"canceled {meta.session_id} · {meta.state}",
    )


@effects.non_idempotent
@schema.describes(selector=_SELECTOR_HELP)
@main.command(name="delete")
@click.argument("selector")
@click.option("--yes", "-y", "assume_yes", is_flag=True, help="Delete without being asked.")
@click.option(
    "--format",
    "format_name",
    type=click.Choice(("text", "json")),
    help=_FORMAT_OUTPUT_HELP,
)
@_json_option("Emit the result as JSON.")
@_color_option()
@click.help_option("-h", "--help")
def delete_command(
    selector: str, assume_yes: bool, format_name: str | None, json_mode: bool
) -> None:
    """Delete a finished session's on-disk state.

    Errors on a starting or running session — cancel it first. The transcript,
    the prompt and the answer go with it and acpc cannot bring them back, so
    the call needs ``--yes``. The session directory remains as a permanent
    identifier tombstone; ``list`` and ``status`` do not treat it as a session.
    Prints the removed session id; ``--json`` also lists the deleted paths.

    Example: ``acpc delete q7x2 --yes``
    """
    selected_format = _select_format(format_name, json_mode)
    meta = _load_view_session(selector)
    advertised_paths = sessions.session_paths(meta.session_id)
    try:
        # Both checks run before the gate: an unknown or still-running session
        # fails as itself, and neither failure is one --yes could resolve.
        sessions.ensure_deletable(meta)
    except sessions.SessionStateError as error:
        raise _session_problem(error).with_context(session_id=meta.session_id) from None
    interaction.require_confirmation(
        assume_yes,
        message=f"delete {meta.session_id}: deleting a session's state needs confirmation",
        hint=f"Run: acpc delete {meta.session_id} --yes",
    )
    try:
        sessions.delete_session(meta.session_id)
    except sessions.SessionStateError as error:
        raise _session_problem(error).with_context(session_id=meta.session_id) from None
    payload = {
        "session_id": meta.session_id,
        "removed": True,
        "changed": True,
        "paths": advertised_paths,
    }
    _emit_maintenance_result(
        payload,
        selected_format=selected_format,
        text=f"removed {meta.session_id}\n",
        summary=f"removed session {meta.session_id}",
    )


@effects.non_idempotent
@main.command(name="prune")
@click.option(
    "--older-than",
    default=None,
    metavar="D",
    help=(
        "Delete finished sessions older than this age, as a duration such as 7d or 24h; "
        "absent, the configured retention window applies."
    ),
)
@click.option("--dry-run", "-n", is_flag=True, help="List candidates without deleting them.")
@click.option("--yes", "-y", "assume_yes", is_flag=True, help="Delete without being asked.")
@click.option(
    "--format",
    "format_name",
    type=click.Choice(("text", "json")),
    help=_FORMAT_OUTPUT_HELP,
)
@_json_option("Emit the result as JSON.")
@_color_option()
@click.help_option("-h", "--help")
def prune_command(
    older_than: str | None,
    dry_run: bool,
    assume_yes: bool,
    format_name: str | None,
    json_mode: bool,
) -> None:
    """Delete finished sessions older than the retention period.

    Bare ``prune`` uses the ``retention`` key in the global config
    (``~/.acpc/config.toml``, default 90d; ``ACPC_HOME`` moves the root) — it is
    never "delete everything". ``--older-than`` overrides it for this call, and
    deleting every finished session takes an explicit ``--older-than 0d``. Age is
    measured from when the session finished. Running sessions are never touched.
    Identifier tombstones are permanent: pruning releases session data, never an
    identifier reservation.

    It resolves the target set before confirmation. An empty set is an unchanged
    success without confirmation; a non-empty set needs ``--yes``. ``--dry-run``
    lists the same targets and never does.

    Example: ``acpc prune --older-than 7d --dry-run``
    """
    selected_format = _select_format(format_name, json_mode)
    try:
        settings = config.load_config()
        raw_duration = older_than if older_than is not None else settings.retention
        duration = config.parse_duration(raw_duration, allow_zero=True)
        if older_than is None and duration <= 0:
            raise UsageProblem(
                f"config retention '{settings.retention}' resolves to zero — bare prune would "
                "delete every finished session; pass --older-than 0d to do that explicitly"
            )
        try:
            candidates = sessions.prune_candidates(older_than=duration)
        except OSError as error:
            raise AgentProblem(
                f"prune could not read session state: {error}",
                kind=errors.UNAVAILABLE,
                context={"operation": "prune"},
            ) from None
        if candidates and not dry_run:
            interaction.require_confirmation(
                assume_yes,
                message="prune: deleting the sessions it selects needs confirmation",
                hint="Run: acpc prune --dry-run to see them, then repeat with --yes",
            )
            try:
                candidates = sessions.prune_sessions(
                    older_than=duration,
                    dry_run=False,
                    candidates=candidates,
                )
            except OSError as error:
                raise AgentProblem(
                    f"prune could not remove session state: {error}",
                    kind=errors.OPERATION_FAILED,
                    context={"operation": "prune"},
                ) from None
    except AcpcError:
        raise
    except sessions.SessionError as error:
        raise _session_problem(error) from None
    except OSError as error:
        raise AgentProblem(
            f"prune could not read session state: {error}",
            kind=errors.UNAVAILABLE,
            context={"operation": "prune"},
        ) from None
    except (config.ConfigError, ValueError) as error:
        raise UsageProblem(str(error)) from None

    session_ids = [meta.session_id for meta in candidates]
    payload = {
        "targets": session_ids,
        "changed": bool(session_ids) and not dry_run,
        "requires_confirmation": bool(session_ids),
    }
    _emit_maintenance_result(
        payload,
        selected_format=selected_format,
        text="\n".join(session_ids) + "\n" if session_ids else None,
        summary=f"prune {'would remove' if dry_run else 'removed'} {len(session_ids)} session(s)",
    )


def _load_view_session(selector: str) -> sessions.SessionMeta:
    """Verify liveness before a targeted view reports a session."""
    session_id = selector
    try:
        # `last` is a convenience for whoever is at the keyboard, so it turns
        # on the same rule as every other question acpc puts to a person.
        # Collecting the output in a file does not move that person away.
        session_id = sessions.resolve_selector(
            selector, allow_last=interaction.interactive_context()
        )
        return sessions.load(session_id)
    except sessions.SessionError as error:
        problem = _session_problem(error)
        if isinstance(error, sessions.SessionNotFound):
            problem = problem.with_context(session_id=selector, status=None)
        else:
            problem = problem.with_context(session_id=session_id)
        raise problem from None


async def _collect_preparing_sessions(targets: Sequence[str]) -> set[str]:
    """Read the daemon's in-memory preparation register without starting it."""
    preparing: set[str] = set()
    for target in set(targets):
        # Observe, never greet: `status` is read-only and a greeting from a
        # different build would stand the daemon down.
        daemon = await daemon_client.observe(target)
        if daemon is None:
            continue
        try:
            reply = await daemon.status()
        finally:
            await daemon.close()
        values = reply.get("preparing", [])
        if isinstance(values, list):
            preparing.update(value for value in values if isinstance(value, str))
    return preparing


def _status_view_meta(meta: sessions.SessionMeta) -> sessions.SessionMeta:
    """Overlay the daemon-only ``preparing`` phase for status rendering."""
    if meta.target is None:
        return meta
    preparing = asyncio.run(_collect_preparing_sessions([meta.target]))
    return replace(meta, state="preparing") if meta.session_id in preparing else meta


_LOG_DEFAULT_TAIL = 20
_LOG_WAIT_POLL_INTERVAL = 0.05


def _wait_for_new_events(
    transcript_file: transcript.Transcript,
    *,
    since: int,
    tail: int | None,
    limit: int | None,
    timeout: float | None,
    condense: bool = False,
) -> transcript.TranscriptPage | None:
    """Wait for transcript activity at the module's fixed polling interval."""
    deadline = None if timeout is None else time.monotonic() + timeout

    while True:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(_LOG_WAIT_POLL_INTERVAL, remaining))
        else:
            time.sleep(_LOG_WAIT_POLL_INTERVAL)

        available = _read_transcript_page(transcript_file, since=since)
        if available.events:
            return _read_transcript_page(
                transcript_file,
                since=since,
                tail=tail,
                limit=limit,
                condense=condense,
            )


def _read_transcript_page(
    transcript_file: transcript.Transcript,
    *,
    since: int = 0,
    tail: int | None = None,
    limit: int | None = None,
    condense: bool = False,
) -> transcript.TranscriptPage:
    """Turn damaged transcript state into the CLI's one-line usage error."""
    try:
        page = (
            transcript_file.read(since=since)
            if condense
            else transcript_file.read(since=since, tail=tail)
        )
        if not condense:
            if limit is None:
                return page
            selected = page.events[:limit]
            next_cursor = int(selected[-1]["i"]) if selected else since
            return transcript.TranscriptPage(selected, next_cursor)
        selected = render.condense_events(page.events)
        if tail is not None:
            selected = selected[-tail:] if tail else []
        if limit is not None:
            selected = selected[:limit]
        next_cursor = int(selected[-1]["i"]) if selected else since
        return transcript.TranscriptPage(selected, next_cursor)
    except transcript.TranscriptError as error:
        raise AcpcError(str(error), kind=errors.CORRUPT_STATE) from None


def _latest_failure_message(session_id: str) -> str | None:
    """Read the recorded cause for a failed wait summary, if one exists."""
    try:
        events = transcript.Transcript(sessions.transcript_path(session_id)).read().events
    except (OSError, transcript.TranscriptError):
        return None
    for event in reversed(events):
        if event.get("type") == "error" and isinstance(event.get("message"), str):
            return " ".join(event["message"].split())
    return None


@effects.read_only
@schema.output_description(_STATUS_OUTPUT_DESCRIPTION)
@schema.describes(selector=_SELECTOR_HELP)
@main.command(name="status")
@click.argument("selector")
@click.option(
    "--format",
    "format_name",
    type=click.Choice(("text", "json")),
    help=_FORMAT_OUTPUT_HELP,
)
@_color_option()
@_json_option("Emit a JSON status object.")
@click.help_option("-h", "--help")
def status_command(selector: str, format_name: str | None, json_mode: bool) -> None:
    """Show liveness-verified metadata for one session without reading transcripts.

    State is verified against the process behind it, so a ``running`` session
    whose process is gone reads ``unknown`` rather than stale ``running``. A
    daemon-owned continuation in its pre-prompt window is shown as ``preparing``.

    Example: ``acpc status <session-id> --json``
    """
    selected_format = _select_format(format_name, json_mode)
    meta = _status_view_meta(_load_view_session(selector))
    if selected_format == "json":
        _write_stdout(json.dumps(render.status_detail_json(meta), ensure_ascii=False) + "\n")
    else:
        _write_stdout(render.render_status_detail(meta))


def _status_collection(
    format_name: str | None,
    json_mode: bool,
    limit: int,
    plain: bool,
    explicit_limit: bool,
) -> None:
    selected_format = _select_format(format_name, json_mode, plain=plain)
    if selected_format == "plain" and not explicit_limit:
        raise UsageProblem("--plain requires an explicit --limit")
    metas = sessions.list_sessions()
    targets = [meta.target for meta in metas if meta.target is not None]
    preparing = asyncio.run(_collect_preparing_sessions(targets)) if targets else set()
    metas = [
        replace(meta, state="preparing") if meta.session_id in preparing else meta for meta in metas
    ]
    if selected_format == "json":
        _write_stdout(
            json.dumps(render.status_list_json(metas, limit=limit), ensure_ascii=False) + "\n"
        )
    elif selected_format == "plain":
        selected = render.status_items(metas, limit=limit)
        _write_stdout("".join(f"{meta.session_id}\n" for meta in selected))
    else:
        _write_stdout(render.render_status_list(metas, limit=limit))


@effects.read_only
@schema.describes(
    limit="Return at most N sessions; the default is finite and has_more reports the rest.",
    plain=(
        "Print one session id per line; requires explicit --limit and cannot be combined "
        "with --json or a different --format."
    ),
)
@main.command(name="list")
@click.option(
    "--limit",
    type=click.IntRange(min=0),
    default=render.DEFAULT_STATUS_LIMIT,
    show_default=True,
    metavar="N",
    help="Return at most N sessions; the default is finite and has_more reports the rest.",
)
@click.option(
    "--plain",
    is_flag=True,
    help=(
        "Print one session id per line; requires explicit --limit and cannot be combined "
        "with --json or a different --format."
    ),
)
@click.option(
    "--format",
    "format_name",
    type=click.Choice(("text", "json", "plain")),
    help=_FORMAT_COLLECTION_HELP,
)
@_color_option()
@_json_option("Emit a JSON session collection.")
@click.help_option("-h", "--help")
@click.pass_context
def list_command(
    ctx: click.Context,
    limit: int,
    plain: bool,
    format_name: str | None,
    json_mode: bool,
) -> None:
    """List liveness-verified sessions as a bounded collection.
    The window is the first N of this order: active sessions first, newest by creation time, then
    finished sessions, newest by finish time, falling back to creation time when a session has
    none; ties are broken by session id, descending in both groups.

    The collection includes active sessions and the most recent finished
    sessions. A daemon-owned continuation in its pre-prompt window is shown as
    ``preparing`` from the daemon's in-memory register.

    Example: ``acpc list --limit 20 --json``
    """
    explicit_limit = ctx.get_parameter_source("limit") is click.core.ParameterSource.COMMANDLINE
    _status_collection(format_name, json_mode, limit, plain, explicit_limit)


@effects.read_only
@schema.emits_record_stream
@schema.format_defaults(tty="text", non_tty="text")
@schema.describes(
    selector=_SELECTOR_HELP,
    since=(
        "Select events after this cursor; 0 or greater. Emit selected records in transcript "
        "order. With --follow, start after this cursor and continue without a default window."
    ),
    limit=(
        "Emit at most N records from the selected position in transcript order; it does not "
        "select that position. The default non-follow window is the last 20 records. With "
        "--follow, it ends the read after N records. Conflicts with --tail."
    ),
    tail=(
        "Select the last N matching records and emit them in transcript order; 0 or greater. "
        "The default non-follow window is the last 20 records. With --follow, replay those "
        "records, then continue without a default window. Conflicts with --limit."
    ),
)
@main.command(name="log")
@click.argument("selector")
@click.option(
    "--since",
    type=click.IntRange(min=0),
    default=None,
    metavar="N",
    help=(
        "Select events after this cursor and emit selected records in transcript order; with "
        "--follow, start there and continue without a default window."
    ),
)
@click.option(
    "--limit",
    type=click.IntRange(min=0),
    default=None,
    metavar="N",
    help=(
        "Emit at most N records from the selected position; it does not select that position. "
        "The default non-follow window is the last 20 records. With --follow it ends after N "
        "records. Conflicts with --tail."
    ),
)
@click.option(
    "--tail",
    type=click.IntRange(min=0),
    default=None,
    metavar="N",
    help=(
        "Select the last N matching records and emit them in transcript order. The default "
        "non-follow window is the last 20 records. With --follow replay those records, then "
        "continue without a default window. Conflicts with --limit."
    ),
)
@click.option(
    "--prose",
    is_flag=True,
    help="Render full agent messages; mutually exclusive with --json and --format ndjson.",
)
@_json_option("Emit raw transcript events as NDJSON; mutually exclusive with --prose.")
@click.option(
    "--format",
    "format_name",
    type=click.Choice(("text", "ndjson")),
    help=_FORMAT_STREAM_HELP,
)
@_color_option()
@click.option(
    "--max-output",
    type=click.IntRange(min=0),
    default=render.DEFAULT_LOG_MAX_OUTPUT,
    metavar="BYTES",
    help="Cap rendered output bytes; 0 disables the cap.",
)
@click.option(
    "--wait-new",
    is_flag=True,
    help="Wait for new transcript events; mutually exclusive with --follow.",
)
@click.option(
    "--follow",
    is_flag=True,
    help=(
        "Collect transcript-order events until the session ends. Without --limit or --tail, "
        "start after --since (or at the transcript start) with no default window; --tail "
        "replays its last N records first, and --limit ends after N records. Exit 124 on "
        "--timeout, 4 on --max-output; mutually exclusive with --wait-new."
    ),
)
@click.option(
    "--timeout",
    type=TimeoutParamType(allow_zero=True),
    default=None,
    metavar="S",
    help=(
        f"Give up waiting after this duration ({_DURATION_SYNTAX}, or 0) and exit 124; "
        "unbounded by default; "
        "requires --wait-new or --follow."
    ),
)
@click.option("--quiet", is_flag=True, help="Suppress the stderr footer.")
@click.help_option("-h", "--help")
def log_command(
    selector: str,
    since: int | None,
    limit: int | None,
    tail: int | None,
    prose: bool,
    json_mode: bool,
    format_name: str | None,
    max_output: int,
    wait_new: bool,
    follow: bool,
    timeout: float | None,
    quiet: bool,
) -> None:
    """Render selected transcript events and keep metadata on stderr.

    Without --since, --limit, or --tail this shows the last 20 events.

    Example: ``acpc log <session-id> --prose --since 0``
    """
    selected_format = _select_format(format_name, json_mode, stream=True, native_text=True)
    json_mode = selected_format == "ndjson"
    if prose and json_mode:
        raise UsageProblem("--prose and --json are mutually exclusive views")
    if wait_new and follow:
        raise UsageProblem(
            "--wait-new and --follow are mutually exclusive — --follow already waits"
        )
    if limit is not None and tail is not None:
        raise UsageProblem("--limit and --tail are mutually exclusive")
    if timeout is not None and not (wait_new or follow):
        raise UsageProblem("--timeout requires --wait-new or --follow")

    meta = _load_view_session(selector)
    try:
        transcript_file = transcript.Transcript(sessions.transcript_path(meta.session_id))
    except transcript.TranscriptError as error:
        raise AcpcError(
            str(error), kind=errors.CORRUPT_STATE, context={"session_id": meta.session_id}
        ) from None
    explicit_since = since is not None
    cursor = 0 if since is None else since
    # Only an explicit --since is checked, so the extra read that finds the
    # transcript's end is only paid for when there is something to check.
    since_note = None
    if explicit_since:
        highest_cursor = _read_transcript_page(transcript_file).next_cursor
        if cursor > highest_cursor:
            since_note = _since_past_end_note(cursor, highest_cursor)
    selection_tail = tail
    if selection_tail is None and limit is None and not explicit_since and not follow:
        selection_tail = _LOG_DEFAULT_TAIL

    if follow:
        _follow_log(
            meta,
            transcript_file,
            cursor=cursor,
            limit=limit,
            replay_tail=tail,
            prose=prose,
            json_mode=json_mode,
            max_output=max_output,
            timeout=timeout,
            quiet=quiet,
            since_note=since_note,
            condense=not prose and not json_mode,
        )
        return

    _render_log_page(
        meta,
        transcript_file,
        cursor=cursor,
        tail=selection_tail,
        limit=limit,
        prose=prose,
        json_mode=json_mode,
        max_output=max_output,
        wait_new=wait_new,
        explicit_since=explicit_since,
        timeout=timeout,
        quiet=quiet,
        since_note=since_note,
    )


def _select_log_page(
    meta: sessions.SessionMeta,
    transcript_file: transcript.Transcript,
    *,
    cursor: int,
    tail: int | None,
    limit: int | None,
    prose: bool,
    json_mode: bool,
    wait_new: bool,
    explicit_since: bool,
    timeout: float | None,
) -> tuple[sessions.SessionMeta, transcript.TranscriptPage, int, bool, bool]:
    """Select the page `log` reports, waiting once for a new event if asked.

    Returns the (possibly refreshed) session meta, the page, the cursor it
    was read from (`--wait-new` may advance it before the read), and whether
    the wait timed out / gave up waiting for a new event.
    """
    selection_tail = tail
    if wait_new and not explicit_since:
        cursor = _read_transcript_page(transcript_file).next_cursor
    page = _read_transcript_page(
        transcript_file,
        since=cursor,
        tail=selection_tail,
        limit=limit,
        condense=not prose and not json_mode,
    )
    timed_out = False
    gave_up_waiting = False
    if wait_new and not page.events and limit != 0:
        if meta.state in vocab.FINISHED_STATES:
            # SPEC `--wait-new`: a finished session cannot produce new
            # activity, so the call returns at once (the `logs -f`
            # convention: following a stopped stream ends).
            timed_out = True
        else:
            waited = _wait_for_new_events(
                transcript_file,
                since=cursor,
                tail=selection_tail,
                limit=limit,
                timeout=timeout,
                condense=not prose and not json_mode,
            )
            if waited is None:
                timed_out = True
                gave_up_waiting = True
            else:
                page = waited
        if timed_out:
            # SPEC `--wait-new`: the timeout exit still prints the footer.  A
            # bare 124 with zero bytes is indistinguishable from a hang, and
            # the footer is what tells a poller the session already finished.
            page = transcript.TranscriptPage([], cursor)
    if wait_new:
        # The wait may have outlived the state this command started with; the
        # footer is the caller's termination signal, so it must be current.
        meta = _load_view_session(meta.session_id)
    return meta, page, cursor, timed_out, gave_up_waiting


def _emit_log_page(
    meta: sessions.SessionMeta,
    transcript_file: transcript.Transcript,
    page: transcript.TranscriptPage,
    *,
    cursor: int,
    prose: bool,
    json_mode: bool,
    max_output: int,
    quiet: bool,
    since_note: str | None,
    timeout: float | None,
    timed_out: bool,
    gave_up_waiting: bool,
) -> None:
    """Render one selected page and its footer; raise on a `--wait-new` timeout."""
    full_last_message = meta.state in {"failed", "unknown"}
    rendered = render.render_events(
        page.events,
        prose=prose,
        json_mode=json_mode,
        max_output=max_output,
        transcript_path=sessions.transcript_path(meta.session_id),
        cursor=cursor,
        full_last_message=full_last_message,
    )
    _write_stdout(rendered.text)
    if not quiet:
        if rendered.truncation_note is not None:
            _echo_metadata(rendered.truncation_note)
        if since_note is not None:
            _echo_metadata(since_note)
        if gave_up_waiting and meta.state not in vocab.FINISHED_STATES:
            _echo_metadata(_still_running_note(meta.session_id, timeout))
        # The cursor is a global transcript index, not an event count.  Read
        # the actual end so an empty page after a large --since stays honest.
        event_count = _read_transcript_page(transcript_file).next_cursor
        footer = render.format_log_footer(
            meta,
            cursor=rendered.next_cursor,
            event_count=event_count,
            page_start=rendered.first_event,
            page_end=rendered.last_event,
        )
        _echo_metadata(footer)
    if timed_out:
        raise AcpcError(
            f"no new events on session {meta.session_id} within the window",
            kind=errors.TIMEOUT,
            exit_code=vocab.EXIT_TIMEOUT,
            retryable=True,
            hint=f"Run: acpc log {meta.session_id} --wait-new --since {rendered.next_cursor}",
            context={"session_id": meta.session_id, "cursor": rendered.next_cursor},
        )


def _render_log_page(
    meta: sessions.SessionMeta,
    transcript_file: transcript.Transcript,
    *,
    cursor: int,
    tail: int | None,
    limit: int | None,
    prose: bool,
    json_mode: bool,
    max_output: int,
    wait_new: bool,
    explicit_since: bool,
    timeout: float | None,
    quiet: bool,
    since_note: str | None,
) -> None:
    """Print one page of a transcript, optionally waiting for it to exist.

    This is `log` without `--follow`: a single read that either has events
    already or waits once for the first new one, then the footer that tells a
    poller where to resume and whether the session is still going.
    """
    meta, page, cursor, timed_out, gave_up_waiting = _select_log_page(
        meta,
        transcript_file,
        cursor=cursor,
        tail=tail,
        limit=limit,
        prose=prose,
        json_mode=json_mode,
        wait_new=wait_new,
        explicit_since=explicit_since,
        timeout=timeout,
    )
    _emit_log_page(
        meta,
        transcript_file,
        page,
        cursor=cursor,
        prose=prose,
        json_mode=json_mode,
        max_output=max_output,
        quiet=quiet,
        since_note=since_note,
        timeout=timeout,
        timed_out=timed_out,
        gave_up_waiting=gave_up_waiting,
    )


def _sleep_until(deadline: float | None) -> None:
    """Sleep one poll interval, never past the deadline."""
    if deadline is None:
        time.sleep(_LOG_WAIT_POLL_INTERVAL)
        return
    time.sleep(min(_LOG_WAIT_POLL_INTERVAL, max(0.0, deadline - time.monotonic())))


def _emit_follow_page(
    events: Sequence[Mapping[str, Any]],
    *,
    prose: bool,
    json_mode: bool,
    max_output: int,
    used: int,
    transcript_path: Path,
    cursor: int,
) -> tuple[int, int, bool, int | None, int | None, str | None]:
    """Render one page inside the follow budget.

    SPEC `log --follow`: `--max-output` budgets the whole stream, so each page
    is rendered against what is left of it.  Truncation diagnostics stay on
    stderr, keeping stdout a stream of transcript records.
    """
    budget = 0 if max_output == 0 else max(1, max_output - used)
    rendered = render.render_events(
        events,
        prose=prose,
        json_mode=json_mode,
        max_output=budget,
        transcript_path=transcript_path,
        cursor=cursor,
    )
    _write_stdout(rendered.text)
    return (
        rendered.next_cursor,
        used + len(rendered.text.encode("utf-8")),
        rendered.truncated,
        rendered.first_event,
        rendered.last_event,
        rendered.truncation_note,
    )


def _follow_start_cursor(
    transcript_file: transcript.Transcript,
    *,
    since: int,
    tail: int | None,
    condense: bool = False,
) -> int:
    """Turn the replay depth into the cursor the follow starts from.

    SPEC `log --follow`: the replay is a start point, not a filter on the
    stream — an explicit `--limit 0` means "from here on", so with nothing to replay the
    cursor moves to the transcript's current end rather than staying put and
    letting the first page hand back the whole history.
    """
    replay = _read_transcript_page(transcript_file, since=since, tail=tail, condense=condense)
    if replay.events:
        first = replay.events[0]
        start = first.get("_group_start", first["i"])
        return int(start) - 1
    return _read_transcript_page(transcript_file, since=since).next_cursor


def _follow_log(
    meta: sessions.SessionMeta,
    transcript_file: transcript.Transcript,
    *,
    cursor: int,
    limit: int | None,
    replay_tail: int | None,
    prose: bool,
    json_mode: bool,
    max_output: int,
    timeout: float | None,
    quiet: bool,
    since_note: str | None,
    condense: bool,
) -> None:
    """Collect events until the session ends, the timeout expires, or the
    budget runs out — SPEC `log --follow`'s three endings, one exit code each."""
    transcript_path = sessions.transcript_path(meta.session_id)
    deadline = None if timeout is None else time.monotonic() + timeout
    if limit is None:
        cursor = _follow_start_cursor(
            transcript_file, since=cursor, tail=replay_tail, condense=condense
        )
    used = 0
    exhausted = False
    timed_out = False
    read_count = 0
    page_start: int | None = None
    page_end: int | None = None
    truncation_note: str | None = None

    while True:
        page = _read_transcript_page(transcript_file, since=cursor)
        if page.events:
            if limit is not None:
                remaining = limit - read_count
                if remaining <= 0:
                    break
                page = transcript.TranscriptPage(page.events[:remaining], page.events[0]["i"] - 1)
            (
                cursor,
                used,
                exhausted,
                rendered_start,
                rendered_end,
                page_note,
            ) = _emit_follow_page(
                page.events,
                prose=prose,
                json_mode=json_mode,
                max_output=max_output,
                used=used,
                transcript_path=transcript_path,
                cursor=cursor,
            )
            if rendered_start is not None:
                read_count += len(page.events)
                if page_start is None:
                    page_start = rendered_start
                page_end = rendered_end
            if page_note is not None:
                truncation_note = page_note
            if exhausted:
                break
            if limit is not None and read_count >= limit:
                break
            continue
        meta = _load_view_session(meta.session_id)
        if meta.state in vocab.FINISHED_STATES:
            # Events are appended before the final state is recorded, so a page
            # read after observing that state is the complete remainder.
            if _read_transcript_page(transcript_file, since=cursor).events:
                continue
            break
        if deadline is not None and time.monotonic() >= deadline:
            timed_out = True
            break
        _sleep_until(deadline)

    _finish_follow_log(
        meta,
        transcript_file,
        cursor=cursor,
        page_start=page_start,
        page_end=page_end,
        exhausted=exhausted,
        timed_out=timed_out,
        quiet=quiet,
        since_note=since_note,
        timeout=timeout,
        max_output=max_output,
        truncation_note=truncation_note,
    )


def _finish_follow_log(
    meta: sessions.SessionMeta,
    transcript_file: transcript.Transcript,
    *,
    cursor: int,
    page_start: int | None,
    page_end: int | None,
    exhausted: bool,
    timed_out: bool,
    quiet: bool,
    since_note: str | None,
    timeout: float | None,
    max_output: int,
    truncation_note: str | None,
) -> None:
    """Render follow's current footer and raise its bounded-ending status."""
    # The follow outlived the state it started with; the footer is the caller's
    # termination signal, so it has to be current.
    meta = _load_view_session(meta.session_id)
    if not quiet:
        if truncation_note is not None:
            _echo_metadata(truncation_note)
        if since_note is not None:
            _echo_metadata(since_note)
        if timed_out and meta.state not in vocab.FINISHED_STATES:
            _echo_metadata(_still_running_note(meta.session_id, timeout))
        if exhausted:
            _echo_metadata(_budget_exhausted_note(meta.session_id, max_output, cursor))
        event_count = _read_transcript_page(transcript_file).next_cursor
        _echo_metadata(
            render.format_log_footer(
                meta,
                cursor=cursor,
                event_count=event_count,
                page_start=page_start,
                page_end=page_end,
            )
        )
    if exhausted:
        # Not a failure: the caller asked for at most `--max-output` bytes and
        # got them. Reaching a read limit ends that read, it does not end the
        # transcript, so the way out is the budget code and the footer's
        # cursor — no envelope, because nothing went wrong.
        raise SystemExit(vocab.EXIT_BUDGET)
    if timed_out:
        raise AcpcError(
            f"gave up following session {meta.session_id}",
            kind=errors.TIMEOUT,
            exit_code=vocab.EXIT_TIMEOUT,
            retryable=True,
            hint=f"Run: acpc log {meta.session_id} --follow --since {cursor}",
            context={"session_id": meta.session_id, "cursor": cursor},
        )


def _budget_exhausted_note(session_id: str, max_output: int, cursor: int) -> str:
    """SPEC exit codes: a cut stream is not a completed follow, so the way out
    says what stopped it and how to pick the stream back up."""
    return (
        f"-- stopped: --max-output {max_output} exhausted — resume with: "
        f"acpc log {session_id} --follow --since {cursor}"
    )


def _since_past_end_note(since: int, highest_cursor: int) -> str:
    """SPEC `log --since`: report an explicit cursor beyond the transcript."""
    return f"-- --since {since} is past the transcript's end (highest cursor: {highest_cursor})"


def _still_running_note(session_id: str, timeout: float | None) -> str:
    """SPEC exit codes: a wait timeout never touches the session, and the
    caller deciding what to do next needs both halves said out loud."""
    waited = "" if timeout is None else f" after {timeout:g}s"
    return (
        f"-- still running (gave up waiting{waited}) — session continues; "
        f"acpc cancel {session_id} to cancel"
    )


def _resolve_run_call(
    agent: str,
    *,
    model: str | None,
    effort: str | None,
    mode: str | None,
    permissions: str | None,
    home: str | None,
) -> CallResolution:
    try:
        registry = AgentRegistry()
        resolution = registry.resolve_call(
            agent,
            model=model,
            effort=effort,
            mode=mode,
            permissions=permissions,
            home=home,
        )
        if permissions is None:
            _warn_permission_alias(registry.permission_alias(agent))
        _warn_unlisted_effort_model(resolution)
        return resolution
    except RegistryError as error:
        raise _registry_problem(error) from None


def _run_preview(
    resolution: CallResolution,
    *,
    permissions: str | None,
    background: bool,
    cwd: str,
    selected_format: str,
) -> None:
    policy, permissions_clamp, permissions_source = _resolve_permissions(
        permissions, resolution, background=background, preview=True
    )
    previewed = replace(resolution, permissions=policy, permissions_clamp=permissions_clamp)
    if policy is not None:
        previewed = _select_resolution(previewed)
    payload = runner.resolution_payload(previewed, cwd=cwd, permissions_source=permissions_source)
    _emit_resolution(payload, json_mode=selected_format == "json")


@effects.read_only
@schema.format_defaults(tty="text", non_tty="text")
@schema.describes(
    agent=(
        "Registry entry whose call configuration is resolved without dispatching a turn; "
        "an adapter or variant, as `acpc agents list` lists them."
    ),
)
@main.command(name="resolve")
@_resolution_options
@click.argument("agent")
@click.option(
    "--format",
    "format_name",
    type=click.Choice(("text", "json")),
    help=_FORMAT_NATIVE_HELP,
)
@_json_option("Emit the resolved call as JSON.")
@_color_option()
@click.help_option("-h", "--help")
def resolve_command(
    agent: str,
    cwd: str | None,
    model: str | None,
    effort: str | None,
    permissions: str | None,
    mode: str | None,
    home: str | None,
    format_name: str | None,
    json_mode: bool,
) -> None:
    """Show how an agent call resolves, without dispatching a session.

    The output records the selected values, their provenance, the adapter command
    and the working directory that a later ``run`` would use.

    Example: ``acpc resolve codex --permissions execute``
    """
    selected_format = _select_format(format_name, json_mode, native_text=True)
    permissions = _normalize_permission(permissions)
    resolution = _resolve_run_call(
        agent,
        model=model,
        effort=effort,
        mode=mode,
        permissions=permissions,
        home=home,
    )
    resolved_cwd = str(Path(cwd).expanduser().resolve()) if cwd else os.getcwd()
    _run_preview(
        resolution,
        permissions=permissions,
        background=False,
        cwd=resolved_cwd,
        selected_format=selected_format,
    )


def _claim_run_alias(alias: str | None) -> None:
    if alias is None:
        return
    try:
        warning = sessions.claim_name(alias)
    except sessions.SessionNameError as error:
        raise _session_problem(error) from None
    if warning:
        click.echo(f"-- {warning}", err=True)


def _create_run_session(
    resolution: CallResolution,
    *,
    prompt: str,
    cwd: str,
    alias: str | None,
    permissions_source: str | None,
) -> sessions.SessionMeta:
    settings = config.load_config()
    runner.auto_prune(settings.retention_seconds)
    try:
        return sessions.create_session(
            entry=resolution.entry.entry,
            base_adapter=resolution.entry.base_adapter,
            prompt=prompt,
            resolution=runner.session_resolution(
                resolution,
                cwd=cwd,
                permissions_source=permissions_source,
            ),
            target=runner.call_target(resolution),
            name=alias,
        )
    except sessions.SessionError as error:
        raise _session_problem(error) from None


def _run_foreground(
    meta: sessions.SessionMeta,
    request: runner.TurnRequest,
    *,
    output_file: str | None,
    selected_format: str,
    max_output: int,
    quiet: bool,
) -> None:
    if not quiet:
        _echo_metadata(output.format_session_line(meta))
    try:
        outcome = runner.execute_turn(meta.session_id, request)
    except runner.RunnerError as error:
        raise _runner_problem(error).with_context(session_id=meta.session_id) from None
    if outcome.wait_timed_out:
        _emit_wait_timeout(meta.session_id, output_file, turn=meta.turns)
    final = sessions.read_meta(meta.session_id)
    presentation = _select_presentation(selected_format)
    result = output.render_result(
        final,
        outcome.answer,
        json_mode=presentation == "json",
        tagged=presentation == "tagged",
        max_output=max_output,
        changed=True,
    )
    _emit_turn_result(
        result,
        output_file=output_file,
        json_mode=presentation == "json",
    )
    if outcome.state == "detached":
        _echo_metadata(
            f"-- detached, still RUNNING: {meta.session_id}"
            f" — answer: acpc wait {meta.session_id} · cancel: acpc cancel {meta.session_id}"
        )
        _end_turn_for(meta.session_id, outcome)
    if not quiet:
        _echo_metadata(
            output.format_summary(
                final,
                route_note=_route_note(outcome),
                truncated_output_file=result.output_file if result.truncated else None,
            )
        )
    _end_turn_for(meta.session_id, outcome)


@effects.non_idempotent
@schema.format_defaults(tty="text", non_tty="text")
@schema.output_description(_ANSWER_OUTPUT_DESCRIPTION)
@schema.reads_stdin("prompt_text")
@schema.describes(
    agent=(
        "Registry entry to dispatch: an adapter or a variant, as `acpc agents list` lists them."
    ),
    prompt_text=_PROMPT_HELP,
    output_file=_OUTPUT_FILE_DESCRIPTION,
)
@main.command(name="run")
@_resolution_options
@click.argument("agent")
@click.argument("prompt_text", required=False)
@click.option(
    "--prompt-file",
    "prompt_file",
    metavar="FILE",
    help=(
        f"Read the prompt from a file; at most {_PROMPT_LIMIT_HELP}. One of three "
        "prompt sources with the argument and `-`, exactly one of which must be given."
    ),
)
@click.option("--output-file", "output_file", metavar="FILE", help=_OUTPUT_FILE_HELP)
@click.option(
    "--format",
    "format_name",
    type=click.Choice(("text", "json")),
    help=_FORMAT_NATIVE_HELP,
)
@click.option(
    "--timeout",
    type=TimeoutParamType(),
    metavar="S",
    help=_TIMEOUT_HELP,
)
@click.option(
    "--cancel-after",
    type=TimeoutParamType(),
    metavar="S",
    help=(
        f"Cancel the session after this duration ({_DURATION_SYNTAX}); unlike --timeout, "
        "this changes the work itself."
    ),
)
@click.option(
    "--name",
    "alias",
    metavar="ALIAS",
    help="Human-typeable handle for this session; `last` is reserved as a selector.",
)
@click.option(
    "--max-output",
    type=click.IntRange(min=0),
    default=output.DEFAULT_MAX_OUTPUT,
    metavar="BYTES",
    help="Cap on stdout bytes; 0 disables the cap.",
)
@click.option(
    "--background",
    "--bg",
    "background",
    is_flag=True,
    help=(
        "Dispatch and return the session id; without --permissions it first asks at the "
        "terminal which policy to detach with, and it refuses --permissions ask."
    ),
)
@click.option("--quiet", is_flag=True, help="Suppress the stderr summary line.")
@_json_option(_JSON_CHOICE_HELP)
@_color_option()
@click.help_option("-h", "--help")
def run_command(
    agent: str,
    prompt_text: str | None,
    prompt_file: str | None,
    cwd: str | None,
    model: str | None,
    effort: str | None,
    permissions: str | None,
    mode: str | None,
    home: str | None,
    output_file: str | None,
    format_name: str | None,
    timeout: float | None,
    cancel_after: float | None,
    alias: str | None,
    background: bool,
    max_output: int,
    quiet: bool,
    json_mode: bool,
) -> None:
    """Dispatch one agent; block by default, or use ``--background`` or ``--bg`` to detach,
    and print an observed result even when the turn fails.

    One prompt source is required, and prompts over 1 MiB are rejected before
    session creation. A call that observed no turn prints no result. Permission
    defaults follow the TTY and output format.

    Example: ``acpc run codex "Fix the failing test" --permissions execute``
    """
    selected_format = _select_format(format_name, json_mode, native_text=True)
    if background and timeout is not None:
        raise UsageProblem("--timeout only bounds waiting; use --cancel-after with --background")
    permissions = _normalize_permission(permissions)
    resolution = _resolve_run_call(
        agent,
        model=model,
        effort=effort,
        mode=mode,
        permissions=permissions,
        home=home,
    )
    defaulted_permissions = permissions is None and resolution.permissions is None
    resolved_cwd = str(Path(cwd).expanduser().resolve()) if cwd else os.getcwd()
    prompt = _read_prompt(
        prompt_text,
        prompt_file,
        operation="run",
        hint="Run: acpc run AGENT PROMPT",
    )
    try:
        runner.adapter_command(resolution)
    except runner.RunnerError as error:
        raise _runner_problem(error) from None
    _claim_run_alias(alias)
    policy, permissions_clamp, permissions_source = _resolve_permissions(
        permissions, resolution, background=background
    )
    resolution = _select_resolution(
        replace(resolution, permissions=policy, permissions_clamp=permissions_clamp)
    )
    meta = _create_run_session(
        resolution,
        prompt=prompt,
        cwd=resolved_cwd,
        alias=alias,
        permissions_source=permissions_source or ("default" if defaulted_permissions else None),
    )
    request = runner.TurnRequest(
        resolution=resolution,
        prompt=prompt,
        cwd=resolved_cwd,
        wait_timeout=timeout,
        cancel_after=cancel_after,
        permission_prompt=_tty_permission_prompt if policy == "ask" else None,
    )

    if background:
        _dispatch_background(
            meta.session_id,
            request,
            presentation=_select_presentation(selected_format),
            output_file=output_file,
            max_output=max_output,
        )
        return
    _run_foreground(
        meta,
        request,
        output_file=output_file,
        selected_format=selected_format,
        max_output=max_output,
        quiet=quiet,
    )


# What a finished turn means when it did not end in `succeeded`. The exit code
# already says which ending it was; the kind is what a caller matches on.
#
# A command that waits for the turn it started reports any terminal state
# other than success as `operation_failed`: the operation ran to an end and
# that end was not the one asked for.  `interrupted` is reserved for a command
# that was itself cut short — Ctrl-C — not for observing a session somebody
# else cancelled, which is a finished operation like any other.  The state
# that separates them travels in `context.status`.
_TURN_FAILURE_KINDS = {
    "failed": errors.OPERATION_FAILED,
    "unknown": errors.OPERATION_FAILED,
    "canceled": errors.OPERATION_FAILED,
    # The daemon still owns the turn: acpc stopped watching without seeing how
    # it ends, and saying "failed" would claim knowledge it does not have.
    "detached": errors.OUTCOME_UNKNOWN,
    "terminated": errors.OPERATION_FAILED,
}

# States `runner.exit_code_for` settles on their own, before it looks at
# `stop_reason`.  `_end_turn` has to weigh the two in the same order, or a
# turn could leave with one story in its exit code and another in its kind.
_SIGNAL_STATES = frozenset({"canceled", "detached", "terminated"})

_TURN_FAILURE_MESSAGES = {
    "failed": "session {id} failed",
    "unknown": "session {id} has an unknown outcome: the process behind it is gone",
    "canceled": "session {id} was canceled",
    "detached": "acpc detached from session {id}; the turn is still running",
    "terminated": "session {id} was terminated",
}


def _end_turn_for(session_id: str, outcome: runner.TurnOutcome) -> NoReturn:
    """Leave on a turn this command ran, reading the ending off its outcome."""
    _end_turn(
        session_id,
        outcome.state,
        outcome.stop_reason,
        outcome.exit_code,
        interrupted=outcome.interrupted,
    )


def _end_turn(
    session_id: str,
    state: str,
    stop_reason: str | None,
    exit_code: int,
    *,
    interrupted: bool = False,
) -> NoReturn:
    """Leave an answer-printing command, saying in one shape how it ended.

    The exit code has always mirrored the session result; this adds the
    machine-readable reason next to it, carrying the session id so a caller
    that was cut off mid-turn can still reach the work (R7a).

    `interrupted` is the one thing the session's own state cannot tell: a
    `cancelled` session looks the same whoever cancelled it, and a command
    stopped by its caller's Ctrl-C is a different failure from a command that
    watched an operation end badly.
    """
    state = vocab.normalize_session_state(state)
    if exit_code == vocab.EXIT_OK:
        raise SystemExit(exit_code)
    if interrupted:
        raise _interrupted(session_id=session_id)
    if state not in _SIGNAL_STATES and stop_reason == "permission_denied":
        kind = errors.PERMISSION_DENIED
        message = f"session {session_id} was denied a permission it needed"
    else:
        kind = _TURN_FAILURE_KINDS.get(state, errors.OPERATION_FAILED)
        template = _TURN_FAILURE_MESSAGES.get(state, "session {id} did not finish")
        message = template.format(id=session_id)
    if state in {"failed", "unknown"} and (detail := _latest_failure_message(session_id)):
        message = f"{message}: {detail}"
    hint = (
        f"Run: acpc wait {session_id}"
        if state == "detached"
        else f"Run: acpc log {session_id} --since 0"
    )
    context: dict[str, Any] = {"session_id": session_id}
    if kind == errors.OPERATION_FAILED:
        # `operation_failed` promises the identifier *and* the state it ended
        # in: the kind says the operation finished badly, and `status` is what
        # says which badly — a refusal and a cancellation need different next
        # steps and share this kind.
        context["status"] = state
    raise AcpcError(
        message,
        kind=kind,
        exit_code=exit_code,
        hint=hint,
        context=context,
    )


def _route_note(outcome: runner.TurnOutcome) -> str | None:
    """Fold the routing and queueing notes into the one `--` summary line.

    SPEC counts `--` lines: a queued turn and a direct-child fallback are both
    notes about how the call was served, so they ride the same line.
    """
    notes = [note for note in (outcome.route_note, _queue_note(outcome)) if note]
    return " · ".join(notes) if notes else None


def _queue_note(outcome: runner.TurnOutcome) -> str | None:
    return "queued for a daemon slot" if outcome.queued else None


def _dispatch_background(
    session_id: str,
    request: runner.TurnRequest,
    *,
    presentation: str,
    output_file: str | None = None,
    max_output: int = output.DEFAULT_MAX_OUTPUT,
    emit_failure_result: bool = True,
    extra: Callable[[sessions.SessionMeta], dict[str, Any]] | None = None,
) -> None:
    """Hand the turn to the daemon and print what the caller needs to find it.

    SPEC `run --bg`: on a terminal, stdout is exactly the session id and its
    directory; off one, `text` prints the tagged receipt (V6a) instead.
    """
    problem = asyncio.run(runner.dispatch_background(session_id, request))
    if problem is not None:
        # The session exists by now: the caller has to be able to reach it
        # even though the dispatch that would have run it failed (R7a).  No
        # No turn was observed, so this call has no result to write anywhere
        # under the answer-result contract (O2e).
        if not emit_failure_result and output_file is not None:
            output.write_output_file(output_file, "")
        if isinstance(problem, daemon_client.DaemonUnavailable):
            # SPEC.md `daemon`: no daemon could serve the call at all — kept
            # apart from an ordinary agent failure the daemon reported itself.
            raise AgentProblem(
                problem.reason, kind=errors.UNAVAILABLE, context={"session_id": session_id}
            )
        raise AgentProblem(problem, context={"session_id": session_id})
    meta = sessions.read_meta(session_id)
    result = output.render_result(
        meta,
        json_mode=presentation == "json",
        tagged=presentation == "tagged",
        background=True,
        max_output=max_output,
        changed=True,
        include_partial=emit_failure_result,
        extra=extra(meta) if extra is not None else None,
    )
    if not _write_rendered_file(output_file, result):
        _write_stdout(result.text)


# SPEC.md `continue`: a session with nothing in flight to correct in place —
# `starting`, `preparing`, `waiting` — has no running turn for an in-place
# `steer` to reach, so the hinted command adds `--steer-mode cancel-then-start`
# and never fails with a second `conflict`.
_STEER_NEEDS_CANCEL_THEN_START = frozenset({"starting", "preparing", "waiting"})


def _continue_conflict_hint(meta: sessions.SessionMeta) -> str:
    steer_mode_flag = (
        " --steer-mode cancel-then-start" if meta.state in _STEER_NEEDS_CANCEL_THEN_START else ""
    )
    return (
        f'Run: acpc steer {meta.session_id} "<instruction>"{steer_mode_flag} to correct the '
        f"turn, or acpc wait {meta.session_id} to wait for it"
    )


def _continuation_prompt(meta: sessions.SessionMeta) -> str:
    """SPEC `continue`: what a call with no message at all resumes.

    Reached only once the caller supplied neither ``PROMPT``, ``-`` nor
    ``--prompt-file`` and the session is not active (checked first). A
    `succeeded` turn left nothing unfinished, so the call is a usage error
    rather than a silent empty-prompt turn; every other finished state —
    `canceled`, `failed` or `unknown` — sends acpc's own continuation
    instruction naming that state as the cause
    (`runner.continuation_instruction`).
    """
    if meta.state == "succeeded":
        raise UsageProblem(
            f"session {meta.session_id} succeeded on its last turn — nothing to continue",
            hint="the last turn completed; give continue a message",
        )
    return runner.continuation_instruction(meta.state)


@effects.non_idempotent
@schema.format_defaults(tty="text", non_tty="text")
@schema.output_description(_CONTINUE_OUTPUT_DESCRIPTION)
@schema.reads_stdin("prompt_text")
@schema.describes(
    selector=_SELECTOR_HELP,
    prompt_text=_CONTINUE_PROMPT_HELP,
    output_file=_OUTPUT_FILE_DESCRIPTION,
)
@main.command(name="continue")
@click.argument("selector")
@click.argument("prompt_text", required=False)
@click.option(
    "--prompt-file",
    "prompt_file",
    metavar="FILE",
    help=(
        f"Read the prompt from a file; at most {_PROMPT_LIMIT_HELP}. One of three "
        "prompt sources with the argument and `-`, exactly one of which must be given."
    ),
)
@click.option("--output-file", "output_file", metavar="FILE", help=_OUTPUT_FILE_HELP)
@click.option(
    "--format",
    "format_name",
    type=click.Choice(("text", "json")),
    help=_FORMAT_NATIVE_HELP,
)
@click.option(
    "--permissions",
    type=click.Choice(_PERMISSION_CHOICES),
    metavar="P",
    help=(
        "Permission scale for this and later turns: none, read, edit, execute, all or ask; "
        "the run default is ask when acpc could put the question — stdin and stdout both "
        "terminals, no --json, NO_INPUT unset — and read in every other case. "
        "Without this flag, continue reuses its stored policy. write and prompt are "
        "deprecated aliases for execute and ask; --permissions re-runs mode selection against "
        "the adapter's current [modes]."
    ),
)
@click.option(
    "--background",
    "--bg",
    "background",
    is_flag=True,
    help=(
        "Dispatch and return the session id; refused while the policy in effect is ask, "
        "which needs someone still attached to answer."
    ),
)
@click.option(
    "--timeout",
    type=TimeoutParamType(),
    metavar="S",
    help=_TIMEOUT_HELP,
)
@click.option(
    "--cancel-after",
    type=TimeoutParamType(),
    metavar="S",
    help=(
        f"Cancel the session after this duration ({_DURATION_SYNTAX}); unlike --timeout, "
        "this changes the work itself."
    ),
)
@click.option(
    "--max-output",
    type=click.IntRange(min=0),
    default=output.DEFAULT_MAX_OUTPUT,
    metavar="BYTES",
    help="Cap on stdout bytes; 0 disables the cap.",
)
@click.option("--quiet", is_flag=True, help="Suppress the stderr summary line.")
@_json_option(_JSON_CHOICE_HELP)
@_color_option()
@click.help_option("-h", "--help")
def continue_command(
    selector: str,
    prompt_text: str | None,
    prompt_file: str | None,
    output_file: str | None,
    format_name: str | None,
    background: bool,
    timeout: float | None,
    cancel_after: float | None,
    max_output: int,
    quiet: bool,
    json_mode: bool,
    permissions: str | None,
) -> None:
    """Continue a finished session and print an observed result, even on failure.

    Model, effort, mode, permissions and home come from the session, not from
    re-resolving the agent entry — editing an entry never changes a session
    mid-conversation. A canceled session keeps its adapter context when the
    adapter supports continuation. A call that observed no turn prints no
    result. ``--permissions`` is the one ``run`` resolution flag ``continue``
    accepts: it applies to this turn and every turn after it. ``PROMPT`` is
    optional after an interrupted turn — canceled, failed or unknown — where
    it defaults to acpc's own continuation instruction; a succeeded turn
    still requires a message.

    Example: ``acpc continue <session-id> "Run the tests again"``
    """
    selected_format = _select_format(format_name, json_mode, native_text=True)
    if background and timeout is not None:
        raise UsageProblem("--timeout only bounds waiting; use --cancel-after with --background")
    permissions = _normalize_permission(permissions)

    has_message = prompt_text is not None or prompt_file is not None
    meta = _load_view_session(selector)
    if meta.is_active:
        raise AcpcError(
            f"session {meta.session_id} is {meta.state} — it cannot be continued",
            kind=errors.CONFLICT,
            retryable=True,
            hint=_continue_conflict_hint(meta),
            context={"session_id": meta.session_id},
        )
    prompt = (
        _read_prompt(
            prompt_text,
            prompt_file,
            operation="continue",
            hint="Run: acpc continue SESSION_ID PROMPT",
        )
        if has_message
        else _continuation_prompt(meta)
    )
    _dispatch_follow_up(
        meta,
        prompt,
        output_file=output_file,
        permissions=permissions,
        background=background,
        timeout=timeout,
        cancel_after=cancel_after,
        max_output=max_output,
        quiet=quiet,
        presentation=_select_presentation(selected_format),
    )


def _follow_up_request(
    session_id: str,
    prompt: str,
    *,
    permissions: str | None,
    background: bool,
    timeout: float | None,
    cancel_after: float | None,
) -> tuple[sessions.SessionMeta, runner.TurnRequest]:
    """Build the next turn on a finished session from its stored resolution.

    The session is re-read here rather than reusing the caller's snapshot: the
    policy, the mode selection and the request all have to describe the same
    version of the record.
    """
    try:
        current = sessions.read_meta(session_id)
        stored_policy = _stored_permission_policy(current)
        transcript.Transcript(sessions.transcript_path(session_id)).read()
    except (sessions.SessionError, transcript.TranscriptError) as error:
        raise _follow_up_problem(error, session_id) from None
    policy = permissions if permissions is not None else stored_policy
    policy, permissions_clamp = _clamp_inherited_ceiling(policy)
    interactive = interaction.interactive_context() and not background
    if policy == "ask" and not interactive:
        # Same split as `run`: each cause is a different situation, and
        # "needs a terminal" is baffling advice to someone sitting at one.
        cause = (
            "cannot be continued with --bg, which returns before a request could be answered"
            if background
            else interaction.why_not_interactive()
        )
        raise UsageProblem(
            f"this session uses --permissions ask, which {cause}; "
            "continue it from a terminal or start a new session with another policy"
        )
    try:
        stored_resolution = runner.resolution_from_session(current)
    except runner.RunnerError as error:
        raise _follow_up_problem(error, session_id) from None
    selection: CallResolution | None = None
    if (
        permissions is not None
        or stored_resolution.mode_spec is None
        or permissions_clamp is not None
    ):
        selection = _continue_selection(current, policy)
        selection = replace(selection, permissions_clamp=permissions_clamp)
    updated_resolution = (
        _updated_session_resolution(
            current,
            selection,
            policy=policy,
            policy_changed=permissions is not None,
        )
        if selection is not None
        else None
    )
    request_resolution = (
        runner.resolution_from_session(replace(current, resolution=updated_resolution))
        if updated_resolution is not None
        else None
    )
    try:
        request = runner.continue_request(
            current,
            prompt,
            wait_timeout=timeout,
            cancel_after=cancel_after,
            permission_prompt=(_tty_permission_prompt if policy == "ask" and interactive else None),
            resolution=request_resolution,
            defer_rotation=True,
            rotation_resolution=updated_resolution,
        )
    except (
        AcpcError,
        sessions.SessionError,
        transcript.TranscriptError,
        runner.RunnerError,
        OSError,
    ) as error:
        raise _follow_up_problem(error, session_id) from None
    return current, request


def _dispatch_follow_up(
    meta: sessions.SessionMeta,
    prompt: str,
    *,
    output_file: str | None,
    permissions: str | None,
    background: bool,
    timeout: float | None,
    cancel_after: float | None,
    max_output: int,
    quiet: bool,
    presentation: str,
    emit_failure_result: bool = True,
    extra: Callable[[sessions.SessionMeta], dict[str, Any]] | None = None,
) -> None:
    """Run the next turn on a finished session using shared turn machinery.

    ``extra`` is for the commands whose result carries fields the shared
    answer shape does not: it is called with the result's own session record,
    so `steer` can report the turn the correction landed on, and its
    ``correction_result`` rides the stderr summary the same way.
    """
    current, request = _follow_up_request(
        meta.session_id,
        prompt,
        permissions=permissions,
        background=background,
        timeout=timeout,
        cancel_after=cancel_after,
    )

    if background:
        _dispatch_background(
            meta.session_id,
            request,
            presentation=presentation,
            output_file=output_file,
            max_output=max_output,
            emit_failure_result=emit_failure_result,
            extra=extra,
        )
        return

    if not quiet:
        _echo_metadata(output.format_session_line(current))

    try:
        outcome = runner.execute_turn(meta.session_id, request)
    except runner.ResumeRotationError as error:
        # Without a turn token the store never opened the turn — something
        # else holds the session.  With one, the turn opened and the record it
        # rotated onto turned out to be unusable.
        held = error.turn_token is None
        raise AcpcError(
            str(error),
            kind=errors.CONFLICT if held else errors.CORRUPT_STATE,
            retryable=True if held else None,
            context={"session_id": meta.session_id},
        ) from None
    except runner.RunnerError as error:
        raise _runner_problem(error).with_context(session_id=meta.session_id) from None

    if outcome.wait_timed_out:
        _emit_wait_timeout(meta.session_id, output_file, turn=current.turns + 1)

    final = sessions.read_meta(meta.session_id)
    extra_fields = extra(final) if extra is not None else None

    result = output.render_result(
        final,
        outcome.answer,
        json_mode=presentation == "json",
        tagged=presentation == "tagged",
        max_output=max_output,
        changed=True,
        include_partial=emit_failure_result,
        extra=extra_fields,
    )
    _emit_turn_result(
        result,
        output_file=output_file,
        json_mode=presentation == "json",
        emit_failure_result=emit_failure_result,
        success=outcome.exit_code == vocab.EXIT_OK,
    )
    if outcome.state == "detached":
        _echo_metadata(
            f"-- detached, still RUNNING: {meta.session_id}"
            f" — answer: acpc wait {meta.session_id} · cancel: acpc cancel {meta.session_id}"
        )
        _end_turn_for(meta.session_id, outcome)
    if not quiet:
        _echo_metadata(
            output.format_summary(
                final,
                route_note=_route_note(outcome),
                correction_result=(extra_fields or {}).get("correction_result"),
                truncated_output_file=result.output_file if result.truncated else None,
            )
        )
    _end_turn_for(meta.session_id, outcome)


# SPEC `steer`: the preamble is fixed text, so the callee reads the redirect
# as a redirect rather than as a fresh unrelated task.
STEER_PREAMBLE = (
    "Your previous turn was interrupted by the operator; this instruction takes precedence:"
)


def _steer_prompt(instruction: str) -> str:
    return f"{STEER_PREAMBLE}\n\n{instruction}"


_STEER_MODE_HELP = (
    "How the correction reaches the turn: in-place keeps the turn, cancel-then-start interrupts "
    "it and starts a new one; default: the session's capabilities.steer_mode."
)

# The daemon bounds its own `_session/steering` request at 10 s; this is that
# bound plus room for the request and its reply to cross the socket, so a
# daemon that answers in time is always heard before this client gives up.
_STEER_IPC_TIMEOUT = 15.0


def _check_steer_option_conflicts(
    background: bool,
    timeout: float | None,
    cancel_after: float | None,
    steer_mode: str | None,
) -> None:
    """Reject the `steer` option combinations that make no sense together."""
    if background and timeout is not None:
        raise UsageProblem("--timeout only bounds waiting; use --cancel-after with --background")
    if cancel_after is not None and steer_mode != vocab.STEER_CANCEL_THEN_START:
        # SPEC `steer`: only cancel-then-start starts a turn to bound, so the
        # rule is static. Making it a capability question would give the same
        # command line two different meanings on two sessions.
        raise UsageProblem(
            "--cancel-after is accepted only with --steer-mode cancel-then-start, "
            "the only mode that starts a turn to bound"
        )


def _load_steerable_session(selector: str, steer_mode: str | None) -> sessions.SessionMeta:
    """Load the target session, refusing it if there is no turn to interrupt."""
    meta = _load_view_session(selector)
    if not meta.is_active:
        meta = _status_view_meta(meta)
    if not meta.is_active and meta.state != "preparing":
        raise AcpcError(
            f"session {meta.session_id} is {meta.state} — there is no turn to interrupt",
            kind=errors.CONFLICT,
            hint=f"Run: acpc continue {meta.session_id}",
            context={
                "session_id": meta.session_id,
                "capabilities": output.session_capabilities(meta),
            },
        )
    if steer_mode == vocab.STEER_IN_PLACE and meta.state == "waiting":
        # SPEC `steer`: a session waiting through a usage limit has no turn in
        # flight to correct — the channel exists but nothing is on the other
        # end of it right now, which is a state, not a permanent incapability
        # (`not_supported`), so `_select_steer_mode` never gets to answer.
        raise AcpcError(
            f"session {meta.session_id} is waiting for a usage limit to reset — "
            "there is no turn in flight to correct in place",
            kind=errors.CONFLICT,
            hint=f"Run: acpc steer {meta.session_id} ... --steer-mode cancel-then-start",
            context={
                "session_id": meta.session_id,
                "capabilities": output.session_capabilities(meta),
                "correction_result": {
                    "steer_mode": vocab.STEER_IN_PLACE,
                    "target_turn": meta.turns,
                    "target_status": meta.state,
                    "message_state": "not_delivered",
                },
            },
        )
    return meta


@effects.non_idempotent
@schema.format_defaults(tty="text", non_tty="text")
@schema.output_description(_STEER_OUTPUT_DESCRIPTION)
@schema.reads_stdin("instruction_text")
@schema.describes(
    selector=_SELECTOR_HELP,
    instruction_text=(
        "The redirect, or `-` to read it from stdin; --prompt-file is the third source and "
        f"exactly one of the three may be given. At most {_PROMPT_LIMIT_HELP} of UTF-8. "
        "An interrupted turn receives it under a fixed preamble."
    ),
    steer_mode=_STEER_MODE_HELP,
    output_file=_STEER_OUTPUT_FILE_DESCRIPTION,
)
@main.command(name="steer")
@click.argument("selector")
@click.argument("instruction_text", required=False)
@click.option(
    "--prompt-file",
    "prompt_file",
    metavar="FILE",
    help=(
        f"Read the instruction from a file; at most {_PROMPT_LIMIT_HELP}. One of three "
        "sources with the argument and `-`, exactly one of which must be given."
    ),
)
@click.option("--output-file", "output_file", metavar="FILE", help=_STEER_OUTPUT_FILE_HELP)
@click.option(
    "--steer-mode",
    "steer_mode",
    type=click.Choice(vocab.STEER_MODES),
    help=_STEER_MODE_HELP,
)
@click.option(
    "--format",
    "format_name",
    type=click.Choice(("text", "json")),
    help=_FORMAT_NATIVE_HELP,
)
@click.option(
    "--background",
    "--bg",
    "background",
    is_flag=True,
    help="Dispatch and return the session id.",
)
@click.option(
    "--timeout",
    type=TimeoutParamType(),
    metavar="S",
    help=_TIMEOUT_HELP,
)
@click.option(
    "--cancel-after",
    type=TimeoutParamType(),
    metavar="S",
    help=(
        f"Cancel the redirected turn after this duration ({_DURATION_SYNTAX}); unlike "
        "--timeout, this changes the work itself."
    ),
)
@click.option(
    "--max-output",
    type=click.IntRange(min=0),
    default=output.DEFAULT_MAX_OUTPUT,
    metavar="BYTES",
    help="Cap on stdout bytes; 0 disables the cap.",
)
@click.option("--quiet", is_flag=True, help="Suppress the stderr summary line.")
@_json_option(_JSON_CHOICE_HELP)
@_color_option()
@click.help_option("-h", "--help")
def steer_command(
    selector: str,
    instruction_text: str | None,
    prompt_file: str | None,
    output_file: str | None,
    steer_mode: str | None,
    format_name: str | None,
    background: bool,
    timeout: float | None,
    cancel_after: float | None,
    max_output: int,
    quiet: bool,
    json_mode: bool,
) -> None:
    """Correct the running turn: in place when the adapter supports it, else cancel and redirect.

    ``in-place`` adds the instruction to the turn in flight through the adapter's
    steering extension, at the next point the adapter allows (usually after the
    current tool call). The turn keeps its number, prompt and answer files; the
    result's ``correction_result`` says ``accepted`` when the adapter took the
    instruction, which is not proof the model obeyed it.

    ``cancel-then-start`` cancels the turn (ACP session/cancel), waits for the ack,
    then starts the next turn with the instruction under a fixed preamble. It is
    the default when the session does not support in-place, and the only mode
    that accepts ``--cancel-after``. If a daemon-owned ``continue`` is still
    preparing, nothing was interrupted and the instruction goes plainly.

    A finished session is a ``conflict``; the follow-up verb is ``acpc continue``.
    Without ``--background`` the command blocks and prints the observed turn's answer.

    Example: ``acpc steer x7k2 "Stop editing; diagnose only"``
    """
    selected_format = _select_format(format_name, json_mode, native_text=True)
    _check_steer_option_conflicts(background, timeout, cancel_after, steer_mode)
    instruction = _read_prompt(
        instruction_text,
        prompt_file,
        operation="steer",
        hint="Run: acpc steer SESSION_ID INSTRUCTION",
    )
    meta = _load_steerable_session(selector, steer_mode)
    selected_mode = _select_steer_mode(meta, steer_mode)
    target = meta.target
    if selected_mode == vocab.STEER_IN_PLACE:
        if target is None:  # unreachable: the mode is never chosen without a target
            raise _in_place_unsupported(meta)
        _steer_in_place(
            meta,
            target,
            instruction,
            selected_format=selected_format,
            output_file=output_file,
            background=background,
            timeout=timeout,
            max_output=max_output,
            quiet=quiet,
        )
        return
    _steer_cancel_then_start(
        meta,
        instruction,
        selected_format=selected_format,
        output_file=output_file,
        background=background,
        timeout=timeout,
        cancel_after=cancel_after,
        max_output=max_output,
        quiet=quiet,
    )


def _in_place_unsupported(meta: sessions.SessionMeta) -> AcpcError:
    """The one refusal for a session that has no channel to steer through.

    A direct child owns no socket, and an adapter that never declared the
    extension has nothing to send to: both are the same answer, and SPEC
    `steer` says there is never a silent fallback to the other mode.
    """
    return AcpcError(
        f"session {meta.session_id} cannot be corrected in place",
        kind=errors.NOT_SUPPORTED,
        retryable=False,
        hint=f"Run: acpc steer {meta.session_id} ... --steer-mode cancel-then-start",
        context={
            "session_id": meta.session_id,
            "capabilities": output.session_capabilities(meta),
        },
    )


def _select_steer_mode(meta: sessions.SessionMeta, explicit: str | None) -> str:
    """Choose the mode before anything has happened, refusing an impossible one.

    SPEC `steer`: an explicitly selected mode the session does not support
    fails as `not_supported` with no effect, and there is never a silent
    fallback to the other mode.
    """
    if explicit == vocab.STEER_IN_PLACE and (
        meta.steer_mode != vocab.STEER_IN_PLACE or meta.target is None
    ):
        raise _in_place_unsupported(meta)
    if explicit is not None:
        return explicit
    return meta.steer_mode or vocab.STEER_CANCEL_THEN_START


def _steer_cancel_then_start(
    meta: sessions.SessionMeta,
    instruction: str,
    *,
    selected_format: str,
    output_file: str | None,
    background: bool,
    timeout: float | None,
    cancel_after: float | None,
    max_output: int,
    quiet: bool,
) -> None:
    """Interrupt the turn in flight and send the instruction to the next one.

    SPEC `steer`: the correcting call is `cancel` plus `continue` without the
    race in the middle, and the result says which turn was selected, what it
    ended as, and that acpc accepted the replacement. Every failure of the
    cancellation step is reported the same way as a failure after it: with
    `session_id`, `capabilities` and `correction_result` in `context`.
    """
    target_turn = meta.turns
    capabilities = output.session_capabilities(meta)
    session_id = meta.session_id
    try:
        cancel_result = _cancel_session(meta)
    except AcpcError as error:
        raise _cancel_stage_failure(error, session_id, target_turn, capabilities, None) from None
    _check_cancel_stage(cancel_result, session_id, target_turn, capabilities)
    meta = cancel_result.meta
    interrupted = (
        meta.state == "canceled" and meta.stop_reason != runner.PREPARATION_CANCELLED_REASON
    )
    if not interrupted and not quiet:
        # SPEC `steer`: nothing was interrupted, so the preamble would lie.
        if meta.stop_reason == runner.PREPARATION_CANCELLED_REASON:
            _echo_metadata(
                "-- nothing was interrupted during preparation; continuing as a plain follow-up"
            )
        else:
            _echo_metadata(
                "-- the turn finished on its own before the cancel landed; "
                "continuing as a plain follow-up"
            )
    correction = {
        "steer_mode": vocab.STEER_CANCEL_THEN_START,
        "target_turn": target_turn,
        "target_status": meta.state,
        "message_state": "accepted",
    }

    def extra(final: sessions.SessionMeta) -> dict[str, Any]:
        return {
            "turn": final.turns,
            "capabilities": output.session_capabilities(final),
            "correction_result": correction,
        }

    try:
        _dispatch_follow_up(
            meta,
            _steer_prompt(instruction) if interrupted else instruction,
            output_file=output_file,
            permissions=None,
            background=background,
            timeout=timeout,
            cancel_after=cancel_after,
            max_output=max_output,
            quiet=quiet,
            presentation=_select_presentation(selected_format),
            emit_failure_result=False,
            extra=extra,
        )
    except AcpcError as error:
        raise _redirect_failure(
            error, meta.session_id, target_turn, correction, capabilities
        ) from None


def _check_cancel_stage(
    cancel_result: _CancelResult,
    session_id: str,
    target_turn: int,
    capabilities: Mapping[str, Any],
) -> None:
    """Fail loudly when the cancellation step did not select a turn to redirect.

    SPEC `steer`: a deadline with the selected turn still running is a
    `timeout`, and a daemon that already moved on to a newer turn is a
    `conflict` — neither leaves anything for this call to redirect.
    """
    if cancel_result.deadline_passed:
        raise _cancel_stage_failure(
            AcpcError(
                f"session {session_id} timed out while turn {target_turn} was still running",
                kind=errors.TIMEOUT,
                hint=f"Run: acpc status {session_id}",
            ),
            session_id,
            target_turn,
            capabilities,
            cancel_result.meta.state,
        )
    if cancel_result.stale:
        raise _cancel_stage_failure(
            AcpcError(
                f"session {session_id} started a new turn before it could be corrected",
                kind=errors.CONFLICT,
                hint=f"Run: acpc steer {session_id} ...",
            ),
            session_id,
            target_turn,
            capabilities,
            cancel_result.meta.state,
        )


def _cancel_stage_failure(
    error: AcpcError,
    session_id: str,
    target_turn: int,
    capabilities: Mapping[str, Any],
    target_status: str | None,
) -> AcpcError:
    """Attach what acpc knows when the cancellation step itself failed.

    SPEC `steer`: every failure of the cancellation step carries `session_id`,
    `capabilities` and `correction_result` in `context`; nothing was ever
    sent, so `message_state` is always `not_delivered`. ``target_status`` is
    the last state acpc could observe for the selected turn; a caller that
    has none of its own passes `None` and gets a fresh best-effort read, or
    `null` when even that fails.
    """
    if target_status is None:
        try:
            target_status = sessions.load(session_id).state
        except sessions.SessionError:
            target_status = None
    correction = {
        "steer_mode": vocab.STEER_CANCEL_THEN_START,
        "target_turn": target_turn,
        "target_status": target_status,
        "message_state": "not_delivered",
    }
    return error.with_context(
        session_id=session_id, correction_result=correction, capabilities=dict(capabilities)
    )


def _redirect_failure(
    error: AcpcError,
    session_id: str,
    target_turn: int,
    correction: Mapping[str, Any],
    capabilities: Mapping[str, Any],
) -> AcpcError:
    """Attach what acpc knows about the redirect to a failure it ended on.

    Whether the replacement turn exists is read from the session, not assumed
    from where the failure was raised: a deadline that expired after the turn
    was accepted is a different fact from a dispatch that never claimed it.
    """
    try:
        current = sessions.read_meta(session_id)
    except sessions.SessionError:
        message_state = "unknown"
    else:
        # A conflict means another call's turn moved the session on, not ours.
        opened = current.turns != target_turn and error.kind != errors.CONFLICT
        message_state = "accepted" if opened else "not_delivered"
    return error.with_context(
        correction_result={**correction, "message_state": message_state},
        capabilities=dict(capabilities),
    )


@dataclass(frozen=True, slots=True)
class _SteerDelivery:
    """What acpc knows about one attempted in-place correction."""

    message_state: str
    kind: str | None = None
    message: str = ""


def _in_place_delivery(reply: Any, session_id: str) -> _SteerDelivery:
    """Read the daemon's answer about one in-place correction.

    SPEC `steer` names one `kind` and one `message_state` per situation acpc
    can be in, and this is the only place that maps the daemon's wire reply
    onto them.
    """
    if isinstance(reply, daemon_client.DaemonUnavailable):
        return _SteerDelivery(
            "not_delivered",
            errors.UNAVAILABLE,
            f"the daemon serving session {session_id} could not be reached: {reply.reason}",
        )
    if not isinstance(reply, Mapping):
        return _SteerDelivery(
            "unknown",
            errors.OUTCOME_UNKNOWN,
            f"session {session_id} did not answer the correction request",
        )
    if reply.get("ok") is True:
        return _SteerDelivery("accepted")
    kind = reply.get("kind")
    if kind == errors.NOT_SUPPORTED:
        return _SteerDelivery(
            "not_delivered",
            errors.NOT_SUPPORTED,
            f"the adapter serving session {session_id} rejected in-place steering",
        )
    if kind == errors.CONFLICT:
        return _SteerDelivery(
            "not_delivered",
            errors.CONFLICT,
            f"session {session_id} has no turn in flight; the instruction was not delivered",
        )
    if kind == errors.UNAVAILABLE:
        return _SteerDelivery(
            "not_delivered",
            errors.UNAVAILABLE,
            f"the daemon serving session {session_id} could not take the instruction",
        )
    return _SteerDelivery(
        "unknown",
        errors.OUTCOME_UNKNOWN,
        f"session {session_id} did not confirm the in-place correction; it may or may not "
        "have been applied",
    )


def _steer_extra(meta: sessions.SessionMeta, correction: Mapping[str, Any]) -> dict[str, Any]:
    """The three fields every `steer` result carries on top of the shared shape."""
    return {
        "turn": meta.turns,
        "capabilities": output.session_capabilities(meta),
        "correction_result": dict(correction),
    }


def _steer_in_place(
    meta: sessions.SessionMeta,
    target: str,
    instruction: str,
    *,
    selected_format: str,
    output_file: str | None,
    background: bool,
    timeout: float | None,
    max_output: int,
    quiet: bool,
) -> None:
    """Deliver the instruction to the turn in flight and report what is known.

    SPEC `steer`: nothing is cancelled, rotated or re-prompted here, and a
    delivery acpc cannot confirm never becomes a cancel-then-start.
    """
    if meta.state in {"starting", "preparing"}:
        raise AcpcError(
            f"session {meta.session_id} is {meta.state} — no prompt is in flight yet",
            kind=errors.CONFLICT,
            retryable=True,
            hint=(
                f"Run: acpc wait {meta.session_id}, or acpc steer {meta.session_id} ... "
                "--steer-mode cancel-then-start to cancel the preparation"
            ),
            context={
                "session_id": meta.session_id,
                "capabilities": output.session_capabilities(meta),
            },
        )
    capabilities = output.session_capabilities(meta)
    delivery = _in_place_delivery(
        asyncio.run(_steer_with_daemon(target, meta.session_id, instruction)),
        meta.session_id,
    )
    correction: dict[str, Any] = {
        "steer_mode": vocab.STEER_IN_PLACE,
        "target_turn": meta.turns,
        "target_status": meta.state,
        "message_state": delivery.message_state,
    }
    if delivery.kind is not None:
        hint = f"Run: acpc wait {meta.session_id}"
        if delivery.kind == errors.NOT_SUPPORTED:
            hint = f"Run: acpc steer {meta.session_id} ... --steer-mode cancel-then-start"
        raise AcpcError(
            delivery.message,
            kind=delivery.kind,
            retryable=False,
            hint=hint,
            context={
                "session_id": meta.session_id,
                "correction_result": correction,
                "capabilities": capabilities,
            },
        )
    if background:
        # The receipt describes the acceptance, so it renders the state the
        # correction targeted; a re-read here could already show the turn's
        # end, which the `steer` schema does not publish.
        presentation = _select_presentation(selected_format)
        result = output.render_result(
            meta,
            json_mode=presentation == "json",
            tagged=presentation == "tagged",
            background=True,
            max_output=max_output,
            changed=True,
            include_partial=False,
            extra=_steer_extra(meta, correction),
        )
        if not _write_rendered_file(output_file, result):
            _write_stdout(result.text)
        if not quiet:
            _echo_metadata(f"-- steer in-place accepted · session {meta.session_id}")
        return
    _observe_steered_turn(
        meta,
        correction,
        capabilities,
        selected_format=selected_format,
        output_file=output_file,
        timeout=timeout,
        max_output=max_output,
        quiet=quiet,
    )


async def _steer_with_daemon(target: str, session_id: str, text: str) -> Any:
    """Ask the daemon to correct the turn, bounded like the cancel it resembles."""
    try:
        return await asyncio.wait_for(
            daemon_client.steer_turn(target, session_id, text),
            timeout=_STEER_IPC_TIMEOUT,
        )
    except TimeoutError:
        # The daemon bounds its own request well inside this one, so a reply
        # that never comes means the socket died with the outcome unknown.
        return None
    except Exception:  # noqa: BLE001
        return None


def _observe_steered_turn(
    meta: sessions.SessionMeta,
    correction: Mapping[str, Any],
    capabilities: Mapping[str, Any],
    *,
    selected_format: str,
    output_file: str | None,
    timeout: float | None,
    max_output: int,
    quiet: bool,
) -> None:
    """Block on a corrected turn and print what it produced.

    SPEC `steer`: `--timeout` bounds this client's wait only, and a turn that
    ends badly is a failure with the correction facts attached — the
    instruction was accepted, and what the turn did with it is the session's
    own result.
    """
    session_id = meta.session_id
    observed_status = meta.state
    if not quiet:
        _echo_metadata(output.format_session_line(meta))
    try:
        turn, state = runner.wait_for_session(session_id, timeout=timeout)
        if state is None:
            if not quiet:
                _echo_metadata(_still_running_note(session_id, timeout))
            observed_status = sessions.read_meta(session_id).state
            raise _timeout_failure(session_id, turn=turn, status=observed_status, retryable=False)
        final = sessions.read_meta(session_id)
        observed_status = final.state
        observed_correction = {**correction, "target_status": final.state}
        if final.state != "succeeded":
            raise AcpcError(
                f"session {session_id} {final.state} after the correction",
                kind=errors.OPERATION_FAILED,
                exit_code=runner.exit_code_for(final.state, final.stop_reason),
                hint=f"Run: acpc log {session_id} --since 0",
                context={
                    "session_id": session_id,
                    "status": final.state,
                    "correction_result": observed_correction,
                },
            )
        presentation = _select_presentation(selected_format)
        result = output.render_result(
            final,
            _answer_text(session_id),
            json_mode=presentation == "json",
            tagged=presentation == "tagged",
            max_output=max_output,
            changed=True,
            include_partial=False,
            extra=_steer_extra(final, observed_correction),
        )
        _emit_turn_result(result, output_file=output_file, json_mode=presentation == "json")
        if not quiet:
            _echo_metadata(
                output.format_summary(
                    final,
                    correction_result=observed_correction,
                    truncated_output_file=result.output_file if result.truncated else None,
                )
            )
    except (click.Abort, KeyboardInterrupt, Exception) as error:  # noqa: BLE001
        # Everything a blocking observation can fail on, including this call's
        # own deadline and the caller's Ctrl-C, leaves with the same context.
        problem = _wait_failure(error, session_id=session_id, observed_status=observed_status)
        if "correction_result" not in (problem.context or {}):
            problem = problem.with_context(correction_result=dict(correction))
        raise problem.with_context(capabilities=dict(capabilities)) from None


@effects.read_only
@schema.format_defaults(tty="text", non_tty="text")
@schema.output_description(_WAIT_OUTPUT_DESCRIPTION)
@schema.describes(selector=_SELECTOR_HELP, output_file=_OUTPUT_FILE_DESCRIPTION)
@main.command(name="wait")
@click.argument("selector")
@click.option(
    "--timeout",
    type=TimeoutParamType(allow_zero=True),
    default=None,
    metavar="S",
    help=_WAIT_TIMEOUT_HELP,
)
@click.option("--output-file", "output_file", metavar="FILE", help=_OUTPUT_FILE_HELP)
@click.option(
    "--format",
    "format_name",
    type=click.Choice(("text", "json")),
    help=_FORMAT_NATIVE_HELP,
)
@click.option(
    "--max-output",
    type=click.IntRange(min=0),
    default=output.DEFAULT_MAX_OUTPUT,
    metavar="BYTES",
    help="Cap rendered output bytes; 0 disables the cap.",
)
@click.option("--quiet", is_flag=True, help="Suppress the stderr summary line.")
@_json_option(_JSON_CHOICE_HELP)
@_color_option()
@click.help_option("-h", "--help")
def wait_command(
    selector: str,
    timeout: float | None,
    output_file: str | None,
    format_name: str | None,
    max_output: int,
    quiet: bool,
    json_mode: bool,
) -> None:
    """Block until the session's current turn ends, then print its observed result.

    The turn observed is the one active when this call starts (M1g): a later
    ``continue`` or ``steer`` that opens a newer turn does not change what this
    call reports. The exit code mirrors the turn's result. A turn already
    finished is returned immediately — the free way to reprint an answer.
    ``--timeout`` stops the waiting only and exits 124: the session keeps
    running, and no result is printed, even when a partial answer was recorded
    (``log --tail`` reads that instead). A session that failed adds a
    ``failure:`` segment to the stderr summary, naming what acpc observed and
    the next step to take.

    Example: ``acpc wait <session-id> --timeout 120``
    """
    session_id = selector
    observed_status: str | None = None
    try:
        selected_format = _select_format(format_name, json_mode, native_text=True)
        meta = _load_view_session(selector)
        session_id = meta.session_id
        observed_status = meta.state
        turn, state = runner.wait_for_session(meta.session_id, timeout=timeout)
        if state is None:
            # SPEC `wait`: the timeout stops waiting only — the session runs on,
            # and no result is written, even for a turn already observed
            # in-flight (V5a).
            if not quiet:
                _echo_metadata(_still_running_note(meta.session_id, timeout))
            try:
                observed_status = sessions.read_meta(meta.session_id).state
            except (sessions.SessionError, OSError):
                raise AcpcError(
                    f"gave up waiting for session {meta.session_id}; its state is unknown",
                    kind=errors.OUTCOME_UNKNOWN,
                    retryable=False,
                    context={"session_id": meta.session_id, "status": None},
                ) from None
            raise _timeout_failure(
                meta.session_id, turn=turn, status=observed_status, retryable=True
            )

        observed_status = state
        final, answer, is_current = _turn_result_source(meta.session_id, turn)
        observed_status = final.state

        presentation = _select_presentation(selected_format)
        result = output.render_result(
            final,
            answer,
            json_mode=presentation == "json",
            tagged=presentation == "tagged",
            max_output=max_output,
            turn=(None if is_current else turn),
        )
        exit_code = runner.exit_code_for(final.state, final.stop_reason)
        _emit_turn_result(
            result,
            output_file=output_file,
            json_mode=presentation == "json",
        )
        if not quiet:
            summary = output.format_summary(
                final, truncated_output_file=result.output_file if result.truncated else None
            )
            if (
                is_current
                and final.state == "failed"
                and (failure_message := _latest_failure_message(final.session_id))
            ):
                summary += f" | failure: {failure_message}"
            _echo_metadata(summary)
        _end_turn(
            final.session_id,
            final.state,
            final.stop_reason,
            exit_code,
        )
    except (click.Abort, KeyboardInterrupt) as error:
        raise _wait_failure(error, session_id=session_id, observed_status=observed_status) from None
    except Exception as error:  # noqa: BLE001
        # Output and state failures can happen after the last observation too.
        raise _wait_failure(error, session_id=session_id, observed_status=observed_status) from None


def _answer_text(session_id: str) -> str:
    try:
        return sessions.answer_path(session_id).read_text(encoding="utf-8")
    except OSError:
        return ""


def _turn_answer_text(session_id: str, turn: int) -> str:
    try:
        return sessions.turn_path(session_id, "answer", turn).read_text(encoding="utf-8")
    except OSError:
        return ""


def _turn_result_source(session_id: str, turn: int) -> tuple[sessions.SessionMeta, str, bool]:
    """The finished turn's metadata and answer, wherever rotation left them.

    SPEC.md *State on disk*: once the session has rotated past `turn`, its
    record and answer are parked as `meta.<turn>.json` and `answer.<turn>.md`;
    while it has not, they are still the session's current files (M1g, V5a).
    The third value says which: `wait`'s stderr summary reads only the
    transcript's tail when it is true, since a parked turn's own errors are
    not reliably separable from a newer turn's in the shared transcript.
    """
    current = sessions.read_meta(session_id)
    if current.turns == turn:
        return current, _answer_text(session_id), True
    return sessions.read_turn_meta(session_id, turn), _turn_answer_text(session_id, turn), False


def _daemon_idle_column(idle_seconds: float | None) -> str:
    """Render a target's idle age, or `·` when it is serving or has no history."""
    if idle_seconds is None:
        return "·"
    return f"idle {output.format_duration(idle_seconds)}"


@effects.read_only
@main.group(name="daemon", invoke_without_command=True)
@click.help_option("-h", "--help")
@click.pass_context
def daemon_group(ctx: click.Context) -> None:
    """Inspect and stop the per-target daemons.

    Example: ``acpc daemon status``
    """
    if ctx.invoked_subcommand is None:
        raise UsageProblem("daemon requires a subcommand", hint="Run: acpc daemon status")


@effects.read_only
@schema.describes(
    agent="Registry entry whose daemons are reported; absent, every daemon on this machine."
)
@daemon_group.command(name="status")
@click.argument("agent", required=False)
@click.option(
    "--limit",
    type=click.IntRange(min=0),
    default=render.DEFAULT_STATUS_LIMIT,
    show_default=True,
    help="Return at most N daemons.",
)
@click.option(
    "--plain",
    is_flag=True,
    help=(
        "Print one daemon target per line; requires explicit --limit and cannot be combined "
        "with --json or a different --format."
    ),
)
@click.option(
    "--format",
    "format_name",
    type=click.Choice(("text", "json", "plain")),
    help=_FORMAT_COLLECTION_HELP,
)
@_json_option("Emit the status as JSON.")
@_color_option()
@click.help_option("-h", "--help")
@click.pass_context
def daemon_status_command(
    ctx: click.Context,
    agent: str | None,
    limit: int,
    plain: bool,
    format_name: str | None,
    json_mode: bool,
) -> None:
    """Report each live daemon with its acpc version, pid, uptime, idle age and log path.
    Entries are ordered by target name, ascending, and the default window is the first 20 of that
    order.

    Example: ``acpc daemon status --json``
    """
    selected_format = _select_format(format_name, json_mode, plain=plain)
    if (
        selected_format == "plain"
        and ctx.get_parameter_source("limit") is not click.core.ParameterSource.COMMANDLINE
    ):
        raise UsageProblem("--plain requires an explicit --limit")
    all_entries = asyncio.run(_collect_daemon_status(agent))
    entries = all_entries[:limit]
    if selected_format == "json":
        _emit_json(output.collection_envelope(entries, has_more=len(entries) < len(all_entries)))
        return
    if selected_format == "plain":
        _write_stdout("".join(f"{item['target']}\n" for item in entries))
        return
    if not all_entries:
        click.echo("-- no daemons running", err=True)
        return
    if not entries:
        _write_stdout(f"-- 0 of {len(all_entries)} — use --limit to change\n")
        return
    rows = [
        (
            str(item["target"]),
            f"acpc {item['version']}",
            f"pid {item['pid']}",
            f"up {output.format_duration(item['uptime_seconds'])}",
            f"· {_daemon_idle_column(item['idle_seconds'])}",
            f"· {len(item['sessions'])} sessions",
            f"· {item['log']}",
        )
        for item in entries
    ]
    lines = render.format_table(rows, separator="  ")
    _write_stdout("\n".join(lines) + "\n")
    if len(entries) < len(all_entries):
        _write_stdout(f"-- {len(entries)} of {len(all_entries)} — use --limit to change\n")


async def _collect_daemon_status(
    agent: str | None, *, clock: render.Clock | None = None
) -> list[dict[str, Any]]:
    now = time.time() if clock is None else clock()
    session_metas = sessions.list_sessions(clock=lambda: now)
    entries: list[dict[str, Any]] = []
    for target in runner.daemon_targets_for(agent) if agent else runner.all_daemon_targets():
        # Observe, never greet: greeting stands a daemon of another build down,
        # which is a change of state this command promises not to make.
        daemon = await daemon_client.observe(target)
        if daemon is None:
            continue
        try:
            reply = await daemon.status()
        finally:
            await daemon.close()
        if reply.get("ok"):
            entry = {key: value for key, value in reply.items() if key != "ok"}
            entry["idle_seconds"] = render.daemon_idle_seconds(session_metas, target, now=now)
            entries.append(entry)
    return entries


@effects.idempotent
@schema.describes(
    agent=(
        "Registry entry whose daemon is stopped; absent, every daemon on this machine, "
        "which is the call that needs --yes."
    )
)
@daemon_group.command(name="stop")
@click.argument("agent", required=False)
@click.option(
    "--force",
    "-f",
    is_flag=True,
    help="Stop even when the target has running or starting sessions; they are failed, not unknown.",
)
@click.option(
    "--dry-run", "-n", is_flag=True, help="List the daemons it would stop, and stop none."
)
@click.option("--yes", "-y", "assume_yes", is_flag=True, help="Stop them without being asked.")
@click.option(
    "--format",
    "format_name",
    type=click.Choice(("text", "json")),
    help=_FORMAT_OUTPUT_HELP,
)
@_json_option("Emit the result as JSON.")
@_color_option()
@click.help_option("-h", "--help")
def daemon_stop_command(
    agent: str | None,
    force: bool,
    dry_run: bool,
    assume_yes: bool,
    format_name: str | None,
    json_mode: bool,
) -> None:
    """Stop daemons; active sessions refuse the stop unless ``--force``.

    Named with an agent it stops that one daemon, which the next run starts
    again. Bare, it addresses every daemon on this machine and cannot say in
    advance which, so it needs ``--yes``; ``--dry-run`` names them first.

    ``--yes`` and ``--force`` answer different questions and neither implies
    the other: ``--yes`` confirms the stop, ``--force`` overrides the refusal
    that active sessions raise.

    Example: ``acpc daemon stop mock``
    """
    selected_format = _select_format(format_name, json_mode)
    targets = runner.daemon_targets_for(agent) if agent else runner.all_daemon_targets()
    if not force:
        _refuse_stop_over_active_sessions(agent, targets)
    if agent is None and targets and not dry_run:
        # Last, after the precondition: a refusal caused by active sessions is
        # not something confirming the stop would resolve.
        interaction.require_confirmation(
            assume_yes,
            message="daemon stop: stopping every daemon on this machine needs confirmation",
            hint="Run: acpc daemon stop --dry-run to see them, then repeat with --yes",
        )
    if not force and not dry_run:
        # The confirmation prompt may leave enough time for a new turn to
        # appear. Recheck the precondition on the mutation side of the gate.
        _refuse_stop_over_active_sessions(agent, targets)
    if dry_run:
        # The preview talks to nothing. The lock files already name every
        # daemon the mutating call would address, and greeting one is itself
        # an effect: a daemon of another version stands down on contact.
        stopped = list(targets)
    else:
        stopped = asyncio.run(_stop_daemons(targets))
    payload = {
        "targets": stopped,
        "changed": bool(stopped) and not dry_run,
        "requires_confirmation": agent is None and bool(targets),
    }
    _emit_maintenance_result(
        payload,
        selected_format=selected_format,
        text=None,
        summary=f"{'would stop' if dry_run else 'stopped'} {len(stopped)} daemon(s)",
    )


def _refuse_stop_over_active_sessions(agent: str | None, targets: Sequence[str]) -> None:
    """Refuse a stop that would fail live sessions, naming the override.

    A documented precondition, not a bad call: `--force` is what overrides it,
    and `--yes` never does.
    """
    addressed = set(targets)
    active = [
        meta for meta in sessions.list_sessions() if meta.target in addressed and meta.is_active
    ]
    if not active:
        return
    count = len(active)
    noun = "session" if count == 1 else "sessions"
    scope = f" {agent}" if agent else ""
    ids = ", ".join(meta.session_id for meta in active)
    raise AcpcError(
        f"daemon stop{scope}: {count} active {noun} ({ids}) — wait or stop them first",
        kind=errors.PRECONDITION_FAILED,
        hint=f"Run: acpc daemon stop{scope} --force",
        context={"sessions": [meta.session_id for meta in active]},
    )


async def _stop_daemons(targets: Sequence[str]) -> list[str]:
    """Stop the addressed daemons, returning the ones this call reached."""
    reached: list[str] = []
    for target in targets:
        daemon = await daemon_client.connect(target)
        if daemon is None:
            continue
        try:
            reply = await daemon.stop()
        finally:
            await daemon.close()
        if reply.get("ok"):
            reached.append(target)
    return reached
