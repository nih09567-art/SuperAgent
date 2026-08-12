"""Executable mechanism cases for ORCH-006, ORCH-007, ORCH-011 and ORCH-012."""

from __future__ import annotations

import asyncio

import pytest

from remote_agents.hr_assistant_agent import RemoteHRAssistantAgent
from remote_agents.knowledge_agent import RemoteKnowledgeAgent
from remote_agents.report_agent import RemoteReportAgent
from src.contracts.agent_contract import AgentContract, DataContractRef
from src.interface.task_graph import TaskGraph, TaskGraphValidationError, TaskSpec, TaskStep
from src.interface.artifact import ArtifactRef
from src.manager.executor.base import ExecuteResult, ExecutionStatus
from src.orchestration.plan_to_task_graph import plan_to_task_graph
from src.orchestration.artifact_payload_store import ArtifactPayloadStore
from src.orchestration.providers import StubRoutingProvider
from src.orchestration.runtime import run_scheduler_workflow
from src.orchestration.schema_registry import get_schema_registry
from src.orchestration.store import ArtifactStore


@pytest.fixture(autouse=True)
def _isolated_stores(tmp_path, monkeypatch):
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
    schemas = {
        "eval.hr.v1": ("employee", "string"),
        "eval.policy.v1": ("policy", "string"),
        "eval.report.v1": ("markdown", "string"),
        "eval.leave.v1": ("days", "integer"),
    }
    for schema_ref, (field, field_type) in schemas.items():
        registry.register(
            schema_ref,
            {"required": [field], "properties": {field: {"type": field_type}}},
        )


def test_orch_006_cycle_is_rejected_before_execution():
    plan = [
        {"step_id": "hr_query", "agent_name": "RemoteHRAssistantAgent", "depends_on": ["generate_report"]},
        {"step_id": "policy_query", "agent_name": "RemoteKnowledgeAgent"},
        {"step_id": "generate_report", "agent_name": "RemoteReportAgent", "depends_on": ["hr_query", "policy_query"]},
    ]
    started = 0
    with pytest.raises(TaskGraphValidationError, match="cycle detected"):
        plan_to_task_graph(plan, task_id="orch-006")
    assert started == 0


@pytest.mark.xfail(
    strict=True,
    reason="ORCH-007 gap: converter validates the fan-in shape but not the gold-standard source-set completeness",
)
def test_orch_007_missing_policy_source_is_rejected_before_execution():
    contracts = {
        "RemoteHRAssistantAgent": RemoteHRAssistantAgent().contract,
        "RemoteKnowledgeAgent": RemoteKnowledgeAgent().contract,
        "RemoteReportAgent": RemoteReportAgent().contract,
    }
    plan = [
        {"step_id": "hr_query", "agent_name": "RemoteHRAssistantAgent"},
        {"step_id": "policy_query", "agent_name": "RemoteKnowledgeAgent"},
        {
            "step_id": "generate_report",
            "agent_name": "RemoteReportAgent",
            "depends_on": ["hr_query", "policy_query"],
            "inputs": [
                {
                    "parameter_name": "report.sources",
                    "source_artifacts": [
                        {"source_step": "hr_query", "source_output": "employee.info"}
                    ],
                    "assembly": {"schema_ref": "report.sources@v1"},
                }
            ],
        },
    ]
    with pytest.raises(TaskGraphValidationError):
        plan_to_task_graph(plan, task_id="orch-007", agent_contracts=contracts)


def _policy_contract() -> AgentContract:
    return AgentContract(
        produces=[DataContractRef(name="policy.info", schema_ref="eval.policy.v1")]
    )


def _policy_envelope(*, success: bool) -> dict:
    return {
        "contract_version": "1.0",
        "status": "success" if success else "error",
        "outputs": {"policy.info": {"policy": "年假制度"}} if success else {},
        "error": None if success else {
            "code": "UPSTREAM_TIMEOUT",
            "message": "controlled timeout",
            "retryable": True,
            "details": {},
        },
        "metadata": {"producer_agent": "RemoteKnowledgeAgent", "schema_version": "1.0"},
    }


async def _collect(state, task_id, execute_step, *, authorize_step=None):
    return [
        event
        async for event in run_scheduler_workflow(
            state,
            task_id=task_id,
            execute_step=execute_step,
            authorize_step=authorize_step,
            routing_provider=StubRoutingProvider(),
        )
    ]


def _artifact(state, task_id, step_id, logical_name):
    payloads = ArtifactPayloadStore(task_id).load_index(state["artifacts"])
    store = ArtifactStore()
    store.load_state(payloads)
    ref = state["step_results"][step_id]["outputs"][logical_name]
    return store.get(ArtifactRef(**ref))


