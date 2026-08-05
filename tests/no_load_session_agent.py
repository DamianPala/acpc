"""Mock agent variant that advertises no ACP session/load capability."""

import asyncio
from typing import Any

from acp import InitializeResponse, run_agent
from acp.schema import AgentCapabilities
from mock_agent import MockAgent


class NoLoadSessionAgent(MockAgent):
    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: Any = None,
        client_info: Any = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        initialized = await super().initialize(
            protocol_version,
            client_capabilities=client_capabilities,
            client_info=client_info,
            **kwargs,
        )
        return InitializeResponse(
            protocol_version=initialized.protocol_version,
            agent_capabilities=AgentCapabilities(load_session=False),
            agent_info=initialized.agent_info,
        )


async def main() -> None:
    await run_agent(NoLoadSessionAgent())


if __name__ == "__main__":
    asyncio.run(main())
