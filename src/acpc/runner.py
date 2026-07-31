"""ACP session runner for acpc.

Main orchestrator: spawns agent process, initializes connection,
creates/loads session, runs prompt, handles signals.
"""

import asyncio
import asyncio.subprocess as aio_subprocess
import contextlib
import os
import shlex
import signal
import subprocess as _subprocess
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import acp
from acp import RequestError
from acp.client import ClientSideConnection
from acp.transports import default_environment

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

# Graceful shutdown timeout (seconds)
_SHUTDOWN_TIMEOUT = 2.0

# Upper bound on waiting for in-flight notifications after a prompt completes
_DRAIN_TIMEOUT = 5.0


@dataclass
class RunConfig:
    """Configuration for a single run."""

    agent_identity: str
    prompt_text: str
    model: str | None = None
    model_preset: str | None = None  # original preset name before resolution (fast/standard/max)
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


# --- Process group management (Zed pattern) ---


def _process_group_kwargs() -> dict[str, Any]:
    """Platform-specific kwargs to spawn adapter in its own process group.

    Linux/macOS: start_new_session=True (calls setsid, adapter becomes PGID leader).
    Windows: CREATE_NEW_PROCESS_GROUP flag.
    """
    if sys.platform == "win32":
        return {"creationflags": _subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_process_tree(pid: int) -> None:
    """Kill adapter and all its children (cross-platform).

    Linux/macOS: killpg sends SIGKILL to the entire process group.
    Windows: taskkill /T recursively kills the process tree.
    """
    if sys.platform == "win32":
        _subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
        )
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


@asynccontextmanager
async def _spawn_agent(
    client: Any,
    command: str,
    *args: str,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> AsyncIterator[tuple[ClientSideConnection, aio_subprocess.Process]]:
    """Spawn ACP agent in its own process group for reliable cleanup.

    Like acp.spawn_agent_process but with process group isolation
    (start_new_session on Unix, CREATE_NEW_PROCESS_GROUP on Windows).
    On exit, kills the entire process tree via killpg/taskkill.
    """
    merged_env = dict(os.environ)
    merged_env.update(default_environment())
    if env:
        merged_env.update(env)

    process = await asyncio.create_subprocess_exec(
        command,
        *args,
        stdin=aio_subprocess.PIPE,
        stdout=aio_subprocess.PIPE,
        stderr=aio_subprocess.PIPE,
        env=merged_env,
        cwd=str(cwd) if cwd is not None else None,
        limit=10_485_760,  # 10 MB; asyncio default (64 KB) too small for large NDJSON frames
        **_process_group_kwargs(),
    )

    if process.stdout is None or process.stdin is None:
        process.kill()
        await process.wait()
        raise RuntimeError("failed to create stdio pipes for agent process")

    conn = ClientSideConnection(client, process.stdin, process.stdout)
    pid = process.pid

    try:
        yield conn, process
    finally:
        # 1. Close ACP connection (protocol-level shutdown)
        with contextlib.suppress(Exception):
            await conn.close()

        # 2. Graceful: close stdin to signal adapter
        if process.stdin is not None:
            try:
                process.stdin.write_eof()
            except (AttributeError, OSError, RuntimeError):
                with contextlib.suppress(Exception):
                    process.stdin.close()
            with contextlib.suppress(Exception):
                await process.stdin.drain()
            with contextlib.suppress(Exception):
                process.stdin.close()

        # 3. Wait briefly for graceful exit
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=_SHUTDOWN_TIMEOUT)

        # 4. Kill entire process tree (adapter + all children)
        if process.returncode is None:
            kill_process_tree(pid)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=_SHUTDOWN_TIMEOUT)


def _setup_signals(
    conn: ClientSideConnection,
    session_id: str,
    pid: int,
) -> None:
    """Set up SIGINT/SIGTERM handlers.

    On signal: cancel ACP session, wait for graceful shutdown, kill process tree.
    """
    loop = asyncio.get_running_loop()

    async def _shutdown(sig_num: int) -> None:
        # 1. Cancel session via ACP protocol
        with contextlib.suppress(RequestError, OSError):
            await conn.cancel(session_id=session_id)

        # 2. Give agent time to shut down gracefully
        await asyncio.sleep(_SHUTDOWN_TIMEOUT)

        # 3. Kill entire process tree
        kill_process_tree(pid)

    def _handler(sig_num: int) -> None:
        loop.create_task(_shutdown(sig_num))

    if sys.platform != "win32":
        loop.add_signal_handler(signal.SIGINT, _handler, signal.SIGINT)
        loop.add_signal_handler(signal.SIGTERM, _handler, signal.SIGTERM)


async def _drain_notifications(conn: ClientSideConnection) -> None:
    """Wait for queued session/update notifications to reach the client.

    The ACP SDK resolves responses inline but routes notifications through a
    queue whose handlers run as separate supervised tasks. A session/prompt
    response can therefore be delivered before the final agent_message_chunk
    has been dispatched, silently truncating --quiet output and -o files.

    Reaches into SDK internals, so every step is optional: if the layout
    changes this degrades to the previous (racy) behaviour rather than
    breaking the run.
    """
    connection = getattr(conn, "_conn", None)
    queue = getattr(connection, "_queue", None)
    supervisor = getattr(connection, "_tasks", None)

    # Best-effort: a completed prompt must never fail because the drain did.
    with contextlib.suppress(Exception):
        async with asyncio.timeout(_DRAIN_TIMEOUT):
            if queue is not None and hasattr(queue, "join"):
                await queue.join()
            pending = {
                task
                for task in getattr(supervisor, "_tasks", set())
                if not task.done() and "notification" in (task.get_name() or "")
            }
            if pending:
                await asyncio.wait(pending)


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


