from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import src.robust.task_logger as task_logger_module
import src.robust.checkpoint as checkpoint_module
from src.robust.checkpoint import CheckpointManager
from src.robust.task_control import TaskControlStore
from src.robust.task_logger import TaskLogger
from src.service.detached_execution import (
    DetachedExecutionRunner,
    LeaseOwnershipLost,
    TaskEventStore,
)
from src.service.web_app import create_app


@pytest.fixture(autouse=True)
def _isolated_task_files(tmp_path, monkeypatch):
    monkeypatch.setattr(
        task_logger_module,
        "checkpoints_dir",
        tmp_path / "checkpoints",
    )
    monkeypatch.setenv("TASK_EVENT_STORE_DIR", str(tmp_path / "task_events"))
    monkeypatch.setenv("TASK_CONTROL_STORE_DIR", str(tmp_path / "task_controls"))
    monkeypatch.setenv(
        "RECOVERY_REVIEW_STORE_DIR", str(tmp_path / "recovery_reviews")
    )
    monkeypatch.setenv("GOVERNANCE_EVENT_STORE_DIR", str(tmp_path / "governance"))
    monkeypatch.setattr(
        checkpoint_module,
        "checkpoints_dir",
        tmp_path / "checkpoints",
    )


def test_event_store_assigns_monotonic_sequences_and_replays_after_cursor(tmp_path):
    store = TaskEventStore(tmp_path / "events")
    store.reset("task-replay")
    first = store.append(
        "task-replay",
        {"event": "start_of_workflow", "data": {"task_id": "task-replay"}},
    )
    second = store.append(
        "task-replay",
        {"event": "step_result", "data": {"task_id": "task-replay"}},
    )
    third = store.append(
        "task-replay",
        {
            "event": "end_of_workflow",
            "data": {"task_id": "task-replay", "status": "SUCCEEDED"},
        },
    )
    store.mark_producer_done("task-replay")

    assert [first["sequence"], second["sequence"], third["sequence"]] == [1, 2, 3]
    assert [item["sequence"] for item in store.list_after("task-replay", 1)] == [2, 3]
    assert store.metadata("task-replay")["terminal_status"] == "SUCCEEDED"
    assert store.metadata("task-replay")["producer_done"] is True
    assert store.delete("task-replay") == 1
    assert store.exists("task-replay") is False


def test_detached_runner_finishes_without_any_sse_subscriber(tmp_path):
    async def scenario():
        store = TaskEventStore(tmp_path / "events")
        runner = DetachedExecutionRunner(store)
        release = asyncio.Event()

        async def producer():
            yield {
                "event": "start_of_workflow",
                "data": {"task_id": "task-detached"},
            }
            await release.wait()
            yield {
                "event": "end_of_workflow",
                "data": {"task_id": "task-detached", "status": "SUCCEEDED"},
            }

        assert runner.start("task-detached", producer) is True
        await asyncio.sleep(0)
        assert runner.is_running("task-detached") is True
        assert [item["event"] for item in store.list_after("task-detached")] == [
            "start_of_workflow"
        ]

        release.set()
        for _ in range(100):
            if not runner.is_running("task-detached"):
                break
            await asyncio.sleep(0.01)

        assert runner.is_running("task-detached") is False
        assert [item["event"] for item in store.list_after("task-detached")] == [
            "start_of_workflow",
            "end_of_workflow",
        ]
        assert store.metadata("task-detached")["producer_done"] is True

        async def resumed_producer():
            yield {
                "event": "end_of_workflow",
                "data": {"task_id": "task-detached", "status": "SUCCEEDED"},
            }

        assert runner.start(
            "task-detached", resumed_producer, reset_events=False
        ) is True
        assert store.metadata("task-detached")["producer_done"] is False
        assert store.metadata("task-detached")["terminal_status"] == ""
        for _ in range(100):
            if not runner.is_running("task-detached"):
                break
            await asyncio.sleep(0.01)
        assert [
            item["sequence"] for item in store.list_after("task-detached")
        ] == [1, 2, 3]

    asyncio.run(scenario())


def test_reconnect_endpoint_replays_only_events_after_requested_sequence():
    task = TaskLogger("task-web-replay", "u1:wf", "reconnect")
    task.execution_phase = "execution"
    task.execution_user_id = "u1"
    task.log_workflow_start("reconnect")

    app = create_app()
    store = app.state.task_event_store
    store.reset(task.task_id)
    store.append(
        task.task_id,
        {"event": "start_of_workflow", "data": {"task_id": task.task_id}},
    )
    store.append(
        task.task_id,
        {"event": "step_result", "data": {"task_id": task.task_id}},
    )
    store.append(
        task.task_id,
        {
            "event": "end_of_workflow",
            "data": {"task_id": task.task_id, "status": "SUCCEEDED"},
        },
    )
    store.mark_producer_done(task.task_id)

    with TestClient(app) as client:
        response = client.get(
            f"/api/tasks/{task.task_id}/events?after_sequence=1",
            headers={"X-Authenticated-User": "u1"},
        )

    assert response.status_code == 200
    assert response.headers["X-Task-Event-Sequence"] == "3"
    assert response.headers["X-Task-Terminal-Status"] == "SUCCEEDED"
    assert "event: start_of_workflow" not in response.text
    assert "event: step_result" in response.text
    assert "event: end_of_workflow" in response.text
    assert '"sequence": 2' in response.text
    assert '"sequence": 3' in response.text


