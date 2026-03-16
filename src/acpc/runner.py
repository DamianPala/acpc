"""ACP session runner for acpc.

Main orchestrator: spawns agent process, initializes connection,
creates/loads session, runs prompt, handles signals.
"""

import asyncio
import os
import shlex
import signal
import sys
from dataclasses import dataclass, field

import acp
from acp import RequestError
from acp.client import ClientSideConnection

from acpc.sessions import (
    add_running,
    load_last_session,
    make_running_session,
    remove_running,
    save_last_session,
)

# --- Exit codes ---

EXIT_AGENT_ERROR = 1
EXIT_USAGE_ERROR = 2
EXIT_PERMISSION_DENIED = 3
EXIT_TIMEOUT = 124
EXIT_SIGINT = 130
EXIT_SIGPIPE = 141
EXIT_SIGTERM = 143

# Map ACP stop reasons to exit codes
_STOP_REASON_EXIT: dict[str, int] = {
    "end_turn": 0,
    "max_tokens": EXIT_AGENT_ERROR,
    "max_turn_requests": EXIT_AGENT_ERROR,
    "refusal": EXIT_AGENT_ERROR,
    "cancelled": EXIT_SIGINT,
}


@dataclass
class RunConfig:
    """Configuration for a single run."""

    agent_identity: str
    prompt_text: str
    model: str | None = None
    mode: str | None = None
    permission_level: str = "prompt"
    cwd: str | None = None
    session_id: str | None = None
    use_last: bool = False
    output_mode: str = "text"
    output_file: str | None = None
    timeout: int | None = None
    is_tty: bool = True
    env: dict[str, str] = field(default_factory=dict)


def _get_preexec_fn():  # type: ignore[no-untyped-def]
    """Return os.setpgrp for Unix, None for Windows.

    CREATE_NEW_PROCESS_GROUP is handled via subprocess flags on Windows.
    """
    if sys.platform != "win32":
        return os.setpgrp
    return None


def _setup_signals(
    conn: ClientSideConnection,
    session_id: str,
    pid: int,
) -> None:
    """Set up SIGINT/SIGTERM handlers.

    On signal: cancel session, wait 5s, SIGTERM process group, wait 2s, SIGKILL.
    """
    loop = asyncio.get_running_loop()

    async def _shutdown(sig_num: int) -> None:
        try:
            await conn.cancel(session_id=session_id)
        except (RequestError, OSError):
            pass

        # Give agent 5s to shut down gracefully
        await asyncio.sleep(5)

        pgid = _get_pgid(pid)
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                return

            await asyncio.sleep(2)
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def _handler(sig_num: int) -> None:
        loop.create_task(_shutdown(sig_num))

    if sys.platform != "win32":
        loop.add_signal_handler(signal.SIGINT, _handler, signal.SIGINT)
        loop.add_signal_handler(signal.SIGTERM, _handler, signal.SIGTERM)


def _get_pgid(pid: int) -> int | None:
    """Get process group ID, or None if process is gone."""
    if sys.platform == "win32":
        return None
    try:
        return os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        return None


async def _heartbeat(quiet: bool) -> None:
    """Print periodic status to stderr during long-running prompts.

    Prints every 60 seconds. Suppressed in quiet mode.
    """
    if quiet:
        return
    start = asyncio.get_event_loop().time()
    while True:
        await asyncio.sleep(60)
        elapsed = asyncio.get_event_loop().time() - start
        minutes = int(elapsed) // 60
        seconds = int(elapsed) % 60
        print(f"[acpc] still running... ({minutes}m {seconds}s)", file=sys.stderr, flush=True)


