"""Executable composite harness for orchestration cases ORCH-013..020.

The natural-language boundary uses the production task profiler.  The
orchestration boundary uses the production TaskGraph/Scheduler/Artifact,
approval and receipt implementations with deterministic Agent test doubles.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from src.contracts.agent_contract import AgentContract, DataContractRef
from src.interface.artifact import ArtifactRef
from src.interface.task_graph import TaskGraph, TaskSpec, TaskStep
from src.manager.executor.base import ExecuteResult, ExecutionStatus
from src.orchestration.artifact_payload_store import ArtifactPayloadStore
from src.orchestration.providers import StubRoutingProvider
from src.orchestration.runtime import run_scheduler_workflow
from src.orchestration.schema_registry import get_schema_registry
from src.orchestration.store import ArtifactStore
from src.orchestrator.task_profiler import profile_task
from src.robust.checkpoint import CheckpointManager
from src.security.enforcement import ApprovalRequiredError


DATASET = Path(__file__).parent / "evaluations" / "multi_agent_orchestration_eval.json"

SCHEMAS = {
    "eval.employee.v1": {
        "required": ["employee"],
        "properties": {"employee": {"type": "string"}},
    },
    "eval.travel.v1": {
        "required": ["destination"],
        "properties": {"destination": {"type": "string"}},
    },
    "eval.weather.v1": {
        "required": ["location"],
        "properties": {"location": {"type": "string"}},
    },
    "eval.report.v1": {
        "required": ["markdown"],
        "properties": {"markdown": {"type": "string"}},
    },
    "eval.company.v1": {
        "required": ["companies"],
        "properties": {"companies": {"type": "array"}},
    },
    "eval.risk.v1": {
        "required": ["risk"],
        "properties": {"risk": {"type": "string"}},
    },
    "eval.salary.v1": {
        "required": ["salary"],
        "properties": {"salary": {"type": "string"}},
    },
    "eval.document.v1": {
        "required": ["status", "file_name"],
        "properties": {
            "status": {"type": "string"},
            "file_name": {"type": "string"},
        },
    },
    "eval.schedule.v1": {
        "required": ["available"],
        "properties": {"available": {"type": "boolean"}},
    },
    "eval.meeting.v1": {
        "required": ["meeting_id"],
        "properties": {"meeting_id": {"type": "string"}},
    },
    "eval.notification.v1": {
        "required": ["sent"],
        "properties": {"sent": {"type": "boolean"}},
    },
}


@pytest.fixture(autouse=True)
def _isolated_runtime_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for env_name, leaf in (
        ("ARTIFACT_PAYLOAD_STORE_DIR", "artifacts"),
        ("RECEIPT_STORE_DIR", "receipts"),
        ("APPROVAL_STORE_DIR", "approvals"),
        ("GOVERNANCE_EVENT_STORE_DIR", "governance"),
        ("RECONCILIATION_STORE_DIR", "reconciliation"),
    ):
        monkeypatch.setenv(env_name, str(tmp_path / leaf))
    monkeypatch.setattr("src.service.env.S_ABAC_ENABLED", False, raising=False)
    registry = get_schema_registry()
    for schema_ref, schema in SCHEMAS.items():
        registry.register(schema_ref, schema)


def _case(case_id: str) -> dict[str, Any]:
    payload = json.loads(DATASET.read_text(encoding="utf-8"))
    return next(item for item in payload["cases"] if item["id"] == case_id)


async def _profile(case_id: str):
    case = _case(case_id)
    return await profile_task(
        case["query"], task_id=case_id.lower(), recognition_mode="rule"
    )


def _assert_intents(profile: Any, expected: set[str]) -> None:
    assert expected <= set(profile.intents)
    assert profile.needs_clarification is False
    assert profile.missing_fields == []


def _state(case_id: str, graph: TaskGraph) -> dict[str, Any]:
    return {
        "workflow_id": f"wf-{case_id.lower()}",
        "user_id": "eval-user",
        "task_graph": graph,
        "messages": [],
        "original_user_query": _case(case_id)["query"],
    }


async def _run(
    state: dict[str, Any],
    case_id: str,
    execute_step,
    *,
    authorize_step=None,
    checkpoint_manager=None,
) -> list[dict[str, Any]]:
    return [
        event
        async for event in run_scheduler_workflow(
            state,
            task_id=case_id.lower(),
            execute_step=execute_step,
            authorize_step=authorize_step,
            checkpoint_manager=checkpoint_manager,
            routing_provider=StubRoutingProvider(),
        )
    ]


def _terminal(events: list[dict[str, Any]]) -> dict[str, Any]:
    assert events[-1]["event"] == "end_of_workflow"
    return events[-1]["data"]


def _artifact_names(state: dict[str, Any]) -> set[str]:
    return {
        logical_name
        for result in (state.get("step_results") or {}).values()
        for logical_name in (result.get("outputs") or {})
    }


def _artifact(state: dict[str, Any], case_id: str, step_id: str, logical_name: str):
    payloads = ArtifactPayloadStore(case_id.lower()).load_index(state["artifacts"])
    store = ArtifactStore()
    store.load_state(payloads)
    ref = state["step_results"][step_id]["outputs"][logical_name]
    return store.get(ArtifactRef(**ref))


def _review_payload(agent_id: str) -> dict[str, Any]:
    return {
        "subject": {"subject_type": "user", "id": "eval-user", "attributes": {}},
        "object": {
            "object_type": "agent",
            "id": agent_id,
            "attributes": {"requires_approval": True},
        },
        "scenario": {"task_scenario": {}, "environment": {}, "business_context": {}},
        "action": {"verb": "dispatch", "attributes": {"action_type": "write"}},
        "policy_result": {
            "allowed": False,
            "decision": "REVIEW_REQUIRED",
            "human_review_required": True,
            "reason": "Evaluation approval gate",
        },
        "approval_signature": f"eval-signature-{agent_id}",
    }


def _weather_travel_graph(case_id: str) -> TaskGraph:
    return TaskGraph(
        spec=TaskSpec(task_id=case_id.lower(), subject="eval-user"),
        steps=[
            TaskStep(
                step_id="employee_lookup", agent_name="RemoteHRAssistantAgent",
                preferred_resource_id="RemoteHRAssistantAgent", expected_outputs=["employee.info"],
                expected_schema_ref="eval.employee.v1",
            ),
            TaskStep(
                step_id="weather_lookup", agent_name="RemoteWeatherAgent",
                preferred_resource_id="RemoteWeatherAgent", expected_outputs=["weather.forecast"],
                expected_schema_ref="eval.weather.v1",
            ),
            TaskStep(
                step_id="travel_lookup", agent_name="RemoteOfficeAssistantAgent",
                preferred_resource_id="RemoteOfficeAssistantAgent", depends_on=["employee_lookup"],
                expected_outputs=["employee.travel_records"], expected_schema_ref="eval.travel.v1",
                input_bindings=[{"parameter_name": "employee", "source_step": "employee_lookup", "source_output": "employee.info"}],
            ),
            TaskStep(
                step_id="generate_travel_brief", agent_name="RemoteReportAgent",
                preferred_resource_id="RemoteReportAgent", depends_on=["weather_lookup", "travel_lookup"],
                expected_outputs=["report.markdown"], expected_schema_ref="eval.report.v1",
                input_bindings=[
                    {"parameter_name": "weather", "source_step": "weather_lookup", "source_output": "weather.forecast"},
                    {"parameter_name": "travel", "source_step": "travel_lookup", "source_output": "employee.travel_records"},
                ],
            ),
        ],
    )


def test_orch_013_weather_and_employee_parallel_fan_in() -> None:
    profile = asyncio.run(_profile("ORCH-013"))
    _assert_intents(profile, {"weather_query", "employee_information_query", "travel_service", "report_generation"})
    state = _state("ORCH-013", _weather_travel_graph("ORCH-013"))
    gate = asyncio.Event()
    started: set[str] = set()

    async def execute_step(*, step, inputs, **_kwargs):
        if step.step_id in {"employee_lookup", "weather_lookup"}:
            started.add(step.step_id)
            if len(started) == 2:
                gate.set()
            await asyncio.wait_for(gate.wait(), timeout=1)
        if step.step_id == "employee_lookup":
            result = {"employee": "王强"}
        elif step.step_id == "weather_lookup":
            result = {"location": "北京"}
        elif step.step_id == "travel_lookup":
            result = {"destination": "北京"}
        else:
            result = {"markdown": f"{inputs['travel']['destination']}行前天气报告"}
        return ExecuteResult(ExecutionStatus.SUCCESS, result)

    events = asyncio.run(_run(state, "ORCH-013", execute_step))
    assert _terminal(events)["status"] == "SUCCEEDED"
    assert _artifact_names(state) == {"employee.info", "weather.forecast", "employee.travel_records", "report.markdown"}
    report = _artifact(state, "ORCH-013", "generate_travel_brief", "report.markdown")
    assert {ref.artifact_id for ref in report.derived_from} == {
        state["step_results"]["weather_lookup"]["outputs"]["weather.forecast"]["artifact_id"],
        state["step_results"]["travel_lookup"]["outputs"]["employee.travel_records"]["artifact_id"],
    }


def test_orch_014_weather_failure_blocks_only_dependent_report() -> None:
    profile = asyncio.run(_profile("ORCH-014"))
    _assert_intents(profile, {"weather_query", "employee_information_query", "travel_service", "report_generation"})
    state = _state("ORCH-014", _weather_travel_graph("ORCH-014"))
    calls: list[str] = []

    async def execute_step(*, step, **_kwargs):
        calls.append(step.step_id)
        if step.step_id == "weather_lookup":
            return ExecuteResult(ExecutionStatus.FAILED, error="controlled HTTP 503")
        outputs = {
            "employee_lookup": {"employee": "王强"},
            "travel_lookup": {"destination": "北京"},
        }
        return ExecuteResult(ExecutionStatus.SUCCESS, outputs[step.step_id])

    events = asyncio.run(_run(state, "ORCH-014", execute_step))
    terminal = _terminal(events)
    assert terminal["status"] == "PARTIAL_FAILED"
    assert set(terminal["failed_steps"]) == {"weather_lookup"}
    assert "generate_travel_brief" in terminal["blocked_steps"]
    assert set(calls) == {"employee_lookup", "weather_lookup", "travel_lookup"}
    assert _artifact_names(state) == {"employee.info", "employee.travel_records"}


def _risk_graph(case_id: str) -> TaskGraph:
    return TaskGraph(
        spec=TaskSpec(task_id=case_id.lower(), subject="eval-user"),
        steps=[
            TaskStep(step_id="company_research", agent_name="RemoteUnicornSelectorAgent", preferred_resource_id="RemoteUnicornSelectorAgent", expected_outputs=["company.records"], expected_schema_ref="eval.company.v1"),
            TaskStep(step_id="risk_query", agent_name="RemoteBusinessRiskAgent", preferred_resource_id="RemoteBusinessRiskAgent", expected_outputs=["risk.records"], expected_schema_ref="eval.risk.v1"),
            TaskStep(
                step_id="generate_risk_report", agent_name="RemoteReportAgent", preferred_resource_id="RemoteReportAgent",
                depends_on=["company_research", "risk_query"], expected_outputs=["report.markdown"], expected_schema_ref="eval.report.v1",
                input_bindings=[
                    {"parameter_name": "companies", "source_step": "company_research", "source_output": "company.records"},
                    {"parameter_name": "risks", "source_step": "risk_query", "source_output": "risk.records"},
                ],
            ),
            TaskStep(
                step_id="send_risk_report", agent_name="RemoteEmailAgent", preferred_resource_id="RemoteEmailAgent",
                operation_mode="send", depends_on=["generate_risk_report"], expected_outputs=["email.dispatch.receipt"],
                input_bindings=[{"parameter_name": "report", "source_step": "generate_risk_report", "source_output": "report.markdown"}],
            ),
        ],
    )


def test_orch_015_parallel_research_fan_in_then_approval() -> None:
    profile = asyncio.run(_profile("ORCH-015"))
    _assert_intents(profile, {"information_research", "risk_analysis", "report_generation", "message_or_email_send"})
    state = _state("ORCH-015", _risk_graph("ORCH-015"))
    side_effect_calls = 0

    async def execute_step(*, step, **_kwargs):
        nonlocal side_effect_calls
        if step.step_id == "send_risk_report":
            side_effect_calls += 1
        outputs = {
            "company_research": {"companies": ["A", "B", "C"]},
            "risk_query": {"risk": "medium"},
            "generate_risk_report": {"markdown": "risk report"},
            "send_risk_report": {"sent": {"id": "mail-015"}},
        }
        return ExecuteResult(ExecutionStatus.SUCCESS, outputs[step.step_id])

    async def authorize_step(*, step, **_kwargs):
        if step.step_id == "send_risk_report":
            raise ApprovalRequiredError("approval required", _review_payload("RemoteEmailAgent"))
        return {"allowed": True, "decision": "ALLOW"}

    events = asyncio.run(_run(state, "ORCH-015", execute_step, authorize_step=authorize_step))
    assert _terminal(events)["status"] == "APPROVAL_REQUIRED"
    assert side_effect_calls == 0
    assert _artifact_names(state) == {"company.records", "risk.records", "report.markdown"}
    report = _artifact(state, "ORCH-015", "generate_risk_report", "report.markdown")
    assert len(report.derived_from) == 2


@pytest.mark.xfail(
    strict=True,
    reason="ORCH-016 gap: first non-executed consumer is classified FAILED instead of SKIPPED",
)
def test_orch_016_invalid_risk_schema_blocks_report_and_send() -> None:
    profile = asyncio.run(_profile("ORCH-016"))
    _assert_intents(profile, {"information_research", "risk_analysis", "report_generation", "message_or_email_send"})
    state = _state("ORCH-016", _risk_graph("ORCH-016"))
    calls: list[str] = []

    async def execute_step(*, step, **_kwargs):
        calls.append(step.step_id)
        outputs = {
            "company_research": {"companies": ["A", "B", "C"]},
            "risk_query": {"unexpected": "not a risk contract"},
        }
        return ExecuteResult(ExecutionStatus.SUCCESS, outputs[step.step_id])

    events = asyncio.run(_run(state, "ORCH-016", execute_step))
    terminal = _terminal(events)
    assert terminal["status"] == "PARTIAL_FAILED"
    assert set(calls) == {"company_research", "risk_query"}
    assert "generate_risk_report" in terminal["blocked_steps"]
    assert "send_risk_report" in terminal["blocked_steps"]
    assert _artifact_names(state) == {"company.records"}


def _income_graph(case_id: str) -> TaskGraph:
    contract = AgentContract(
        produces=[
            DataContractRef(name="employee.info", schema_ref="eval.employee.v1"),
            DataContractRef(name="employee.salary", schema_ref="eval.salary.v1"),
        ]
    )
    return TaskGraph(
        spec=TaskSpec(task_id=case_id.lower(), subject="eval-user"),
        steps=[
            TaskStep(
                step_id="query_employee_salary", agent_name="RemoteHRAssistantAgent", preferred_resource_id="RemoteHRAssistantAgent",
                expected_outputs=["employee.info", "employee.salary"],
                expected_schema_refs={"employee.info": "eval.employee.v1", "employee.salary": "eval.salary.v1"},
                agent_contract=contract,
            ),
            TaskStep(
                step_id="generate_income_proof", agent_name="RemoteDocumentAgent", preferred_resource_id="RemoteDocumentAgent",
                depends_on=["query_employee_salary"], expected_outputs=["document.file"], expected_schema_ref="eval.document.v1",
                input_bindings=[
                    {"parameter_name": "employee", "source_step": "query_employee_salary", "source_output": "employee.info"},
                    {"parameter_name": "salary", "source_step": "query_employee_salary", "source_output": "employee.salary"},
                ],
            ),
            TaskStep(
                step_id="send_income_proof", agent_name="RemoteEmailAgent", preferred_resource_id="RemoteEmailAgent",
                operation_mode="send", depends_on=["generate_income_proof"], expected_outputs=["email.dispatch.receipt"],
                input_bindings=[{"parameter_name": "document", "source_step": "generate_income_proof", "source_output": "document.file"}],
            ),
        ],
    )


@pytest.mark.xfail(
    strict=True,
    reason="ORCH-017 gap: raw salary is currently exposed in scheduler event data",
)
def test_orch_017_sensitive_salary_chain_stops_before_send(tmp_path: Path) -> None:
    profile = asyncio.run(_profile("ORCH-017"))
    _assert_intents(profile, {"employee_information_query", "salary_query", "document_generation", "message_or_email_send"})
    state = _state("ORCH-017", _income_graph("ORCH-017"))
    checkpoint_manager = CheckpointManager(tmp_path / "checkpoints")
    send_calls = 0

    async def execute_step(*, step, inputs, **_kwargs):
        nonlocal send_calls
        if step.step_id == "query_employee_salary":
            result = {
                "contract_version": "1.0", "status": "success",
                "outputs": {"employee.info": {"employee": "李娜"}, "employee.salary": {"salary": "42000"}},
                "error": None,
                "metadata": {"producer_agent": "RemoteHRAssistantAgent", "schema_version": "1.0"},
            }
        elif step.step_id == "generate_income_proof":
            assert inputs["salary"]["salary"] == "42000"
            result = {"status": "generated", "file_name": "income-proof.pdf"}
        else:
            send_calls += 1
            result = {"sent": {"id": "mail-017"}}
        return ExecuteResult(ExecutionStatus.SUCCESS, result)

    async def authorize_step(*, step, **_kwargs):
        if step.step_id == "send_income_proof":
            raise ApprovalRequiredError("approval required", _review_payload("RemoteEmailAgent"))
        return {"allowed": True, "decision": "ALLOW"}

    events = asyncio.run(_run(state, "ORCH-017", execute_step, authorize_step=authorize_step, checkpoint_manager=checkpoint_manager))
    assert _terminal(events)["status"] == "APPROVAL_REQUIRED"
    assert send_calls == 0
    assert _artifact_names(state) == {"employee.info", "employee.salary", "document.file"}
    observable = json.dumps({"state": state, "events": events}, ensure_ascii=False, default=str)
    assert "42000" not in observable
    for checkpoint_file in (tmp_path / "checkpoints").rglob("*.json"):
        assert "42000" not in checkpoint_file.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("case_id", "expected_missing"),
    [
        ("ORCH-018", ["recipient"]),
        ("ORCH-019", ["meeting.participants", "meeting.time"]),
    ],
)
def test_orch_018_019_clarify_before_any_execution(case_id: str, expected_missing: list[str]) -> None:
    profile = asyncio.run(_profile(case_id))
    expected = _case(case_id)["expected"]
    assert profile.needs_clarification is True
    assert profile.missing_fields == expected_missing
    assert profile.clarification_questions
    assert expected["started_step_count"] == 0
    assert expected["produced_artifact_count"] == 0


def _meeting_graph() -> TaskGraph:
    return TaskGraph(
        spec=TaskSpec(task_id="orch-020", subject="eval-user"),
        steps=[
            TaskStep(step_id="schedule_query", agent_name="RemoteScheduleAgent", preferred_resource_id="RemoteScheduleAgent", expected_outputs=["schedule.result"], expected_schema_ref="eval.schedule.v1"),
            TaskStep(
                step_id="arrange_meeting", agent_name="RemoteMeetingManagerAgent", preferred_resource_id="RemoteMeetingManagerAgent",
                operation_mode="write", depends_on=["schedule_query"], expected_outputs=["meeting.result"], expected_schema_ref="eval.meeting.v1",
                input_bindings=[{"parameter_name": "schedule", "source_step": "schedule_query", "source_output": "schedule.result"}],
            ),
            TaskStep(
                step_id="notify_participants", agent_name="RemoteCommunicationAgent", preferred_resource_id="RemoteCommunicationAgent",
                operation_mode="send", depends_on=["arrange_meeting"], expected_outputs=["communication.result"], expected_schema_ref="eval.notification.v1",
                input_bindings=[{"parameter_name": "meeting", "source_step": "arrange_meeting", "source_output": "meeting.result"}],
            ),
        ],
    )


def test_orch_020_chained_approvals_and_duplicate_resume_are_idempotent() -> None:
    profile = asyncio.run(_profile("ORCH-020"))
    _assert_intents(profile, {"schedule_management", "meeting_arrangement", "message_or_email_send"})
    state = _state("ORCH-020", _meeting_graph())
    phase = {"value": 0}
    calls = {"schedule_query": 0, "arrange_meeting": 0, "notify_participants": 0}

    async def execute_step(*, step, inputs, **_kwargs):
        calls[step.step_id] += 1
        if step.step_id == "schedule_query":
            result = {"available": True}
        elif step.step_id == "arrange_meeting":
            assert inputs["schedule"]["available"] is True
            result = {"meeting_id": "meeting-020", "meeting_info": {"meeting": {"id": "meeting-020"}}}
        else:
            assert inputs["meeting"]["meeting_id"] == "meeting-020"
            result = {"sent": True, "provider_message_id": "notification-020"}
        return ExecuteResult(ExecutionStatus.SUCCESS, result)

    async def authorize_step(*, step, **_kwargs):
        if step.step_id == "arrange_meeting" and phase["value"] < 1:
            raise ApprovalRequiredError("meeting approval required", _review_payload("RemoteMeetingManagerAgent"))
        if step.step_id == "notify_participants" and phase["value"] < 2:
            raise ApprovalRequiredError("notification approval required", _review_payload("RemoteCommunicationAgent"))
        return {"allowed": True, "decision": "ALLOW_APPROVED"}

    first = asyncio.run(_run(state, "ORCH-020", execute_step, authorize_step=authorize_step))
    assert _terminal(first)["status"] == "APPROVAL_REQUIRED"
    assert calls == {"schedule_query": 1, "arrange_meeting": 0, "notify_participants": 0}

    phase["value"] = 1
    second = asyncio.run(_run(state, "ORCH-020", execute_step, authorize_step=authorize_step))
    assert _terminal(second)["status"] == "APPROVAL_REQUIRED"
    assert calls == {"schedule_query": 1, "arrange_meeting": 1, "notify_participants": 0}

    phase["value"] = 2
    third = asyncio.run(_run(state, "ORCH-020", execute_step, authorize_step=authorize_step))
    assert _terminal(third)["status"] == "SUCCEEDED"
    assert calls == {"schedule_query": 1, "arrange_meeting": 1, "notify_participants": 1}

    duplicate = asyncio.run(_run(state, "ORCH-020", execute_step, authorize_step=authorize_step))
    assert _terminal(duplicate)["status"] == "SUCCEEDED"
    assert calls == {"schedule_query": 1, "arrange_meeting": 1, "notify_participants": 1}
    assert _artifact_names(state) == {"schedule.result", "meeting.result", "communication.result"}
