from __future__ import annotations

import asyncio
from types import SimpleNamespace

from src.contracts.agent_card import AgentCard
from src.contracts.agent_contract import AgentContract, DataContractRef
from src.orchestration.contract_planning import (
    contract_closure,
    trusted_planning_catalog,
    validate_plan_candidate_closure,
)
from src.orchestrator.task_profiler import profile_task
from src.workflow import coor_task


def _contract(
    *,
    requires: list[tuple[str, str]] | None = None,
    produces: list[tuple[str, str]],
) -> AgentContract:
    return AgentContract(
        contract_version="1.0",
        requires=[
            DataContractRef(name=name, schema_ref=schema_ref)
            for name, schema_ref in (requires or [])
        ],
        produces=[
            DataContractRef(name=name, schema_ref=schema_ref)
            for name, schema_ref in produces
        ],
    )


CONTRACTS = {
    "RemoteHRAssistantAgent": _contract(
        produces=[("employee.info", "employee.info@v1")]
    ),
    "RemoteKnowledgeAgent": _contract(
        produces=[("policy.info", "policy.info@v2")]
    ),
    "RemoteOfficeAssistantAgent": _contract(
        requires=[("employee.info", "employee.info@v1")],
        produces=[("employee.leave_records", "employee.leave_records@v1")],
    ),
    "RemoteReportAgent": _contract(
        requires=[("report.sources", "report.sources@v1")],
        produces=[("report.markdown", "report.markdown@v1")],
    ),
    "RemoteEmailDispatchAgent": _contract(
        requires=[("email.dispatch.request", "email.dispatch.request@v1")],
        produces=[("email.dispatch.receipt", "email.dispatch.receipt@v1")],
    ),
}


def _cards() -> list[AgentCard]:
    cards = []
    for name, contract in CONTRACTS.items():
        action = "send" if name == "RemoteEmailDispatchAgent" else "read"
        tools = (
            ["query_leave_record"]
            if name == "RemoteOfficeAssistantAgent"
            else ["remote_email_tool"]
            if name == "RemoteEmailDispatchAgent"
            else []
        )
        cards.append(
            AgentCard(
                agent_id=name,
                name=name,
                capabilities=["test"],
                supported_actions=[action],
                planning_eligible=True,
                planning_agent_contract=contract,
                planning_tool_scopes=tools,
            )
        )
    return cards


def _agents():
    result = []
    for card in _cards():
        result.append(
            SimpleNamespace(
                user_id="share",
                agent_name=card.agent_id,
                planning_agent_contract=card.planning_agent_contract,
                agent_contract=None,
                planning_selected_tools=card.planning_tool_scopes,
                requires=[],
                produces=[],
                input_schema_refs={},
                output_schema_refs={},
            )
        )
    return result


def test_task_profile_describes_business_targets_not_agent_count() -> None:
    profile = asyncio.run(
        profile_task(
            "查询王强的工龄和年假政策，查询历史请假记录，生成报告，"
            "经确认后发送邮件给人事部门。",
            task_id="dynamic-five",
            recognition_mode="rule",
        )
    )

    assert {
        "employee_information_query",
        "knowledge_lookup",
        "leave_record_query",
        "report_generation",
        "message_or_email_send",
    } <= set(profile.intents)
    assert profile.required_business_data == [
        "employee.info",
        "policy.info",
        "employee.leave_records",
    ]
    assert profile.expected_deliverables == [
        "report.markdown",
        "email.dispatch.receipt",
    ]
    assert profile.side_effects == ["email_dispatch"]
    assert profile.operation_mode == "send"


def test_defense_simple_question_profiles_to_three_contract_targets() -> None:
    profile = asyncio.run(
        profile_task(
            "查询王强工龄，根据国家规定判断年假天数并生成报告。",
            task_id="dynamic-three",
            recognition_mode="rule",
        )
    )

    assert {
        "employee_information_query",
        "knowledge_lookup",
        "report_generation",
    } <= set(profile.intents)
    assert profile.required_business_data == ["employee.info", "policy.info"]
    assert profile.expected_deliverables == ["report.markdown"]
    assert profile.side_effects == []