def test_reconnect_endpoint_enforces_task_owner():
    task = TaskLogger("task-web-owner", "u1:wf", "owner")
    task.execution_phase = "execution"
    task.execution_user_id = "u1"
    task.log_workflow_start("owner")

    app = create_app()
    app.state.task_event_store.reset(task.task_id)
    app.state.task_event_store.mark_producer_done(task.task_id)
    with TestClient(app) as client:
        response = client.get(
            f"/api/tasks/{task.task_id}/events",
            headers={"X-Authenticated-User": "u2"},
        )

    assert response.status_code == 403


def _running_recovery_task(
    tmp_path,
    *,
    task_id: str,
    operation_mode: str,
    open_attempt: bool = False,
) -> TaskLogger:
    task = TaskLogger(task_id, "u1:wf", "recover me")
    task.execution_phase = "execution"
    task.execution_user_id = "u1"
    task.log_workflow_start("recover me")
    task.set_workflow_snapshot(
        [],
        task_graph={
            "steps": [
                {
                    "step_id": "step_1",
                    "agent_name": "TestAgent",
                    "operation_mode": operation_mode,
                    "external_side_effect": operation_mode != "read",
                }
            ]
        },
    )
    if open_attempt:
        task.record_orchestration_attempt(
            step_id="step_1",
            attempt=1,
            phase="primary",
            planned_agent="TestAgent",
            executed_agent="TestAgent",
            event="start",
            monotonic_ns=1,
        )
    TaskControlStore().ensure_running(
        task_id,
        workflow_id=task.workflow_id,
        user_id="u1",
    )
    CheckpointManager(tmp_path / "checkpoints").save_checkpoint(
        workflow_id=task.workflow_id,
        task_id=task_id,
        step=0,
        node_name="scheduler",
        next_node="scheduler",
        state={
            "messages": [{"role": "user", "content": "recover me"}],
            "completed_steps": [],
        },
    )
    return task


def test_expired_read_only_worker_recovers_to_safe_checkpoint(tmp_path):
    task = _running_recovery_task(
        tmp_path,
        task_id="task-expired-read",
        operation_mode="read",
    )
    store = TaskEventStore(tmp_path / "task_events")
    store.reset(task.task_id)
    store.mark_producer_started(task.task_id, worker_id="dead-worker", lease_seconds=1)
    runner = DetachedExecutionRunner(store)

    recovered = runner.recover_expired_tasks(
        now=datetime.now(timezone.utc) + timedelta(seconds=2)
    )

    assert len(recovered) == 1
    assert recovered[0]["state"] == "RECOVERY_REQUIRED"
    assert recovered[0]["checkpoint_step"] == 0
    assert recovered[0]["resume_step"] == 1
    assert recovered[0]["requires_review"] is False
    assert TaskLogger.load(task.task_id).status == "RECOVERY_REQUIRED"
    assert [event["event"] for event in store.list_after(task.task_id)] == [
        "workflow_interrupted"
    ]
    assert store.metadata(task.task_id)["producer_done"] is True
    assert runner.recover_expired_tasks(
        now=datetime.now(timezone.utc) + timedelta(seconds=3)
    ) == []


def test_expired_side_effect_worker_fails_closed_for_human_review(tmp_path):
    task = _running_recovery_task(
        tmp_path,
        task_id="task-expired-send",
        operation_mode="send",
        open_attempt=True,
    )
    store = TaskEventStore(tmp_path / "task_events")
    store.reset(task.task_id)
    store.mark_producer_started(task.task_id, worker_id="dead-worker", lease_seconds=1)

    recovered = DetachedExecutionRunner(store).recover_expired_tasks(
        now=datetime.now(timezone.utc) + timedelta(seconds=2)
    )

    assert len(recovered) == 1
    assert recovered[0]["requires_review"] is True
    assert recovered[0]["uncertain_side_effect_steps"] == ["step_1"]
    resumed = TaskControlStore().get(task.task_id)
    assert resumed["state"] == "RECOVERY_REQUIRED"
    assert resumed["resume_step"] == 1


def test_live_worker_lease_is_not_recovered(tmp_path):
    task = _running_recovery_task(
        tmp_path,
        task_id="task-live-worker",
        operation_mode="read",
    )
    store = TaskEventStore(tmp_path / "task_events")
    store.reset(task.task_id)
    store.mark_producer_started(task.task_id, worker_id="live-worker", lease_seconds=30)

    recovered = DetachedExecutionRunner(store).recover_expired_tasks(
        now=datetime.now(timezone.utc)
    )

    assert recovered == []
    assert TaskLogger.load(task.task_id).status == "running"
    assert store.metadata(task.task_id)["producer_done"] is False


def test_expired_lease_takeover_fences_stale_worker_events(tmp_path):
    store = TaskEventStore(tmp_path / "events")
    store.reset("task-fenced")
    store.mark_producer_started(
        "task-fenced",
        worker_id="stale-worker",
        lease_seconds=1,
    )
    claimed = store.claim_expired_lease(
        "task-fenced",
        expected_worker_id="stale-worker",
        recovery_worker_id="recovery-worker",
        now=datetime.now(timezone.utc) + timedelta(seconds=2),
    )

    assert claimed is True
    with pytest.raises(LeaseOwnershipLost):
        store.append(
            "task-fenced",
            {"event": "step_result", "data": {}},
            worker_id="stale-worker",
        )
    recovered_event = store.append(
        "task-fenced",
        {"event": "workflow_interrupted", "data": {}},
        worker_id="recovery-worker",
    )
    assert recovered_event["sequence"] == 1
