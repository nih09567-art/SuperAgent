#!/usr/bin/env python
"""Knowledge Agent - queries HR policies and labor laws."""

from typing import Any, Dict, List
import logging

from src.contracts.agent_contract import AgentContract, DataContractRef
from src.contracts.agent_result import AgentResultError

from .base_agent import BaseRemoteAgent

logger = logging.getLogger(__name__)


class RemoteKnowledgeAgent(BaseRemoteAgent):
    """Knowledge Agent for querying HR policies and labor laws."""

    def __init__(self):
        super().__init__(
            name="RemoteKnowledgeAgent",
            prompt="You are a knowledge assistant that helps query HR policies and labor laws.",
            contract=AgentContract(
                contract_version="1.0",
                produces=[
                    DataContractRef(
                        name="policy.info",
                        schema_ref="policy.info@v2",
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
        """Execute knowledge query - single tool agent."""
        if not tools or len(tools) == 0:
            return self.result_envelope(
                error=AgentResultError(
                    code="NO_TOOL_PROVIDED",
                    message="No knowledge tool was provided",
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
                messages=messages
            )

            logger.info(f"[{self.name}] Calling {tool_name}")
            result = await self.call_tool(
                tool_name=tool_name,
                arguments=arguments,
                timeout=60  # Knowledge queries may take longer
            )
            payload = {
                # Keep the remote tool's types intact. Contract validation must
                # see malformed metadata and fail closed instead of accepting a
                # sanitized approximation of the result.
                "query": (
                    result["query"] if "query" in result else arguments.get("query")
                ),
                "answer": result.get("answer"),
                "knowledge_items_count": result.get("knowledge_items_count"),
                "policy_scope": (
                    result["policy_scope"]
                    if "policy_scope" in result
                    else "statutory"
                ),
            }
            for field in ("sources", "matched_items", "not_found"):
                if field in result:
                    payload[field] = result[field]
            return self.result_envelope(outputs={"policy.info": payload})
        except Exception as exc:
            return self.result_envelope(
                error=self.execution_error(exc, tool_name=tool_name)
            )
