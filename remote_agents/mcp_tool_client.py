"""Shared MCP client and legacy-to-office tool adapters for remote agents."""

import asyncio
import json
import os
from typing import Any, Dict, Optional, Tuple

from langchain_mcp_adapters.client import MultiServerMCPClient

from src.manager.mcp import mcp_client_config


_COURSE_MARKERS = ("课程", "培训", "学习记录", "报名", "course", "training")
OFFICE_MCP_TOOL_NAMES = {
    "search_employees", "get_employee_profile", "get_org_unit", "get_manager_chain",
    "get_employee_contact", "list_calendar_events", "find_free_slots",
    "create_calendar_event", "update_calendar_event", "cancel_calendar_event",
    "search_courses", "get_course_detail", "list_course_sessions", "enroll_course",
    "get_learning_record", "search_travel_policy", "estimate_trip_cost",
    "create_travel_request", "get_travel_request", "cancel_travel_request",
    "find_meeting_slots", "create_meeting", "update_meeting", "cancel_meeting",
    "get_meeting_minutes", "resolve_contacts", "list_message_templates",
    "send_email", "send_message", "get_delivery_status",
}


def _first_text(value: Any) -> str:
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value or "")


def resolve_office_mcp_call(
    tool_name: str, arguments: Dict[str, Any]
) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Translate existing remote tool contracts into the office MCP facade."""

    args = dict(arguments or {})
    if tool_name in OFFICE_MCP_TOOL_NAMES:
        return tool_name, args
    if tool_name == "remote_person_info_tool":
        return (
            "search_employees",
            {
                "keyword": str(args.get("keyword") or ""),
                "department": _first_text(args.get("org_keywords")),
                "job_keyword": _first_text(args.get("job_keywords")),
                "limit": int(args.get("limit") or 10),
            },
        )
    if tool_name == "get_calendar_events_tool":
        return (
            "list_calendar_events",
            {
                "start_date": str(args.get("start_date") or ""),
                "end_date": str(args.get("end_date") or ""),
            },
        )
    if tool_name == "create_calendar_event_tool":
        return (
            "create_calendar_event",
            {
                key: args.get(key, "")
                for key in ("summary", "start_date", "start_time", "notes", "category")
            },
        )
    if tool_name == "remote_schedule_tool":
        schedule = args.get("schedule") if isinstance(args.get("schedule"), dict) else {}
        if str(args.get("action") or "").lower() == "create":
            return (
                "create_calendar_event",
                {
                    "summary": str(schedule.get("summary") or schedule.get("title") or "日程"),
                    "start_date": str(schedule.get("date") or args.get("start_date") or ""),
                    "start_time": str(schedule.get("time") or ""),
                    "notes": str(schedule.get("notes") or ""),
                    "category": str(schedule.get("category") or "日程"),
                },
            )
        return (
            "list_calendar_events",
            {
                "start_date": str(args.get("start_date") or ""),
                "end_date": str(args.get("end_date") or args.get("start_date") or ""),
            },
        )
    if tool_name == "knowledge_search_tool":
        query = str(args.get("query") or "")
        if any(marker.lower() in query.lower() for marker in _COURSE_MARKERS):
            return "search_courses", {"query": query, "limit": int(args.get("limit") or 10)}
        return None
    if tool_name == "save_travel_record":
        travel = args.get("travel_data") if isinstance(args.get("travel_data"), dict) else {}
        return (
            "create_travel_request",
            {
                "employee_id": str(args.get("employee_id") or ""),
                "employee_name": str(args.get("employee_name") or ""),
                "destination": str(travel.get("destination") or ""),
                "start_date": str(travel.get("start_date") or ""),
                "end_date": str(travel.get("end_date") or ""),
                "purpose": str(travel.get("purpose") or ""),
            },
        )
    if tool_name == "query_travel_record":
        filters = args.get("filters") if isinstance(args.get("filters"), dict) else {}
        return (
            "get_travel_request",
            {
                "employee_id": str(args.get("employee_id") or ""),
                "employee_name": str(args.get("employee_name") or ""),
                "record_id": str(filters.get("record_id") or ""),
            },
        )
    if tool_name == "remote_meeting_scheduling_tool":
        meeting = args.get("meeting") if isinstance(args.get("meeting"), dict) else {}
        if str(args.get("action") or "create").lower() == "create":
            participants = meeting.get("participants") or []
            if not isinstance(participants, list):
                participants = [str(participants)]
            return (
                "create_meeting",
                {
                    "title": str(meeting.get("title") or "视频会议"),
                    "meeting_date": str(meeting.get("date") or ""),
                    "meeting_time": str(meeting.get("time") or ""),
                    "participants": participants,
                    "agenda": str(meeting.get("agenda") or ""),
                },
            )
        return (
            "find_meeting_slots",
            {"target_date": str(args.get("date") or meeting.get("date") or "")},
        )
    if tool_name == "remote_contact_query_tool":
        return (
            "resolve_contacts",
            {
                "name": str(args.get("name") or _first_text(args.get("names"))),
                "department": str(args.get("department") or ""),
                "position": str(args.get("position") or ""),
            },
        )
    if tool_name == "remote_email_tool":
        return (
            "send_email",
            {
                "to": str(args.get("to") or ""),
                "subject": str(args.get("subject") or ""),
                "body": str(args.get("body") or ""),
            },
        )
    return None


def _normalize_result(tool_name: str, value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        result = value
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"MCP tool {tool_name} returned non-JSON text") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError(f"MCP tool {tool_name} returned non-object JSON")
        result = parsed
    else:
        raise RuntimeError(f"MCP tool {tool_name} returned an invalid result")
    if str(result.get("status") or "").lower() in {"error", "failed"}:
        raise RuntimeError(result.get("error") or f"MCP tool {tool_name} failed")
    return result


class OfficeMCPToolClient:
    """Process-level client whose LangChain tool objects create MCP sessions per call."""

    def __init__(self) -> None:
        self._tools: Dict[str, Any] = {}
        self._load_lock = asyncio.Lock()

    async def _ensure_tools(self) -> Dict[str, Any]:
        if self._tools:
            return self._tools
        async with self._load_lock:
            if self._tools:
                return self._tools
            server_name = str(os.getenv("OFFICE_MCP_SERVER_NAME") or "office-mcp")
            config = mcp_client_config()
            connection = config.get(server_name)
            if not isinstance(connection, dict):
                raise RuntimeError(f"MCP server is not configured: {server_name}")
            client = MultiServerMCPClient({server_name: connection})
            loaded = await client.get_tools(server_name=server_name)
            self._tools = {
                str(getattr(tool, "name", "")): tool
                for tool in loaded
                if str(getattr(tool, "name", ""))
            }
            return self._tools

    async def call(
        self, tool_name: str, arguments: Dict[str, Any], timeout: Optional[int] = None
    ) -> Dict[str, Any]:
        tools = await self._ensure_tools()
        tool = tools.get(tool_name)
        if tool is None:
            raise RuntimeError(f"MCP tool not found: {tool_name}")
        effective_timeout = timeout or int(os.getenv("OFFICE_MCP_TIMEOUT", "60"))
        value = await asyncio.wait_for(tool.ainvoke(arguments), timeout=effective_timeout)
        return _normalize_result(tool_name, value)


_SHARED_CLIENT = OfficeMCPToolClient()


async def call_office_mcp_tool(
    tool_name: str, arguments: Dict[str, Any], timeout: Optional[int] = None
) -> Dict[str, Any]:
    return await _SHARED_CLIENT.call(tool_name, arguments, timeout)
