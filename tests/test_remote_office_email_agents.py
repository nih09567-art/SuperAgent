from __future__ import annotations

import asyncio
import json
from typing import Any

from remote_agents.email_dispatch_agent import RemoteEmailDispatchAgent
from remote_agents.office_assistant_agent import RemoteOfficeAssistantAgent
from src.contracts.agent_contract import AgentContract, DataContractRef
from src.interface.artifact import Artifact
from src.interface.task_graph import TaskStep
from src.orchestration.scheduler import TaskScheduler


class FakeExtractor:
    async def extract(
        self,
        *,
        agent_name: str,
        agent_prompt: str,
        tool: dict[str, Any],
        messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if tool.get("name") == "query_leave_record":
            return {"employee_id": "E001", "employee_name": "李娜"}
        return {}


class EmptyOfficeExtractor:
    async def extract(self, **_: Any) -> dict[str, Any]:
        return {}


def _messages(payload: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {
            "role": "user",
            "content": "EXECUTION_CONTEXT\n" + json.dumps(payload, ensure_ascii=False),
        }
    ]


def test_office_query_returns_governed_leave_record_output(monkeypatch) -> None:
    agent = RemoteOfficeAssistantAgent()
    assert agent.contract is not None
    assert agent.contract.output_schema_refs == {
        "employee.leave_records": "employee.leave_records@v1"
    }

    async def fake_call_tool(
        *, tool_name: str, arguments: dict[str, Any], **_: Any
    ) -> dict[str, Any]:
        assert tool_name == "query_leave_record"
        assert arguments["employee_id"] == "E001"
        return {
            "status": "success",
            "count": 1,
            "records": [
                {
                    "record_id": "L001",
                    "employee_id": "E001",
                    "employee_name": "李娜",
                    "leave_type": "年假",
                    "start_date": "2026-08-01",
                    "end_date": "2026-08-03",
                    "status": "已审批",
                }
            ],
        }

    monkeypatch.setattr(agent, "call_tool", fake_call_tool)
    result = asyncio.run(
        agent.execute(
            tools=[{"name": "query_leave_record"}],
            messages=_messages({"step": {"step_id": "leave"}}),
            context={},
            parameter_extractor=FakeExtractor(),
        )
    )

    payload = result["outputs"]["employee.leave_records"]
    assert result["status"] == "success"
    assert payload["employee_id"] == "E001"
    assert payload["matched_count"] == 1
    assert payload["records"][0]["days"] == 3
    assert payload["records"][0]["approval_status"] == "已审批"
    assert "employee_name" not in payload["records"][0]


def test_office_query_binds_employee_from_resolved_artifact_when_extractor_is_empty(
    monkeypatch,
) -> None:
    agent = RemoteOfficeAssistantAgent()
    captured: dict[str, Any] = {}

    async def fake_call_tool(
        *, tool_name: str, arguments: dict[str, Any], **_: Any
    ) -> dict[str, Any]:
        captured.update(arguments)
        assert tool_name == "query_leave_record"
        return {"status": "success", "records": []}

    monkeypatch.setattr(agent, "call_tool", fake_call_tool)
    result = asyncio.run(
        agent.execute(
            tools=[{"name": "query_leave_record"}],
            messages=_messages(
                {
                    "task_profile": {"entities": {"employee_name": "王强"}},
                    "resolved_inputs": {
                        "employee.info": {
                            "records": [{"adtEmpeNm": "王强", "pcsTrdYrlmt": 20}]
                        }
                    },
                }
            ),
            context={},
            parameter_extractor=EmptyOfficeExtractor(),
        )
    )

    assert result["status"] == "success"
    assert captured == {"employee_name": "王强"}


def test_office_query_prefers_trusted_employee_and_removes_unauthorized_id(
    monkeypatch,
) -> None:
    agent = RemoteOfficeAssistantAgent()
    captured: dict[str, Any] = {}

    async def fake_call_tool(
        *, tool_name: str, arguments: dict[str, Any], **_: Any
    ) -> dict[str, Any]:
        captured.update(arguments)
        return {"status": "success", "records": []}

    class ConflictingExtractor:
        async def extract(self, **_: Any) -> dict[str, Any]:
            return {"employee_name": "其他员工", "employee_id": "forged-id"}

    monkeypatch.setattr(agent, "call_tool", fake_call_tool)
    result = asyncio.run(
        agent.execute(
            tools=[{"name": "query_leave_record"}],
            messages=_messages(
                {
                    "resolved_inputs": {
                        "employee.info": {"records": [{"adtEmpeNm": "王强"}]}
                    }
                }
            ),
            context={},
            parameter_extractor=ConflictingExtractor(),
        )
    )

    assert result["status"] == "success"
    assert captured == {"employee_name": "王强"}


def test_office_query_replaces_empty_employee_id_with_resolved_identity(
    monkeypatch,
) -> None:
    agent = RemoteOfficeAssistantAgent()
    captured: dict[str, Any] = {}

    async def fake_call_tool(
        *, tool_name: str, arguments: dict[str, Any], **_: Any
    ) -> dict[str, Any]:
        captured.update(arguments)
        return {"status": "success", "records": []}

    class EmptyIdExtractor:
        async def extract(self, **_: Any) -> dict[str, Any]:
            return {"employee_id": ""}

    monkeypatch.setattr(agent, "call_tool", fake_call_tool)
    result = asyncio.run(
        agent.execute(
            tools=[{"name": "query_leave_record"}],
            messages=_messages(
                {
                    "resolved_inputs": {
                        "employee.info": {
                            "records": [
                                {
                                    "employee_id": "E001",
                                    "employee_name": "李娜",
                                }
                            ]
                        }
                    }
                }
            ),
            context={},
            parameter_extractor=EmptyIdExtractor(),
        )
    )

    assert result["status"] == "success"
    assert captured == {"employee_id": "E001", "employee_name": "李娜"}


def test_email_dispatch_returns_typed_receipt_from_assembled_request(
    monkeypatch,
) -> None:
    agent = RemoteEmailDispatchAgent()
    assert agent.contract is not None
    assert agent.contract.output_schema_refs == {
        "email.dispatch.receipt": "email.dispatch.receipt@v1"
    }
    captured: dict[str, Any] = {}

    async def fake_call_tool(
        *, tool_name: str, arguments: dict[str, Any], **_: Any
    ) -> dict[str, Any]:
        captured.update(arguments)
        return {"status": "success", "message_id": "mail-42"}

    monkeypatch.setattr(agent, "call_tool", fake_call_tool)
    result = asyncio.run(
        agent.execute(
            tools=[{"name": "remote_email_tool"}],
            messages=_messages(
                {
                    "task_profile": {"entities": {"recipient": "人事部门"}},
                    "resolved_inputs": {
                        "email.dispatch.request": {
                            "recipients": ["人事部门"],
                            "subject": "年假报告",
                            "body": "# 年假报告",
                            "source_report_artifact_id": "artifact-report",
                            "approval_id": "approval-42",
                            "idempotency_key": "idem-42",
                        }
                    },
                }
            ),
            context={},
            parameter_extractor=FakeExtractor(),
        )
    )

    receipt = result["outputs"]["email.dispatch.receipt"]
    assert result["status"] == "success"
    assert captured == {
        "to": "人事部门",
        "subject": "年假报告",
        "body": "# 年假报告",
    }
    assert receipt["provider_message_id"] == "mail-42"
    assert receipt["approval_id"] == "approval-42"
    assert receipt["idempotency_key"] == "idem-42"


def test_scheduler_assembles_strict_email_request_from_report_artifact() -> None:
    scheduler = TaskScheduler(execute_step=lambda **_: None)
    report_ref = scheduler.store.put(
        Artifact(
            logical_name="report.markdown",
            schema_ref="report.markdown@v1",
            payload="# 年假报告",
            schema_valid=True,
        )
    )
    scheduler._outputs = {"report": {"report.markdown": report_ref}}
    step = TaskStep(
        step_id="email",
        agent_contract=AgentContract(
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
        input_bindings=[
            {
                "parameter_name": "email.dispatch.request",
                "source_artifacts": [
                    {"source_step": "report", "source_output": "report.markdown"}
                ],
                "assembly": {"schema_ref": "email.dispatch.request@v1"},
            }
        ],
    )

    resolved, _sensitivities, refs = scheduler._resolve_inputs(
        step,
        {"task_profile": {"entities": {"recipient": "人事部门"}}},
    )

    request = resolved["email.dispatch.request"]
    assert request["recipients"] == ["人事部门"]
    assert request["body"] == "# 年假报告"
    assert request["source_report_artifact_id"] == report_ref.artifact_id
    assert request["approval_id"] == ""
    assert request["idempotency_key"] == ""
    assert set(request) == {
        "recipients",
        "subject",
        "body",
        "source_report_artifact_id",
        "approval_id",
        "idempotency_key",
    }
    assert refs == [report_ref]
