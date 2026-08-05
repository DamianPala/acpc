"""CLI entry point.

SPEC.md *Command surface*. Verbs land slice by slice; this module owns flag
parsing, usage errors (exit 2), the TTY rules and the fixed exit codes, and
delegates everything else to the layer that owns it.
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import click

from acpc import (
    __version__,
    config,
    daemon_client,
    output,
    render,
    runner,
    sessions,
    transcript,
    vocab,
)
from acpc.registry import AgentRegistry, CallResolution, RegistryError


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


@click.group(invoke_without_command=True)
@click.version_option(__version__, "-V", "--version", message="acpc %(version)s")
@click.help_option("-h", "--help")
@click.pass_context
def main(ctx: click.Context) -> None:
    """acpc — dispatch coding agents over ACP."""
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


def _resolve_permissions(explicit: str | None, resolution: CallResolution, *, tty: bool) -> str:
    """Apply SPEC's TTY rules to the resolved permission policy."""
    policy = explicit if explicit is not None else resolution.permissions
    if policy is None:
        policy = "prompt" if tty else "read"
    if policy == "prompt" and not tty:
        raise UsageProblem(
            "--permissions prompt needs a terminal to ask on; "
            "pass --permissions read, write, all or none"
        )
    return policy


def _guard_bypass_mode(mode: str | None, policy: str, resolution: CallResolution) -> None:
    """SPEC *Permissions*: a bypass mode evades the policy unless it is `all`."""
    if mode is None or policy == "all":
        return
    if mode in resolution.entry.bypass_modes:
        raise UsageProblem(
            f"--mode {mode} bypasses permission requests on {resolution.entry.entry}; "
            "it is only accepted with --permissions all"
        )


def _tty_permission_prompt(kind: str, title: str) -> bool:
    """Ask the human on /dev/tty — never on stdin (SPEC *Output contract*)."""
    try:
        with open("/dev/tty", "r+", encoding="utf-8") as tty:
            tty.write(f"acpc: allow {kind}? {title} [y/N] ")
            tty.flush()
            return tty.readline().strip().lower() in {"y", "yes"}
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
        lines.append(f"{name:<12} {value} ({item['source']})")
    if payload["cwd"]:
        lines.append(f"cwd          {payload['cwd']}")
    if payload["env"]:
        declared = " · ".join(f"{key}={value}" for key, value in payload["env"].items())
        lines.append(f"env          {declared}")
    if payload["env_passthrough"]:
        lines.append(f"passthrough  {' · '.join(payload['env_passthrough'])}")
    click.echo("\n".join(lines))


def _write_stdout(text: str) -> None:
    """Write the answer, turning a closed stdout into SPEC's exit 141."""
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except BrokenPipeError:
        # Keep the interpreter's own shutdown flush from raising again.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        sys.exit(vocab.EXIT_SIGPIPE)


def _load_view_session(selector: str) -> sessions.SessionMeta:
    """Verify liveness before a targeted view reports a session."""
    try:
        session_id = sessions.resolve_selector(selector, allow_last=_stdout_is_tty())
        return sessions.load(session_id)
    except sessions.SessionError as error:
        raise UsageProblem(str(error)) from None


_LOG_DEFAULT_TAIL = 20
_LOG_WAIT_POLL_INTERVAL = 0.05


def _wait_for_new_events(
    transcript_file: transcript.Transcript,
    *,
    since: int,
    tail: int | None,
    timeout: float | None,
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

        available = transcript_file.read(since=since)
        if available.events:
            return transcript_file.read(since=since, tail=tail)


@main.command(name="status")
@click.argument("selector", required=False)
@click.option("--all", "all_sessions", is_flag=True, help="Show every session.")
@click.option("--json", "json_mode", is_flag=True, help="Emit a JSON status object.")
@click.help_option("-h", "--help")
def status_command(selector: str | None, all_sessions: bool, json_mode: bool) -> None:
    """Show liveness-verified session metadata without reading transcripts."""
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
@click.option("--since", type=click.IntRange(min=0), default=None, metavar="N")
@click.option("--tail", type=click.IntRange(min=0), default=None, metavar="N")
@click.option("--prose", is_flag=True, help="Render full agent messages.")
@click.option("--json", "json_mode", is_flag=True, help="Emit raw transcript events as NDJSON.")
@click.option(
    "--max-output",
    type=click.IntRange(min=0),
    default=render.DEFAULT_LOG_MAX_OUTPUT,
    show_default=True,
    metavar="BYTES",
)
@click.option("--wait-new", is_flag=True, help="Wait for new transcript events.")
@click.option("--timeout", type=click.FloatRange(min=0), default=None, metavar="S")
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
    timeout: float | None,
    quiet: bool,
) -> None:
    """Render selected transcript events and keep metadata on stderr."""
    if prose and json_mode:
        raise UsageProblem("--prose and --json are mutually exclusive views")
    if timeout is not None and not wait_new:
        raise UsageProblem("--timeout requires --wait-new")

    meta = _load_view_session(selector)
    transcript_file = transcript.Transcript(sessions.transcript_path(meta.session_id))
    explicit_since = since is not None
    cursor = 0 if since is None else since
    selection_tail = tail
    if selection_tail is None and not explicit_since:
        selection_tail = _LOG_DEFAULT_TAIL

    if wait_new and not explicit_since:
        cursor = transcript_file.read(since=0).next_cursor
    page = transcript_file.read(since=cursor, tail=selection_tail)
    if wait_new and not page.events:
        page = _wait_for_new_events(
            transcript_file,
            since=cursor,
            tail=selection_tail,
            timeout=timeout,
        )
        if page is None:
            raise SystemExit(vocab.EXIT_TIMEOUT)

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
        # The cursor is a global transcript index, not an event count.  Read
        # the actual end so an empty page after a large --since stays honest.
        event_count = transcript_file.read(since=0).next_cursor
        footer = render.format_log_footer(
            meta,
            cursor=rendered.next_cursor,
            event_count=event_count,
        )
        click.echo(footer, err=True)


