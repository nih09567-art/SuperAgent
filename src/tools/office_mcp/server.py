"""FastMCP prototype for six digital-employee office scenarios.

The server intentionally keeps the business data simulated.  Existing mock
operations are delegated to ``mock_remote_tool_skill`` so the MCP facade and
the legacy HTTP facade use the same fixtures and result shapes.
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP

import mock_remote_tool_skill as legacy


OFFICE_MCP_PORT = int(os.getenv("OFFICE_MCP_PORT", "8013"))

mcp = FastMCP(
    "office-mcp",
    instructions=(
        "Mock MCP tools for personnel, calendar, course, travel, meeting, "
        "and messaging scenarios in a bank digital-employee prototype."
    ),
    host="127.0.0.1",
    port=OFFICE_MCP_PORT,
)


async def _legacy_call(tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Invoke the existing mock implementation without duplicating its logic."""

    response = await legacy.tool(
        legacy.ToolRequest(tool=tool_name, arguments=arguments),
        authorization=None,
    )
    if isinstance(response, JSONResponse):
        payload = json.loads(bytes(response.body).decode("utf-8"))
        raise RuntimeError(payload.get("error") or f"{tool_name} failed")
    if not isinstance(response, dict):
        raise RuntimeError(f"{tool_name} returned an invalid response")
    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"{tool_name} returned an invalid result")
    if str(result.get("status") or "").lower() in {"error", "failed"}:
        raise RuntimeError(result.get("error") or f"{tool_name} failed")
    return result


def _course_path() -> Path:
    configured = str(os.getenv("OFFICE_MCP_COURSE_DATA") or "").strip()
    if configured:
        return Path(configured).resolve()
    return Path(__file__).resolve().parents[3] / "assets" / "course_catalog.json"


def _load_courses() -> Dict[str, Any]:
    return json.loads(_course_path().read_text(encoding="utf-8-sig"))


def _save_courses(payload: Dict[str, Any]) -> None:
    _course_path().write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _find_course(payload: Dict[str, Any], course_id: str) -> Optional[Dict[str, Any]]:
    return next(
        (
            item
            for item in payload.get("courses", [])
            if str(item.get("course_id")) == str(course_id)
        ),
        None,
    )


def _message_templates() -> List[Dict[str, str]]:
    return [
        {
            "template_id": "TPL-COURSE",
            "name": "课程报名通知",
            "subject": "课程报名结果通知",
            "body": "您报名的课程已受理，请按时参加。",
        },
        {
            "template_id": "TPL-MEETING",
            "name": "会议通知",
            "subject": "会议安排通知",
            "body": "会议已安排，请查看时间和议程。",
        },
        {
            "template_id": "TPL-TRAVEL",
            "name": "差旅申请通知",
            "subject": "差旅申请状态更新",
            "body": "您的差旅申请状态已更新。",
        },
    ]


# ---------------------------------------------------------------------------
# Personnel query (5 tools)
# ---------------------------------------------------------------------------


@mcp.tool()
async def search_employees(
    keyword: str = "",
    department: str = "",
    job_keyword: str = "",
    limit: int = 10,
) -> Dict[str, Any]:
    """Search simulated employee profiles by keyword, department, or job."""

    arguments: Dict[str, Any] = {"limit": limit}
    if keyword:
        arguments["keyword"] = keyword
    if department:
        arguments["org_keywords"] = [department]
    if job_keyword:
        arguments["job_keywords"] = [job_keyword]
    return await _legacy_call("remote_person_info_tool", arguments)


@mcp.tool()
async def get_employee_profile(
    employee_name: str = "", employee_id: str = ""
) -> Dict[str, Any]:
    """Read one simulated employee profile by name or employee identifier."""

    keyword = employee_name or employee_id
    return await _legacy_call(
        "remote_person_info_tool", {"keyword": keyword, "limit": 1}
    )


@mcp.tool()
async def get_org_unit(
    department: str, limit: int = 20
) -> Dict[str, Any]:
    """List simulated employees belonging to an organization unit."""

    result = await _legacy_call(
        "remote_person_info_tool",
        {"org_keywords": [department], "limit": limit},
    )
    return {"status": "success", "department": department, "result": result}


@mcp.tool()
async def get_manager_chain(
    employee_name: str = "", employee_id: str = ""
) -> Dict[str, Any]:
    """Return a deterministic mock manager chain for an employee."""

    profile = await get_employee_profile(employee_name, employee_id)
    display_name = employee_name or employee_id or "目标员工"
    return {
        "status": "success",
        "employee": display_name,
        "profile": profile,
        "manager_chain": [
            {"level": 1, "name": "部门负责人", "title": "部门总经理"},
            {"level": 2, "name": "分管负责人", "title": "分行副行长"},
        ],
    }


