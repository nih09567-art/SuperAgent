#!/usr/bin/env python
"""Email Dispatch Agent - sends emails."""

from typing import Any, Dict, List
import json
import logging
from datetime import datetime, timezone

from src.contracts.agent_contract import AgentContract, DataContractRef
from src.contracts.agent_result import AgentResultError

from .base_agent import BaseRemoteAgent

logger = logging.getLogger(__name__)


def _execution_brief(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    for message in reversed(messages):
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or "EXECUTION_CONTEXT" not in content:
            continue
        try:
            payload = json.loads(content.split("EXECUTION_CONTEXT", 1)[1].strip())
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def _upstream_body(brief: Dict[str, Any]) -> str:
    resolved = brief.get("resolved_inputs") or {}
    if not isinstance(resolved, dict):
        return ""
    for value in resolved.values():
        if isinstance(value, dict):
            for key in ("markdown", "content", "message", "file_path"):
                candidate = value.get(key)
                if candidate not in (None, ""):
                    return str(candidate)
        elif value not in (None, ""):
            return str(value)
    return ""


def _resolved_dispatch_request(brief: Dict[str, Any]) -> Dict[str, Any]:
    resolved = brief.get("resolved_inputs") or {}
    request = (
        resolved.get("email.dispatch.request") if isinstance(resolved, dict) else None
    )
    return request if isinstance(request, dict) else {}


class RemoteEmailDispatchAgent(BaseRemoteAgent):
    """Email Dispatch Agent for sending emails."""

    def __init__(self):
        super().__init__(
            name="RemoteEmailDispatchAgent",
            prompt="You are an email dispatcher that sends emails to recipients.",
            contract=AgentContract(
                contract_version="1.0",
                requires=[
                    DataContractRef(
                        name="email.dispatch.request",
                        schema_ref="email.dispatch.request@v1",
                    )
                ],
                produces=[
                    DataContractRef(
                        name="email.dispatch.receipt",
                        schema_ref="email.dispatch.receipt@v1",
                    )
                ],
            ),
        )

    async def execute(
        self,
        tools: List[Dict[str, Any]],
        messages: List[Dict[str, Any]],
        context: Dict[str, Any],
        parameter_extractor: Any,
    ) -> Dict[str, Any]:
        """Execute email sending - single tool agent."""
        if not tools or len(tools) == 0:
            return {"error": "No tools provided"}

        tool = tools[0]
        tool_name = tool.get("name", "unknown")

        logger.info(f"[{self.name}] Extracting parameters for {tool_name}")
        arguments = await parameter_extractor.extract(
            agent_name=self.name, agent_prompt=self.prompt, tool=tool, messages=messages
        )
        if not isinstance(arguments, dict):
            arguments = {}

        # The model still performs parameter extraction and any model error is
        # propagated.  This is contract normalization only: reuse recipient and
        # upstream content already validated by TaskProfile/ArtifactResolver
        # when the extracted optional fields are empty.
        brief = _execution_brief(messages)
        request = _resolved_dispatch_request(brief)
        profile = brief.get("task_profile") or {}
        entities = profile.get("entities") or {}
        recipients = request.get("recipients")
        recipient = str(
            (recipients[0] if isinstance(recipients, list) and recipients else None)
            or entities.get("recipient")
            or ""
        ).strip()
        if recipient and not str(arguments.get("to") or "").strip():
            arguments["to"] = recipient
        if request.get("subject") and not str(arguments.get("subject") or "").strip():
            arguments["subject"] = str(request["subject"])
        if not str(arguments.get("body") or "").strip():
            arguments["body"] = str(request.get("body") or _upstream_body(brief))
        if not str(arguments.get("subject") or "").strip():
            arguments["subject"] = str(entities.get("document_type") or "任务结果通知")

        logger.info(f"[{self.name}] Calling {tool_name}")
        try:
            result = await self.call_tool(
                tool_name=tool_name,
                arguments=arguments,
            )
        except Exception as exc:
            return self.result_envelope(
                error=self.execution_error(exc, tool_name=tool_name)
            )

        if not isinstance(result, dict) or str(
            result.get("status") or ""
        ).lower() not in {
            "success",
            "sent",
            "simulated",
        }:
            message = str((result or {}).get("error") or "email dispatch failed")
            return self.result_envelope(
                error=AgentResultError(
                    code="EMAIL_DISPATCH_FAILED",
                    message=message,
                    retryable=bool((result or {}).get("safe_to_retry")),
                )
            )

        provider_message_id = str(
            result.get("provider_message_id")
            or result.get("message_id")
            or result.get("external_operation_id")
            or (result.get("sent") or {}).get("id")
            or ""
        )
        receipt = {
            "dispatch_mode": "simulated",
            "provider_message_id": provider_message_id,
            "status": (
                "sent" if str(result.get("status")).lower() == "sent" else "simulated"
            ),
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "approval_id": str(request.get("approval_id") or "not_required"),
            "idempotency_key": str(request.get("idempotency_key") or ""),
        }
        return self.result_envelope(outputs={"email.dispatch.receipt": receipt})
