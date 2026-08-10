"""CLI entry point.

SPEC.md *Command surface*. Verbs land slice by slice; this module owns flag
parsing, usage errors (exit 2), the TTY rules and the fixed exit codes, and
delegates everything else to the layer that owns it.
"""

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import click

from acpc import (
    __version__,
    cache,
    config,
    daemon_client,
    output,
    paths,
    proc,
    render,
    runner,
    sessions,
    skills,
    transcript,
    vocab,
)
from acpc.permissions import ModeSelectionError, PermissionLevel, select_mode
from acpc.registry import (
    AgentRegistry,
    CallResolution,
    FieldSource,
    RegistryError,
    ResolvedEntry,
)

_PERMISSION_CHOICES = (*vocab.PERMISSION_VALUES, *vocab.PERMISSION_ALIASES)
_WARNED_PERMISSION_ALIASES: set[str] = set()


class UsageProblem(click.ClickException):
    """A usage error: one actionable line on stderr, exit 2."""

    exit_code = vocab.EXIT_USAGE

    def format_message(self) -> str:
        return self.message


class AgentProblem(click.ClickException):
    """An agent-side failure: one actionable line on stderr, exit 1."""

    exit_code = vocab.EXIT_AGENT_ERROR

    def format_message(self) -> str:
        return self.message


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
  the id works mid-run with log / stop / steer.

Long or uncertain task (background):
  acpc run <agent> "Run the tests" --bg --json    # {"session_id": ..., "paths": ...}
  acpc wait <id> --quiet                          # block until done, prints the answer
  Truncated or huge answer? Read <dir>/answer.md selectively — always complete.
  In a shell that can background calls, `wait` becomes a completion push.

Checking on a run:
  acpc log <id>                    # the default: instant snapshot, condensed
  Need only the result? wait <id>. Don't block on a run you won't act on.

Supervising a risky run you intend to steer/stop mid-flight — the one
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
  run/wait it embeds the answer; add -o FILE to keep the answer out of it.
  Content reads best as markdown: answer.md, log --prose.
  Tight context: lower the cap, e.g. --max-output 16384.
  Every --timeout takes seconds (90) or a duration (90s, 5m, 1h).

Maintenance and setup:
  status            running + the 5 most recent finished (--all for every session)
  stop <id>         stop a running session; it stays resumable with continue
  rm <id>           delete a finished session's on-disk state
  prune             delete finished sessions older than retention (--older-than D)
  install <agent>   install the agent's adapter
  Killing acpc does not stop the session — acpc stop does.

Common commands:
  run, continue, steer, wait, status, log, agents, skills, daemon,
  stop, rm, prune, install
  Use `acpc <command> --help` for the command's full reference.

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
        """Render Click usage errors as the CLI's single actionable line."""
        if not kwargs.get("standalone_mode", True):
            return super().main(*args, **kwargs)
        kwargs["standalone_mode"] = False
        try:
            return super().main(*args, **kwargs)
        except click.UsageError as error:
            command_path = error.ctx.command_path if error.ctx is not None else None
            message = _friendly_usage_message(error.format_message(), command_path=command_path)
            click.echo(f"Error: {message}", err=True)
            raise SystemExit(error.exit_code) from None
        except click.ClickException as error:
            error.show()
            raise SystemExit(error.exit_code) from None


def _friendly_usage_message(message: str, *, command_path: str | None = None) -> str:
    """Replace known neighboring-tool spellings with their acpc equivalents."""
    follow_hint = (
        "--follow is not a flag on this command — following a session is: "
        "acpc log <id> --follow [--timeout S]"
    )
    detach_hint = (
        '--detach is not an acpc flag — background dispatch is: acpc run <agent> "<prompt>" --bg'
    )
    aliases = {
        "--follow": follow_hint,
        "-f": follow_hint,
        "--detach": detach_hint,
        "-d": detach_hint,
        "-C": "-C is not an acpc flag — the working-directory flag is --cwd DIR",
    }
    if "No such option" in message:
        for spelling, replacement in aliases.items():
            markers = (
                f"No such option: {spelling}",
                f"No such option '{spelling}'",
                f'No such option "{spelling}"',
            )
            if any(marker in message for marker in markers):
                return replacement
    command_parts = (command_path or "").split()
    daemon_group = command_parts[-1:] == ["daemon"]
    daemon_stop = command_parts[-2:] == ["daemon", "stop"]
    if daemon_stop and "No such option" in message:
        for marker in (
            "No such option: --all",
            "No such option '--all'",
            'No such option "--all"',
        ):
            if marker in message:
                return (
                    "--all is not a daemon flag — bare acpc daemon stop already addresses every "
                    "daemon"
                )
    if daemon_group and "No such command" in message:
        for spelling in ("list", "ls", "ps"):
            if (
                f"No such command '{spelling}'" in message
                or f'No such command "{spelling}"' in message
            ):
                return f"no such command '{spelling}' — the daemon view is: acpc daemon status"
        for spelling in ("start", "restart"):
            if (
                f"No such command '{spelling}'" in message
                or f'No such command "{spelling}"' in message
            ):
                return (
                    f"no such command '{spelling}' — daemons start on first use; acpc daemon "
                    "stop <agent> and the next run is the restart"
                )
    if "No such command" in message and (
        "No such command 'logs'" in message or 'No such command "logs"' in message
    ):
        return "no such command 'logs' — the viewing command is: acpc log <id>"
    return message


@click.group(
    cls=_CheatSheetGroup,
    invoke_without_command=True,
    context_settings={"show_default": True},
)
@click.version_option(__version__, "-V", "--version", message="acpc %(version)s")
@click.help_option("-h", "--help")
@click.pass_context
def main(ctx: click.Context) -> None:
    """acpc — dispatch coding agents over ACP."""
    # Fresh per invocation: in-process callers (tests) would otherwise inherit
    # the previous command's unterminated-stdout state.
    global _stdout_line_open
    _stdout_line_open = False
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


def _stdout_is_tty() -> bool:
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _read_prompt(prompt_text: str | None, prompt_file: str | None) -> str:
    """Resolve the single prompt source, or fail naming the options."""
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
        return sys.stdin.read()
    if prompt_file is not None:
        try:
            return Path(prompt_file).expanduser().read_text(encoding="utf-8")
        except OSError as error:
            raise UsageProblem(f"--prompt-file {prompt_file}: {error.strerror}") from None
    return prompt_text or ""


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