def test_orch_011_read_retry_closes_three_agent_workflow():
    graph = TaskGraph(
        spec=TaskSpec(task_id="orch-011", subject="eval-user"),
        steps=[
            TaskStep(step_id="hr_query", agent_name="RemoteHRAssistantAgent", preferred_resource_id="RemoteHRAssistantAgent", expected_outputs=["employee.info"], expected_schema_ref="eval.hr.v1"),
            TaskStep(step_id="policy_query", agent_name="RemoteKnowledgeAgent", preferred_resource_id="RemoteKnowledgeAgent", retry=1, expected_outputs=["policy.info"], expected_schema_ref="eval.policy.v1", agent_contract=_policy_contract()),
            TaskStep(
                step_id="generate_report", agent_name="RemoteReportAgent", preferred_resource_id="RemoteReportAgent",
                depends_on=["hr_query", "policy_query"], expected_outputs=["report.markdown"], expected_schema_ref="eval.report.v1",
                input_bindings=[
                    {"parameter_name": "employee", "source_step": "hr_query", "source_output": "employee.info"},
                    {"parameter_name": "policy", "source_step": "policy_query", "source_output": "policy.info"},
                ],
            ),
        ],
    )
    attempts = {"hr_query": 0, "policy_query": 0, "generate_report": 0}

    async def execute_step(*, step, **_kwargs):
        attempts[step.step_id] += 1
        if step.step_id == "hr_query":
            return ExecuteResult(ExecutionStatus.SUCCESS, {"employee": "王强"})
        if step.step_id == "policy_query":
            return ExecuteResult(
                ExecutionStatus.SUCCESS,
                _policy_envelope(success=attempts[step.step_id] > 1),
            )
        return ExecuteResult(ExecutionStatus.SUCCESS, {"markdown": "年假报告"})

    state = {"workflow_id": "wf-orch-011", "user_id": "eval-user", "task_graph": graph, "messages": []}
    events = asyncio.run(_collect(state, "orch-011", execute_step))
    assert events[-1]["data"]["status"] == "SUCCEEDED"
    assert attempts == {"hr_query": 1, "policy_query": 2, "generate_report": 1}
    assert set(state["step_results"]["generate_report"]["outputs"]) == {"report.markdown"}
    report = _artifact(state, "orch-011", "generate_report", "report.markdown")
    assert len(report.derived_from) == 2


def test_orch_012_uncertain_send_requires_reconciliation_without_retry():
    graph = TaskGraph(
        spec=TaskSpec(task_id="orch-012", subject="eval-user"),
        steps=[
            TaskStep(step_id="hr_query", agent_name="HR", preferred_resource_id="HR", expected_outputs=["employee.info"], expected_schema_ref="eval.hr.v1"),
            TaskStep(step_id="policy_query", agent_name="Knowledge", preferred_resource_id="Knowledge", expected_outputs=["policy.info"], expected_schema_ref="eval.policy.v1"),
            TaskStep(step_id="leave_query", agent_name="Office", preferred_resource_id="Office", depends_on=["hr_query"], expected_outputs=["employee.leave_records"], expected_schema_ref="eval.leave.v1", input_bindings=[{"parameter_name": "employee", "source_step": "hr_query", "source_output": "employee.info"}]),
            TaskStep(step_id="report", agent_name="Report", preferred_resource_id="Report", depends_on=["hr_query", "policy_query", "leave_query"], expected_outputs=["report.markdown"], expected_schema_ref="eval.report.v1", input_bindings=[
                {"parameter_name": "employee", "source_step": "hr_query", "source_output": "employee.info"},
                {"parameter_name": "policy", "source_step": "policy_query", "source_output": "policy.info"},
                {"parameter_name": "leave", "source_step": "leave_query", "source_output": "employee.leave_records"},
            ]),
            TaskStep(step_id="send_email", agent_name="Email", preferred_resource_id="Email", operation_mode="send", retry=5, depends_on=["report"], expected_outputs=["email.dispatch.receipt"], input_bindings=[{"parameter_name": "report", "source_step": "report", "source_output": "report.markdown"}]),
        ],
    )
    calls = {step.step_id: 0 for step in graph.steps}

    async def execute_step(*, step, **_kwargs):
        calls[step.step_id] += 1
        outputs = {
            "hr_query": {"employee": "王强"},
            "policy_query": {"policy": "年假制度"},
            "leave_query": {"days": 2},
            "report": {"markdown": "年假报告"},
        }
        if step.step_id == "send_email":
            return ExecuteResult(ExecutionStatus.FAILED, error="response lost after external dispatch")
        return ExecuteResult(ExecutionStatus.SUCCESS, outputs[step.step_id])

    state = {"workflow_id": "wf-orch-012", "user_id": "eval-user", "task_graph": graph, "messages": []}
    approvals = []

    async def authorize_step(*, step, **_kwargs):
        if step.step_id == "send_email":
            approvals.append(step.step_id)
        return {"allowed": True, "decision": "ALLOW_APPROVED"}

    events = asyncio.run(
        _collect(state, "orch-012", execute_step, authorize_step=authorize_step)
    )
    assert events[-1]["data"]["status"] == "NEEDS_RECONCILIATION"
    assert calls["send_email"] == 1
    assert approvals == ["send_email"]
    artifact_names = {
        name
        for result in state["step_results"].values()
        for name in (result.get("outputs") or {})
    }
    assert {"employee.info", "policy.info", "employee.leave_records", "report.markdown"} <= artifact_names
    assert "email.dispatch.receipt" not in artifact_names
    report = _artifact(state, "orch-012", "report", "report.markdown")
    assert len(report.derived_from) == 3
