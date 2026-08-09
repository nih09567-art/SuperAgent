"""Run six deterministic Remote Agent -> MCP demo scenarios.

Start ``mock_office_mcp_server.py`` first.  This script deliberately replaces
LLM parameter extraction with fixed demo inputs so the protocol and Agent path
can be reproduced without model credentials.
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict

from remote_agents.base_agent import (
    bind_authorized_remote_tools,
    reset_authorized_remote_tools,
)
from remote_agents.factory import AgentFactory


class DemoParameterExtractor:
    def __init__(self, arguments: Dict[str, Any]):
        self.arguments = arguments

    async def extract(self, *_args, **_kwargs) -> Dict[str, Any]:
        return dict(self.arguments)


def _tool_definition(agent_name: str, tool_name: str) -> Dict[str, Any]:
    registry_path = Path(__file__).resolve().parents[1] / "mock_remote_registry.json"
    resources = json.loads(registry_path.read_text(encoding="utf-8-sig"))["resources"]
    agent = next(
        item
        for item in resources
        if item.get("type") == "agent" and item.get("name") == agent_name
    )
    return next(
        item
        for item in agent.get("metadata", {}).get("selected_tools", [])
        if item.get("name") == tool_name
    )


SCENARIOS = [
    {
        "name": "人员查询",
        "agent": "RemoteHRAssistantAgent",
        "tool": "remote_person_info_tool",
        "query": "查询员工王强的基础信息",
        "arguments": {"keyword": "王强", "limit": 3},
        "authorized": {"employee_name": "王强"},
    },
    {
        "name": "日程管理",
        "agent": "RemoteHRCalendarAgent",
        "tool": "get_calendar_events_tool",
        "query": "查询2026年8月18日的日程",
        "arguments": {"start_date": "2026-08-18", "end_date": "2026-08-18"},
        "authorized": {"start_date": "2026-08-18", "end_date": "2026-08-18"},
    },
    {
        "name": "课程检索",
        "agent": "RemoteKnowledgeAgent",
        "tool": "knowledge_search_tool",
        "query": "查询大模型与智能体培训课程",
        "arguments": {"query": "查询大模型与智能体培训课程"},
        "authorized": {},
    },
    {
        "name": "员工差旅",
        "agent": "RemoteOfficeAssistantAgent",
        "tool": "query_travel_record",
        "query": "查询员工王强的差旅申请",
        "arguments": {"employee_id": "86000103", "employee_name": "王强", "filters": {}},
        "authorized": {"employee_id": "86000103", "employee_name": "王强"},
    },
    {
        "name": "会议助手",
        "agent": "RemoteMeetingManagerAgent",
        "tool": "remote_meeting_scheduling_tool",
        "query": "查询2026年8月18日可安排会议的时间",
        "arguments": {
            "action": "query",
            "date": "2026-08-18",
        },
        "authorized": {"date": "2026-08-18"},
    },
    {
        "name": "消息发送",
        "agent": "RemoteEmailDispatchAgent",
        "tool": "remote_email_tool",
        "query": "把原型演示通知发送给人事部门",
        "arguments": {
            "to": "hr@example.test",
            "subject": "数字员工MCP原型演示",
            "body": "原型演示已准备完成。",
        },
        "authorized": {"resolved_recipient_addresses": ["hr@example.test"]},
    },
]


async def _run_scenario(scenario: Dict[str, Any]) -> Dict[str, Any]:
    agent = AgentFactory.get_agent(scenario["agent"])
    token = bind_authorized_remote_tools(
        {
            "authorized_remote_tools": [
                {
                    "tool_name": scenario["tool"],
                    "arguments": scenario["authorized"],
                }
            ]
        }
    )
    try:
        return await agent.execute(
            tools=[_tool_definition(scenario["agent"], scenario["tool"])],
            messages=[{"role": "user", "content": scenario["query"]}],
            context={"demo": True},
            parameter_extractor=DemoParameterExtractor(scenario["arguments"]),
        )
    finally:
        reset_authorized_remote_tools(token)


async def main() -> None:
    os.environ["REMOTE_TOOL_TRANSPORT"] = "hybrid"
    results = []
    for scenario in SCENARIOS:
        result = await _run_scenario(scenario)
        status = str(result.get("status") or "").lower()
        success = status in {"success", "succeeded"}
        results.append(
            {
                "scenario": scenario["name"],
                "agent": scenario["agent"],
                "logical_tool": scenario["tool"],
                "success": success,
                "result": result,
            }
        )
        print(f"[{scenario['name']}] {scenario['agent']} success={success}")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    if not all(item["success"] for item in results):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
