"""Behavioral checks for ACP subprocess teardown."""

import asyncio

from acpc import spawn


def test_acp_receive_loop_is_cancelled_before_dispatcher_close() -> None:
    async def scenario() -> None:
        receive_started = asyncio.Event()

        async def receive_loop() -> None:
            receive_started.set()
            await asyncio.Event().wait()

        receive_task = asyncio.create_task(receive_loop())
        await receive_started.wait()
        close_saw_cancelled_receive = False

        class RawConnection:
            _recv_task = receive_task

        class ClientConnection:
            _conn = RawConnection()

            async def close(self) -> None:
                nonlocal close_saw_cancelled_receive
                close_saw_cancelled_receive = receive_task.cancelled()

        await spawn._close_acp_connection(ClientConnection())  # type: ignore[arg-type]
        assert close_saw_cancelled_receive

    asyncio.run(scenario())
