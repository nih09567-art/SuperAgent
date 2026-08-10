import json
from types import SimpleNamespace

from src.orchestration.observability import build_orchestration_view


def _task_log():
    return SimpleNamespace(
        task_id="task-1",
        workflow_id="alice:wf",
        user_query="生成请假报告",
        status="SUCCEEDED",
        execution_phase="execution",
        created_at="2026-08-10T00:00:00+00:00",
        finished_at="2026-08-10T00:00:02+00:00",
        execution_plan_hash="plan-hash",
        planning_steps=[{"description": "secret planner note", "secret": "hidden"}],
        task_graph={
            "spec": {"task_id": "task-1"},
            "steps": [
                {"step_id": "hr", "agent_name": "HR", "depends_on": []},
                {"step_id": "kb", "agent_name": "Knowledge", "depends_on": []},
                {
                    "step_id": "report",
                    "agent_name": "Report",
                    "depends_on": ["hr", "kb"],
                    "input_bindings": [
                        {
                            "param": "sources",
                            "source_artifacts": [
                                {"source_step": "hr", "source_output": "employee.info"},
                                {"source_step": "kb", "source_output": "policy.info"},
                            ],
                        }
                    ],
                },
            ],
        },
        orchestration_attempts=[
            {"sequence": 1, "step_id": "hr", "attempt": 1, "phase": "primary", "event": "start", "monotonic_ns": 100, "timestamp": "t1"},
            {"sequence": 2, "step_id": "kb", "attempt": 1, "phase": "primary", "event": "start", "monotonic_ns": 110, "timestamp": "t2"},
            {"sequence": 3, "step_id": "hr", "attempt": 1, "phase": "primary", "event": "end", "monotonic_ns": 200, "timestamp": "t3", "status": "SUCCEEDED"},
            {"sequence": 4, "step_id": "kb", "attempt": 1, "phase": "primary", "event": "end", "monotonic_ns": 220, "timestamp": "t4", "status": "SUCCEEDED"},
        ],
        orchestration_step_results={"hr": {"status": "SUCCEEDED"}},
        history=[],
        tool_selection_decisions={
            "hr": {
                "mode": "audit",
                "recommended_mcp_tool": "search_employees",
                "arguments": {"employee_name": "secret"},
                "candidates": [
                    {"tool_key": "office:search_employees", "score": 0.9, "input_schema": {"secret": True}}
                ],
            }
        },
        artifact_lineage=[
            {
                "producer_step_id": "hr",
                "artifact_id": "artifact-1234567890-very-secret",
                "logical_name": "employee.info",
                "schema_ref": "employee.info@v1",
                "payload": {"salary": 99999},
                "derived_from": [{"artifact_id": "source-1234567890-very-secret", "version": 1}],
            }
        ],
    )


def test_orchestration_view_preserves_parallel_graph_and_fan_in_without_payloads():
    view = build_orchestration_view(_task_log())

    assert {edge["source"] for edge in view["graph"]["dependency_edges"]} == {"hr", "kb"}
    assert len(view["graph"]["artifact_edges"]) == 2
    assert len(view["runtime"]["attempts"]) == 2
    assert view["runtime"]["attempts"][0]["duration_ms"] == 0.0
    assert view["planning"]["validation"]["status"] == "VERIFIED_FOR_EXECUTION"

    serialized = json.dumps(view, ensure_ascii=False)
    assert "secret planner note" not in serialized
    assert "salary" not in serialized
    assert "employee_name" not in serialized
    assert "input_schema" not in serialized
    assert "artifact-1234567890-very-secret" not in serialized
    assert "source-1234567890-very-secret" not in serialized
    assert view["tool_decisions"]["hr"]["mode"] == "audit"


def test_orchestration_view_marks_missing_runtime_graph_as_not_recorded():
    log = _task_log()
    log.task_graph = {}
    log.planning_steps = []

    view = build_orchestration_view(log)

    assert view["graph"]["available"] is False
    assert view["planning"]["validation"]["status"] == "NOT_RECORDED"


def test_orchestration_view_replays_legacy_agent_completion_without_fake_timing():
    log = _task_log()
    log.orchestration_step_results = {}
    log.orchestration_attempts = []
    log.history = [
        {
            "event": "start_of_agent",
            "node_name": "agent_proxy",
            "sub_agent_name": "HR",
        },
        {
            "event": "end_of_agent",
            "node_name": "agent_proxy",
            "sub_agent_name": "HR",
        },
    ]

    view = build_orchestration_view(log)

    assert view["runtime"]["step_states"]["hr"] == {
        "status": "COMPLETED",
        "evidence": "legacy_history",
    }
    assert view["runtime"]["attempts"] == []
