import asyncio
import json
from pathlib import Path

import pytest

import mock_remote_tool_skill as legacy
from remote_agents.base_agent import (
    BaseRemoteAgent,
    bind_authorized_remote_tools,
    reset_authorized_remote_tools,
)
from remote_agents.mcp_tool_client import resolve_office_mcp_call
from src.tools.office_mcp.server import mcp


EXPECTED_TOOLS = {
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


class _DemoAgent(BaseRemoteAgent):
    async def execute(self, tools, messages, context, parameter_extractor):
        return {}


def _write(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture
def isolated_office_state(tmp_path, monkeypatch):
    calendar = tmp_path / "calendar.json"
    travel = tmp_path / "travel.json"
    meetings = tmp_path / "meetings.json"
    email = tmp_path / "email.json"
    courses = tmp_path / "courses.json"
    _write(
        calendar,
        {
            "events": [
                {
                    "id": "event-001",
                    "summary": "部门例会",
                    "start_date": "2026-08-18",
                    "start_time": "09:00",
                }
            ]
        },
    )
    _write(
        travel,
        [
            {
                "record_id": "TRAVEL-001",
                "employee_id": "86000103",
                "employee_name": "王强",
                "destination": "深圳",
                "status": "待审批",
            }
        ],
    )
    _write(
        meetings,
        [
            {
                "id": "meeting-0001",
                "title": "项目例会",
                "date": "2026-08-18",
                "time": "10:00",
                "participants": ["王强"],
                "status": "scheduled",
            }
        ],
    )
    _write(email, {"emails": []})
    source_course = Path(__file__).resolve().parents[1] / "assets" / "course_catalog.json"
    courses.write_text(source_course.read_text(encoding="utf-8"), encoding="utf-8")

    monkeypatch.setattr(legacy, "_calendar_path", lambda: calendar)
    monkeypatch.setattr(legacy, "_travel_applications_path", lambda: travel)
    monkeypatch.setattr(legacy, "_meeting_path", lambda: meetings)
    monkeypatch.setenv("MOCK_EMAIL_LOG_PATH", str(email))
    monkeypatch.setenv("OFFICE_MCP_COURSE_DATA", str(courses))
    legacy._CALENDAR_CACHE = None
    legacy._TRAVEL_CACHE = None
    legacy._MEETING_CACHE = None
    legacy._EMAIL_CACHE = None
    yield
    legacy._CALENDAR_CACHE = None
    legacy._TRAVEL_CACHE = None
    legacy._MEETING_CACHE = None
    legacy._EMAIL_CACHE = None


def test_office_mcp_registers_exactly_thirty_tools():
    names = {tool.name for tool in mcp._tool_manager.list_tools()}
    assert names == EXPECTED_TOOLS
    assert len(names) == 30


def test_legacy_tool_mapping_covers_six_demo_scenarios():
    cases = {
        "remote_person_info_tool": {"keyword": "王强"},
        "get_calendar_events_tool": {
            "start_date": "2026-08-18",
            "end_date": "2026-08-18",
        },
        "knowledge_search_tool": {"query": "查询大模型培训课程"},
        "query_travel_record": {"employee_name": "王强"},
        "remote_meeting_scheduling_tool": {
            "action": "create",
            "meeting": {"date": "2026-08-18", "time": "10:00"},
        },
        "remote_email_tool": {
            "to": "hr@example.test",
            "subject": "通知",
            "body": "测试",
        },
    }
    resolved = {
        name: resolve_office_mcp_call(name, arguments)[0]
        for name, arguments in cases.items()
    }
    assert resolved == {
        "remote_person_info_tool": "search_employees",
        "get_calendar_events_tool": "list_calendar_events",
        "knowledge_search_tool": "search_courses",
        "query_travel_record": "get_travel_request",
        "remote_meeting_scheduling_tool": "create_meeting",
        "remote_email_tool": "send_email",
    }


def test_base_remote_agent_uses_mcp_after_existing_authorization(monkeypatch):
    captured = {}

    async def fake_call(tool_name, arguments, timeout=None):
        captured.update(tool_name=tool_name, arguments=arguments, timeout=timeout)
        return {"status": "success", "courses": []}

    monkeypatch.setenv("REMOTE_TOOL_TRANSPORT", "hybrid")
    monkeypatch.setattr(
        "remote_agents.mcp_tool_client.call_office_mcp_tool", fake_call
    )
    token = bind_authorized_remote_tools(
        {
            "authorized_remote_tools": [
                {"tool_name": "knowledge_search_tool", "arguments": {}}
            ]
        }
    )
    try:
        result = asyncio.run(
            _DemoAgent("demo", "demo").call_tool(
                "knowledge_search_tool", {"query": "查询大模型培训课程"}, timeout=9
            )
        )
    finally:
        reset_authorized_remote_tools(token)
    assert result["status"] == "success"
    assert captured == {
        "tool_name": "search_courses",
        "arguments": {"query": "查询大模型培训课程", "limit": 10},
        "timeout": 9,
    }


def test_all_thirty_tools_are_callable(isolated_office_state):
    calls = [
        ("search_employees", {"keyword": "王强"}),
        ("get_employee_profile", {"employee_name": "王强"}),
        ("get_org_unit", {"department": "人力资源"}),
        ("get_manager_chain", {"employee_name": "王强"}),
        ("get_employee_contact", {"name": "张三"}),
        ("list_calendar_events", {"start_date": "2026-08-18", "end_date": "2026-08-18"}),
        ("find_free_slots", {"target_date": "2026-08-18"}),
        ("create_calendar_event", {"summary": "培训", "start_date": "2026-08-19"}),
        ("update_calendar_event", {"event_id": "event-001", "summary": "更新例会"}),
        ("cancel_calendar_event", {"event_id": "event-001"}),
        ("search_courses", {"query": "大模型"}),
        ("get_course_detail", {"course_id": "COURSE-002"}),
        ("list_course_sessions", {"course_id": "COURSE-002"}),
        ("enroll_course", {"employee_id": "86000103", "employee_name": "王强", "course_id": "COURSE-002", "session_id": "SESSION-002"}),
        ("get_learning_record", {"employee_id": "86000103"}),
        ("search_travel_policy", {"destination": "深圳"}),
        ("estimate_trip_cost", {"destination": "深圳", "days": 2}),
        ("create_travel_request", {"employee_id": "86000103", "employee_name": "王强", "destination": "深圳", "start_date": "2026-08-20", "end_date": "2026-08-21"}),
        ("get_travel_request", {"employee_id": "86000103"}),
        ("cancel_travel_request", {"record_id": "TRAVEL-001"}),
        ("find_meeting_slots", {"target_date": "2026-08-18"}),
        ("create_meeting", {"title": "项目会", "meeting_date": "2026-08-18", "meeting_time": "14:00", "participants": ["王强"]}),
        ("update_meeting", {"meeting_id": "meeting-0001", "title": "更新项目会"}),
        ("cancel_meeting", {"meeting_id": "meeting-0001"}),
        ("get_meeting_minutes", {"meeting_id": "meeting-0001"}),
        ("resolve_contacts", {"name": "张三"}),
        ("list_message_templates", {}),
        ("send_email", {"to": "hr@example.test", "subject": "通知", "body": "原型测试"}),
        ("send_message", {"recipient": "王强", "content": "原型测试"}),
        ("get_delivery_status", {"message_id": "MSG-001"}),
    ]
    assert len(calls) == 30
    for name, arguments in calls:
        result = asyncio.run(mcp._tool_manager.call_tool(name, arguments))
        assert isinstance(result, dict), name
        assert result.get("status") == "success", (name, result)
