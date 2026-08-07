#!/usr/bin/env python
"""Office Assistant Agent - handles leave and travel applications."""

from datetime import datetime, timezone
from typing import Any, Dict, List
import logging
import json

from src.contracts.agent_contract import AgentContract, DataContractRef

from .base_agent import BaseRemoteAgent

logger = logging.getLogger(__name__)


def _execution_context_brief(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Read the newest Scheduler-created execution brief."""

    for message in reversed(messages or []):
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.startswith("EXECUTION_CONTEXT"):
            continue
        _, _, raw = content.partition("\n")
        try:
            brief = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(brief, dict):
            return brief
    return {}


def _resolved_employee_identity(messages: List[Dict[str, Any]]) -> Dict[str, str]:
    """Resolve query identity from trusted Artifact inputs and profile entities."""

    brief = _execution_context_brief(messages)
    resolved = brief.get("resolved_inputs") or {}
    employee_info = resolved.get("employee.info") if isinstance(resolved, dict) else None
    if isinstance(employee_info, dict) and isinstance(employee_info.get("payload"), dict):
        employee_info = employee_info["payload"]

    record: Dict[str, Any] = {}
    if isinstance(employee_info, dict):
        records = employee_info.get("records")
        if isinstance(records, list):
            record = next((item for item in records if isinstance(item, dict)), {})
        elif employee_info:
            record = employee_info

    def first_value(*keys: str) -> str:
        for key in keys:
            value = record.get(key)
            if value not in (None, ""):
                return str(value).strip()
        return ""

    name = first_value("employee_name", "adtEmpeNm", "name")
    employee_id = first_value("employee_id", "employeeId")

    profile = brief.get("task_profile") or {}
    entities = profile.get("entities") if isinstance(profile, dict) else {}
    if isinstance(entities, dict):
        name = name or str(entities.get("employee_name") or "").strip()
        employee_id = employee_id or str(entities.get("employee_id") or "").strip()
    return {"employee_name": name, "employee_id": employee_id}


def _bind_trusted_employee_identity(
    arguments: Dict[str, Any], messages: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Bind query identity to trusted upstream data before the remote tool call."""

    bound = dict(arguments or {})
    identity = _resolved_employee_identity(messages)
    trusted_name = identity["employee_name"]
    trusted_id = identity["employee_id"]
    if trusted_name:
        bound["employee_name"] = trusted_name
    if trusted_id:
        bound["employee_id"] = trusted_id
    elif trusted_name:
        # A model-generated ID is not authorized when the upstream contract did
        # not provide one; the name remains the governed lookup key.
        bound.pop("employee_id", None)
    return bound


def _leave_record_payload(result: Any, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Project the legacy office-tool response onto the governed leave schema."""

    raw_records = result.get("records", []) if isinstance(result, dict) else []
    if not isinstance(raw_records, list):
        raw_records = []
    projected: list[dict[str, Any]] = []
    employee_id = str(arguments.get("employee_id") or "").strip()
    for raw in raw_records:
        if not isinstance(raw, dict):
            continue
        start = str(raw.get("start_date") or "").strip()
        end = str(raw.get("end_date") or "").strip()
        days = raw.get("days")
        if days in (None, "") and start and end:
            try:
                days = (
                    datetime.fromisoformat(end) - datetime.fromisoformat(start)
                ).days + 1
            except ValueError:
                days = 0
        if days in (None, ""):
            days = 0
        try:
            days = float(days)
            days = int(days) if days.is_integer() else days
        except (TypeError, ValueError):
            days = 0
        employee_id = employee_id or str(raw.get("employee_id") or "").strip()
        projected.append(
            {
                "record_id": str(raw.get("record_id") or "").strip(),
                "leave_type": str(raw.get("leave_type") or "").strip(),
                "start_date": start,
                "end_date": end,
                "days": days,
                "approval_status": str(
                    raw.get("approval_status") or raw.get("status") or ""
                ).strip(),
            }
        )
    return {
        "employee_id": employee_id,
        "records": projected,
        "matched_count": len(projected),
        "queried_at": datetime.now(timezone.utc).isoformat(),
    }


class RemoteOfficeAssistantAgent(BaseRemoteAgent):
    """
    Office Assistant Agent that handles:
    - Save leave records
    - Query leave records
    - Save travel records
    - Query travel records
    """

    def __init__(self):
        super().__init__(
            name="RemoteOfficeAssistantAgent",
            prompt="""You are an office assistant that helps employees with leave and travel applications.

Your responsibilities:
1. Save leave application records
2. Query leave application records
3. Save travel application records
4. Query travel application records

Important notes:
- Extract key information from the conversation (dates, reasons, destinations, etc.)
- Convert relative dates (like "next Wednesday") to specific dates in YYYY-MM-DD format
- Today's date is <<CURRENT_DATE>>
- If information is incomplete, you should still try to extract what's available
- Extract employee_id and employee_name from previous agent results in the conversation history
""",
            contract=AgentContract(
                contract_version="1.0",
                requires=[
                    DataContractRef(
                        name="employee.info",
                        schema_ref="employee.info@v1",
                    )
                ],
                produces=[
                    DataContractRef(
                        name="employee.leave_records",
                        schema_ref="employee.leave_records@v1",
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
        """
        Execute office assistant logic:
        1. Select the appropriate tool based on user intent
        2. Extract parameters
        3. Call the tool
        4. Return result
        """
        logger.info(f"[{self.name}] Starting execution with {len(tools)} tools")

        if not tools:
            return {"status": "failed", "error": "No tools provided"}

        try:
            # If multiple tools, let the extractor select the best one
            if len(tools) > 1:
                logger.info(
                    f"[{self.name}] Multiple tools available, selecting best match"
                )
                selected_tool, params = (
                    await parameter_extractor.select_tool_and_extract(
                        agent_name=self.name,
                        agent_prompt=self.prompt,
                        tools=tools,
                        messages=messages,
                    )
                )
                tool_name = selected_tool.get("name")
            else:
                # Single tool, just extract parameters
                tool = tools[0]
                tool_name = tool.get("name")
                logger.info(f"[{self.name}] Single tool: {tool_name}")
                params = await parameter_extractor.extract(
                    agent_name=self.name,
                    agent_prompt=self.prompt,
                    tool=tool,
                    messages=messages,
                )

            logger.info(
                f"[{self.name}] Tool: {tool_name}, Params: {json.dumps(params, ensure_ascii=False)}"
            )

            if tool_name == "query_leave_record":
                params = _bind_trusted_employee_identity(params, messages)
                logger.info(
                    f"[{self.name}] Bound trusted employee identity: "
                    f"{json.dumps(params, ensure_ascii=False)}"
                )

            # Call the selected tool
            result = await self.call_tool(
                tool_name=tool_name,
                arguments=params,
            )

            if tool_name == "query_leave_record":
                if (
                    not isinstance(result, dict)
                    or str(result.get("status") or "").lower() != "success"
                ):
                    error = self.execution_error(
                        RuntimeError(
                            str((result or {}).get("error") or "leave query failed")
                        ),
                        tool_name=tool_name,
                    )
                    return self.result_envelope(error=error)
                return self.result_envelope(
                    outputs={
                        "employee.leave_records": _leave_record_payload(result, params)
                    }
                )

            logger.info(f"[{self.name}] Tool execution completed successfully")
            return result

        except Exception as e:
            logger.error(f"[{self.name}] Execution failed: {e}")
            import traceback

            logger.error(f"[{self.name}] Traceback: {traceback.format_exc()}")
            return self.result_envelope(
                error=self.execution_error(e, tool_name=str(tool_name or "office_tool"))
            )
