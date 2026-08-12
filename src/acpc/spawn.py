"""Adapter subprocess spawn with process-group isolation.

Harvested from the 0.3.0.dev1 runner. The two hard-won details:

- `limit=10_485_760` on the stdio streams — with the donor's acp version,
  asyncio's 64 KB default killed the connection mid-answer on large NDJSON
  frames. acp >= 0.11 reassembles oversized lines itself, so today the large
  limit is belt-and-braces: it keeps frames in one read and stays safe if
  that reassembly ever changes.
- The teardown ladder: protocol close → stdin EOF → bounded wait → process
  *tree* kill (killpg / taskkill), but only while the captured group leader is
  unreaped, because a reaped leader's numeric group ID may be recycled.

Unlike the donor, the caller passes the fully constructed environment
(`acpc.environment.adapter_environment`) — spawning stays decoupled from
entry resolution.
"""

import asyncio
import contextlib
import sys
from asyncio import subprocess as aio_subprocess
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from acp.client import ClientSideConnection

from acpc.proc import kill_process_tree, process_group_kwargs

NDJSON_STREAM_LIMIT = 10_485_760  # 10 MB; asyncio default (64 KB) too small for NDJSON frames
_EXIT_TIMEOUT = 1.0


async def _close_acp_connection(conn: ClientSideConnection) -> None:
    """Stop ACP reception before its dispatcher queue is closed.

    ACP 0.13 closes the notification queue in ``Connection.close`` before it
    cancels the receive loop. A frame arriving in that interval is then
    published to the closed queue and logs a spurious receive-loop failure.
    The receive task is an implementation seam of the pinned ACP transport;
    cancelling it first preserves the adapter connection's normal teardown.
    """
    raw_connection = getattr(conn, "_conn", None)
    receive_task = getattr(raw_connection, "_recv_task", None)
    if isinstance(receive_task, asyncio.Task) and not receive_task.done():
        receive_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await receive_task
    with contextlib.suppress(Exception):
        await conn.close()


async def _forward_stderr(stream: asyncio.StreamReader) -> None:
    """Forward adapter stderr to acpc's stderr while the adapter is running."""
    try:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            try:
                stderr_buffer = getattr(sys.stderr, "buffer", None)
                if stderr_buffer is not None:
                    stderr_buffer.write(chunk)
                    stderr_buffer.flush()
                else:
                    sys.stderr.write(chunk.decode(errors="replace"))
                    sys.stderr.flush()
            except (OSError, UnicodeError, ValueError):
                return
    except (OSError, ValueError):
        return


async def _stop_stderr_forwarder(task: asyncio.Task[None]) -> None:
    """Wait briefly for stderr EOF, then cancel the forwarder if needed."""
    # A forwarder failure is non-fatal by design; only the timeout needs acting on.
    with contextlib.suppress(Exception):
        try:
            await asyncio.wait_for(task, timeout=_EXIT_TIMEOUT)
        except TimeoutError:
            task.cancel()
    if not task.done():
        task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


@asynccontextmanager
async def spawn_adapter(
    client: Any,
    command: str,
    *args: str,
    env: Mapping[str, str],
    cwd: str | None = None,
    drain_stderr: bool = False,
) -> AsyncIterator[tuple[ClientSideConnection, aio_subprocess.Process]]:
    """Spawn an ACP adapter in its own process group for reliable cleanup.

    Like acp.spawn_agent_process but with process-group isolation
    (start_new_session on Unix, CREATE_NEW_PROCESS_GROUP on Windows).
    On exit, kills the entire process tree via killpg/taskkill.

    When enabled, drain the adapter's stderr pipe and forward it to acpc's
    stderr. Callers that consume the pipe themselves must leave this disabled.
    """
    process = await asyncio.create_subprocess_exec(
        command,
        *args,
        stdin=aio_subprocess.PIPE,
        stdout=aio_subprocess.PIPE,
        stderr=aio_subprocess.PIPE,
        env=dict(env),
        cwd=str(cwd) if cwd is not None else None,
        limit=NDJSON_STREAM_LIMIT,
        **process_group_kwargs(),
    )

    if process.stdout is None or process.stdin is None or process.stderr is None:
        process.kill()
        await process.wait()
        raise RuntimeError("failed to create stdio pipes for agent process")

    conn = ClientSideConnection(client, process.stdin, process.stdout)
    stderr_task = (
        asyncio.create_task(
            _forward_stderr(process.stderr),
            name="acpc.adapter.stderr",
        )
        if drain_stderr
        else None
    )
    pid = process.pid
    process_group_id = pid if sys.platform != "win32" else None

    try:
        yield conn, process
    finally:
        # 1. Close ACP connection (protocol-level shutdown)
        await _close_acp_connection(conn)

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
            await asyncio.wait_for(process.wait(), timeout=_EXIT_TIMEOUT)

        # 4. Kill the process tree only while the captured leader is still
        # unreaped. Once it has been reaped, its numeric group ID may have
        # been recycled for an unrelated process group.
        if process.returncode is None:
            kill_process_tree(pid, process_group_id=process_group_id)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=_EXIT_TIMEOUT)

        # 5. Drain any final diagnostics, but never let a broken or inherited
        # stderr pipe keep the runner alive.
        if stderr_task is not None:
            await _stop_stderr_forwarder(stderr_task)
