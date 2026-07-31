import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import src.robust.task_logger as task_logger_mod
from src.robust.task_logger import TaskLogger
from src.service.server import Server
from src.service.web_app import create_app


class ExecutionIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self._original_checkpoints_dir = task_logger_mod.checkpoints_dir
        task_logger_mod.checkpoints_dir = Path(self._temp_dir.name) / "checkpoints"

    def tearDown(self):
        task_logger_mod.checkpoints_dir = self._original_checkpoints_dir
        self._temp_dir.cleanup()

    @staticmethod
    def _request_payload():
        return {
            "user_id": "u1",
            "lang": "zh",
            "messages": [{"role": "user", "content": "execute approved plan"}],
            "debug": False,
            "deep_thinking_mode": False,
            "search_before_planning": False,
            "coor_agents": [],
            "workmode": "production",
            "workflow_id": "u1:wf-1",
            "execution_attempt_id": "attempt-1",
            "execution_idempotency_key": "conversation-1:attempt-1",
            "execution_plan_hash": "a" * 64,
        }

    def test_task_logger_reservation_is_atomic_and_persists_identity(self):
        identity = {
            "task_id": "exec-atomic",
            "workflow_id": "u1:wf-1",
            "user_query": "execute approved plan",
            "attempt_id": "attempt-1",
            "idempotency_key": "conversation-1:attempt-1",
            "plan_hash": "a" * 64,
        }
        first_reserved, first = TaskLogger.reserve_execution(**identity)
        second_reserved, second = TaskLogger.reserve_execution(**identity)

        self.assertTrue(first_reserved)
        self.assertFalse(second_reserved)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(second.status, "reserved")
        self.assertEqual(second.execution_attempt_id, "attempt-1")
        self.assertEqual(second.execution_plan_hash, "a" * 64)

    def test_reserved_task_can_be_finalized_before_workflow_activation(self):
        reserved, task = TaskLogger.reserve_execution(
            task_id="exec-preparation-failed",
            workflow_id="u1:wf-1",
            user_query="execute approved plan",
            attempt_id="attempt-1",
            idempotency_key="conversation-1:attempt-1",
            plan_hash="a" * 64,
        )

        self.assertTrue(reserved)
        self.assertIsNotNone(task)
        task.log_workflow_terminal("FAILED", error="workflow preparation failed")

        restored = TaskLogger.load("exec-preparation-failed")
        self.assertIsNotNone(restored)
        self.assertEqual(restored.status, "FAILED")
        self.assertEqual(restored.error, "workflow preparation failed")

    def test_duplicate_production_request_returns_existing_task_without_rerun(self):
        calls = []

        async def fake_workflow(request):
            calls.append(request.execution_task_id)
            yield {
                "event": "start_of_workflow",
                "data": {
                    "task_id": request.execution_task_id,
                    "workflow_id": request.workflow_id,
                },
            }

        app = create_app()
        with patch.object(Server, "_run_agent_workflow", side_effect=fake_workflow):
            with TestClient(app) as client:
                first = client.post("/api/workflows/run", json=self._request_payload())
                second_payload = self._request_payload()
                second_payload["execution_attempt_id"] = "attempt-from-second-tab"
                second = client.post("/api/workflows/run", json=second_payload)

        self.assertEqual(first.status_code, 200)
        task_id = first.headers.get("X-Task-ID")
        self.assertTrue(task_id)
        self.assertEqual(calls, [task_id])
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["detail"]["code"], "DUPLICATE_EXECUTION")
        self.assertEqual(second.json()["detail"]["task_id"], task_id)
        self.assertEqual(second.json()["detail"]["execution_attempt_id"], "attempt-1")

    def test_client_cannot_choose_internal_execution_task_id(self):
        captured_task_ids = []

        async def fake_workflow(request):
            captured_task_ids.append(request.execution_task_id)
            yield {
                "event": "start_of_workflow",
                "data": {
                    "task_id": request.execution_task_id,
                    "workflow_id": request.workflow_id,
                },
            }

        payload = self._request_payload()
        payload["execution_task_id"] = "../../client-selected-task"
        app = create_app()
        with patch.object(Server, "_run_agent_workflow", side_effect=fake_workflow):
            with TestClient(app) as client:
                response = client.post("/api/workflows/run", json=payload)

        self.assertEqual(response.status_code, 200)
        generated_task_id = response.headers.get("X-Task-ID")
        self.assertTrue(generated_task_id.startswith("exec-"))
        self.assertEqual(captured_task_ids, [generated_task_id])
        self.assertNotEqual(generated_task_id, payload["execution_task_id"])


if __name__ == "__main__":
    unittest.main()