@main.command(name="run")
@click.argument("agent")
@click.argument("prompt_text", required=False)
@click.option("--prompt-file", "prompt_file", metavar="FILE", help="Read the prompt from a file.")
@click.option("--cwd", metavar="DIR", help="Working directory of the callee.")
@click.option("--model", metavar="M", help="Model tier (fast/standard/max) or a raw model ID.")
@click.option("--effort", metavar="E", help="Reasoning effort level.")
@click.option(
    "--permissions",
    type=click.Choice(vocab.PERMISSION_VALUES),
    help="Approval policy for ACP permission requests.",
)
@click.option("--mode", metavar="M", help="Callee's operating mode (ACP session/set_mode).")
@click.option("--home", metavar="DIR", help="Vendor home override.")
@click.option("-o", "--output", "output_file", metavar="FILE", help="Write the answer to a file.")
@click.option("--timeout", type=float, metavar="S", help="Cancel the session after S seconds.")
@click.option("--name", "alias", metavar="ALIAS", help="Human-typeable handle for this session.")
@click.option("--dry-run", is_flag=True, help="Print what this call resolves to, then exit.")
@click.option(
    "--max-output",
    type=click.IntRange(min=0),
    default=output.DEFAULT_MAX_OUTPUT,
    show_default=True,
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
    """Dispatch one agent; block and print the final answer."""
    tty = _stdout_is_tty()

    try:
        registry = AgentRegistry()
        resolution = registry.resolve_call(
            agent, model=model, effort=effort, permissions=permissions, home=home
        )
    except RegistryError as error:
        raise UsageProblem(str(error)) from None

    policy = _resolve_permissions(permissions, resolution, tty=tty)
    _guard_bypass_mode(mode, policy, resolution)
    resolved_cwd = str(Path(cwd).expanduser().resolve()) if cwd else None

    if dry_run:
        payload = runner.resolution_payload(resolution, cwd=resolved_cwd)
        payload["resolved"]["permissions"]["value"] = policy
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
        resolution=runner.session_resolution(resolution, cwd=resolved_cwd),
        target=runner.call_target(resolution),
        name=alias,
    )

    request = runner.TurnRequest(
        resolution=resolution,
        prompt=prompt,
        cwd=resolved_cwd,
        mode=mode,
        timeout=timeout,
        permission_prompt=_tty_permission_prompt if policy == "prompt" else None,
    )
    # The policy the TTY rules produced is what the client must enforce.
    request = _with_policy(request, policy)

    if background:
        _dispatch_background(meta.session_id, request, json_mode=json_mode)
        return

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
        click.echo(f"-- {meta.session_id} detached · still running", err=True)
        raise SystemExit(outcome.exit_code)

    if not quiet:
        output.emit_summary(final, route_note=_route_note(outcome))

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


def _with_policy(request: runner.TurnRequest, policy: str) -> runner.TurnRequest:
    """Return the request with the TTY-resolved permission policy applied."""
    from dataclasses import replace

    from acpc.registry import CallResolution as _CallResolution

    resolution: _CallResolution = replace(request.resolution, permissions=policy)
    return replace(request, resolution=resolution)


