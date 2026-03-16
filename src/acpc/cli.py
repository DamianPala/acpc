"""CLI entry point for acpc."""

import os
import signal
import subprocess
import sys

import click

from acpc import __version__


class RawEpilogGroup(click.Group):
    """Click group that preserves epilog whitespace."""

    def format_epilog(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:  # noqa: ARG002
        if self.epilog:
            formatter.write("\n")
            # Write each line without rewrapping
            for line in self.epilog.split("\n"):
                formatter.write(f"{line}\n")


CHEAT_SHEET = """\

# acpc cheat sheet

## Quick start
acpc prompt codex "fix the tests"
acpc prompt claude "analyze repo" --model sonnet
echo "prompt" | acpc prompt codex -

## Multi-turn
acpc prompt codex "remember: X=42"
acpc prompt codex --last "what is X?"
acpc prompt codex -s SESSION_ID "follow up"

## Model & mode
acpc prompt codex "task" --model o3           # ACP: session/set_model
acpc prompt claude "plan" --mode plan         # ACP: session/set_mode

## Permissions (default: auto-detect TTY)
acpc prompt codex "task" --permissions all    # approve everything
acpc prompt codex "task" --permissions read   # read-only
acpc prompt codex "task" --permissions write  # read + write, no delete
acpc prompt codex "task" --permissions none   # deny everything (dry run)

## Output (stdout = response, stderr = diagnostics)
acpc prompt codex "task" --quiet              # final text only
acpc prompt codex "task" --json               # NDJSON ACP events
acpc prompt codex "task" -o result.md         # write to file

## Input
acpc prompt codex --input-file prompt.md      # from file
echo "fix" | acpc prompt codex -              # from stdin

## Process management
acpc status                                   # running sessions
acpc stop codex                               # stop by agent
acpc stop -s SESSION_ID                       # stop by session

## Other
acpc agents                                   # list + install status
acpc sessions codex                           # agent sessions (ACP)
acpc install codex                            # install adapter

## Flag -> ACP mapping
# -s        -> session/load          --model   -> session/set_model
# --mode    -> session/set_mode      --cwd     -> session/new (cwd)
# --permissions -> request_permission  Ctrl+C  -> session/cancel
"""


@click.group(cls=RawEpilogGroup, epilog=CHEAT_SHEET)
@click.version_option(__version__, prog_name="acpc")
def cli() -> None:
    """acpc - Thin CLI client for the Agent Client Protocol (ACP)."""


# ---------------------------------------------------------------------------
# prompt (+ run alias)
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("agent")
@click.argument("prompt_text", required=False)
@click.option("--last", is_flag=True, help="Resume last session")
@click.option("-s", "--session", "session_id", help="Resume session by ID")
@click.option("--model", help="Set model (ACP: session/set_model)")
@click.option("--mode", help="Set mode (ACP: session/set_mode)")
@click.option(
    "--permissions",
    type=click.Choice(["all", "write", "read", "none", "prompt"]),
    help="Permission policy",
)
@click.option("--cwd", type=click.Path(exists=True), help="Working directory")
@click.option("--json", "use_json", is_flag=True, help="NDJSON output")
@click.option("--quiet", is_flag=True, help="Final text only")
@click.option("-o", "--output", "output_file", type=click.Path(), help="Write output to file")
@click.option("--input-file", type=click.Path(exists=True), help="Read prompt from file")
@click.option("--timeout", type=int, help="Timeout in seconds")
def prompt(
    agent: str,
    prompt_text: str | None,
    last: bool,
    session_id: str | None,
    model: str | None,
    mode: str | None,
    permissions: str | None,
    cwd: str | None,
    use_json: bool,
    quiet: bool,
    output_file: str | None,
    input_file: str | None,
    timeout: int | None,
) -> None:
    """Send a prompt to an ACP agent."""
    import asyncio

    from acpc.agents import AgentNotFoundError, load_agent
    from acpc.output import stderr_error
    from acpc.runner import RunConfig, run

    # Validate agent exists
    try:
        load_agent(agent)
    except AgentNotFoundError as e:
        stderr_error(str(e))
        sys.exit(2)

    # Determine prompt text
    final_prompt: str | None = None
    if input_file:
        final_prompt = open(input_file, encoding="utf-8").read()
    elif prompt_text == "-":
        final_prompt = sys.stdin.read()
    elif prompt_text:
        final_prompt = prompt_text
    elif not sys.stdin.isatty():
        final_prompt = sys.stdin.read()

    if not final_prompt:
        stderr_error("no prompt provided (use argument, --input-file, or pipe to stdin)")
        sys.exit(2)

    # Determine output mode
    if use_json:
        output_mode = "json"
    elif quiet:
        output_mode = "quiet"
    else:
        output_mode = "text"

    # Determine permissions
    if permissions is None:
        permissions = "prompt" if sys.stdin.isatty() else "read"

    config = RunConfig(
        agent_identity=agent,
        prompt_text=final_prompt,
        model=model,
        mode=mode,
        permission_level=permissions,
        cwd=cwd,
        session_id=session_id,
        use_last=last,
        output_mode=output_mode,
        output_file=output_file,
        timeout=timeout,
        is_tty=sys.stdin.isatty(),
    )

    exit_code = asyncio.run(run(config))
    sys.exit(exit_code)


# Make 'run' an alias for 'prompt'
cli.add_command(prompt, name="run")


# ---------------------------------------------------------------------------
# agents
# ---------------------------------------------------------------------------


@cli.command()
def agents() -> None:
    """List available agents and their install status."""
    from acpc import agents as agents_module

    agent_list = agents_module.list_agents()
    if not agent_list:
        click.echo("No agents registered.", err=True)
        return

    # Calculate column widths for alignment
    id_width = max(len(a.identity) for a in agent_list)
    desc_width = max(len(f"{a.name} ({a.author})") for a in agent_list)

    for a in agent_list:
        installed = "installed" if agents_module.is_installed(a) else "not installed"
        desc = f"{a.name} ({a.author})"
        click.echo(f"{a.identity:<{id_width}}  {desc:<{desc_width}}  {installed}")


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("agent")
def sessions(agent: str) -> None:
    """List sessions for an agent (via ACP)."""
    from acpc.output import stderr_error

    stderr_error("sessions listing requires ACP connection (not yet implemented)")
    sys.exit(1)


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("agent")
def install(agent: str) -> None:
    """Install an ACP agent adapter."""
    from acpc.agents import AgentNotFoundError, load_agent
    from acpc.output import stderr, stderr_error

    try:
        agent_def = load_agent(agent)
    except AgentNotFoundError as e:
        stderr_error(str(e))
        sys.exit(2)

    stderr(f"installing {agent_def.identity} via: {agent_def.install_command}")
    result = subprocess.run(
        agent_def.install_command,
        shell=True,
        check=False,
    )
    if result.returncode != 0:
        stderr_error(f"install command exited with code {result.returncode}")
        sys.exit(1)
    stderr(f"{agent_def.identity} installed successfully")


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("agent", required=False)
@click.option("-s", "--session", "session_id", help="Stop specific session")
def stop(agent: str | None, session_id: str | None) -> None:
    """Stop running agent sessions."""
    from acpc.output import stderr, stderr_error
    from acpc.sessions import get_running_by_agent, list_running, remove_running

    if session_id:
        running = list_running()
        if session_id not in running:
            stderr_error(f"session {session_id} not found in running sessions")
            sys.exit(1)
        rs = running[session_id]
        try:
            os.kill(rs.pid, signal.SIGTERM)
            stderr(f"sent SIGTERM to session {session_id} (pid {rs.pid})")
        except ProcessLookupError:
            stderr(f"process {rs.pid} already exited")
        except PermissionError:
            stderr_error(f"permission denied sending signal to pid {rs.pid}")
            sys.exit(1)
        remove_running(session_id)
        return

    if agent:
        sessions = get_running_by_agent(agent)
        if not sessions:
            stderr_error(f"no running sessions for agent '{agent}'")
            sys.exit(1)
        for rs in sessions:
            try:
                os.kill(rs.pid, signal.SIGTERM)
                stderr(f"sent SIGTERM to session {rs.session_id} (pid {rs.pid})")
            except ProcessLookupError:
                stderr(f"process {rs.pid} already exited")
            except PermissionError:
                stderr_error(f"permission denied sending signal to pid {rs.pid}")
            remove_running(rs.session_id)
        return

    stderr_error("specify an agent name or --session ID")
    sys.exit(2)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@cli.command()
def status() -> None:
    """Show running sessions."""
    from acpc.sessions import list_running

    running = list_running()
    if not running:
        click.echo("No running sessions.", err=True)
        return

    # Header
    header = f"{'SESSION_ID':<40}  {'AGENT':<12}  {'PID':<8}  {'CWD':<30}  {'STARTED'}"
    click.echo(header)
    for rs in running.values():
        line = f"{rs.session_id:<40}  {rs.agent:<12}  {rs.pid:<8}  {rs.cwd:<30}  {rs.started}"
        click.echo(line)


# ---------------------------------------------------------------------------
# generate-completion
# ---------------------------------------------------------------------------

from acpc._completion import add_completion_command  # noqa: E402

add_completion_command(cli, "acpc")
