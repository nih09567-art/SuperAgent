import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import src.robust.task_logger as task_logger_mod
from src.robust.task_logger import TaskLogger
from src.service.server import Server
from src.service.web_app import _canonical_execution_plan_hash, create_app


class ExecutionIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self._original_checkpoints_dir = task_logger_mod.checkpoints_dir
        task_logger_mod.checkpoints_dir = Path(self._temp_dir.name) / "checkpoints"
        self._server_plan_hash = patch(
            "src.service.web_app._server_execution_plan_hash",
            return_value="a" * 64,
        )
        self._server_plan_hash.start()

    def tearDown(self):
        self._server_plan_hash.stop()
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
        }

    @staticmethod
    def _authorization_payload(**updates):
        payload = {
            "user_id": "u1",
            "workflow_id": "u1:wf-1",
            "plan_hash": "a" * 64,
            "user_query": "execute approved plan",
        }
        payload.update(updates)
        return payload

    def _authorized_request(self, client, **authorization_updates):
        authorization = client.post(
            "/api/workflows/execution-authorizations",
            json=self._authorization_payload(**authorization_updates),
        )
        self.assertEqual(authorization.status_code, 200, authorization.text)
        identity = authorization.json()
        payload = self._request_payload()
        payload.update({
            "execution_task_id": identity["task_id"],
            "execution_attempt_id": identity["execution_attempt_id"],
            "execution_idempotency_key": identity["execution_idempotency_key"],
            "execution_plan_hash": identity["execution_plan_hash"],
            "execution_authorization_token": identity[
                "execution_authorization_token"
            ],
        })
        return payload, identity

    def test_server_plan_hash_matches_browser_canonical_json(self):
        self.assertEqual(
            _canonical_execution_plan_hash(
                "u1:wf",
                [{
                    "title": "query",
                    "description": "employee",
                    "agent_name": "RemoteHR",
                    "note": "read",
                }],
            ),
            "0322ffaa8065b3004441fa8b941476f6edf48519ba37fef3fb09ff2769a91c27",
        )

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

    def test_claimed_reservation_activates_without_persisting_token_hash(self):
        token = "server-issued-one-time-token"
        reserved, task = TaskLogger.reserve_execution(
            task_id="exec-activation",
            workflow_id="u1:wf-1",
            user_query="execute approved plan",
            attempt_id="attempt-activation",
            idempotency_key="production:activation",
            plan_hash="a" * 64,
            user_id="u1",
            authorization_token_hash=(
                TaskLogger.hash_execution_authorization_token(token)
            ),
        )
        self.assertTrue(reserved)
        claimed, claimed_task, failure_code = (
            TaskLogger.claim_execution_authorization(
                task_id="exec-activation",
                authorization_token=token,
                user_id="u1",
                workflow_id="u1:wf-1",
                plan_hash="a" * 64,
            )
        )
        self.assertTrue(claimed, failure_code)
        claimed_task.activate_reserved_execution()

        restored = TaskLogger.load("exec-activation")
        self.assertEqual(restored.status, "running")
        self.assertEqual(restored.reservation_expires_at, "")
        self.assertEqual(restored.execution_authorization_token_hash, "")

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
                payload, identity = self._authorized_request(client)
                first = client.post("/api/workflows/run", json=payload)
                second = client.post("/api/workflows/run", json=payload)

        self.assertEqual(first.status_code, 200)
        task_id = first.headers.get("X-Task-ID")
        self.assertTrue(task_id)
        self.assertEqual(calls, [task_id])
        self.assertEqual(second.status_code, 409)
        self.assertEqual(
            second.json()["detail"]["code"],
            "EXECUTION_AUTHORIZATION_ALREADY_CLAIMED",
        )
        self.assertEqual(second.json()["detail"]["task_id"], task_id)
        self.assertEqual(
            second.json()["detail"]["execution_attempt_id"],
            identity["execution_attempt_id"],
        )

    def test_server_issues_internal_execution_task_id(self):
        captured_task_ids = []
        captured_tokens = []

        async def fake_workflow(request):
            captured_task_ids.append(request.execution_task_id)
            captured_tokens.append(request.execution_authorization_token)
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
                payload, identity = self._authorized_request(client)
                response = client.post("/api/workflows/run", json=payload)

        self.assertEqual(response.status_code, 200)
        generated_task_id = response.headers.get("X-Task-ID")
        self.assertTrue(generated_task_id.startswith("exec-"))
        self.assertEqual(captured_task_ids, [generated_task_id])
        self.assertEqual(captured_tokens, [None])
        self.assertEqual(generated_task_id, identity["task_id"])

    def test_missing_server_authorization_cannot_execute_production(self):
        calls = []

        async def fake_workflow(request):
            calls.append(request.execution_task_id)
            if False:
                yield None

        app = create_app()
        with patch.object(Server, "_run_agent_workflow", side_effect=fake_workflow):
            with TestClient(app) as client:
                response = client.post(
                    "/api/workflows/run", json=self._request_payload()
                )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(calls, [])

    def test_changing_client_idempotency_key_cannot_create_another_task(self):
        calls = []

        async def fake_workflow(request):
            calls.append(request.execution_task_id)
            if False:
                yield None

        app = create_app()
        with patch.object(Server, "_run_agent_workflow", side_effect=fake_workflow):
            with TestClient(app) as client:
                payload, identity = self._authorized_request(client)
                payload["execution_idempotency_key"] = "client-selected-new-key"
                response = client.post("/api/workflows/run", json=payload)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json()["detail"]["code"], "EXECUTION_IDENTITY_MISMATCH"
        )
        self.assertEqual(calls, [])
        restored = TaskLogger.load(identity["task_id"])
        self.assertIsNotNone(restored)
        self.assertEqual(restored.status, "reserved")
        self.assertEqual(restored.execution_authorization_claimed_at, "")

    def test_wrong_server_authorization_token_is_rejected_without_claiming(self):
        app = create_app()
        with TestClient(app) as client:
            payload, identity = self._authorized_request(client)
            payload["execution_authorization_token"] = "wrong-token"
            response = client.post("/api/workflows/run", json=payload)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json()["detail"]["code"],
            "EXECUTION_AUTHORIZATION_MISMATCH",
        )
        restored = TaskLogger.load(identity["task_id"])
        self.assertEqual(restored.status, "reserved")
        self.assertEqual(restored.execution_authorization_claimed_at, "")

    def test_unknown_client_task_id_cannot_execute(self):
        app = create_app()
        with TestClient(app) as client:
            payload, _ = self._authorized_request(client)
            payload["execution_task_id"] = "exec-client-selected"
            response = client.post("/api/workflows/run", json=payload)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json()["detail"]["code"],
            "EXECUTION_TASK_ID_MISMATCH",
        )

    def test_confirmation_rejects_plan_that_changed_on_server(self):
        app = create_app()
        with patch(
            "src.service.web_app._server_execution_plan_hash",
            return_value="b" * 64,
        ):
            with TestClient(app) as client:
                response = client.post(
                    "/api/workflows/execution-authorizations",
                    json=self._authorization_payload(),
                )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["code"], "EXECUTION_PLAN_CHANGED"
        )

    def test_expired_reservation_is_failed_and_can_be_explicitly_retried(self):
        app = create_app()
        with TestClient(app) as client:
            _, first_identity = self._authorized_request(client)
            task = TaskLogger.load(first_identity["task_id"])
            self.assertIsNotNone(task)
            task.reservation_expires_at = (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).isoformat()
            task._flush()

            recovered = client.get(
                f"/api/tasks/{first_identity['task_id']}/log"
            )
            self.assertEqual(recovered.status_code, 200)
            self.assertEqual(recovered.json()["status"], "FAILED")
            self.assertEqual(
                recovered.json()["reservation_failure_code"],
                "RESERVATION_EXPIRED",
            )
            self.assertNotIn(
                "execution_authorization_token_hash", recovered.json()
            )

            blocked_retry = client.post(
                "/api/workflows/execution-authorizations",
                json=self._authorization_payload(),
            )
            self.assertEqual(blocked_retry.status_code, 409)
            self.assertTrue(blocked_retry.json()["detail"]["retry_allowed"])

            retry = client.post(
                "/api/workflows/execution-authorizations",
                json=self._authorization_payload(retry_expired=True),
            )
            self.assertEqual(retry.status_code, 200, retry.text)
            self.assertEqual(retry.json()["task_id"], first_identity["task_id"])
            self.assertNotEqual(
                retry.json()["execution_attempt_id"],
                first_identity["execution_attempt_id"],
            )


if __name__ == "__main__":
    unittest.main()