@main.command(name="continue")
@click.argument("selector")
@click.argument("prompt_text", required=False)
@click.option("--prompt-file", "prompt_file", metavar="FILE", help="Read the prompt from a file.")
@click.option("-o", "--output", "output_file", metavar="FILE", help="Write the answer to a file.")
@click.option("--bg", "background", is_flag=True, help="Dispatch and return the session id.")
@click.option("--timeout", type=float, metavar="S", help="Cancel the session after S seconds.")
@click.option(
    "--max-output",
    type=click.IntRange(min=0),
    default=output.DEFAULT_MAX_OUTPUT,
    show_default=True,
    metavar="BYTES",
    help="Cap on stdout bytes; 0 disables the cap.",
)
@click.option("--quiet", is_flag=True, help="Suppress the stderr summary line.")
@click.option("--json", "json_mode", is_flag=True, help="Emit this command's output as JSON.")
@click.option("--permissions", metavar="P", hidden=True)
@click.option("--model", metavar="M", hidden=True)
@click.option("--effort", metavar="E", hidden=True)
@click.option("--mode", metavar="M", hidden=True)
@click.option("--cwd", metavar="DIR", hidden=True)
@click.option("--home", metavar="DIR", hidden=True)
@click.option("--name", "alias", metavar="ALIAS", hidden=True)
@click.option("--dry-run", is_flag=True, hidden=True)
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
    """Continue a finished session using its stored adapter resolution."""
    run_only = {
        "--permissions": permissions,
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
            rule = "continue reuses the session's permissions — drop --permissions"
            raise UsageProblem(f"{rule} (run-only flag: {flag})")

    prompt = _read_prompt(prompt_text, prompt_file)
    meta = _load_view_session(selector)
    if meta.is_active:
        raise UsageProblem(
            f"session {meta.session_id} is {meta.state} — wait for the current turn to finish"
        )
    try:
        request = runner.continue_request(
            meta,
            prompt,
            timeout=timeout,
            permission_prompt=(
                _tty_permission_prompt
                if meta.resolution.get("resolved", {}).get("permissions", {}).get("value")
                == "prompt"
                and _stdout_is_tty()
                else None
            ),
        )
        rotated = sessions.rotate_turn(meta.session_id)
        sessions.write_prompt(rotated.session_id, prompt)
    except (runner.RunnerError, sessions.SessionError) as error:
        raise UsageProblem(str(error)) from None

    if background:
        _dispatch_background(meta.session_id, request, json_mode=json_mode)
        return

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
        click.echo(f"-- {meta.session_id} detached · still running", err=True)
        raise SystemExit(outcome.exit_code)
    if not quiet:
        output.emit_summary(final, route_note=_route_note(outcome))
    raise SystemExit(outcome.exit_code)


@main.command(name="wait")
@click.argument("selector")
@click.option("--timeout", type=click.FloatRange(min=0), default=None, metavar="S")
@click.option("-o", "--output", "output_file", metavar="FILE", help="Write the answer to a file.")
@click.option(
    "--max-output",
    type=click.IntRange(min=0),
    default=output.DEFAULT_MAX_OUTPUT,
    show_default=True,
    metavar="BYTES",
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
    """Block until a background session finishes, then print its answer."""
    meta = _load_view_session(selector)
    state = runner.wait_for_session(meta.session_id, timeout=timeout)
    if state is None:
        # SPEC `wait`: the timeout stops waiting only — the session runs on.
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
        output.emit_summary(final)
    raise SystemExit(runner.exit_code_for(final.state, final.stop_reason))


def _answer_text(session_id: str) -> str:
    try:
        return sessions.answer_path(session_id).read_text(encoding="utf-8")
    except OSError:
        return ""


@main.group(name="daemon", invoke_without_command=False)
@click.help_option("-h", "--help")
def daemon_group() -> None:
    """Inspect and stop the per-target daemons."""


@daemon_group.command(name="status")
@click.argument("agent", required=False)
@click.option("--json", "json_mode", is_flag=True, help="Emit the status as JSON.")
@click.help_option("-h", "--help")
def daemon_status_command(agent: str | None, json_mode: bool) -> None:
    """Report each live daemon with its pid, uptime and log path."""
    import asyncio

    entries = asyncio.run(_collect_daemon_status(agent))
    if json_mode:
        import json

        _write_stdout(json.dumps({"daemons": entries}, ensure_ascii=False) + "\n")
        return
    if not entries:
        click.echo("-- no daemons running", err=True)
        return
    lines = [
        f"{item['target']:<28} pid {item['pid']:<8} up {output.format_duration(item['uptime'])} "
        f"· {len(item['sessions'])} sessions · {item['log']}"
        for item in entries
    ]
    _write_stdout("\n".join(lines) + "\n")


async def _collect_daemon_status(agent: str | None) -> list[dict[str, Any]]:
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
            entries.append({key: value for key, value in reply.items() if key != "ok"})
    return entries


@daemon_group.command(name="stop")
@click.argument("agent", required=False)
@click.help_option("-h", "--help")
def daemon_stop_command(agent: str | None) -> None:
    """Stop daemons; their sessions are failed with a reason, never orphaned."""
    import asyncio

    stopped = asyncio.run(_stop_daemons(agent))
    click.echo(f"-- stopped {stopped} daemon(s)", err=True)


async def _stop_daemons(agent: str | None) -> int:
    stopped = 0
    for target in runner.daemon_targets_for(agent) if agent else runner.all_daemon_targets():
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
