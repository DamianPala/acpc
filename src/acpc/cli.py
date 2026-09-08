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

# Values `TimeoutParamType` accepts, stated wherever one is taken: the type
# name alone ("duration") does not tell a caller what to write.
_DURATION_SYNTAX = (
    "seconds, such as 90, or a value suffixed s, m, h, d or w, such as 90s, 5m, 1h30m"
)

_OUTPUT_FILE_HELP = (
    "Write exactly what stdout would receive to a file; on success stdout stays empty, and "
    "a failed machine-format turn writes an empty file. A relative path resolves against the "
    "directory acpc was invoked from, and the file is overwritten. The session's full "
    "answer.md lives in its session directory and follows its retention and prune policy "
    "(90 days by default)."
)

# The same file, said in full for the schema: `--help` has no room for it.
_OUTPUT_FILE_DESCRIPTION = (
    f"{_OUTPUT_FILE_HELP.rstrip('.')}. A leading `~` is expanded, and a missing parent "
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
    success: bool,
) -> None:
    # A failed machine-format document must leave stdout empty; still create
    # the requested mirror file so callers can read it after every exit code.
    if output_file is not None:
        file_result = result if success or not json_mode else output.OutputResult("", False, 0)
        _write_rendered_file(output_file, file_result)
        return
    if success or not json_mode:
        _write_stdout(result.text)


