#!/usr/bin/env python
"""Business Risk Agent - queries credit risk data."""

from typing import Any, Dict, List
import logging

from src.contracts.agent_contract import AgentContract, DataContractRef
from src.contracts.agent_result import AgentResultError

from .base_agent import BaseRemoteAgent

logger = logging.getLogger(__name__)


class RemoteBusinessRiskAgent(BaseRemoteAgent):
    """Business Risk Agent for querying credit risk metrics."""

    def __init__(self):
        super().__init__(
            name="RemoteBusinessRiskAgent",
            prompt="You are a business risk analyst that queries credit risk databases.",
            contract=AgentContract(
                contract_version="1.0",
                produces=[
                    DataContractRef(
                        name="risk.records",
                        schema_ref="structured_agent_result@v1",
                    )
                ],
            ),
        )

    async def execute(
        self,
        tools: List[Dict[str, Any]],
        messages: List[Dict[str, Any]],
        context: Dict[str, Any],
        parameter_extractor: Any
    ) -> Dict[str, Any]:
        """Execute credit risk query - single tool agent."""
        if not tools or len(tools) == 0:
            return self.result_envelope(
                error=AgentResultError(
                    code="NO_TOOL_PROVIDED",
                    message="No business risk tool was provided",
                    retryable=False,
                )
            )

        tool = tools[0]
        tool_name = tool.get("name", "unknown")

        try:
            logger.info(f"[{self.name}] Extracting parameters for {tool_name}")
            arguments = await parameter_extractor.extract(
                agent_name=self.name,
                agent_prompt=self.prompt,
                tool=tool,
                messages=messages,
            )

            logger.info(f"[{self.name}] Calling {tool_name}")
            result = await self.call_tool(
                tool_name=tool_name,
                arguments=arguments,
            )
            return self.result_envelope(outputs={"risk.records": result})
        except Exception as exc:
            return self.result_envelope(
                error=self.execution_error(exc, tool_name=tool_name)
            )
