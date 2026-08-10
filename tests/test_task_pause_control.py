from fastapi.testclient import TestClient
import pytest

import src.robust.task_logger as task_logger_module
from src.robust.task_control import TaskControlStore
from src.robust.task_logger import TaskLogger
from src.service.web_app import create_app


@pytest.fixture(autouse=True)
def _isolated_pause_stores(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_CONTROL_STORE_DIR", str(tmp_path / "task_controls"))
    monkeypatch.setenv("GOVERNANCE_EVENT_STORE_DIR", str(tmp_path / "governance"))
    monkeypatch.setattr(
        task_logger_module,
        "_get_task_logs_dir",
        lambda: tmp_path / "task_logs",
    )


def test_task_control_pause_resume_lifecycle_is_persistent():
    store = TaskControlStore()
    running = store.ensure_running(
        "task-control",
        workflow_id="u1:wf",
        user_id="u1",
    )
    assert running["state"] == "RUNNING"

    requested = store.request_pause("task-control", user_id="u1")
    assert requested["state"] == "PAUSE_REQUESTED"
    assert store.pause_requested("task-control") is True

    paused = store.mark_paused(
        "task-control",
        checkpoint_step=2,
        resume_step=3,
        completed_steps=["s1", "s2"],
    )
    assert paused["state"] == "PAUSED"
    assert paused["resume_step"] == 3

    resumed = store.resume("task-control", user_id="u1")
    assert resumed["state"] == "RUNNING"
    assert resumed["resumed_at"]


def test_pause_api_is_idempotent_and_enforces_task_owner():
    task = TaskLogger("task-api-pause", "u1:wf", "pause me")
    task.execution_user_id = "u1"
    task.log_workflow_start("pause me")
    client = TestClient(create_app())

    forbidden = client.post(
        "/api/tasks/task-api-pause/pause",
        json={"user_id": "u2"},
    )
    assert forbidden.status_code == 403

    first = client.post(
        "/api/tasks/task-api-pause/pause",
        json={"user_id": "u1", "reason": "demo"},
    )
    second = client.post(
        "/api/tasks/task-api-pause/pause",
        json={"user_id": "u1", "reason": "demo"},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["state"] == "PAUSE_REQUESTED"
    assert second.json()["pause_requested_at"] == first.json()["pause_requested_at"]
    control = client.get("/api/tasks/task-api-pause/control")
    assert control.status_code == 200
    assert control.json()["state"] == "PAUSE_REQUESTED"
