"""Annual-leave workflow status semantics at the scheduler boundary."""

import asyncio

from src.interface.artifact import StepStatus
from src.interface.task_graph import TaskGraph, TaskSpec, TaskStep, WorkflowStatus
from src.manager.executor.base import ExecuteResult, ExecutionStatus
from src.orchestration.scheduler import TaskScheduler


class _AnnualLeaveExecutor:
    async def __call__(self, *, step, selected_agent, inputs, context):
        if step.step_id == "generate_report":
            return ExecuteResult(
                status=ExecutionStatus.FAILED,
                error="report tool returned an invalid result",
            )
        return ExecuteResult(
            status=ExecutionStatus.SUCCESS,
            result={"artifact": step.step_id},
        )


def test_report_failure_keeps_upstreams_succeeded_and_marks_workflow_partial():
    graph = TaskGraph(
        spec=TaskSpec(task_id="annual-leave-report-failure"),
        steps=[
            TaskStep(
                step_id="hr_query",
                agent_name="RemoteHRAssistantAgent",
                preferred_resource_id="RemoteHRAssistantAgent",
            ),
            TaskStep(
                step_id="policy_query",
                agent_name="RemoteKnowledgeAgent",
                preferred_resource_id="RemoteKnowledgeAgent",
            ),
            TaskStep(
                step_id="generate_report",
                depends_on=["hr_query", "policy_query"],
                agent_name="RemoteReportAgent",
                preferred_resource_id="RemoteReportAgent",
            ),
        ],
    )

    result = asyncio.run(
        TaskScheduler(execute_step=_AnnualLeaveExecutor()).run(graph)
    )

    assert result["hr_query"].status == StepStatus.SUCCEEDED
    assert result["policy_query"].status == StepStatus.SUCCEEDED
    assert result["generate_report"].status == StepStatus.FAILED
    assert result["generate_report"].outputs == {}
    assert result.terminal_status == WorkflowStatus.PARTIAL_FAILED