def _stored_permission_policy(meta: sessions.SessionMeta) -> str:
    """Read and normalize the permission policy from a validated session."""
    resolved = meta.resolution.get("resolved")
    if not isinstance(resolved, dict):
        raise UsageProblem(f"session {meta.session_id} has no stored permission resolution")
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
        resolution = entry.resolve_call(
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
        raise UsageProblem(str(error)) from None


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
        raise UsageProblem(f"session {meta.session_id} has no stored resolution object")
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
    adapter = payload.get("adapter")
    if not isinstance(adapter, dict):
        adapter = {}
    adapter = {
        key: adapter[key] for key in ("home_env", "effort_config_id", "modes") if key in adapter
    }
    adapter["modes"] = runner.mode_catalog_payload(resolution.entry.modes)
    if resolution.mode is not None and resolution.mode_spec is not None:
        adapter.update(
            mode=resolution.mode,
            grants=resolution.mode_spec.grants,
            delegates=resolution.mode_spec.delegates,
        )
    payload["adapter"] = adapter
    return payload


def _target_for_persisted_resolution(meta: sessions.SessionMeta) -> str:
    """Key the daemon from the exact resolution the next turn will rebuild."""
    return runner.call_target(runner.resolution_from_session(meta))


def _finalize_follow_up_failure(session_id: str, error: BaseException) -> None:
    """Close a rotated turn when request preparation cannot finish."""
    with contextlib.suppress(OSError, sessions.SessionError):
        sessions.write_answer(session_id, f"{error}\n")
    with contextlib.suppress(OSError, sessions.SessionError):
        sessions.transition(
            session_id,
            "failed",
            exit_code=vocab.EXIT_AGENT_ERROR,
            stop_reason="error",
        )


def _resolve_permissions(
    explicit: str | None,
    resolution: CallResolution,
    *,
    tty: bool,
    background: bool = False,
) -> tuple[str, tuple[str, str] | None]:
    """Apply SPEC's TTY rules to the resolved permission policy.

    A background client has already gone away by the time a permission
    request arrives, so `--bg` follows the non-TTY rule even when stdout is
    a terminal — and says so, because "needs a terminal" is baffling advice
    to someone who is sitting at one.
    """
    interactive = tty and not background
    policy = explicit if explicit is not None else resolution.permissions
    if policy is None:
        policy = "ask" if interactive else "read"
    policy, clamp = _clamp_inherited_ceiling(policy)
    if policy == "ask" and not interactive:
        cause = (
            "cannot be used with --bg, which returns before a request could be answered"
            if background
            else "needs a terminal to ask on"
        )
        raise UsageProblem(
            f"--permissions ask {cause}; pass --permissions none, read, edit, execute or all"
        )
    return policy, clamp


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
                f"available policies: {supported}"
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
) -> UsageProblem:
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
    return UsageProblem(f"{reason}; declared modes: {modes}")


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

    The terminal is opened twice, once per direction: a single "r+" handle
    raises `io.UnsupportedOperation: File or stream is not seekable`, which is
    an OSError and would be caught below as a silent denial.
    """
    try:
        with (
            open("/dev/tty", "w", encoding="utf-8") as ask,
            open("/dev/tty", encoding="utf-8") as answer,
        ):
            ask.write(f"acpc: allow {kind}? {title} [y/N] ")
            ask.flush()
            return answer.readline().strip().lower() in {"y", "yes"}
    except OSError:
        return False


def _emit_dry_run(payload: dict[str, Any], *, json_mode: bool) -> None:
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
            lines.append(f"{name:<12} {value} ({reason}) · {delegation}")
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


def _write_stdout(text: str) -> None:
    """Write the answer, turning a closed stdout into SPEC's exit 141."""
    global _stdout_line_open
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except BrokenPipeError:
        # Keep the interpreter's own shutdown flush from raising again.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        sys.exit(vocab.EXIT_SIGPIPE)
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
    status = entry.install_status
    if status == "missing":
        status = f"missing → acpc install {entry.entry}"
    description = (
        render.snippet(entry.description, limit=_ROSTER_DESCRIPTION_LIMIT)
        if entry.description is not None
        else ""
    )
    return (entry.entry, entry.name, status, description)