@mcp.tool()
async def get_employee_contact(
    name: str = "", department: str = "", position: str = ""
) -> Dict[str, Any]:
    """Resolve a simulated employee contact."""

    return await _legacy_call(
        "remote_contact_query_tool",
        {"name": name, "department": department, "position": position},
    )


# ---------------------------------------------------------------------------
# Calendar management (5 tools)
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_calendar_events(start_date: str, end_date: str) -> Dict[str, Any]:
    """List simulated calendar events in an inclusive date range."""

    return await _legacy_call(
        "get_calendar_events_tool",
        {"start_date": start_date, "end_date": end_date},
    )


@mcp.tool()
async def find_free_slots(
    target_date: str, duration_minutes: int = 60
) -> Dict[str, Any]:
    """Find simple mock free slots after excluding event start times."""

    result = await list_calendar_events(target_date, target_date)
    raw_events = result.get("events") or result.get("records") or []
    busy = {
        str(item.get("start_time"))
        for item in raw_events
        if isinstance(item, dict) and item.get("start_time")
    }
    candidates = ["09:00", "10:00", "14:00", "15:00", "16:00"]
    return {
        "status": "success",
        "date": target_date,
        "duration_minutes": duration_minutes,
        "free_slots": [item for item in candidates if item not in busy],
    }


@mcp.tool()
async def create_calendar_event(
    summary: str,
    start_date: str,
    start_time: str = "",
    notes: str = "",
    category: str = "日程",
) -> Dict[str, Any]:
    """Create a simulated calendar event."""

    return await _legacy_call(
        "create_calendar_event_tool",
        {
            "summary": summary,
            "start_date": start_date,
            "start_time": start_time,
            "notes": notes,
            "category": category,
        },
    )


@mcp.tool()
async def update_calendar_event(
    event_id: str,
    summary: str = "",
    start_date: str = "",
    start_time: str = "",
    notes: str = "",
) -> Dict[str, Any]:
    """Update fields on a simulated calendar event."""

    payload = legacy._load_calendar_events()
    event = next(
        (item for item in payload.get("events", []) if item.get("id") == event_id),
        None,
    )
    if event is None:
        return {"status": "not_found", "event_id": event_id}
    for key, value in {
        "summary": summary,
        "start_date": start_date,
        "start_time": start_time,
        "notes": notes,
    }.items():
        if value:
            event[key] = value
    legacy._save_calendar_events(payload)
    return {"status": "success", "event": event}


@mcp.tool()
async def cancel_calendar_event(event_id: str) -> Dict[str, Any]:
    """Cancel a simulated calendar event by marking its status."""

    payload = legacy._load_calendar_events()
    event = next(
        (item for item in payload.get("events", []) if item.get("id") == event_id),
        None,
    )
    if event is None:
        return {"status": "not_found", "event_id": event_id}
    event["status"] = "cancelled"
    legacy._save_calendar_events(payload)
    return {"status": "success", "event": event}


# ---------------------------------------------------------------------------
# Course retrieval (5 tools)
# ---------------------------------------------------------------------------


@mcp.tool()
async def search_courses(
    query: str = "", category: str = "", limit: int = 10
) -> Dict[str, Any]:
    """Search the simulated internal course catalog."""

    payload = _load_courses()
    query_lower = query.lower().strip()
    category_lower = category.lower().strip()
    topic_terms = [
        term
        for term in ("大模型", "智能体", "数字化", "差旅", "合规", "人工智能", "mcp")
        if term in query_lower
    ]
    generic_course_query = any(
        term in query_lower for term in ("课程", "培训", "course", "training")
    )
    courses = []
    for item in payload.get("courses", []):
        haystack = " ".join(
            str(item.get(key) or "")
            for key in ("course_id", "title", "category", "provider", "description")
        ).lower()
        if query_lower and query_lower not in haystack:
            if topic_terms and not any(term in haystack for term in topic_terms):
                continue
            if not topic_terms and not generic_course_query:
                continue
        if category_lower and category_lower not in str(item.get("category", "")).lower():
            continue
        courses.append(item)
    courses = courses[: max(1, limit)]
    return {
        "status": "success",
        "query": query,
        "answer": f"找到{len(courses)}门匹配课程",
        "knowledge_items_count": len(courses),
        "policy_scope": "internal_training",
        "matched_items": courses,
        "courses": courses,
    }


@mcp.tool()
async def get_course_detail(course_id: str) -> Dict[str, Any]:
    """Read details for one simulated course."""

    course = _find_course(_load_courses(), course_id)
    if course is None:
        return {"status": "not_found", "course_id": course_id}
    return {"status": "success", "course": course}