def test_contract_closure_selects_dynamic_three_and_five_agents() -> None:
    cards = _cards()
    simple = contract_closure(
        {
            "required_business_data": ["employee.info", "policy.info"],
            "expected_deliverables": ["report.markdown"],
        },
        cards,
    )
    complex_result = contract_closure(
        {
            "required_business_data": [
                "employee.info",
                "policy.info",
                "employee.leave_records",
            ],
            "expected_deliverables": [
                "report.markdown",
                "email.dispatch.receipt",
            ],
        },
        cards,
    )

    assert simple.complete
    assert simple.selected_agent_ids == (
        "RemoteHRAssistantAgent",
        "RemoteKnowledgeAgent",
        "RemoteReportAgent",
    )
    assert complex_result.complete
    assert complex_result.selected_agent_ids == (
        "RemoteHRAssistantAgent",
        "RemoteKnowledgeAgent",
        "RemoteOfficeAssistantAgent",
        "RemoteReportAgent",
        "RemoteEmailDispatchAgent",
    )
    assert validate_plan_candidate_closure(
        [
            {"agent_name": agent_id}
            for agent_id in complex_result.selected_agent_ids
        ],
        complex_result,
    ) == []


def test_trusted_catalog_marks_email_side_effect_and_office_query_scope() -> None:
    catalog = {
        item["agent_name"]: item
        for item in trusted_planning_catalog(_cards(), CONTRACTS)
    }

    assert catalog["RemoteOfficeAssistantAgent"]["planning_tool_scopes"] == [
        "query_leave_record"
    ]
    assert catalog["RemoteOfficeAssistantAgent"]["operation_modes"] == ["read"]
    assert catalog["RemoteOfficeAssistantAgent"]["external_side_effect"] is False
    assert catalog["RemoteEmailDispatchAgent"]["external_side_effect"] is True


def test_data_flow_uses_planning_contracts_and_builds_fanin_and_email_request(
    monkeypatch,
) -> None:
    async def list_agents():
        return _agents()

    monkeypatch.setattr(coor_task.agent_manager.agent_registry, "list", list_agents)
    steps = [
        {
            "step_id": "employee",
            "agent_name": "RemoteHRAssistantAgent",
            "intents": ["employee_information_query"],
            "depends_on": [],
        },
        {
            "step_id": "policy",
            "agent_name": "RemoteKnowledgeAgent",
            "intents": ["knowledge_lookup"],
            "depends_on": [],
        },
        {
            "step_id": "leave",
            "agent_name": "RemoteOfficeAssistantAgent",
            "intents": ["leave_record_query"],
            "depends_on": ["employee"],
        },
        {
            "step_id": "report",
            "agent_name": "RemoteReportAgent",
            "intents": ["report_generation"],
            "depends_on": ["employee", "policy", "leave"],
        },
        {
            "step_id": "email",
            "agent_name": "RemoteEmailDispatchAgent",
            "intents": ["message_or_email_send"],
            "depends_on": ["report"],
        },
    ]

    valid, errors = asyncio.run(coor_task._validate_plan_data_flow(steps, "alice"))

    assert valid, errors
    assert steps[2]["inputs"] == [
        {
            "parameter_name": "employee.info",
            "source_step": "employee",
            "source_output": "employee.info",
        }
    ]
    report_binding = steps[3]["inputs"][0]
    assert report_binding["parameter_name"] == "report.sources"
    assert {
        (item["source_step"], item["source_output"])
        for item in report_binding["source_artifacts"]
    } == {
        ("employee", "employee.info"),
        ("policy", "policy.info"),
        ("leave", "employee.leave_records"),
    }
    email_binding = steps[4]["inputs"][0]
    assert email_binding["parameter_name"] == "email.dispatch.request"
    assert email_binding["source_artifacts"] == [
        {"source_step": "report", "source_output": "report.markdown"}
    ]
    assert email_binding["assembly"]["schema_ref"] == (
        "email.dispatch.request@v1"
    )