def _find_model_config_option(session_response: Any | None) -> Any | None:
    """Return the unwrapped ACP model config option, if advertised."""
    if session_response is None:
        return None
    for config_option in getattr(session_response, "config_options", None) or []:
        config_option = getattr(config_option, "root", config_option)
        if (
            getattr(config_option, "category", None) == "model"
            or getattr(config_option, "id", None) == "model"
        ):
            return config_option
    return None


async def _try_set_model(
    conn: ClientSideConnection,
    session_id: str,
    model: str,
    session_response: Any | None,
    log: Callable[[str], None],
) -> bool:
    """Set the model through the current ACP API, with legacy fallback.

    Returns True if the adapter accepted the model selection call.
    """
    model_config = _find_model_config_option(session_response)
    model_id = model
    reasoning_effort = None
    base_model, separator, suffix = model.rpartition("/")
    if separator and suffix in {
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "ultra",
    }:
        model_id = base_model
        reasoning_effort = suffix

    valid_ids: list[str] = []
    if model_config is not None:
        valid_ids = [
            option.value
            for option in getattr(model_config, "options", None) or []
            if isinstance(getattr(option, "value", None), str)
        ]
    elif session_response is not None and hasattr(session_response, "models"):
        models_state = session_response.models
        if models_state is not None and hasattr(models_state, "available_models"):
            available = models_state.available_models
            if available:
                valid_ids = [m.model_id for m in available if hasattr(m, "model_id")]

    # Lists can be incomplete, so warn but still pass the model through.
    if valid_ids and model_id not in valid_ids:
        log(  # type: ignore[operator]
            f"note: model '{model_id}' not in advertised models "
            f"({', '.join(valid_ids[:5])}), trying anyway"
        )

    try:
        if model_config is not None:
            await conn.set_config_option(
                config_id=getattr(model_config, "id", "model"),
                session_id=session_id,
                value=model_id,
            )
            if reasoning_effort is not None:
                await conn.set_config_option(
                    config_id="reasoning_effort",
                    session_id=session_id,
                    value=reasoning_effort,
                )
        else:
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
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task


def _cache_available_models(agent: str, new_session_resp: Any) -> bool:
    """Cache models advertised by either the legacy or current ACP schema."""
    try:
        model_list: list[dict[str, Any]] = []

        # ACP originally exposed models through models.available_models.
        models_state = getattr(new_session_resp, "models", None)
        available = getattr(models_state, "available_models", None)
        for model in available or []:
            model_id = getattr(model, "model_id", None)
            if not isinstance(model_id, str):
                continue
            entry = {"model_id": model_id}
            display_name = getattr(model, "display_name", None)
            if isinstance(display_name, str):
                entry["display_name"] = display_name
            model_list.append(entry)

        # Current ACP exposes model selection as a session config option.
        if not model_list:
            config_option = _find_model_config_option(new_session_resp)
            for option in getattr(config_option, "options", None) or []:
                model_id = getattr(option, "value", None)
                if not isinstance(model_id, str):
                    continue
                entry = {"model_id": model_id}
                display_name = getattr(option, "name", None)
                if isinstance(display_name, str):
                    entry["display_name"] = display_name
                model_list.append(entry)

        if not model_list:
            return False

        from acpc.models_cache import save_models

        save_models(agent, model_list)
        return True
    except Exception:
        return False  # Best-effort, never fail the prompt


async def run(config: RunConfig) -> int:
    """Execute a prompt against an ACP agent. Returns exit code.

    Steps:
    1. Load agent from registry
    2. Create OutputHandler + AcpcClient
    3. Spawn agent process in its own process group
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

    # 4. Spawn in own process group and run
    try:
        async with _spawn_agent(
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
                stderr(
                    f"warning: --last resolved to session {session_id}. "
                    "For reliable multi-agent orchestration, use -s SESSION_ID instead."
                )

            session_resp = None
            if session_id and supports_load:
                try:
                    with client.replaying_history():
                        session_resp = await conn.load_session(cwd=cwd, session_id=session_id)
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
                session_resp = await conn.new_session(cwd=cwd)
                session_id = session_resp.session_id

            if session_resp is not None:
                # Cache advertised models as a side effect (zero extra cost).
                _cache_available_models(config.agent_identity, session_resp)

            # Emit run info
            if config.model:
                if config.model_preset:
                    stderr(
                        f"agent: {config.agent_identity}, model: {config.model} (preset: {config.model_preset})"
                    )
                else:
                    stderr(f"agent: {config.agent_identity}, model: {config.model}")
            else:
                stderr(f"agent: {config.agent_identity}")
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
                    conn, session_id, config.model, session_resp, stderr
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
            await _drain_notifications(conn)

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