@mcp.tool()
async def list_course_sessions(
    course_id: str, start_date: str = ""
) -> Dict[str, Any]:
    """List available sessions for a simulated course."""

    course = _find_course(_load_courses(), course_id)
    if course is None:
        return {"status": "not_found", "course_id": course_id}
    sessions = [
        item
        for item in course.get("sessions", [])
        if not start_date or str(item.get("date", "")) >= start_date
    ]
    return {"status": "success", "course_id": course_id, "sessions": sessions}


@mcp.tool()
async def enroll_course(
    employee_id: str,
    employee_name: str,
    course_id: str,
    session_id: str,
) -> Dict[str, Any]:
    """Create a simulated course enrollment."""

    payload = _load_courses()
    course = _find_course(payload, course_id)
    if course is None:
        return {"status": "not_found", "course_id": course_id}
    session = next(
        (
            item
            for item in course.get("sessions", [])
            if item.get("session_id") == session_id
        ),
        None,
    )
    if session is None:
        return {"status": "not_found", "session_id": session_id}
    enrollment = {
        "employee_id": employee_id,
        "employee_name": employee_name,
        "course_id": course_id,
        "session_id": session_id,
        "status": "已报名",
        "progress": 0,
    }
    existing = payload.setdefault("enrollments", [])
    duplicate = next(
        (
            item
            for item in existing
            if item.get("employee_id") == employee_id
            and item.get("session_id") == session_id
        ),
        None,
    )
    if duplicate is None:
        existing.append(enrollment)
        if int(session.get("available", 0)) > 0:
            session["available"] = int(session["available"]) - 1
        _save_courses(payload)
    else:
        enrollment = duplicate
    return {"status": "success", "enrollment": enrollment}


@mcp.tool()
async def get_learning_record(
    employee_id: str = "", employee_name: str = ""
) -> Dict[str, Any]:
    """Read simulated course enrollment and progress records."""

    payload = _load_courses()
    records = [
        item
        for item in payload.get("enrollments", [])
        if (not employee_id or item.get("employee_id") == employee_id)
        and (not employee_name or item.get("employee_name") == employee_name)
    ]
    return {"status": "success", "count": len(records), "records": records}


# ---------------------------------------------------------------------------
# Employee travel (5 tools)
# ---------------------------------------------------------------------------


@mcp.tool()
async def search_travel_policy(destination: str = "") -> Dict[str, Any]:
    """Return a compact simulated employee travel policy."""

    return {
        "status": "success",
        "destination": destination,
        "policy": {
            "transport": "优先选择经济舱或二等座",
            "hotel_limit_cny": 600,
            "meal_allowance_cny_per_day": 120,
            "approval": "出发前提交差旅申请",
        },
    }


@mcp.tool()
async def estimate_trip_cost(
    destination: str,
    days: int,
    transport_cost: float = 800,
    hotel_cost_per_day: float = 500,
) -> Dict[str, Any]:
    """Estimate a simulated trip cost from simple configurable rates."""

    safe_days = max(1, days)
    total = transport_cost + safe_days * hotel_cost_per_day + safe_days * 120
    return {
        "status": "success",
        "destination": destination,
        "days": safe_days,
        "currency": "CNY",
        "estimated_total": round(total, 2),
        "breakdown": {
            "transport": transport_cost,
            "hotel": safe_days * hotel_cost_per_day,
            "meal_allowance": safe_days * 120,
        },
    }


@mcp.tool()
async def create_travel_request(
    employee_id: str,
    employee_name: str,
    destination: str,
    start_date: str,
    end_date: str,
    purpose: str = "",
) -> Dict[str, Any]:
    """Create a simulated employee travel request."""

    return await _legacy_call(
        "save_travel_record",
        {
            "employee_id": employee_id,
            "employee_name": employee_name,
            "travel_data": {
                "destination": destination,
                "start_date": start_date,
                "end_date": end_date,
                "purpose": purpose,
            },
        },
    )


@mcp.tool()
async def get_travel_request(
    employee_id: str = "",
    employee_name: str = "",
    record_id: str = "",
) -> Dict[str, Any]:
    """Read simulated employee travel requests."""

    records = [
        item
        for item in legacy._load_travel_applications()
        if (not employee_id or item.get("employee_id") == employee_id)
        and (not employee_name or item.get("employee_name") == employee_name)
        and (not record_id or item.get("record_id") == record_id)
    ]
    return {"status": "success", "count": len(records), "records": records}


@mcp.tool()
async def cancel_travel_request(record_id: str) -> Dict[str, Any]:
    """Cancel a simulated travel request."""

    records = legacy._load_travel_applications()
    record = next((item for item in records if item.get("record_id") == record_id), None)
    if record is None:
        return {"status": "not_found", "record_id": record_id}
    record["status"] = "已取消"
    legacy._save_travel_applications(records)
    return {"status": "success", "record": record}


