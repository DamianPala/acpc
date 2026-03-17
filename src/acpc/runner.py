"""ACP session runner for acpc.

Main orchestrator: spawns agent process, initializes connection,
creates/loads session, runs prompt, handles signals.
"""

import asyncio
import os
import shlex
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

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
    loop = asyncio.get_running_loop()
    start = loop.time()
    while True:
        await asyncio.sleep(60)
        elapsed = loop.time() - start
        minutes = int(elapsed) // 60
        seconds = int(elapsed) % 60
        print(f"[acpc] still running... ({minutes}m {seconds}s)", file=sys.stderr, flush=True)


async def _try_set_model(
    conn: ClientSideConnection,
    session_id: str,
    model: str,
    new_session_response: Any | None,
    log: Callable[[str], None],
) -> bool:
    """Try to set the model, pre-validating against available models if possible.

    Returns True if set_session_model was called (even if adapter accepted silently).
    """
    # Warn if model not in available_models, but still try (list may be incomplete)
    if new_session_response is not None and hasattr(new_session_response, "models"):
        models_state = new_session_response.models
        if models_state is not None and hasattr(models_state, "available_models"):
            available = models_state.available_models
            if available:
                valid_ids = [m.model_id for m in available if hasattr(m, "model_id")]
                if valid_ids and model not in valid_ids:
                    log(  # type: ignore[operator]
                        f"note: model '{model}' not in advertised models "
                        f"({', '.join(valid_ids[:5])}), trying anyway"
                    )

    try:
        await conn.set_session_model(model_id=model, session_id=session_id)
        return True
    except RequestError as e:
        log(f"warning: failed to set model '{model}': {e}")  # type: ignore[operator]
        return False


async def _send_prompt(
    conn: ClientSideConnection,
    session_id: str,
    config: "RunConfig",
    model_was_set: bool,
    log: Callable[[str], None],
) -> Any:
    """Send prompt with heartbeat. Retry without model if prompt fails after set_model.

    Some adapters (codex-acp) accept set_session_model but then fail on prompt()
    with Internal error. This retries once without the model override.
    """
    is_quiet = config.output_mode == "quiet"
    heartbeat_task = asyncio.create_task(_heartbeat(is_quiet))
    try:
        prompt_coro = conn.prompt([acp.text_block(config.prompt_text)], session_id=session_id)
        if config.timeout:
            result = await asyncio.wait_for(prompt_coro, timeout=config.timeout)
        else:
            result = await prompt_coro
        return result
    except RequestError as e:
        if not model_was_set:
            raise
        # Model was set and prompt failed. Retry without model override
        log(  # type: ignore[operator]
            f"warning: prompt failed after --model ('{e}'), retrying without model override"
        )
        prompt_coro = conn.prompt([acp.text_block(config.prompt_text)], session_id=session_id)
        if config.timeout:
            return await asyncio.wait_for(prompt_coro, timeout=config.timeout)
        return await prompt_coro
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass


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
    from acpc.agents import load_agent
    from acpc.client import AcpcClient, PermissionLevel
    from acpc.output import (
        OutputHandler,
        OutputMode,
        stderr,
        stderr_error,
        stderr_resume,
        stderr_session,
    )

    # 1. Load agent (raises AgentNotFoundError if not found, caught by cli.py)
    agent = load_agent(config.agent_identity)

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
            if process.pid is None:
                stderr_error("agent process failed to start")
                return EXIT_AGENT_ERROR

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

            new_session_resp = None
            if session_id is None:
                new_session_resp = await conn.new_session(cwd=cwd)
                session_id = new_session_resp.session_id

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
            model_was_set = False
            if config.model:
                model_was_set = await _try_set_model(
                    conn, session_id, config.model, new_session_resp, stderr
                )

            if config.mode:
                try:
                    await conn.set_session_mode(
                        mode_id=config.mode,
                        session_id=session_id,
                    )
                except RequestError as e:
                    stderr(f"warning: failed to set mode '{config.mode}': {e}")

            # 8. Send prompt (with heartbeat, retry on model error)
            result = await _send_prompt(conn, session_id, config, model_was_set, stderr)

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
