"""CLI entry point.

SPEC.md *Command surface*. Verbs land slice by slice; this module owns flag
parsing, usage errors (exit 2), the TTY rules and the fixed exit codes, and
delegates everything else to the layer that owns it.
"""

import os
import sys
from pathlib import Path
from typing import Any

import click

from acpc import __version__, config, output, runner, sessions, vocab
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
        resolution=runner.resolution_payload(resolution, cwd=resolved_cwd),
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

    if not quiet:
        output.emit_summary(final, route_note=outcome.route_note)

    raise SystemExit(outcome.exit_code)


def _with_policy(request: runner.TurnRequest, policy: str) -> runner.TurnRequest:
    """Return the request with the TTY-resolved permission policy applied."""
    from dataclasses import replace

    from acpc.registry import CallResolution as _CallResolution

    resolution: _CallResolution = replace(request.resolution, permissions=policy)
    return replace(request, resolution=resolution)