# ---------------------------------------------------------------------------
# Meeting assistant (5 tools)
# ---------------------------------------------------------------------------


@mcp.tool()
async def find_meeting_slots(
    target_date: str, duration_minutes: int = 60
) -> Dict[str, Any]:
    """Find candidate meeting slots using the simulated calendar."""

    return await find_free_slots(target_date, duration_minutes)


@mcp.tool()
async def create_meeting(
    title: str,
    meeting_date: str,
    meeting_time: str,
    participants: List[str],
    agenda: str = "",
) -> Dict[str, Any]:
    """Create a simulated meeting."""

    return await _legacy_call(
        "remote_meeting_scheduling_tool",
        {
            "action": "create",
            "meeting": {
                "title": title,
                "date": meeting_date,
                "time": meeting_time,
                "participants": participants,
                "agenda": agenda,
            },
        },
    )


@mcp.tool()
async def update_meeting(
    meeting_id: str,
    title: str = "",
    meeting_date: str = "",
    meeting_time: str = "",
    agenda: str = "",
) -> Dict[str, Any]:
    """Update a simulated meeting."""

    meetings = legacy._load_meetings()
    meeting = next((item for item in meetings if item.get("id") == meeting_id), None)
    if meeting is None:
        return {"status": "not_found", "meeting_id": meeting_id}
    for key, value in {
        "title": title,
        "date": meeting_date,
        "time": meeting_time,
        "agenda": agenda,
    }.items():
        if value:
            meeting[key] = value
    legacy._save_meetings(meetings)
    return {"status": "success", "meeting": meeting}


@mcp.tool()
async def cancel_meeting(meeting_id: str) -> Dict[str, Any]:
    """Cancel a simulated meeting."""

    meetings = legacy._load_meetings()
    meeting = next((item for item in meetings if item.get("id") == meeting_id), None)
    if meeting is None:
        return {"status": "not_found", "meeting_id": meeting_id}
    meeting["status"] = "cancelled"
    legacy._save_meetings(meetings)
    return {"status": "success", "meeting": meeting}


@mcp.tool()
async def get_meeting_minutes(meeting_id: str) -> Dict[str, Any]:
    """Return deterministic mock minutes for a simulated meeting."""

    meetings = legacy._load_meetings()
    meeting = next((item for item in meetings if item.get("id") == meeting_id), None)
    if meeting is None:
        return {"status": "not_found", "meeting_id": meeting_id}
    return {
        "status": "success",
        "meeting_id": meeting_id,
        "minutes": {
            "title": meeting.get("title"),
            "summary": "原型系统自动生成的模拟会议纪要。",
            "decisions": ["按会议议程继续推进"],
            "action_items": ["会后同步相关材料"],
        },
    }


# ---------------------------------------------------------------------------
# Messaging (5 tools)
# ---------------------------------------------------------------------------


@mcp.tool()
async def resolve_contacts(
    name: str = "", department: str = "", position: str = ""
) -> Dict[str, Any]:
    """Resolve message recipients from the simulated contact directory."""

    return await get_employee_contact(name, department, position)


@mcp.tool()
async def list_message_templates(template_id: str = "") -> Dict[str, Any]:
    """List reusable simulated message templates."""

    templates = [
        item
        for item in _message_templates()
        if not template_id or item["template_id"] == template_id
    ]
    return {"status": "success", "count": len(templates), "templates": templates}


@mcp.tool()
async def send_email(to: str, subject: str, body: str) -> Dict[str, Any]:
    """Send a simulated email and return a delivery receipt."""

    return await _legacy_call(
        "remote_email_tool", {"to": to, "subject": subject, "body": body}
    )


@mcp.tool()
async def send_message(
    recipient: str,
    content: str,
    channel: str = "internal",
    subject: str = "数字员工消息",
) -> Dict[str, Any]:
    """Send a simulated internal message or email-like notification."""

    if "@" in recipient:
        result = await send_email(recipient, subject, content)
        return {"status": "success", "channel": channel, "receipt": result}
    message_id = f"MSG-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    return {
        "status": "success",
        "channel": channel,
        "message_id": message_id,
        "recipient": recipient,
        "delivery_status": "delivered",
    }


@mcp.tool()
async def get_delivery_status(message_id: str) -> Dict[str, Any]:
    """Return a deterministic mock delivery status."""

    return {
        "status": "success",
        "message_id": message_id,
        "delivery_status": "delivered",
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }


if __name__ == "__main__":
    mcp.run(transport="sse")