def _variant_row(entry: ResolvedEntry) -> tuple[str, ...]:
    values = {
        field: _local_variant_value(entry, field)
        for field in ("model", "effort", "permissions", "home")
    }
    description = (
        render.snippet(entry.description, limit=_ROSTER_DESCRIPTION_LIMIT)
        if entry.description is not None
        else ""
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
    description = (
        render.snippet(skill.description, limit=_ROSTER_DESCRIPTION_LIMIT)
        if skill.description is not None
        else ""
    )
    return (skill.name, description)


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
    return {"agents": rows}


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
    return f"{name} ({spec.grants}{delegates})"


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
        raise cache.ProbeError(
            f"{entry.entry}: '{entry.command_head}' is not installed — "
            f"run 'acpc install {entry.base_adapter}'"
        )
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
                    {"grants": entry.modes[mode].grants, "delegates": entry.modes[mode].delegates}
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
        lines.append(f"description  {entry.description}")

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
            rendered = "ask on TTY, read otherwise"
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
        lines.append(f"-- modes/models/commands: acpc agents {entry.base_adapter}")
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


class _NamedViewGroup(click.Group):
    """Treat an unknown first word as the optional named-view argument.

    Subclasses name the command that renders one item; group-level flags the
    caller already typed are forwarded to it, so `--json <name>` and
    `<name> --json` are the same call.
    """

    @property
    def view_command(self) -> click.Command:
        """The command that renders one named item."""
        raise NotImplementedError

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        if args and not args[0].startswith("-") and args[0] not in self.commands:
            forwarded = list(args)
            for parameter in self.params:
                if not isinstance(parameter, click.Option) or not parameter.is_flag:
                    continue
                if parameter.name is None or not ctx.params.get(parameter.name):
                    continue
                flag = next((option for option in parameter.opts if option.startswith("--")), None)
                if flag is not None:
                    forwarded.append(flag)
            return args[0], self.view_command, forwarded
        return super().resolve_command(ctx, args)


class _AgentsGroup(_NamedViewGroup):
    """Treat an unknown first word as the optional agent view name."""

    @property
    def view_command(self) -> click.Command:
        return _agent_view_command


class _SkillsGroup(_NamedViewGroup):
    """Treat an unknown first word as the optional skill view name."""

    @property
    def view_command(self) -> click.Command:
        return _skill_view_command


def _models_overview(registry: AgentRegistry) -> tuple[str, dict[str, Any], list[str]]:
    lines: list[str] = []
    payload: dict[str, Any] = {"agents": []}
    footer_agents: list[str] = []
    for entry in registry.adapters:
        record = cache.read_advertised(entry.entry)
        advertised = _advertised_payload(record)
        models = [str(item) for item in advertised.get("models", [])]
        lines.append(entry.entry)
        lines.extend(
            render.format_table(
                [
                    (tier, preset.model, preset.effort or "·")
                    for tier, preset in entry.presets.items()
                ],
                header=("tier", "model", "effort"),
                prefix="  presets   ",
                continuation_prefix=" " * 12,
            )
        )
        lines.append("  models    " + (" · ".join(models) if models else "·"))
        variants = [item for item in registry.variants if item.base_adapter == entry.entry]
        if variants:
            lines.extend(
                render.format_table(
                    [
                        (
                            variant.entry,
                            _local_variant_value(variant, "model") or "·",
                            _local_variant_value(variant, "effort") or "·",
                        )
                        for variant in variants
                    ],
                    header=("entry", "model", "effort"),
                    prefix="  variants  ",
                    continuation_prefix=" " * 12,
                )
            )
        payload["agents"].append(
            {
                "name": entry.entry,
                "presets": {
                    tier: {"model": item.model, "effort": item.effort}
                    for tier, item in entry.presets.items()
                },
                "models": models,
                "variants": [
                    {
                        "name": variant.entry,
                        "model": _local_variant_value(variant, "model"),
                        "effort": _local_variant_value(variant, "effort"),
                    }
                    for variant in variants
                ],
            }
        )
        if record is not None:
            footer_agents.append(
                f"{entry.entry} {cache.cache_age(record.cached_at)}"
                if cache.cache_age(record.cached_at) == "now"
                else f"{entry.entry} {cache.cache_age(record.cached_at)} ago"
            )
    lines.append("-- cached: " + " · ".join(footer_agents))
    return "\n".join(lines) + "\n", payload, footer_agents


def _emit_json(payload: Mapping[str, Any]) -> None:
    _write_stdout(json.dumps(dict(payload), ensure_ascii=False) + "\n")


def _run_agents_view(
    name: str | None,
    models: bool,
    commands: bool,
    check_live: bool,
    json_mode: bool,
) -> None:
    """List adapters and variants, or inspect advertised adapter data."""
    if models and commands:
        raise UsageProblem("--models and --commands are mutually exclusive views")
    try:
        registry = AgentRegistry()
        if check_live:
            _agents_check(registry, name, json_mode=json_mode)
        elif models:
            if name is None:
                text, payload, footer = _models_overview(registry)
                if json_mode:
                    _emit_json(payload)
                    click.echo("-- cached: " + " · ".join(footer), err=True)
                else:
                    _write_stdout(text)
            else:
                entry = registry.resolve(name)
                record = _ensure_cache(entry)
                text, payload = _render_models(entry, record)
                if json_mode:
                    _emit_json(payload)
                    click.echo(_cache_footer(record), err=True)
                else:
                    _write_stdout(text)
        elif commands:
            if name is None:
                raise UsageProblem("--commands requires an agent name")
            entry = registry.resolve(name)
            record = _ensure_cache(entry)
            text, payload = _render_commands(entry, record)
            if json_mode:
                _emit_json(payload)
                click.echo(
                    _commands_footer(entry.base_adapter, list(payload["commands"]), record),
                    err=True,
                )
            else:
                _write_stdout(text)
        elif name is None:
            payload = _agent_list_payload(registry)
            if json_mode:
                _emit_json(payload)
            else:
                variants = {
                    adapter.entry: [
                        item for item in registry.variants if item.base_adapter == adapter.entry
                    ]
                    for adapter in registry.adapters
                }
                adapter_items = list(registry.adapters)
                adapter_lines = render.format_table(
                    [_agent_row(adapter) for adapter in adapter_items], separator="  "
                )
                adapter_lines_by_entry = {
                    adapter.entry: adapter_lines[index]
                    for index, adapter in enumerate(adapter_items)
                }
                variant_items = [
                    item for adapter in registry.adapters for item in variants[adapter.entry]
                ]
                variant_lines = render.format_table(
                    [_variant_row(item) for item in variant_items],
                    header=("entry", "model", "effort", "permissions", "home", "description"),
                    prefix="  ",
                    separator="  ",
                )
                variant_lines_by_entry = {
                    item.entry: variant_lines[index + 1] for index, item in enumerate(variant_items)
                }
                lines: list[str] = []
                variant_header_added = False
                for adapter in adapter_items:
                    lines.append(adapter_lines_by_entry[adapter.entry])
                    if variants[adapter.entry] and not variant_header_added:
                        lines.append(variant_lines[0])
                        variant_header_added = True
                    lines.extend(
                        variant_lines_by_entry[item.entry] for item in variants[adapter.entry]
                    )
                _write_stdout("\n".join(lines) + "\n")
        else:
            entry = registry.resolve(name)
            if entry.is_variant:
                text, payload, _ = _render_entry_detail(registry, entry)
                if json_mode:
                    _emit_json(payload)
                else:
                    _write_stdout(text)
            else:
                text, payload, _ = _render_entry_detail(registry, entry)
                record = _ensure_cache(entry)
                advertised_text, advertised_payload = _render_advertised_detail(entry, record)
                payload.update(advertised_payload)
                if json_mode:
                    _emit_json(payload)
                    click.echo(_cache_footer(record), err=True)
                else:
                    _write_stdout(text + advertised_text)
    except cache.ProbeError as error:
        if json_mode:
            _emit_json({"error": str(error)})
            raise SystemExit(vocab.EXIT_AGENT_ERROR) from None
        raise AgentProblem(str(error)) from None
    except RegistryError as error:
        if json_mode:
            _emit_json({"error": str(error)})
            raise SystemExit(vocab.EXIT_USAGE) from None
        raise UsageProblem(str(error)) from None


@main.group(name="agents", cls=_AgentsGroup, invoke_without_command=True)
@click.option("--models", is_flag=True, help="Show full advertised presets and models.")
@click.option("--commands", is_flag=True, help="Show advertised slash commands.")
@click.option(
    "--check",
    "check_live",
    is_flag=True,
    help="Launch, authenticate and apply the resolved options.",
)
@click.option("--json", "json_mode", is_flag=True, help="Emit this view as JSON.")
@click.help_option("-h", "--help")
@click.pass_context
def agents_group(
    ctx: click.Context,
    models: bool,
    commands: bool,
    check_live: bool,
    json_mode: bool,
) -> None:
    """List adapters and variants, or inspect advertised adapter data.

    Example: ``acpc agents mock --models``
    """
    if ctx.invoked_subcommand is None:
        _run_agents_view(None, models, commands, check_live, json_mode)


@click.command(name="agent-view")
@click.argument("name")
@click.option("--models", is_flag=True, help="Show full advertised presets and models.")
@click.option("--commands", is_flag=True, help="Show advertised slash commands.")
@click.option(
    "--check",
    "check_live",
    is_flag=True,
    help="Launch, authenticate and apply the resolved options.",
)
@click.option("--json", "json_mode", is_flag=True, help="Emit this view as JSON.")
@click.help_option("-h", "--help")
def _agent_view_command(
    name: str, models: bool, commands: bool, check_live: bool, json_mode: bool
) -> None:
    """Render one named adapter or variant.

    Example: ``acpc agents mock --commands``
    """
    _run_agents_view(name, models, commands, check_live, json_mode)


def _agents_check(registry: AgentRegistry, name: str | None, *, json_mode: bool) -> None:
    if name is None:
        entries = [entry for entry in registry.adapters if entry.installed]
    else:
        selected = registry.resolve(name)
        entries = [registry.resolve(selected.base_adapter)]
    results: list[dict[str, Any]] = []
    for entry in entries:
        try:
            advertised = asyncio.run(cache.probe_advertised(registry.resolve_call(entry.entry)))
            result = {"agent": entry.entry, "ok": True, "models": len(advertised["models"])}
        except cache.ProbeError as error:
            result = {"agent": entry.entry, "ok": False, "error": str(error)}
        results.append(result)
    if json_mode:
        _emit_json({"checks": results})
    else:
        for result in results:
            if result["ok"]:
                _write_stdout(f"{result['agent']} ok\n")
            else:
                _write_stdout(f"{result['agent']} failed: {result['error']}\n")
    if any(not result["ok"] for result in results):
        raise SystemExit(vocab.EXIT_AGENT_ERROR)


@agents_group.command(name="init")
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
@click.option("--json", "json_mode", is_flag=True, help="Emit the created entry as JSON.")
@click.help_option("-h", "--help")
def agents_init_command(
    name: str,
    parent: str,
    model: str | None,
    effort: str | None,
    mode: str | None,
    permissions: str | None,
    home: str | None,
    json_mode: bool,
) -> None:
    """Scaffold a variant entry.

    Example: ``acpc agents init work --extends mock --permissions execute``
    """
    permissions = _normalize_permission(permissions)
    try:
        registry = AgentRegistry()
        registry.resolve(parent)
        if effort is not None:
            registry.resolve_call(parent, effort=effort)
    except RegistryError as error:
        if json_mode:
            _emit_json({"error": str(error)})
            raise SystemExit(vocab.EXIT_USAGE) from None
        raise UsageProblem(str(error)) from None
    if not name or "/" in name or "\\" in name or name in {".", ".."}:
        raise UsageProblem(f"invalid agent name '{name}'")
    target = paths.agents_dir() / f"{name}.toml"
    if target.exists():
        raise UsageProblem(f"agent entry already exists: {target}")
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
    except OSError as error:
        raise UsageProblem(f"cannot write {target}: {error}") from None
    payload = {"name": name, "extends": parent, "path": str(target)}
    if json_mode:
        _emit_json(payload)
    else:
        _write_stdout(f"created {target}\n")


def _run_skills_view(name: str | None, *, json_mode: bool) -> None:
    """List bundled skills or render one skill's body and directory."""
    if name is None:
        bundled = skills.list_skills()
        if json_mode:
            _emit_json({"skills": [_skill_payload(skill, include_body=False) for skill in bundled]})
            return
        rows = render.format_table(
            [_skill_row(skill) for skill in bundled],
            header=("name", "description"),
        )
        _write_stdout("\n".join(rows) + "\n")
        return

    try:
        skill = skills.get_skill(name)
    except skills.SkillNotFoundError:
        raise UsageProblem(f"unknown skill {name!r} — use acpc skills") from None

    if json_mode:
        _emit_json(_skill_payload(skill, include_body=True))
    else:
        _write_stdout(skill.body)
    _echo_metadata(f"-- skill {skill.name} | dir {skill.path}")


@main.group(name="skills", cls=_SkillsGroup, invoke_without_command=True)
@click.option("--json", "json_mode", is_flag=True, help="Emit this view as JSON.")
@click.help_option("-h", "--help")
@click.pass_context
def skills_group(ctx: click.Context, json_mode: bool) -> None:
    """List bundled skills, or render one named skill.

    Example: ``acpc skills provider-bringup``
    """
    if ctx.invoked_subcommand is None:
        _run_skills_view(None, json_mode=json_mode)


@click.command(name="skill-view")
@click.argument("name")
@click.option("--json", "json_mode", is_flag=True, help="Emit this view as JSON.")
@click.help_option("-h", "--help")
def _skill_view_command(name: str, json_mode: bool) -> None:
    """Render one named bundled skill.

    Example: ``acpc skills provider-bringup``
    """
    _run_skills_view(name, json_mode=json_mode)


@main.command(name="install")
@click.argument("agent")
@click.option("--json", "json_mode", is_flag=True, help="Emit the install result as JSON.")
@click.help_option("-h", "--help")
def install_command(agent: str, json_mode: bool) -> None:
    """Run an agent's install command from its registry entry.

    Resolves the agent like ``run`` does, runs its ``install_command`` and
    relays the installer's output; a failing installer exits 1.

    Example: ``acpc install codex``
    """
    try:
        registry = AgentRegistry()
        registry.resolve(agent)

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
        if json_mode:
            _emit_json({"agent": agent, "ok": False, "error": str(error)})
            raise SystemExit(vocab.EXIT_USAGE) from None
        raise UsageProblem(str(error)) from None
    except OSError as error:
        message = f"install {agent} failed: {error}"
        if json_mode:
            _emit_json({"agent": agent, "ok": False, "error": message})
            raise SystemExit(vocab.EXIT_AGENT_ERROR) from None
        raise AgentProblem(message) from None

    for stream in (getattr(result, "stdout", None), getattr(result, "stderr", None)):
        if stream:
            click.echo(stream.rstrip("\n"), err=True)
    return_code = getattr(result, "returncode", 1)
    payload = {"agent": agent, "ok": return_code == 0, "returncode": return_code}
    if json_mode:
        _emit_json(payload)
    elif return_code == 0:
        _write_stdout(f"installed {agent}\n")
    else:
        raise AgentProblem(f"install {agent} failed (exit {return_code})")
    if return_code != 0:
        raise SystemExit(vocab.EXIT_AGENT_ERROR)


async def _cancel_with_daemon(target: str, session_id: str) -> bool | None:
    """Request cancellation without allowing a dead daemon to hang ``stop``."""
    try:
        return await asyncio.wait_for(
            daemon_client.cancel_turn(target, session_id),
            timeout=runner.CANCEL_ACK_TIMEOUT,
        )
    except TimeoutError:
        return None
    except Exception:  # noqa: BLE001
        return False


def _wait_for_stop(session_id: str) -> sessions.SessionMeta:
    """Give a daemon's cancellation time to finalize the session on disk."""
    deadline = time.monotonic() + runner.CANCEL_ACK_TIMEOUT
    while True:
        meta = sessions.load(session_id)
        if not meta.is_active or time.monotonic() >= deadline:
            return meta
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


def _cancel_session(meta: sessions.SessionMeta) -> sessions.SessionMeta:
    """Cancel an active session and return the state it settled into.

    SPEC `stop`: graceful `session/cancel` with a bounded wait for the ack,
    torn down anyway if the callee will not wind down in time. `steer` puts
    the same cancel in front of a follow-up turn, so it lives here rather
    than inside `stop`.
    """
    cancelled = False
    if meta.target is not None:
        cancelled = asyncio.run(_cancel_with_daemon(meta.target, meta.session_id))
    if cancelled is True:
        return _wait_for_stop(meta.session_id)
    if cancelled is None:
        return sessions.load(meta.session_id)
    if meta.pid is None:
        return sessions.transition(
            meta.session_id,
            "cancelled",
            exit_code=vocab.EXIT_CANCELLED,
            stop_reason="stopped by user",
        )
    result = proc.kill_process_tree(meta.pid, meta.process_start_time)
    if result == "refused":
        raise UsageProblem(f"could not stop session {meta.session_id}: refused to signal it")
    return _wait_for_stop(meta.session_id)


def _maintenance_json(payload: Mapping[str, Any]) -> None:
    _write_stdout(json.dumps(dict(payload), ensure_ascii=False) + "\n")


@main.command(name="stop")
@click.argument("selector")
@click.option("--json", "json_mode", is_flag=True, help="Emit the result as JSON.")
@click.help_option("-h", "--help")
def stop_command(selector: str, json_mode: bool) -> None:
    """Stop a running session; it stays resumable with ``acpc continue``.

    Cancels the turn in flight (ACP ``session/cancel``) and waits up to 10s for the
    ack; past that the connection is torn down anyway. Transcript, meta and the
    partial answer stay on disk for post-mortem. Stopping an already-finished
    session is a successful no-op that reports the state it found; an unknown id is
    a usage error.

    Example: ``acpc stop q7x2``
    """
    meta = _load_view_session(selector)
    if meta.is_active:
        meta = _cancel_session(meta)

    payload = {
        "session_id": meta.session_id,
        "state": meta.state,
        "stop_reason": meta.stop_reason,
    }
    if json_mode:
        _maintenance_json(payload)
    else:
        _write_stdout(f"{meta.session_id} {meta.state}\n")
    click.echo(f"-- stop {meta.session_id} · {meta.state}", err=True)


@main.command(name="rm")
@click.argument("selector")
@click.option("--json", "json_mode", is_flag=True, help="Emit the result as JSON.")
@click.help_option("-h", "--help")
def rm_command(selector: str, json_mode: bool) -> None:
    """Delete a finished session's on-disk state.

    Errors on a starting or running session — stop it first. Prints the
    removed session id; ``--json`` also lists the deleted paths.

    Example: ``acpc rm q7x2``
    """
    meta = _load_view_session(selector)
    advertised_paths = sessions.session_paths(meta.session_id)
    try:
        sessions.delete_session(meta.session_id)
    except sessions.SessionStateError as error:
        raise UsageProblem(str(error)) from None
    payload = {"session_id": meta.session_id, "removed": True, "paths": advertised_paths}
    if json_mode:
        _maintenance_json(payload)
    else:
        _write_stdout(f"removed {meta.session_id}\n")
    click.echo(f"-- removed session {meta.session_id}", err=True)


@main.command(name="prune")
@click.option("--older-than", default=None, metavar="D", help="Age threshold, such as 7d.")
@click.option("--dry-run", is_flag=True, help="List candidates without deleting them.")
@click.option("--json", "json_mode", is_flag=True, help="Emit the result as JSON.")
@click.help_option("-h", "--help")
def prune_command(older_than: str | None, dry_run: bool, json_mode: bool) -> None:
    """Delete finished sessions older than the retention period.

    Bare ``prune`` uses the ``retention`` key in the global config
    (``~/.acpc/config.toml``, default 90d; ``ACPC_HOME`` moves the root) — it is
    never "delete everything". ``--older-than`` overrides it for this call, and
    deleting every finished session takes an explicit ``--older-than 0d``. Age is
    measured from when the session finished. Running sessions are never touched.

    Example: ``acpc prune --older-than 7d --dry-run``
    """
    try:
        settings = config.load_config()
        raw_duration = older_than if older_than is not None else settings.retention
        duration = config.parse_duration(raw_duration, allow_zero=True)
        if older_than is None and duration <= 0:
            raise UsageProblem(
                f"config retention '{settings.retention}' resolves to zero — bare prune would "
                "delete every finished session; pass --older-than 0d to do that explicitly"
            )
        candidates = sessions.prune_sessions(older_than=duration, dry_run=dry_run)
    except UsageProblem:
        raise
    except (config.ConfigError, ValueError, sessions.SessionError) as error:
        raise UsageProblem(str(error)) from None

    session_ids = [meta.session_id for meta in candidates]
    payload = {"sessions": session_ids, "dry_run": dry_run}
    if json_mode:
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
        session_id = sessions.resolve_selector(selector, allow_last=_stdout_is_tty())
        return sessions.load(session_id)
    except sessions.SessionError as error:
        raise UsageProblem(str(error)) from None


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
        raise UsageProblem(str(error)) from None


@main.command(name="status")
@click.argument("selector", required=False)
@click.option(
    "--all",
    "all_sessions",
    is_flag=True,
    help="Show every session, not just running + the 5 most recent finished.",
)
@click.option("--json", "json_mode", is_flag=True, help="Emit a JSON status object.")
@click.help_option("-h", "--help")
def status_command(selector: str | None, all_sessions: bool, json_mode: bool) -> None:
    """Show liveness-verified session metadata without reading transcripts.

    With no id and no ``--all``: every running session plus the 5 most recent
    finished ones. With an id: that session's vitals. State is verified against the
    process behind it, so a ``running`` session whose process is gone reads
    ``orphaned`` rather than a stale ``running``.

    Example: ``acpc status <session-id> --json``
    """
    if selector is not None and all_sessions:
        raise UsageProblem("--all cannot be used with a session id")

    if selector is not None:
        meta = _load_view_session(selector)
        if json_mode:
            _write_stdout(json.dumps(render.status_detail_json(meta), ensure_ascii=False) + "\n")
        else:
            _write_stdout(render.render_status_detail(meta))
        return

    metas = sessions.list_sessions()
    if json_mode:
        _write_stdout(
            json.dumps(
                render.status_list_json(metas, all_sessions=all_sessions), ensure_ascii=False
            )
            + "\n"
        )
    else:
        _write_stdout(render.render_status_list(metas, all_sessions=all_sessions))


@main.command(name="log")
@click.argument("selector")
@click.option(
    "--since",
    type=click.IntRange(min=0),
    default=None,
    metavar="N",
    help="Show only events after this cursor; without --since or --tail, show the last 20 events.",
)
@click.option(
    "--tail",
    type=click.IntRange(min=0),
    default=None,
    metavar="N",
    help="Show only the last N selected events; without --since or --tail, show the last 20 events.",
)
@click.option("--prose", is_flag=True, help="Render full agent messages.")
@click.option("--json", "json_mode", is_flag=True, help="Emit raw transcript events as NDJSON.")
@click.option(
    "--max-output",
    type=click.IntRange(min=0),
    default=render.DEFAULT_LOG_MAX_OUTPUT,
    metavar="BYTES",
    help="Cap rendered output bytes; 0 disables the cap.",
)
@click.option("--wait-new", is_flag=True, help="Wait for new transcript events.")
@click.option(
    "-f",
    "--follow",
    is_flag=True,
    help="Collect events until the session ends; exit 124 on --timeout, 4 on --max-output.",
)
@click.option(
    "--timeout",
    type=TimeoutParamType(allow_zero=True),
    default=None,
    metavar="S",
    help=(
        "Give up waiting after this duration (exit 124); absent, it blocks indefinitely; "
        "requires --wait-new or --follow."
    ),
)
@click.option("--quiet", is_flag=True, help="Suppress the stderr footer.")
@click.help_option("-h", "--help")
def log_command(
    selector: str,
    since: int | None,
    tail: int | None,
    prose: bool,
    json_mode: bool,
    max_output: int,
    wait_new: bool,
    follow: bool,
    timeout: float | None,
    quiet: bool,
) -> None:
    """Render selected transcript events and keep metadata on stderr.

    Without --since or --tail this shows the last 20 events.

    Example: ``acpc log <session-id> --prose --since 0``
    """
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
        raise UsageProblem(str(error)) from None
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
    if selection_tail is None and not explicit_since:
        selection_tail = _FOLLOW_DEFAULT_TAIL if follow else _LOG_DEFAULT_TAIL

    if follow:
        _follow_log(
            meta,
            transcript_file,
            cursor=cursor,
            tail=selection_tail,
            prose=prose,
            json_mode=json_mode,
            max_output=max_output,
            timeout=timeout,
            quiet=quiet,
            since_note=since_note,
            condense=not prose and not json_mode,
        )
        return

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

    full_last_message = meta.state in {"failed", "timeout", "orphaned"}
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
        raise SystemExit(vocab.EXIT_TIMEOUT)


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
) -> tuple[int, int, bool, int | None, int | None]:
    """Render one page inside the follow budget.

    SPEC `log --follow`: `--max-output` budgets the whole stream, so each page
    is rendered against what is left of it.  A budget with nothing left still
    renders one byte's worth, which is how the marker naming the transcript
    reaches stdout instead of a silent stop.
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
    stream — `--tail 0` means "from here on", so with nothing to replay the
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
    tail: int | None,
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
    cursor = _follow_start_cursor(transcript_file, since=cursor, tail=tail, condense=condense)
    used = 0
    exhausted = False
    timed_out = False
    page_start: int | None = None
    page_end: int | None = None

    while True:
        page = _read_transcript_page(transcript_file, since=cursor)
        if page.events:
            cursor, used, exhausted, rendered_start, rendered_end = _emit_follow_page(
                page.events,
                prose=prose,
                json_mode=json_mode,
                max_output=max_output,
                used=used,
                transcript_path=transcript_path,
                cursor=cursor,
            )
            if rendered_start is not None:
                if page_start is None:
                    page_start = rendered_start
                page_end = rendered_end
            if exhausted:
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

    # The follow outlived the state it started with; the footer is the caller's
    # termination signal, so it has to be current.
    meta = _load_view_session(meta.session_id)
    if not quiet:
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
        raise SystemExit(vocab.EXIT_BUDGET)
    if timed_out:
        raise SystemExit(vocab.EXIT_TIMEOUT)


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
        f"acpc stop {session_id} to cancel"
    )


@main.command(name="run")
@click.argument("agent")
@click.argument("prompt_text", required=False)
@click.option("--prompt-file", "prompt_file", metavar="FILE", help="Read the prompt from a file.")
@click.option("--cwd", metavar="DIR", help="Working directory of the callee.")
@click.option("--model", metavar="M", help="Model tier (fast/standard/max) or a raw model ID.")
@click.option("--effort", metavar="E", help="Reasoning effort level.")
@click.option(
    "--permissions",
    type=click.Choice(_PERMISSION_CHOICES),
    metavar="P",
    help=(
        "\b\n"
        "Permission scale: none, read, edit, execute, all or ask; absent, ask on a TTY and "
        "read otherwise (--bg counts as non-TTY). execute permits read, edit and execute; "
        "write and prompt are deprecated aliases for execute and ask."
    ),
)
@click.option(
    "--mode",
    metavar="M",
    help=(
        "Vendor mode override; normally unnecessary because --permissions selects the mode. "
        "Refused when it grants more than the policy; values from agents <name>."
    ),
)
@click.option("--home", metavar="DIR", help="Vendor home override.")
@click.option("-o", "--output", "output_file", metavar="FILE", help="Write the answer to a file.")
@click.option(
    "--timeout",
    type=TimeoutParamType(),
    metavar="S",
    help="Cancel the session after this duration; absent, no wall-clock limit (the callee runs until it is done).",
)
@click.option("--name", "alias", metavar="ALIAS", help="Human-typeable handle for this session.")
@click.option("--dry-run", is_flag=True, help="Print what this call resolves to, then exit.")
@click.option(
    "--max-output",
    type=click.IntRange(min=0),
    default=output.DEFAULT_MAX_OUTPUT,
    metavar="BYTES",
    help="Cap on stdout bytes; 0 disables the cap.",
)
@click.option("--bg", "background", is_flag=True, help="Dispatch and return the session id.")
@click.option("--quiet", is_flag=True, help="Suppress the stderr summary line.")
@click.option("--json", "json_mode", is_flag=True, help="Emit this command's output as JSON.")
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
    timeout: float | None,
    alias: str | None,
    dry_run: bool,
    background: bool,
    max_output: int,
    quiet: bool,
    json_mode: bool,
) -> None:
    """Dispatch one agent; block and print the final answer.

    The default permission policy is ``ask`` on a TTY and ``read``
    otherwise.  Background calls always use the non-TTY rule.

    Example: ``acpc run codex "Fix the failing test" --permissions execute``
    """
    permissions = _normalize_permission(permissions)
    tty = _stdout_is_tty()

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
    except RegistryError as error:
        raise UsageProblem(str(error)) from None

    defaulted_permissions = permissions is None and resolution.permissions is None
    policy, permissions_clamp = _resolve_permissions(
        permissions, resolution, tty=tty, background=background
    )
    # The TTY-resolved policy is part of the resolved invocation: meta.json
    # stores everything --dry-run shows, and `continue` reuses it verbatim.
    resolution = _select_resolution(
        replace(resolution, permissions=policy, permissions_clamp=permissions_clamp)
    )
    # Resolved to an absolute path here, at the caller: the adapter receives
    # cwd over session/new, so a relative path would be resolved against
    # whatever process hosts the adapter — the daemon's directory, not the
    # caller's — and vendors reject a literal ".".
    resolved_cwd = str(Path(cwd).expanduser().resolve()) if cwd else os.getcwd()

    if dry_run:
        payload = runner.resolution_payload(resolution, cwd=resolved_cwd)
        _emit_dry_run(payload, json_mode=json_mode)
        return

    prompt = _read_prompt(prompt_text, prompt_file)

    try:
        command_head = runner.adapter_command(resolution)[0]
    except runner.RunnerError as error:
        raise AgentProblem(str(error)) from None
    del command_head

    if alias is not None:
        try:
            warning = sessions.claim_name(alias)
        except sessions.SessionNameError as error:
            raise UsageProblem(str(error)) from None
        if warning:
            click.echo(f"-- {warning}", err=True)

    settings = config.load_config()
    runner.auto_prune(settings.retention_seconds)

    meta = sessions.create_session(
        entry=resolution.entry.entry,
        base_adapter=resolution.entry.base_adapter,
        prompt=prompt,
        resolution=runner.session_resolution(
            resolution,
            cwd=resolved_cwd,
            permissions_source="default" if defaulted_permissions else None,
        ),
        target=runner.call_target(resolution),
        name=alias,
    )

    request = runner.TurnRequest(
        resolution=resolution,
        prompt=prompt,
        cwd=resolved_cwd,
        timeout=timeout,
        permission_prompt=_tty_permission_prompt if policy == "ask" else None,
    )

    if background:
        _dispatch_background(meta.session_id, request, json_mode=json_mode)
        return

    if not quiet:
        _echo_metadata(output.format_session_line(meta))

    try:
        outcome = runner.execute_turn(meta.session_id, request)
    except runner.RunnerError as error:
        raise AgentProblem(str(error)) from None

    final = sessions.read_meta(meta.session_id)
    if output_file is not None:
        output.write_output_file(output_file, outcome.answer)

    result = output.render_result(
        final,
        outcome.answer,
        json_mode=json_mode,
        output_file=output_file,
        max_output=max_output,
    )
    _write_stdout(result.text)

    if outcome.state == "detached":
        # SPEC *Output contract*: the session outlives this client, so the way
        # out has to say which session the caller can still reach.
        _echo_metadata(
            f"-- detached, still RUNNING: {meta.session_id}"
            f" — answer: acpc wait {meta.session_id} · cancel: acpc stop {meta.session_id}"
        )
        raise SystemExit(outcome.exit_code)

    if not quiet:
        _echo_metadata(output.format_summary(final, route_note=_route_note(outcome)))

    raise SystemExit(outcome.exit_code)


def _route_note(outcome: runner.TurnOutcome) -> str | None:
    """Fold the routing and queueing notes into the one `--` summary line.

    SPEC counts `--` lines: a queued turn and a direct-child fallback are both
    notes about how the call was served, so they ride the same line.
    """
    notes = [note for note in (outcome.route_note, _queue_note(outcome)) if note]
    return " · ".join(notes) if notes else None


def _queue_note(outcome: runner.TurnOutcome) -> str | None:
    return "queued for a daemon slot" if outcome.queued else None


def _dispatch_background(session_id: str, request: runner.TurnRequest, *, json_mode: bool) -> None:
    """Hand the turn to the daemon and print what the caller needs to find it.

    SPEC `run --bg`: stdout is exactly the session id and its directory, so a
    shell caller can read both without parsing prose.
    """
    import asyncio

    problem = asyncio.run(runner.dispatch_background(session_id, request))
    if problem is not None:
        raise AgentProblem(problem)
    if json_mode:
        import json

        payload = {"session_id": session_id, "paths": sessions.session_paths(session_id)}
        _write_stdout(json.dumps(payload, ensure_ascii=False) + "\n")
        return
    _write_stdout(f"{session_id}\n{sessions.session_dir(session_id)}\n")


@main.command(name="continue")
@click.argument("selector")
@click.argument("prompt_text", required=False)
@click.option("--prompt-file", "prompt_file", metavar="FILE", help="Read the prompt from a file.")
@click.option("-o", "--output", "output_file", metavar="FILE", help="Write the answer to a file.")
@click.option(
    "--permissions",
    type=click.Choice(_PERMISSION_CHOICES),
    metavar="P",
    help=(
        "Permission scale for this and later turns: none, read, edit, execute, all or ask; "
        "the run default is ask on a TTY and read otherwise (--bg counts as non-TTY). "
        "Without this flag, continue reuses its stored policy. write and prompt are "
        "deprecated aliases for execute and ask; --permissions re-runs mode selection against "
        "the adapter's current [modes]."
    ),
)
@click.option("--bg", "background", is_flag=True, help="Dispatch and return the session id.")
@click.option(
    "--timeout",
    type=TimeoutParamType(),
    metavar="S",
    help="Cancel the session after this duration; absent, no wall-clock limit (the callee runs until it is done).",
)
@click.option(
    "--max-output",
    type=click.IntRange(min=0),
    default=output.DEFAULT_MAX_OUTPUT,
    metavar="BYTES",
    help="Cap on stdout bytes; 0 disables the cap.",
)
@click.option("--quiet", is_flag=True, help="Suppress the stderr summary line.")
@click.option("--json", "json_mode", is_flag=True, help="Emit this command's output as JSON.")
@click.option(
    "--model",
    metavar="M",
    hidden=True,
    help="Run-only model override; continue reuses the stored model.",
)
@click.option(
    "--effort",
    metavar="E",
    hidden=True,
    help="Run-only reasoning effort; continue reuses the stored effort.",
)
@click.option(
    "--mode",
    metavar="M",
    hidden=True,
    help=(
        "Run-only vendor mode override; normally unnecessary because --permissions selects "
        "the mode. On run, refused when it grants more than the policy; values from "
        "agents <name>."
    ),
)
@click.option(
    "--cwd",
    metavar="DIR",
    hidden=True,
    help="Run-only working directory; continue reuses the stored directory.",
)
@click.option(
    "--home",
    metavar="DIR",
    hidden=True,
    help="Run-only vendor home; continue reuses the stored home.",
)
@click.option(
    "--name",
    "alias",
    metavar="ALIAS",
    hidden=True,
    help="Run-only session name; continue reuses the stored name.",
)
@click.option("--dry-run", is_flag=True, hidden=True, help="Run-only resolution preview.")
@click.help_option("-h", "--help")
def continue_command(
    selector: str,
    prompt_text: str | None,
    prompt_file: str | None,
    output_file: str | None,
    background: bool,
    timeout: float | None,
    max_output: int,
    quiet: bool,
    json_mode: bool,
    permissions: str | None,
    model: str | None,
    effort: str | None,
    mode: str | None,
    cwd: str | None,
    home: str | None,
    alias: str | None,
    dry_run: bool,
) -> None:
    """Continue a finished session using its stored adapter resolution.

    Model, effort, mode, permissions and home come from the session, not from
    re-resolving the agent entry — editing an entry never changes a session
    mid-conversation. A session cancelled by ``stop`` is the pause/resume path; its
    adapter context is preserved. ``--permissions`` is the one ``run`` resolution
    flag ``continue`` accepts: it applies to this turn and every turn after it.

    Example: ``acpc continue <session-id> "Run the tests again"``
    """
    permissions = _normalize_permission(permissions)
    run_only = {
        "--model": model,
        "--effort": effort,
        "--mode": mode,
        "--cwd": cwd,
        "--home": home,
        "--name": alias,
        "--dry-run": dry_run,
    }
    for flag, value in run_only.items():
        if value not in (None, False):
            rule = "continue reuses the session's stored settings"
            raise UsageProblem(f"{rule} (run-only flag: {flag})")

    prompt = _read_prompt(prompt_text, prompt_file)
    meta = _load_view_session(selector)
    if meta.is_active:
        raise UsageProblem(
            f"session {meta.session_id} is {meta.state} — wait for the current turn to finish"
        )
    _dispatch_follow_up(
        meta,
        prompt,
        output_file=output_file,
        permissions=permissions,
        background=background,
        timeout=timeout,
        max_output=max_output,
        quiet=quiet,
        json_mode=json_mode,
    )


def _dispatch_follow_up(
    meta: sessions.SessionMeta,
    prompt: str,
    *,
    output_file: str | None,
    permissions: str | None,
    background: bool,
    timeout: float | None,
    max_output: int,
    quiet: bool,
    json_mode: bool,
) -> None:
    """Run the next turn on a finished session — `continue`'s machinery.

    `steer` is `continue` with a cancel in front of it, so both verbs end
    here: one turn on the session's stored resolution, one output contract.
    """
    try:
        current = sessions.read_meta(meta.session_id)
        stored_policy = _stored_permission_policy(current)
    except sessions.SessionError as error:
        raise UsageProblem(str(error)) from None
    policy = permissions if permissions is not None else stored_policy
    policy, permissions_clamp = _clamp_inherited_ceiling(policy)
    interactive = _stdout_is_tty() and not background
    if policy == "ask" and not interactive:
        # Same split as `run`: the two causes are different situations and
        # "needs a terminal" is baffling advice to someone sitting at one.
        cause = (
            "cannot be continued with --bg, which returns before a request could be answered"
            if background
            else "needs a terminal to ask on"
        )
        raise UsageProblem(
            f"this session uses --permissions ask, which {cause}; "
            "continue it from a terminal or start a new session with another policy"
        )
    try:
        stored_resolution = runner.resolution_from_session(current)
    except runner.RunnerError as error:
        raise UsageProblem(str(error)) from None
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
    try:
        runner.continue_request(
            current,
            prompt,
            timeout=timeout,
            permission_prompt=(_tty_permission_prompt if policy == "ask" and interactive else None),
            resolution=selection,
        )
    except runner.RunnerError as error:
        raise UsageProblem(str(error)) from None

    rotated: sessions.SessionMeta | None = None
    try:
        rotated = sessions.rotate_turn(
            meta.session_id,
            resolution=updated_resolution,
            target_from_meta=_target_for_persisted_resolution,
        )
        rotated_policy = _stored_permission_policy(rotated)
        request = runner.continue_request(
            rotated,
            prompt,
            timeout=timeout,
            permission_prompt=(
                _tty_permission_prompt if rotated_policy == "ask" and interactive else None
            ),
        )
        sessions.write_prompt(rotated.session_id, prompt)
    except (runner.RunnerError, sessions.SessionError, OSError, UsageProblem) as error:
        if rotated is not None:
            _finalize_follow_up_failure(meta.session_id, error)
        if isinstance(error, UsageProblem):
            raise
        raise UsageProblem(str(error)) from None

    if background:
        _dispatch_background(meta.session_id, request, json_mode=json_mode)
        return

    if not quiet:
        _echo_metadata(output.format_session_line(rotated))

    try:
        outcome = runner.execute_turn(meta.session_id, request)
    except runner.RunnerError as error:
        raise AgentProblem(str(error)) from None

    final = sessions.read_meta(meta.session_id)
    if output_file is not None:
        output.write_output_file(output_file, outcome.answer)

    result = output.render_result(
        final,
        outcome.answer,
        json_mode=json_mode,
        output_file=output_file,
        max_output=max_output,
    )
    _write_stdout(result.text)
    if outcome.state == "detached":
        _echo_metadata(
            f"-- detached, still RUNNING: {meta.session_id}"
            f" — answer: acpc wait {meta.session_id} · cancel: acpc stop {meta.session_id}"
        )
        raise SystemExit(outcome.exit_code)
    if not quiet:
        _echo_metadata(output.format_summary(final, route_note=_route_note(outcome)))
    raise SystemExit(outcome.exit_code)


# SPEC `steer`: the preamble is fixed text, so the callee reads the redirect
# as a redirect rather than as a fresh unrelated task.
STEER_PREAMBLE = (
    "Your previous turn was interrupted by the operator; this instruction takes precedence:"
)


def _steer_prompt(instruction: str) -> str:
    return f"{STEER_PREAMBLE}\n\n{instruction}"


@main.command(name="steer")
@click.argument("selector")
@click.argument("instruction_text", required=False)
@click.option(
    "--prompt-file", "prompt_file", metavar="FILE", help="Read the instruction from a file."
)
@click.option("-o", "--output", "output_file", metavar="FILE", help="Write the answer to a file.")
@click.option("--bg", "background", is_flag=True, help="Dispatch and return the session id.")
@click.option(
    "--timeout",
    type=TimeoutParamType(),
    metavar="S",
    help="Cancel the redirected turn after this duration; absent, no wall-clock limit (the callee runs until it is done).",
)
@click.option(
    "--max-output",
    type=click.IntRange(min=0),
    default=output.DEFAULT_MAX_OUTPUT,
    metavar="BYTES",
    help="Cap on stdout bytes; 0 disables the cap.",
)
@click.option("--quiet", is_flag=True, help="Suppress the stderr summary line.")
@click.option("--json", "json_mode", is_flag=True, help="Emit this command's output as JSON.")
@click.help_option("-h", "--help")
def steer_command(
    selector: str,
    instruction_text: str | None,
    prompt_file: str | None,
    output_file: str | None,
    background: bool,
    timeout: float | None,
    max_output: int,
    quiet: bool,
    json_mode: bool,
) -> None:
    """Interrupt the running turn and redirect the session in one verb.

    Cancels the turn in flight (ACP session/cancel), waits for the ack, then
    starts the next turn with the instruction under a fixed preamble. The
    interrupted turn's partial answer is kept as that turn's answer file. A
    finished session is a usage error: there is no turn to interrupt, and the
    follow-up verb for it is ``acpc continue``.

    Example: ``acpc steer x7k2 "Stop editing; diagnose only"``
    """
    instruction = _read_prompt(instruction_text, prompt_file)
    meta = _load_view_session(selector)
    if not meta.is_active:
        raise UsageProblem(
            f"session {meta.session_id} is {meta.state} — there is no turn to interrupt; "
            f"the follow-up verb for a finished session is: acpc continue {meta.session_id}"
        )

    meta = _cancel_session(meta)
    interrupted = meta.state == "cancelled"
    if not interrupted and not quiet:
        # SPEC `steer`: nothing was interrupted, so the preamble would lie.
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
        max_output=max_output,
        quiet=quiet,
        json_mode=json_mode,
    )


@main.command(name="wait")
@click.argument("selector")
@click.option(
    "--timeout",
    type=TimeoutParamType(allow_zero=True),
    default=None,
    metavar="S",
    help="Stop waiting after this duration (exit 124; the session keeps running); absent, it blocks indefinitely.",
)
@click.option("-o", "--output", "output_file", metavar="FILE", help="Write the answer to a file.")
@click.option(
    "--max-output",
    type=click.IntRange(min=0),
    default=output.DEFAULT_MAX_OUTPUT,
    metavar="BYTES",
    help="Cap rendered output bytes; 0 disables the cap.",
)
@click.option("--quiet", is_flag=True, help="Suppress the stderr summary line.")
@click.option("--json", "json_mode", is_flag=True, help="Emit this command's output as JSON.")
@click.help_option("-h", "--help")
def wait_command(
    selector: str,
    timeout: float | None,
    output_file: str | None,
    max_output: int,
    quiet: bool,
    json_mode: bool,
) -> None:
    """Block until a background session finishes, then print its answer.

    The exit code mirrors the session result. On an already-finished session it
    returns immediately — the free way to reprint an answer. ``--timeout`` stops
    the waiting only and exits 124: the session keeps running, unlike ``run --timeout``,
    which cancels it.

    Example: ``acpc wait <session-id> --timeout 120``
    """
    meta = _load_view_session(selector)
    state = runner.wait_for_session(meta.session_id, timeout=timeout)
    if state is None:
        # SPEC `wait`: the timeout stops waiting only — the session runs on.
        if not quiet:
            _echo_metadata(_still_running_note(meta.session_id, timeout))
        raise SystemExit(vocab.EXIT_TIMEOUT)

    final = sessions.read_meta(meta.session_id)
    answer = _answer_text(meta.session_id)
    if output_file is not None:
        output.write_output_file(output_file, answer)

    result = output.render_result(
        final,
        answer,
        json_mode=json_mode,
        output_file=output_file,
        max_output=max_output,
    )
    _write_stdout(result.text)
    if not quiet:
        _echo_metadata(output.format_summary(final))
    raise SystemExit(runner.exit_code_for(final.state, final.stop_reason))


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


@main.group(name="daemon", invoke_without_command=False)
@click.help_option("-h", "--help")
def daemon_group() -> None:
    """Inspect and stop the per-target daemons.

    Example: ``acpc daemon status``
    """


@daemon_group.command(name="status")
@click.argument("agent", required=False)
@click.option("--json", "json_mode", is_flag=True, help="Emit the status as JSON.")
@click.help_option("-h", "--help")
def daemon_status_command(agent: str | None, json_mode: bool) -> None:
    """Report each live daemon with its acpc version, pid, uptime, idle age and log path.

    Example: ``acpc daemon status --json``
    """
    import asyncio

    entries = asyncio.run(_collect_daemon_status(agent))
    if json_mode:
        import json

        _write_stdout(json.dumps({"daemons": entries}, ensure_ascii=False) + "\n")
        return
    if not entries:
        click.echo("-- no daemons running", err=True)
        return
    rows = [
        (
            str(item["target"]),
            f"acpc {item['version']}",
            f"pid {item['pid']}",
            f"up {output.format_duration(item['uptime'])}",
            f"· {_daemon_idle_column(item['idle_seconds'])}",
            f"· {len(item['sessions'])} sessions",
            f"· {item['log']}",
        )
        for item in entries
    ]
    lines = render.format_table(rows, separator="  ")
    _write_stdout("\n".join(lines) + "\n")


async def _collect_daemon_status(
    agent: str | None, *, clock: render.Clock | None = None
) -> list[dict[str, Any]]:
    now = time.time() if clock is None else clock()
    session_metas = sessions.list_sessions(clock=lambda: now)
    entries: list[dict[str, Any]] = []
    for target in runner.daemon_targets_for(agent) if agent else runner.all_daemon_targets():
        daemon = await daemon_client.connect(target)
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


@daemon_group.command(name="stop")
@click.argument("agent", required=False)
@click.option(
    "--force",
    is_flag=True,
    help="Stop even when the target has running or starting sessions; they are failed, not orphaned.",
)
@click.help_option("-h", "--help")
def daemon_stop_command(agent: str | None, force: bool) -> None:
    """Stop daemons; active sessions refuse the stop unless ``--force``.

    Example: ``acpc daemon stop mock``
    """
    import asyncio

    stopped = asyncio.run(_stop_daemons(agent, force=force))
    click.echo(f"-- stopped {stopped} daemon(s)", err=True)


async def _stop_daemons(agent: str | None, *, force: bool = False) -> int:
    targets = runner.daemon_targets_for(agent) if agent else runner.all_daemon_targets()
    if not force:
        addressed = set(targets)
        active = [
            meta for meta in sessions.list_sessions() if meta.target in addressed and meta.is_active
        ]
        if active:
            count = len(active)
            noun = "session" if count == 1 else "sessions"
            scope = f" {agent}" if agent else ""
            ids = ", ".join(meta.session_id for meta in active)
            raise UsageProblem(
                f"daemon stop{scope}: {count} active {noun} ({ids}) — wait or stop them first, "
                "or pass --force"
            )

    stopped = 0
    for target in targets:
        daemon = await daemon_client.connect(target)
        if daemon is None:
            continue
        try:
            reply = await daemon.stop()
        finally:
            await daemon.close()
        if reply.get("ok"):
            stopped += 1
    return stopped
