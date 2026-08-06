"""Trusted intent-to-tool authorization for tools hidden behind remote agents.

Remote agents are execution adapters: dispatching an agent is not equivalent to
authorizing every tool that the remote implementation can call.  This module
maps the platform-owned TaskGraph intent to the concrete governed resource so
the scheduler can authorize it before the remote request leaves the process.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from src.security.trusted_recipients import resolve_trusted_recipient_addresses


@dataclass(frozen=True)
class RemoteToolAuthorization:
    tool_name: str
    arguments: dict[str, Any]


# The mapping is intentionally keyed by both agent and intent.  Authorizing all
# selected tools on an agent would make an ordinary employee-profile lookup ask
# for salary approval merely because the HR agent also owns the salary tool.
_INTENT_TOOL_MAP: dict[tuple[str, str], tuple[str, ...]] = {
    ("RemoteWeatherAgent", "weather_query"): ("remote_weather_tool",),
    ("RemoteHRAssistantAgent", "employee_information_query"): ("remote_person_info_tool",),
    ("RemoteHRAssistantAgent", "salary_query"): ("remote_salary_info_tool",),
    ("RemoteUnicornSelectorAgent", "information_research"): ("remote_unicorn_db_tool",),
    ("RemoteBusinessRiskAgent", "risk_analysis"): ("remote_credit_risk_db_tool",),
    ("RemoteReportAgent", "report_generation"): ("remote_report_builder_tool",),
    ("RemoteDocumentGeneratorAgent", "document_generation"): ("remote_docx_generator_tool",),
    ("RemoteEmailDispatchAgent", "message_or_email_send"): ("remote_email_tool",),
    # This agent first resolves a contact and then sends.  Both resources must
    # be in the manifest or the remote request fails closed at the second hop.
    ("RemoteCommunicationAgent", "message_or_email_send"): (
        "remote_contact_query_tool",
        "remote_email_tool",
    ),
    ("RemoteKnowledgeAgent", "knowledge_lookup"): ("knowledge_search_tool",),
    ("RemoteOfficeAssistantAgent", "leave_record_query"): ("query_leave_record",),
    ("RemoteOfficeAssistantAgent", "leave_request"): ("save_leave_record",),
    ("RemoteMeetingManagerAgent", "meeting_arrangement"): (
        "remote_meeting_scheduling_tool",
    ),
    ("RemoteScheduleAgent", "schedule_management"): ("remote_schedule_tool",),
    ("RemoteTodoAgent", "schedule_management"): ("remote_todo_query_tool",),
}

_ADDITIONAL_AGENT_INTENTS: dict[str, tuple[str, ...]] = {
    # These tools are selected by operation mode in the resolver below.
    "RemoteOfficeAssistantAgent": ("travel_service",),
    "RemoteHRCalendarAgent": ("schedule_management",),
}


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _stable_arguments(task_profile: Mapping[str, Any], intent: str) -> dict[str, Any]:
    """Return server-recognized entities that are safe to bind to approval."""

    entities = _as_mapping(task_profile.get("entities"))
    arguments: dict[str, Any] = {}
    for key in (
        "employee_name",
        "employee_id",
        "recipient",
        "recipients",
        "people",
        "document_type",
        "date",
        "start_date",
        "end_date",
        "location",
    ):
        value = entities.get(key)
        if value not in (None, "", [], {}):
            arguments[key] = value
    # Intent is part of the action context even when entity extraction produced
    # no arguments, so two different operations cannot share one approval.
    arguments["intent"] = intent
    return arguments


def required_remote_tool_authorizations(
    *,
    agent_name: str,
    intents: Iterable[str],
    task_profile: Any,
    operation_mode: str = "read",
    include_all_tools: bool = False,
    trusted_administrator: bool = False,
) -> list[RemoteToolAuthorization]:
    """Resolve governed remote tools from trusted scheduler-owned fields."""

    # Kept as a compatibility alias for callers that already identify a
    # trusted administrator. It expands only to concrete mapped tools; it
    # never creates a wildcard or bypasses S-ABAC.
    include_all_tools = include_all_tools or bool(trusted_administrator)
    profile = _as_mapping(task_profile)
    resolved: list[RemoteToolAuthorization] = []
    seen: set[str] = set()
    requested_intents = [str(raw_intent or "").strip() for raw_intent in intents]
    if include_all_tools:
        # Expand to concrete tools owned by this registered Agent.  This is a
        # capability enumeration only; each entry is still evaluated by the
        # normal S-ABAC enforcement hook before it reaches the remote Agent.
        expanded: list[str] = []
        for candidate_agent, candidate_intent in _INTENT_TOOL_MAP:
            if candidate_agent == str(agent_name):
                expanded.append(candidate_intent)
        expanded.extend(_ADDITIONAL_AGENT_INTENTS.get(str(agent_name), ()))
        requested_intents = list(dict.fromkeys(expanded))

    for raw_intent in requested_intents:
        intent = str(raw_intent or "").strip()
        tool_names = _INTENT_TOOL_MAP.get((str(agent_name), intent), ())
        mode = str(operation_mode or "read").lower()
        if agent_name == "RemoteOfficeAssistantAgent" and intent == "travel_service":
            tool_names = (
                ("query_travel_record",)
                if mode in {"read", "query"}
                else ("save_travel_record",)
            )
        elif agent_name == "RemoteHRCalendarAgent" and intent in {
            "schedule_management",
            "meeting_arrangement",
        }:
            tool_names = (
                ("get_calendar_events_tool",)
                if mode in {"read", "query"}
                else ("create_calendar_event_tool",)
            )

        for tool_name in tool_names:
            if tool_name in seen:
                continue
            seen.add(tool_name)
            arguments = _stable_arguments(profile, intent)
            semantic_recipients = arguments.get("recipients") or arguments.get("recipient")
            if (
                agent_name == "RemoteCommunicationAgent"
                and str(semantic_recipients or "").strip()
                in {"参会人", "所有参会人", "全体参会人", "与会人员", "相关人员"}
                and arguments.get("people")
            ):
                semantic_recipients = arguments["people"]
                # Both the contact lookup and the email send must be bound to
                # the concrete participant set used by the Agent.
                arguments["recipients"] = semantic_recipients
                arguments.pop("recipient", None)
            if tool_name == "remote_email_tool":
                arguments["resolved_recipient_addresses"] = (
                    resolve_trusted_recipient_addresses(semantic_recipients)
                )
            resolved.append(
                RemoteToolAuthorization(
                    tool_name=tool_name,
                    arguments=arguments,
                )
            )
    return resolved