def _raise_wait_timeout(session_id: str, output_file: str | None) -> NoReturn:
    """Fail after a wait deadline without changing the accepted session."""
    if output_file is not None:
        output.write_output_file(output_file, "")
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
    raise AcpcError(
        f"session {session_id} timed out while still {observed.state}",
        kind=errors.TIMEOUT,
        retryable=True,
        hint=f"Run: acpc wait {session_id}",
        context={"session_id": session_id, "status": observed.state},
        exit_code=vocab.EXIT_TIMEOUT,
    )


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
        # repeat frees an id; only deleting finished sessions does.
        return AcpcError(
            str(error),
            kind=errors.UNAVAILABLE,
            action="user",
            hint="Run: acpc prune --yes",
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

Quick reference

Short task (fits your tool-call window — blocks, answer on stdout):
  acpc run <agent> "Explain this code"
  acpc run <agent> "Implement the fix" --permissions execute
  execute permits read, edit and execute; edit permits read and edit only
  Dispatch prints `-- session <id> | dir <path>` on stderr right away:
  the id works mid-run with log / cancel / steer.

Long or uncertain task (background):
  acpc run <agent> "Run the tests" --background --json  # {"session_id": ..., "paths": ...}
  acpc wait <id> --quiet                          # block until done, prints the answer
  Truncated or huge answer? Read <dir>/answer.md selectively — always complete.
  In a shell that can background calls, `wait` becomes a completion push.

Checking on a run:
  acpc log <id>                    # the default: instant snapshot, condensed
  Need only the result? wait <id>. Don't block on a run you won't act on.

Supervising a risky run you intend to steer/cancel mid-flight — the one
case for --follow (a bounded digest, not a live view):
  acpc log <id> --follow --timeout 60 --max-output 16384
  Ends at session end (exit 0), the timeout (124) or the cap (4); resume
  with --since <cursor> from the footer.

Steering a running session:
  acpc steer <id> "Stop editing; diagnose only"   # cancel + redirect, history kept

Continue (next turn on a finished session):
  acpc continue <id> "Now fix what you found"

Heredoc prompt:
  acpc run <agent> - --permissions execute <<'PROMPT'
  Review the implementation and make the required edits.
  PROMPT

Context care (agent callers):
  log's default view is condensed one-liners, last 20 events; full via --prose.
  --json = this command's output as a machine envelope, any command. On
  run/wait it embeds the answer; add --output-file FILE to keep the answer out of it.
  Content reads best as markdown: answer.md, log --prose.
  Tight context: lower the cap, e.g. --max-output 16384.
  Every --timeout takes seconds (90) or a duration (90s, 5m, 1h).

Maintenance and setup:
  status <id>       liveness-verified metadata for one session
  list              running + the 20 most recent sessions (--limit N to change)
  cancel <id>       cancel a running session; it stays resumable with continue
  delete <id> --yes delete a finished session's on-disk state
  prune --yes       delete finished sessions older than retention (--older-than D)
  install <agent>   install the agent's adapter (--yes unless you are at a terminal)
  skills list       list bundled how-to skills; skills get <name> prints the body
  SIGINT cancels the turn owned by this command. SIGTERM detaches work already
  taken over by the daemon and ends a direct-worker turn.
  Deleting needs --yes; --dry-run previews prune and bare daemon stop, and
  never needs it. --force is separate: it overrides a documented refusal.

Common commands:
  run, resolve, continue, steer, wait, status, list, log, agents, skills, daemon,
  probe, cancel, delete, prune, install
  Use `acpc <command> --help` for the command's full reference.

Machine-readable interface:
  acpc schema           the whole command surface as JSON: every command with
                        its description and effects
  acpc schema run       one command's arguments, flags, effects and gates
                        (path segments are separate words: acpc schema agents create)
  --json on a command emits that command's own result as JSON.

Flag → ACP
  --mode         → session/set_mode
  --permissions  → session/set_mode + request_permission
                   none · read · edit · execute · all · ask
                   execute permits read, edit and execute
                   write and prompt are deprecated aliases for execute and ask
  --model        → session/new (model)
  --effort       → session/new (effort)"""


class _CheatSheetGroup(click.Group):
    """Use the compact first-contact page for the root command."""

    def get_help(self, ctx: click.Context) -> str:
        return _ROOT_HELP

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
            message = _friendly_usage_message(error.format_message(), command_path=command_path)
            _fail(UsageProblem(message, exit_code=error.exit_code))
        except AcpcError as error:
            _fail(error)
        except click.ClickException as error:
            error_context = getattr(error, "ctx", None)
            command_path = getattr(error_context, "command_path", None)
            message = _friendly_usage_message(error.format_message(), command_path=command_path)
            _fail(AcpcError(message, exit_code=error.exit_code))
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
    follow_hint = (
        "--follow is not a flag on this command — following a session is: "
        "acpc log <id> --follow [--timeout S]"
    )
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
        "-f": follow_hint,
        "--detach": detach_hint,
        "-d": detach_hint,
        "-C": "-C is not an acpc flag — the working-directory flag is --cwd DIR",
        "--tail": "--tail was renamed to --limit — use acpc log <id> --limit N",
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
        return "status requires a session id — use acpc list to list sessions"
    return (
        _friendly_option_hint(message, command_parts)
        or _friendly_command_hint(message, command_parts)
        or message
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


def _read_prompt(prompt_text: str | None, prompt_file: str | None) -> str:
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
            "give exactly one prompt source: a prompt argument, - for stdin, or --prompt-file "
            f"(got {len(sources)})"
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
        import json

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
    return {"items": rows, "has_more": False}


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
        payload = {"items": items, "has_more": len(items) < len(all_items)}
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
        raise UsageProblem("agents needs a subcommand; use `acpc agents list`")


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
    """List adapters and variants; default is 20 entries."""
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
        payload: dict[str, Any] = {
            "items": results,
            "has_more": len(results) < len(_check_entries(registry, None)),
        }
    else:
        payload = {"items": results, "has_more": False}
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
            "release measures. Measuring what a mode actually permits is not in it"
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
@click.option(
    "--format",
    "format_name",
    type=click.Choice(("text", "json")),
    help=_FORMAT_OUTPUT_HELP,
)
@_json_option("Emit the deleted entry as JSON.")
@_color_option()
@click.help_option("-h", "--help")
def agents_delete_command(name: str, format_name: str | None, json_mode: bool) -> None:
    """Delete one entry this machine owns, under ``$ACPC_HOME/agents``.

    The counterpart to ``agents create``: it takes back exactly what that wrote,
    which is why creating an entry needs no confirmation — including the entry
    that overrides an adapter acpc ships. The shipped adapter itself is not
    this machine's, so a name with no file under ``agents`` is refused.

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
    payload = {"items": items, "has_more": len(items) < len(bundled)}
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
        raise UsageProblem("skills needs a subcommand; use `acpc skills list`")


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
    """List bundled skills; default is 20 entries."""
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


@dataclass(frozen=True, slots=True)
class _CancelResult:
    """The observed state and whether this call caused a cancellation request."""

    meta: sessions.SessionMeta
    changed: bool


async def _cancel_with_daemon(
    target: str, session_id: str
) -> _DaemonCancelReply | daemon_client.DaemonUnavailable | None:
    """Request cancellation without allowing a dead daemon to hang ``cancel``."""
    try:
        reply = await asyncio.wait_for(
            daemon_client.cancel_turn(target, session_id),
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
    turn_token = reply.get("turn_token")
    return _DaemonCancelReply(
        accepted=reply.get("ok") is True,
        turn_token=turn_token
        if isinstance(turn_token, int) and not isinstance(turn_token, bool)
        else None,
    )


def _wait_for_cancel(session_id: str, *, expected_turn: int | None = None) -> sessions.SessionMeta:
    """Give a daemon's cancellation time to finalize the session on disk."""
    deadline = time.monotonic() + runner.CANCEL_ACK_TIMEOUT
    while True:
        meta = sessions.load(session_id)
        current_generation = expected_turn is None or meta.turns == expected_turn
        if current_generation and (not meta.is_active or time.monotonic() >= deadline):
            return meta
        if time.monotonic() >= deadline:
            raise AcpcError(
                f"could not observe the canceled turn for session {session_id}",
                kind=errors.OUTCOME_UNKNOWN,
                context={"session_id": session_id, "status": None},
            )
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


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
    if command_line is not None and "acpc.direct_worker" in command_line:
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
        return _CancelResult(
            _wait_for_cancel(meta.session_id, expected_turn=meta.turns),
            True,
        )
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
    return _CancelResult(
        _wait_for_cancel(meta.session_id, expected_turn=meta.turns),
        True,
    )


def _cancel_session(meta: sessions.SessionMeta) -> _CancelResult:
    """Cancel an active session and return the state it settled into.

    SPEC `cancel`: graceful `session/cancel` with a bounded wait for the ack,
    torn down anyway if the callee will not wind down in time. `steer` puts
    the same cancel in front of a follow-up turn, so it lives here rather
    than inside `cancel`.
    """
    expected_turn = meta.turns
    if meta.target is not None:
        reply = asyncio.run(_cancel_with_daemon(meta.target, meta.session_id))
        if isinstance(reply, daemon_client.DaemonUnavailable):
            current = sessions.load(meta.session_id)
            if not current.is_active:
                return _CancelResult(current, False)
            return _cancel_local_session(current)
        if reply is not None and reply.accepted:
            turn_token = reply.turn_token if reply.turn_token is not None else expected_turn
            return _CancelResult(
                _wait_for_cancel(meta.session_id, expected_turn=turn_token),
                True,
            )
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


def _maintenance_json(payload: Mapping[str, Any]) -> None:
    _write_stdout(json.dumps(dict(payload), ensure_ascii=False) + "\n")


@effects.idempotent
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
    """Cancel a running session; it stays usable with ``acpc continue``.

    Cancels the turn in flight (ACP ``session/cancel``) and waits up to 10s for the
    ack; past that the connection is torn down anyway. During a daemon-owned
    continuation preparation it cancels the preparation and writes a no-prompt
    placeholder. Transcript, meta and the partial answer stay on disk for
    post-mortem. Stopping an already-finished session is a successful no-op that
    reports the state it found; an unknown id is reported as ``not_found``.

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
    if selected_format == "json":
        _maintenance_json(payload)
    else:
        _write_stdout(f"{meta.session_id} {meta.state}\n")
    click.echo(f"-- canceled {meta.session_id} · {meta.state}", err=True)


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
    the call needs ``--yes``. Prints the removed session id; ``--json`` also
    lists the deleted paths.

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
    if selected_format == "json":
        _maintenance_json(payload)
    else:
        _write_stdout(f"removed {meta.session_id}\n")
    click.echo(f"-- removed session {meta.session_id}", err=True)


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

    It decides what to delete as it runs, so deleting needs ``--yes``;
    ``--dry-run`` lists the same targets and never does.

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
        if not dry_run:
            # After the threshold resolved, so a bad --older-than fails as one;
            # before anything is read for deletion, so a refusal costs nothing.
            interaction.require_confirmation(
                assume_yes,
                message="prune: deleting the sessions it selects needs confirmation",
                hint="Run: acpc prune --dry-run to see them, then repeat with --yes",
            )
        candidates = sessions.prune_sessions(older_than=duration, dry_run=dry_run)
    except AcpcError:
        raise
    except sessions.SessionError as error:
        raise _session_problem(error) from None
    except OSError as error:
        raise AgentProblem(
            f"prune could not remove session state: {error}",
            kind=errors.OPERATION_FAILED,
            context={"operation": "prune"},
        ) from None
    except (config.ConfigError, ValueError) as error:
        raise UsageProblem(str(error)) from None

    session_ids = [meta.session_id for meta in candidates]
    payload = {
        "targets": session_ids,
        "changed": bool(session_ids) and not dry_run,
        "requires_confirmation": True,
    }
    if selected_format == "json":
        _maintenance_json(payload)
    elif session_ids:
        _write_stdout("\n".join(session_ids) + "\n")
    click.echo(
        f"-- prune {'would remove' if dry_run else 'removed'} {len(session_ids)} session(s)",
        err=True,
    )


def _load_view_session(selector: str) -> sessions.SessionMeta:
    """Verify liveness before a targeted view reports a session."""
    try:
        # `last` is a convenience for whoever is at the keyboard, so it turns
        # on the same rule as every other question acpc puts to a person.
        # Collecting the output in a file does not move that person away.
        session_id = sessions.resolve_selector(
            selector, allow_last=interaction.interactive_context()
        )
        return sessions.load(session_id)
    except sessions.SessionError as error:
        raise _session_problem(error) from None


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
# SPEC `log --follow`: a bounded replay for orientation, the `tail -f` prior.
_FOLLOW_DEFAULT_TAIL = 10
_LOG_WAIT_POLL_INTERVAL = 0.05


def _wait_for_new_events(
    transcript_file: transcript.Transcript,
    *,
    since: int,
    tail: int | None,
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
            return _read_transcript_page(transcript_file, since=since, tail=tail, condense=condense)


def _read_transcript_page(
    transcript_file: transcript.Transcript,
    *,
    since: int = 0,
    tail: int | None = None,
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
            return page
        selected = render.condense_events(page.events)
        if tail is not None:
            selected = selected[-tail:] if tail else []
            next_cursor = int(selected[-1]["i"]) if selected else since
        else:
            next_cursor = page.next_cursor
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
        "Show only events after this cursor; 0 or greater. Without --since or --limit, "
        "the last 20 events."
    ),
    limit=(
        "Bound records returned by this read; 0 or greater. Without --follow the default is "
        "20; with --follow an omitted limit is unbounded."
    ),
)
@main.command(name="log")
@click.argument("selector")
@click.option(
    "--since",
    type=click.IntRange(min=0),
    default=None,
    metavar="N",
    help="Show only events after this cursor; without --since or --limit, show the last 20 events.",
)
@click.option(
    "--limit",
    type=click.IntRange(min=0),
    default=None,
    metavar="N",
    help="Bound records; without --follow the default is 20, while --follow has no default limit.",
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
        "Collect events until the session ends; exit 124 on --timeout, 4 on --max-output; "
        "mutually exclusive with --wait-new."
    ),
)
@click.option(
    "--timeout",
    type=TimeoutParamType(allow_zero=True),
    default=None,
    metavar="S",
    help=(
        f"Give up waiting after this duration ({_DURATION_SYNTAX}, or 0) and exit 124; "
        "absent, it blocks indefinitely; "
        "requires --wait-new or --follow."
    ),
)
@click.option("--quiet", is_flag=True, help="Suppress the stderr footer.")
@click.help_option("-h", "--help")
def log_command(
    selector: str,
    since: int | None,
    limit: int | None,
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

    Without --since or --limit this shows the last 20 events.

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
    selection_tail = None if follow else limit
    if selection_tail is None and not explicit_since and not follow:
        selection_tail = _LOG_DEFAULT_TAIL

    if follow:
        _follow_log(
            meta,
            transcript_file,
            cursor=cursor,
            limit=limit,
            replay_tail=None if limit is not None else _FOLLOW_DEFAULT_TAIL,
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
        prose=prose,
        json_mode=json_mode,
        max_output=max_output,
        wait_new=wait_new,
        explicit_since=explicit_since,
        timeout=timeout,
        quiet=quiet,
        since_note=since_note,
    )


def _render_log_page(
    meta: sessions.SessionMeta,
    transcript_file: transcript.Transcript,
    *,
    cursor: int,
    tail: int | None,
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
    selection_tail = tail
    if wait_new and not explicit_since:
        cursor = _read_transcript_page(transcript_file).next_cursor
    page = _read_transcript_page(
        transcript_file,
        since=cursor,
        tail=selection_tail,
        condense=not prose and not json_mode,
    )
    timed_out = False
    gave_up_waiting = False
    if wait_new and not page.events:
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
        _raise_wait_timeout(meta.session_id, output_file)
    final = sessions.read_meta(meta.session_id)
    result = output.render_result(
        final,
        outcome.answer,
        json_mode=selected_format == "json",
        max_output=max_output,
        changed=True,
    )
    _emit_turn_result(
        result,
        output_file=output_file,
        json_mode=selected_format == "json",
        success=outcome.exit_code == vocab.EXIT_OK,
    )
    if outcome.state == "detached":
        _echo_metadata(
            f"-- detached, still RUNNING: {meta.session_id}"
            f" — answer: acpc wait {meta.session_id} · cancel: acpc cancel {meta.session_id}"
        )
        _end_turn_for(meta.session_id, outcome)
    if not quiet:
        _echo_metadata(output.format_summary(final, route_note=_route_note(outcome)))
    _end_turn_for(meta.session_id, outcome)


@effects.non_idempotent
@schema.format_defaults(tty="text", non_tty="text")
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
    help=(
        f"Stop waiting after this duration ({_DURATION_SYNTAX}); the session keeps running. "
        "Use --cancel-after to cancel the session."
    ),
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
@_json_option("Emit this command's output as JSON.")
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
    """Dispatch one agent; block by default, or use ``--background`` or ``--bg`` to detach.

    One prompt source is required, and prompts over 1 MiB are rejected before
    session creation. Permission defaults follow the TTY and output format.

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
    prompt = _read_prompt(prompt_text, prompt_file)
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
            json_mode=selected_format == "json",
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
    json_mode: bool,
    output_file: str | None = None,
    max_output: int = output.DEFAULT_MAX_OUTPUT,
) -> None:
    """Hand the turn to the daemon and print what the caller needs to find it.

    SPEC `run --bg`: stdout is exactly the session id and its directory, so a
    shell caller can read both without parsing prose.
    """
    import asyncio

    problem = asyncio.run(runner.dispatch_background(session_id, request))
    if problem is not None:
        # The session exists by now: the caller has to be able to reach it
        # even though the dispatch that would have run it failed (R7a).
        if output_file is not None:
            output.write_output_file(output_file, "")
        raise AgentProblem(problem, context={"session_id": session_id})
    meta = sessions.read_meta(session_id)
    result = output.render_result(
        meta,
        json_mode=json_mode,
        background=True,
        max_output=max_output,
        changed=True,
    )
    if not _write_rendered_file(output_file, result):
        _write_stdout(result.text)


@effects.non_idempotent
@schema.format_defaults(tty="text", non_tty="text")
@schema.reads_stdin("prompt_text")
@schema.describes(
    selector=_SELECTOR_HELP,
    prompt_text=_PROMPT_HELP,
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
    help=(
        f"Stop waiting after this duration ({_DURATION_SYNTAX}); the session keeps running. "
        "Use --cancel-after to cancel the session."
    ),
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
@_json_option("Emit this command's output as JSON.")
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
    """Continue a finished session; block and print the answer unless ``--background``.

    Model, effort, mode, permissions and home come from the session, not from
    re-resolving the agent entry — editing an entry never changes a session
    mid-conversation. A canceled session keeps its adapter context when the
    adapter supports continuation. ``--permissions`` is the one ``run`` resolution
    flag ``continue`` accepts: it applies to this turn and every turn after it.

    Example: ``acpc continue <session-id> "Run the tests again"``
    """
    selected_format = _select_format(format_name, json_mode, native_text=True)
    if background and timeout is not None:
        raise UsageProblem("--timeout only bounds waiting; use --cancel-after with --background")
    permissions = _normalize_permission(permissions)

    prompt = _read_prompt(prompt_text, prompt_file)
    meta = _load_view_session(selector)
    if meta.is_active:
        raise AcpcError(
            f"session {meta.session_id} is {meta.state} — wait for the current turn to finish",
            kind=errors.CONFLICT,
            retryable=True,
            hint=f"Run: acpc wait {meta.session_id}",
            context={"session_id": meta.session_id},
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
        json_mode=selected_format == "json",
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
    json_mode: bool,
) -> None:
    """Run the next turn on a finished session — `continue`'s machinery.

    `steer` is `continue` with a cancel in front of it, so both verbs end
    here: one turn on the session's stored resolution, one output contract.
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
            json_mode=json_mode,
            output_file=output_file,
            max_output=max_output,
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
        _raise_wait_timeout(meta.session_id, output_file)

    final = sessions.read_meta(meta.session_id)

    result = output.render_result(
        final,
        outcome.answer,
        json_mode=json_mode,
        max_output=max_output,
        changed=True,
    )
    _emit_turn_result(
        result,
        output_file=output_file,
        json_mode=json_mode,
        success=outcome.exit_code == vocab.EXIT_OK,
    )
    if outcome.state == "detached":
        _echo_metadata(
            f"-- detached, still RUNNING: {meta.session_id}"
            f" — answer: acpc wait {meta.session_id} · cancel: acpc cancel {meta.session_id}"
        )
        _end_turn_for(meta.session_id, outcome)
    if not quiet:
        _echo_metadata(output.format_summary(final, route_note=_route_note(outcome)))
    _end_turn_for(meta.session_id, outcome)


# SPEC `steer`: the preamble is fixed text, so the callee reads the redirect
# as a redirect rather than as a fresh unrelated task.
STEER_PREAMBLE = (
    "Your previous turn was interrupted by the operator; this instruction takes precedence:"
)


def _steer_prompt(instruction: str) -> str:
    return f"{STEER_PREAMBLE}\n\n{instruction}"


@effects.non_idempotent
@schema.format_defaults(tty="text", non_tty="text")
@schema.reads_stdin("instruction_text")
@schema.describes(
    selector=_SELECTOR_HELP,
    instruction_text=(
        "The redirect, or `-` to read it from stdin; --prompt-file is the third source and "
        f"exactly one of the three may be given. At most {_PROMPT_LIMIT_HELP} of UTF-8. "
        "An interrupted turn receives it under a fixed preamble."
    ),
    output_file=_OUTPUT_FILE_DESCRIPTION,
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
@click.option("--output-file", "output_file", metavar="FILE", help=_OUTPUT_FILE_HELP)
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
    help=(
        f"Stop waiting after this duration ({_DURATION_SYNTAX}); the session keeps running. "
        "Use --cancel-after to cancel the session."
    ),
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
@_json_option("Emit this command's output as JSON.")
@_color_option()
@click.help_option("-h", "--help")
def steer_command(
    selector: str,
    instruction_text: str | None,
    prompt_file: str | None,
    output_file: str | None,
    format_name: str | None,
    background: bool,
    timeout: float | None,
    cancel_after: float | None,
    max_output: int,
    quiet: bool,
    json_mode: bool,
) -> None:
    """Redirect the running turn; block and print the answer unless ``--background``.

    Interrupts the running turn and redirects the session in one verb: cancels
    the turn in flight (ACP session/cancel), waits for the ack, then
    starts the next turn with the instruction under a fixed preamble. If a
    daemon-owned ``continue`` is still preparing, no prompt has happened: acpc
    cancels that preparation, reports that nothing was interrupted, and sends
    the instruction plainly. The interrupted turn's partial answer is kept as
    that turn's answer file. A finished session is a usage error: there is no
    turn to interrupt, and the follow-up verb for it is ``acpc continue``.

    Example: ``acpc steer x7k2 "Stop editing; diagnose only"``
    """
    selected_format = _select_format(format_name, json_mode, native_text=True)
    if background and timeout is not None:
        raise UsageProblem("--timeout only bounds waiting; use --cancel-after with --background")
    instruction = _read_prompt(instruction_text, prompt_file)
    meta = _load_view_session(selector)
    if not meta.is_active:
        meta = _status_view_meta(meta)
    if not meta.is_active and meta.state != "preparing":
        raise AcpcError(
            f"session {meta.session_id} is {meta.state} — there is no turn to interrupt",
            kind=errors.CONFLICT,
            hint=f"Run: acpc continue {meta.session_id}",
            context={"session_id": meta.session_id},
        )

    meta = _cancel_session(meta).meta
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
        json_mode=selected_format == "json",
    )


@effects.read_only
@schema.format_defaults(tty="text", non_tty="text")
@schema.describes(selector=_SELECTOR_HELP, output_file=_OUTPUT_FILE_DESCRIPTION)
@main.command(name="wait")
@click.argument("selector")
@click.option(
    "--timeout",
    type=TimeoutParamType(allow_zero=True),
    default=None,
    metavar="S",
    help=(
        f"Stop waiting after this duration ({_DURATION_SYNTAX}, or 0) and exit 124; the "
        "session keeps running; absent, it blocks indefinitely."
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
    "--max-output",
    type=click.IntRange(min=0),
    default=output.DEFAULT_MAX_OUTPUT,
    metavar="BYTES",
    help="Cap rendered output bytes; 0 disables the cap.",
)
@click.option("--quiet", is_flag=True, help="Suppress the stderr summary line.")
@_json_option("Emit this command's output as JSON.")
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
    """Block until a background session finishes, then print its answer.

    The exit code mirrors the session result. On an already-finished session it
    returns immediately — the free way to reprint an answer. ``--timeout`` stops
    the waiting only and exits 124: the session keeps running. A session that failed
    adds a ``failure:`` segment to the stderr summary, naming what acpc observed and
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
        state = runner.wait_for_session(meta.session_id, timeout=timeout)
        if state is None:
            # SPEC `wait`: the timeout stops waiting only — the session runs on.
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
            raise AcpcError(
                f"gave up waiting for session {meta.session_id}; it is still running",
                kind=errors.TIMEOUT,
                exit_code=vocab.EXIT_TIMEOUT,
                retryable=True,
                hint=f"Run: acpc wait {meta.session_id}",
                context={
                    "session_id": meta.session_id,
                    "status": observed_status,
                    "retry_after_ms": int(runner.WAIT_POLL_INTERVAL * 1000),
                },
            )

        observed_status = state
        final = sessions.read_meta(meta.session_id)
        observed_status = final.state
        answer = _answer_text(meta.session_id)

        result = output.render_result(
            final,
            answer,
            json_mode=selected_format == "json",
            max_output=max_output,
        )
        exit_code = runner.exit_code_for(final.state, final.stop_reason)
        _emit_turn_result(
            result,
            output_file=output_file,
            json_mode=selected_format == "json",
            success=exit_code == vocab.EXIT_OK,
        )
        if not quiet:
            summary = output.format_summary(final)
            if final.state == "failed" and (
                failure_message := _latest_failure_message(final.session_id)
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


def _daemon_idle_column(idle_seconds: float | None) -> str:
    """Render a target's idle age, or `·` when it is serving or has no history."""
    if idle_seconds is None:
        return "·"
    return f"idle {output.format_duration(idle_seconds)}"


@effects.read_only
@main.group(name="daemon", invoke_without_command=False)
@click.help_option("-h", "--help")
def daemon_group() -> None:
    """Inspect and stop the per-target daemons.

    Example: ``acpc daemon status``
    """


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

    Example: ``acpc daemon status --json``
    """
    selected_format = _select_format(format_name, json_mode, plain=plain)
    if (
        selected_format == "plain"
        and ctx.get_parameter_source("limit") is not click.core.ParameterSource.COMMANDLINE
    ):
        raise UsageProblem("--plain requires an explicit --limit")
    import asyncio

    all_entries = asyncio.run(_collect_daemon_status(agent))
    entries = all_entries[:limit]
    if selected_format == "json":
        import json

        _write_stdout(
            json.dumps(
                {"items": entries, "has_more": len(entries) < len(all_entries)},
                ensure_ascii=False,
            )
            + "\n"
        )
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
    import asyncio

    targets = runner.daemon_targets_for(agent) if agent else runner.all_daemon_targets()
    if not force:
        _refuse_stop_over_active_sessions(agent, targets)
    if agent is None and not dry_run:
        # Last, after the precondition: a refusal caused by active sessions is
        # not something confirming the stop would resolve.
        interaction.require_confirmation(
            assume_yes,
            message="daemon stop: stopping every daemon on this machine needs confirmation",
            hint="Run: acpc daemon stop --dry-run to see them, then repeat with --yes",
        )
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
        "requires_confirmation": agent is None,
    }
    if selected_format == "json":
        _maintenance_json(payload)
    click.echo(
        f"-- {'would stop' if dry_run else 'stopped'} {len(stopped)} daemon(s)",
        err=True,
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