async def run(config: RunConfig) -> int:
    """Execute a prompt against an ACP agent. Returns exit code.

    Steps:
    1. Load agent from registry
    2. Create OutputHandler + AcpcClient
    3. Spawn agent process via ACP SDK
    4. Initialize connection
    5. Create or load session
    6. Set model/mode if requested
    7. Send prompt
    8. Handle result
    9. Save state and return exit code
    """
    # Import at runtime (these sibling modules don't exist in this worktree yet)
    from acpc.agents import load_agent  # type: ignore[import-not-found]
    from acpc.client import AcpcClient, PermissionLevel  # type: ignore[import-not-found]
    from acpc.output import (
        OutputHandler,
        OutputMode,
        stderr,
        stderr_error,
        stderr_resume,
        stderr_session,
    )  # type: ignore[import-not-found]

    # 1. Load agent
    agent = load_agent(config.agent_identity)
    if agent is None:
        stderr_error(
            f"agent '{config.agent_identity}' not found. "
            f"Run 'acpc agents' to list available agents."
        )
        return EXIT_USAGE_ERROR

    # 2. Create output handler and client
    output = OutputHandler(
        mode=OutputMode(config.output_mode),
        output_file=config.output_file,
    )
    permission = PermissionLevel(config.permission_level)
    client = AcpcClient(output=output, permission_level=permission, is_tty=config.is_tty)

    # 3. Determine command
    parts = shlex.split(agent.run_command)
    command = parts[0]
    args = parts[1:]

    cwd = config.cwd or os.getcwd()

    # 4. Spawn and run
    try:
        async with acp.spawn_agent_process(
            client,
            command,
            *args,
            cwd=cwd,
            env=config.env or None,
        ) as (conn, process):
            assert process.pid is not None

            # 5. Initialize
            init_response = await conn.initialize(
                protocol_version=acp.PROTOCOL_VERSION,
            )

            # Check capabilities
            caps = init_response.agent_capabilities
            supports_load = bool(caps and caps.load_session)

            # 6. Create or load session
            session_id = config.session_id
            explicitly_requested = config.use_last or config.session_id is not None

            if config.use_last and session_id is None:
                session_id = load_last_session(config.agent_identity)
                if session_id is None:
                    stderr_error("no previous session found")
                    return EXIT_USAGE_ERROR

            if session_id and supports_load:
                try:
                    await conn.load_session(cwd=cwd, session_id=session_id)
                except RequestError as e:
                    if explicitly_requested:
                        stderr_error(f"failed to load session {session_id}: {e}")
                        return EXIT_AGENT_ERROR
                    stderr(f"warning: failed to load session {session_id}: {e}, starting new")
                    session_id = None
            elif session_id and not supports_load:
                if explicitly_requested:
                    stderr_error(
                        f"agent '{config.agent_identity}' does not support session loading"
                    )
                    return EXIT_AGENT_ERROR
                stderr(
                    f"warning: agent '{config.agent_identity}' does not support "
                    f"session loading, starting new"
                )
                session_id = None

            if session_id is None:
                new_session = await conn.new_session(cwd=cwd)
                session_id = new_session.session_id

            # Emit session info
            stderr_session(session_id)
            stderr_resume(config.agent_identity, session_id)
            output.on_session_started(session_id)

            # Register running session
            rs = make_running_session(
                session_id=session_id,
                agent=config.agent_identity,
                pid=process.pid,
                cwd=cwd,
            )
            add_running(rs)

            # Set up signal handlers
            _setup_signals(conn, session_id, process.pid)

            # 7. Set model/mode if requested
            if config.model:
                try:
                    await conn.set_session_model(
                        model_id=config.model,
                        session_id=session_id,
                    )
                except RequestError as e:
                    stderr(f"warning: failed to set model '{config.model}': {e}")

            if config.mode:
                try:
                    await conn.set_session_mode(
                        mode_id=config.mode,
                        session_id=session_id,
                    )
                except RequestError as e:
                    stderr(f"warning: failed to set mode '{config.mode}': {e}")

            # 8. Send prompt (with heartbeat)
            is_quiet = config.output_mode == "quiet"
            heartbeat_task = asyncio.create_task(_heartbeat(is_quiet))
            try:
                if config.timeout:
                    result = await asyncio.wait_for(
                        conn.prompt(
                            [acp.text_block(config.prompt_text)],
                            session_id=session_id,
                        ),
                        timeout=config.timeout,
                    )
                else:
                    result = await conn.prompt(
                        [acp.text_block(config.prompt_text)],
                        session_id=session_id,
                    )
            finally:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

            # 9. Finalize output
            exit_code = _STOP_REASON_EXIT.get(result.stop_reason, EXIT_AGENT_ERROR)
            output.on_session_ended(session_id, result.stop_reason, exit_code)
            output.finalize()

            # 10. Save state and return
            save_last_session(config.agent_identity, session_id)
            remove_running(session_id)
            return exit_code

    except asyncio.TimeoutError:
        stderr_error("timeout reached")
        return EXIT_TIMEOUT
    except KeyboardInterrupt:
        return EXIT_SIGINT
    except FileNotFoundError as e:
        stderr_error(
            f"agent command not found: {e}. Run 'acpc install {config.agent_identity}' first."
        )
        return EXIT_USAGE_ERROR
    except RequestError as e:
        stderr_error(f"ACP error: {e}")
        return EXIT_AGENT_ERROR
    except OSError as e:
        stderr_error(f"OS error: {e}")
        return EXIT_AGENT_ERROR
