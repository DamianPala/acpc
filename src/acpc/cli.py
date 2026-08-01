"""CLI entry point for acpc."""

import os
import subprocess
import sys
from pathlib import Path

import click

from acpc import __version__
from acpc.agents import Agent
from acpc.output import stderr_error


class RawEpilogGroup(click.Group):
    """Click group that preserves epilog whitespace."""

    def format_epilog(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:  # noqa: ARG002
        if self.epilog:
            formatter.write("\n")
            # Write each line without rewrapping
            for line in self.epilog.split("\n"):
                formatter.write(f"{line}\n")


def _require_agent(identity: str) -> Agent:
    """Load agent or exit with error. Shared by all commands that need an agent."""
    from acpc.agents import AgentNotFoundError, load_agent

    try:
        return load_agent(identity)
    except AgentNotFoundError as e:
        stderr_error(str(e))
        sys.exit(2)


CHEAT_SHEET = """\

# acpc cheat sheet

## Quick start
acpc prompt codex "fix the tests"
acpc prompt claude "analyze repo" --model sonnet
echo "prompt" | acpc prompt codex -

## Multi-turn
acpc prompt codex "remember: X=42"
acpc prompt codex --last "what is X?"
acpc prompt codex -c "what is X?"
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
acpc daemon status                            # daemon sessions
acpc daemon stop [TARGET]                     # stop daemon(s)

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
@click.version_option(__version__, "-V", "--version", prog_name="acpc")
def cli() -> None:
    """acpc - Thin CLI client for the Agent Client Protocol (ACP)."""


@cli.group()
def daemon() -> None:
    """Manage persistent target daemons."""


@daemon.command(name="status")
@click.argument("target", required=False)
def daemon_status_command(target: str | None) -> None:
    """Show daemon status and hosted sessions."""
    import asyncio

    from acpc.daemon_client import DaemonUnavailableError, daemon_status, daemon_targets

    targets = [target] if target else daemon_targets()
    if not targets:
        click.echo("No running daemons.", err=True)
        return
    for daemon_target in targets:
        try:
            frame = asyncio.run(daemon_status(daemon_target))
        except (DaemonUnavailableError, OSError) as error:
            if target:
                stderr_error(str(error))
                sys.exit(1)
            continue
        _print_daemon_status(daemon_target, frame)


@daemon.command(name="stop")
@click.argument("target", required=False)
def daemon_stop_command(target: str | None) -> None:
    """Stop one target daemon, or every running daemon when TARGET is omitted."""
    import asyncio

    from acpc.daemon_client import DaemonUnavailableError, daemon_targets, shutdown_daemon

    targets = [target] if target else daemon_targets()
    for daemon_target in targets:
        try:
            asyncio.run(shutdown_daemon(daemon_target))
        except DaemonUnavailableError:
            if target:
                stderr_error(f"daemon '{daemon_target}' is not running")
                sys.exit(1)
            continue
        click.echo(f"stopped daemon {daemon_target}")


def _print_daemon_status(target: str, frame: dict[str, object]) -> None:
    """Print one daemon status frame in a stable human-readable form."""
    pid = frame.get("pid", "?")
    uptime = frame.get("uptime_s", "?")
    log_path = frame.get("log", "?")
    click.echo(f"{target}: pid {pid}, uptime {uptime}s, log {log_path}")
    sessions = frame.get("sessions", [])
    if not isinstance(sessions, list) or not sessions:
        click.echo("  sessions: none")
        return
    for session in sessions:
        if not isinstance(session, dict):
            continue
        click.echo(
            f"  {session.get('id', '?')}  {session.get('state', '?')}  "
            f"{session.get('cwd', '?')}  (daemon)"
        )


def _daemon_statuses() -> list[tuple[str, dict[str, object]]]:
    """Read reachable daemon statuses without auto-starting missing daemons."""
    import asyncio

    from acpc.daemon_client import DaemonUnavailableError, daemon_status, daemon_targets

    statuses: list[tuple[str, dict[str, object]]] = []
    for target in daemon_targets():
        try:
            statuses.append((target, asyncio.run(daemon_status(target))))
        except (DaemonUnavailableError, OSError):
            continue
    return statuses


def _find_daemon_session(session_id: str) -> str | None:
    """Return the target that reports a hosted session ID, if any."""
    for target, frame in _daemon_statuses():
        sessions = frame.get("sessions", [])
        if not isinstance(sessions, list):
            continue
        if any(
            isinstance(session, dict) and session.get("id") == session_id for session in sessions
        ):
            return target
    return None


# ---------------------------------------------------------------------------
# prompt (+ run alias)
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("agent")
@click.argument("prompt_text", required=False)
@click.option("--last", is_flag=True, help="Resume last session")
@click.option("-c", "--continue", "continue_session", is_flag=True, help="Resume last session")
@click.option("-s", "--session", "session_id", help="Resume session by ID")
@click.option("--model", help="Model ID or preset (fast/standard/max)")
@click.option("--mode", help="Set mode (ACP: session/set_mode)")
@click.option(
    "--permissions",
    type=click.Choice(["all", "write", "read", "none", "prompt"]),
    help="Permission policy",
)
@click.option("--cwd", type=click.Path(), help="Working directory")
@click.option("--json", "use_json", is_flag=True, help="NDJSON output")
@click.option("--quiet", is_flag=True, help="Final text only")
@click.option("-o", "--output", "output_file", type=click.Path(), help="Write output to file")
@click.option("--input-file", type=click.Path(exists=True), help="Read prompt from file")
@click.option("--timeout", type=int, help="Timeout in seconds")
@click.option("--dry-run", is_flag=True, help="Resolve config and exit without running")
@click.option("--no-daemon", is_flag=True, help="Use the direct adapter path")
@click.option(
    "--print-session-id",
    is_flag=True,
    help="Print the session ID (requires --quiet and -o FILE)",
)
def prompt(
    agent: str,
    prompt_text: str | None,
    last: bool,
    continue_session: bool,
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
    dry_run: bool,
    no_daemon: bool,
    print_session_id: bool,
) -> None:
    """Send a prompt to an ACP agent."""
    import asyncio

    from acpc.agents import AgentNotFoundError
    from acpc.runner import RunConfig, run

    try:
        is_tty = sys.stdin.isatty()

        # Determine prompt text
        final_prompt: str | None = None
        if input_file:
            try:
                with open(input_file, encoding="utf-8") as f:
                    final_prompt = f.read()
            except FileNotFoundError:
                stderr_error(f"input file not found: {input_file}")
                sys.exit(2)
            except OSError as e:
                stderr_error(f"cannot read input file: {e}")
                sys.exit(1)
        elif prompt_text == "-" or (not prompt_text and not is_tty):
            try:
                final_prompt = sys.stdin.read()
            except KeyboardInterrupt:
                sys.exit(130)
        elif prompt_text:
            final_prompt = prompt_text

        if not final_prompt or not final_prompt.strip():
            stderr_error("no prompt provided (use argument, --input-file, or pipe to stdin)")
            sys.exit(2)

        if print_session_id and (not quiet or not output_file or use_json or dry_run):
            stderr_error("--print-session-id requires --quiet -o FILE")
            sys.exit(2)
        if timeout is not None and timeout <= 0:
            stderr_error("--timeout must be a positive number of seconds")
            sys.exit(2)

        if cwd is not None:
            cwd_path = Path(cwd)
            if not cwd_path.is_absolute() or not cwd_path.is_dir():
                stderr_error(f"cwd must be an absolute existing directory: {cwd}")
                sys.exit(2)
            cwd = str(cwd_path.resolve())

        # Determine output mode
        if use_json:
            output_mode = "json"
        elif quiet:
            output_mode = "quiet"
        else:
            output_mode = "text"

        # Determine permissions
        if permissions is None:
            permissions = "prompt" if is_tty else "read"

        daemon_disabled = no_daemon or _env_flag("ACPC_NO_DAEMON")

        # Resolve model presets (fast/standard/max → vendor-specific model ID)
        resolved_model = model
        model_preset = None
        if model:
            from acpc.presets import PRESET_NAMES, resolve_model

            resolved_model = resolve_model(agent, model)
            if model in PRESET_NAMES and resolved_model != model:
                model_preset = model

        if dry_run:
            click.echo(f"agent: {agent}")
            if resolved_model:
                model_info = resolved_model
                if model_preset:
                    model_info += f" (preset: {model_preset})"
                click.echo(f"model: {model_info}")
            if mode:
                click.echo(f"mode: {mode}")
            click.echo(f"permissions: {permissions}")
            click.echo(f"cwd: {cwd or os.getcwd()}")
            click.echo(f"daemon: {'disabled' if daemon_disabled else 'enabled'}")
            sys.exit(0)

        config = RunConfig(
            agent_identity=agent,
            prompt_text=final_prompt,
            model=resolved_model,
            model_preset=model_preset,
            mode=mode,
            permission_level=permissions,
            cwd=cwd,
            session_id=session_id,
            use_last=last or continue_session,
            output_mode=output_mode,
            output_file=output_file,
            timeout=timeout,
            is_tty=is_tty,
            no_daemon=daemon_disabled,
        )
        # RunConfig is shared with the direct runner, which has no need to know
        # about this daemon-only output control yet.
        setattr(config, "print_session_id", print_session_id)

        if print_session_id:
            import contextlib
            import io

            from acpc.sessions import load_last_session

            captured_stdout = io.StringIO()
            with contextlib.redirect_stdout(captured_stdout):
                exit_code = asyncio.run(run(config))
            captured = captured_stdout.getvalue()
            resolved_session_id = load_last_session(agent)
            if (
                exit_code == 0
                and resolved_session_id
                and not captured.startswith(f"{resolved_session_id}\n")
            ):
                click.echo(resolved_session_id)
            sys.stdout.write(captured)
            sys.stdout.flush()
        else:
            exit_code = asyncio.run(run(config))
        sys.exit(exit_code)

    except AgentNotFoundError as e:
        stderr_error(str(e))
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        stderr_error(f"unexpected error: {e}")
        sys.exit(1)


# Make 'run' an alias for 'prompt'
cli.add_command(prompt, name="run")


def _env_flag(name: str) -> bool:
    """Read a conventional boolean environment flag, defaulting to false."""
    value = os.environ.get(name)
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# agents
# ---------------------------------------------------------------------------


@cli.command()
def agents() -> None:
    """List available agents and their install status."""
    try:
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
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        stderr_error(f"unexpected error: {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("agent")
def sessions(agent: str) -> None:
    """List sessions for an agent (via ACP)."""
    try:
        _require_agent(agent)
        stderr_error("sessions listing requires ACP connection (not yet implemented)")
        sys.exit(1)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        stderr_error(f"unexpected error: {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("agent")
def install(agent: str) -> None:
    """Install an ACP agent adapter."""
    import shlex

    from acpc.output import stderr

    try:
        agent_def = _require_agent(agent)
        stderr(f"installing {agent_def.identity} via: {agent_def.install_command}")
        result = subprocess.run(
            shlex.split(agent_def.install_command),
            check=False,
        )
        if result.returncode != 0:
            stderr_error(f"install command exited with code {result.returncode}")
            sys.exit(1)
        stderr(f"{agent_def.identity} installed successfully")
    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        stderr_error(f"unexpected error: {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("agent", required=False)
@click.option("-s", "--session", "session_id", help="Stop specific session")
def stop(agent: str | None, session_id: str | None) -> None:
    """Stop running agent sessions."""
    import asyncio

    from acpc.output import stderr
    from acpc.runner import kill_process_tree
    from acpc.daemon_client import cancel_daemon_prompt
    from acpc.sessions import get_running_by_agent, list_running, remove_running

    def _stop_session(rs, sid: str) -> None:  # noqa: ANN001
        """Stop a single session by killing its process tree."""
        try:
            kill_process_tree(rs.pid)
            stderr(f"stopped session {sid} (pid {rs.pid})")
        except ProcessLookupError:
            stderr(f"process {rs.pid} already exited")
        except PermissionError:
            stderr_error(f"permission denied sending signal to pid {rs.pid}")
            sys.exit(1)
        remove_running(sid)

    try:
        if session_id:
            running = list_running()
            if session_id not in running:
                target = _find_daemon_session(session_id)
                if target is None:
                    stderr_error(f"session {session_id} not found in running sessions")
                    sys.exit(1)
                accepted = asyncio.run(cancel_daemon_prompt(target, session_id=session_id))
                if not accepted:
                    stderr_error(f"session {session_id} has no active prompt")
                    sys.exit(1)
                stderr(f"cancel requested for daemon session {session_id} ({target})")
                return
            rs = running[session_id]
            if getattr(rs, "daemon", False):
                accepted = asyncio.run(cancel_daemon_prompt(rs.agent, session_id=session_id))
                if not accepted:
                    stderr_error(f"session {session_id} has no active prompt")
                    sys.exit(1)
                stderr(f"cancel requested for daemon session {session_id} ({rs.agent})")
                return
            _stop_session(rs, session_id)
            return

        if agent:
            sessions = get_running_by_agent(agent)
            for rs in sessions:
                if getattr(rs, "daemon", False):
                    accepted = asyncio.run(cancel_daemon_prompt(agent, session_id=rs.session_id))
                    if not accepted:
                        stderr_error(f"session {rs.session_id} has no active prompt")
                        sys.exit(1)
                else:
                    _stop_session(rs, rs.session_id)
            if sessions:
                return
            if agent in {target for target, _ in _daemon_statuses()}:
                accepted = asyncio.run(cancel_daemon_prompt(agent))
                if not accepted:
                    stderr_error(f"daemon '{agent}' has no active prompt")
                    sys.exit(1)
                stderr(f"cancel requested for daemon '{agent}'")
                return
            stderr_error(f"no running sessions for agent '{agent}'")
            sys.exit(1)

        stderr_error("specify an agent name or --session ID")
        sys.exit(2)

    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        stderr_error(f"unexpected error: {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@cli.command()
def status() -> None:
    """Show running sessions."""
    try:
        from acpc.sessions import list_running

        running = list_running()
        daemon_statuses = _daemon_statuses()
        if not running and not daemon_statuses:
            click.echo("No running sessions.", err=True)
            return

        if running:
            header = f"{'SESSION_ID':<40}  {'AGENT':<20}  {'PID':<8}  {'CWD':<30}  {'STARTED'}"
            click.echo(header)
            for rs in running.values():
                line = (
                    f"{rs.session_id:<40}  {rs.agent:<20}  {rs.pid:<8}  {rs.cwd:<30}  {rs.started}"
                )
                click.echo(line)
        for target, frame in daemon_statuses:
            _print_daemon_status(target, frame)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        stderr_error(f"unexpected error: {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("agent", required=False)
def models(agent: str | None) -> None:
    """Show available models and presets for agents.

    Reads from cache (refreshed on every acpc prompt call).
    If cache is stale or missing, fetches live from the adapter.
    """
    import asyncio

    from acpc.agents import list_agents
    from acpc.models_cache import is_cache_fresh, load_cached_models
    from acpc.presets import get_presets
    from acpc.output import stderr

    def _show_agent_models(agent_id: str) -> None:
        presets = get_presets(agent_id)
        cached = load_cached_models(agent_id)

        click.echo(f"{agent_id}:")

        if presets:
            tier_order = ["fast", "standard", "max"]
            ordered = [(k, presets[k]) for k in tier_order if k in presets]
            click.echo("  presets:")
            for tier, model_id in ordered:
                click.echo(f"    {tier:<12} {model_id}")
        else:
            click.echo("  presets: (none)")

        if cached:
            models_list = cached.get("available_models", [])
            click.echo("  models:")
            for m in models_list:
                click.echo(f"    {m.get('model_id', '?')}")
        else:
            click.echo("  models: (not installed)")

    try:
        if agent:
            _require_agent(agent)

            # If cache is stale or missing, fetch live
            if not is_cache_fresh(agent):
                stderr(f"fetching models for {agent}...")
                exit_code = asyncio.run(_fetch_models_live(agent))
                if exit_code != 0:
                    stderr(f"warning: live fetch failed (exit {exit_code}), showing cached data")

            _show_agent_models(agent)
        else:
            # Show all agents, fetch stale/missing for installed ones
            from acpc.agents import is_installed

            all_agents = list_agents()
            stale = [a for a in all_agents if is_installed(a) and not is_cache_fresh(a.identity)]
            if stale:
                names = ", ".join(a.identity for a in stale)
                stderr(f"fetching models for {names}...")
                for a in stale:
                    asyncio.run(_fetch_models_live(a.identity))

            for a in all_agents:
                _show_agent_models(a.identity)
                click.echo()

    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        stderr_error(f"unexpected error: {e}")
        sys.exit(1)


@cli.command(name="models-refresh", hidden=True)
@click.argument("agent")
def models_refresh(agent: str) -> None:
    """Force refresh model cache for an agent (hidden command)."""
    import asyncio

    from acpc.output import stderr

    try:
        _require_agent(agent)
        stderr(f"fetching models for {agent}...")
        exit_code = asyncio.run(_fetch_models_live(agent))
        if exit_code != 0:
            stderr_error(f"live fetch failed (exit {exit_code})")
            sys.exit(1)
        stderr("done")
    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        stderr_error(f"unexpected error: {e}")
        sys.exit(1)


async def _fetch_models_live(agent_identity: str) -> int:
    """Spawn adapter, initialize, new_session, cache models, exit.

    Returns 0 on success, non-zero on failure.
    """
    import os
    import shlex

    import acp
    from acp import RequestError

    from acpc.agents import load_agent
    from acpc.runner import _cache_available_models, _spawn_agent

    agent = load_agent(agent_identity)
    parts = shlex.split(agent.run_command)
    command, args = parts[0], parts[1:]

    class _NoopClient:
        """Minimal client that ignores all callbacks."""

        async def session_update(self, **kwargs):  # type: ignore[no-untyped-def] # noqa: ANN003, ARG002
            pass

        async def request_permission(self, **kwargs):  # type: ignore[no-untyped-def] # noqa: ANN003, ARG002
            return {"allowed": False}

        async def log(self, **kwargs):  # type: ignore[no-untyped-def] # noqa: ANN003, ARG002
            pass

    cwd = os.getcwd()

    import logging

    # Suppress SDK "Receive loop failed" noise during fast shutdown
    logging.getLogger().setLevel(logging.CRITICAL)

    try:
        async with _spawn_agent(_NoopClient(), command, *args, cwd=cwd) as (conn, _process):
            await conn.initialize(protocol_version=acp.PROTOCOL_VERSION)
            resp = await conn.new_session(cwd=cwd)
            return 0 if _cache_available_models(agent_identity, resp) else 1
    except (RequestError, OSError, RuntimeError):
        return 1


# ---------------------------------------------------------------------------
# generate-completion
# ---------------------------------------------------------------------------

from acpc._completion import add_completion_command  # noqa: E402

add_completion_command(cli, "acpc")
