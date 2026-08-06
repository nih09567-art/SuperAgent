import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
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
        self._execution_credentials = patch(
            "src.service.web_app.EXECUTION_USER_API_KEYS_JSON",
            '{"u1":"execution-key-u1","u2":"execution-key-u2"}',
        )
        self._execution_credentials.start()

    def tearDown(self):
        self._execution_credentials.stop()
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

    @staticmethod
    def _authorization_headers(
        confirmation_request_id="confirmation-request-1",
        credential="execution-key-u1",
    ):
        return {
            "Authorization": f"Bearer {credential}",
            "Idempotency-Key": confirmation_request_id,
        }

    def _authorized_request(
        self,
        client,
        *,
        confirmation_request_id="confirmation-request-1",
        credential="execution-key-u1",
        **authorization_updates,
    ):
        authorization = client.post(
            "/api/workflows/execution-authorizations",
            json=self._authorization_payload(**authorization_updates),
            headers=self._authorization_headers(
                confirmation_request_id, credential
            ),
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

    def test_concurrent_reservations_expose_one_complete_record(self):
        identity = {
            "task_id": "exec-concurrent",
            "workflow_id": "u1:wf-1",
            "user_query": "execute approved plan",
            "attempt_id": "attempt-concurrent",
            "idempotency_key": "production:concurrent",
            "plan_hash": "a" * 64,
            "user_id": "u1",
            "confirmation_request_id": "confirmation-concurrent",
        }

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(
                executor.map(
                    lambda _index: TaskLogger.reserve_execution(**identity),
                    range(8),
                )
            )

        self.assertEqual(sum(1 for reserved, _task in results if reserved), 1)
        self.assertTrue(all(task is not None for _reserved, task in results))
        self.assertTrue(all(task.status == "reserved" for _reserved, task in results))

    def test_interrupted_reservation_write_leaves_no_corrupt_final_log(self):
        identity = {
            "task_id": "exec-interrupted-write",
            "workflow_id": "u1:wf-1",
            "user_query": "execute approved plan",
            "attempt_id": "attempt-interrupted",
            "idempotency_key": "production:interrupted",
            "plan_hash": "a" * 64,
            "user_id": "u1",
            "confirmation_request_id": "confirmation-interrupted",
        }

        def interrupted_dump(_payload, stream, **_kwargs):
            stream.write('{"task_id":')
            stream.flush()
            raise OSError("simulated process interruption")

        with patch.object(task_logger_mod.json, "dump", side_effect=interrupted_dump):
            with self.assertRaisesRegex(OSError, "simulated process interruption"):
                TaskLogger.reserve_execution(**identity)

        log_file = Path(self._temp_dir.name) / "task_logs" / "exec-interrupted-write.json"
        self.assertFalse(log_file.exists())

        reserved, task = TaskLogger.reserve_execution(**identity)
        self.assertTrue(reserved)
        self.assertIsNotNone(task)
        self.assertEqual(TaskLogger.load(identity["task_id"]).status, "reserved")

    def test_orphaned_partial_temp_file_does_not_lock_confirmation(self):
        logs_dir = Path(self._temp_dir.name) / "task_logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        orphan = logs_dir / ".exec-orphan.json.crashed.tmp"
        orphan.write_text('{"task_id":', encoding="utf-8")

        reserved, task = TaskLogger.reserve_execution(
            task_id="exec-orphan",
            workflow_id="u1:wf-1",
            user_query="execute approved plan",
            attempt_id="attempt-orphan",
            idempotency_key="production:orphan",
            plan_hash="a" * 64,
            user_id="u1",
            confirmation_request_id="confirmation-orphan",
        )

        self.assertTrue(reserved)
        self.assertIsNotNone(task)
        restored = TaskLogger.load("exec-orphan")
        self.assertIsNotNone(restored)
        self.assertEqual(restored.execution_attempt_id, "attempt-orphan")
        self.assertFalse(orphan.exists())

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
        captured_users = []

        async def fake_workflow(request):
            captured_task_ids.append(request.execution_task_id)
            captured_tokens.append(request.execution_authorization_token)
            captured_users.append(request.user_id)
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
                payload["user_id"] = "forged-client-user"
                response = client.post("/api/workflows/run", json=payload)

        self.assertEqual(response.status_code, 200)
        generated_task_id = response.headers.get("X-Task-ID")
        self.assertTrue(generated_task_id.startswith("exec-"))
        self.assertEqual(captured_task_ids, [generated_task_id])
        self.assertEqual(captured_tokens, [None])
        self.assertEqual(captured_users, ["u1"])
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

    def test_authorization_requires_a_server_configured_user_credential(self):
        app = create_app()
        with TestClient(app) as client:
            missing = client.post(
                "/api/workflows/execution-authorizations",
                json=self._authorization_payload(),
                headers={"Idempotency-Key": "confirmation-missing-auth"},
            )
            wrong = client.post(
                "/api/workflows/execution-authorizations",
                json=self._authorization_payload(),
                headers=self._authorization_headers(
                    "confirmation-wrong-auth", "wrong-key"
                ),
            )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(wrong.status_code, 401)

    def test_authorization_fails_closed_when_credentials_are_not_configured(self):
        app = create_app()
        with patch("src.service.web_app.EXECUTION_USER_API_KEYS_JSON", ""):
            with TestClient(app) as client:
                response = client.post(
                    "/api/workflows/execution-authorizations",
                    json=self._authorization_payload(),
                    headers=self._authorization_headers(
                        "confirmation-no-server-config"
                    ),
                )

        self.assertEqual(response.status_code, 503)

    def test_request_body_user_id_cannot_impersonate_workflow_owner(self):
        app = create_app()
        with TestClient(app) as client:
            own_workflow = client.post(
                "/api/workflows/execution-authorizations",
                json=self._authorization_payload(user_id="victim"),
                headers=self._authorization_headers("confirmation-own-workflow"),
            )
            other_workflow = client.post(
                "/api/workflows/execution-authorizations",
                json=self._authorization_payload(
                    user_id="u1", workflow_id="u2:wf-1"
                ),
                headers=self._authorization_headers("confirmation-other-workflow"),
            )

        self.assertEqual(own_workflow.status_code, 200, own_workflow.text)
        task = TaskLogger.load(own_workflow.json()["task_id"])
        self.assertEqual(task.execution_user_id, "u1")
        self.assertEqual(other_workflow.status_code, 403)

    def test_same_confirmation_request_reuses_server_execution_record(self):
        app = create_app()
        with TestClient(app) as client:
            _, first = self._authorized_request(
                client, confirmation_request_id="confirmation-network-retry"
            )
            _, replay = self._authorized_request(
                client, confirmation_request_id="confirmation-network-retry"
            )

        self.assertEqual(replay["task_id"], first["task_id"])
        self.assertEqual(
            replay["execution_attempt_id"], first["execution_attempt_id"]
        )
        self.assertEqual(
            replay["execution_idempotency_key"],
            first["execution_idempotency_key"],
        )
        self.assertEqual(
            replay["execution_authorization_token"],
            first["execution_authorization_token"],
        )

    def test_used_confirmation_request_cannot_authorize_again(self):
        async def fake_workflow(request):
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
                payload, _ = self._authorized_request(
                    client, confirmation_request_id="confirmation-used-request"
                )
                executed = client.post("/api/workflows/run", json=payload)
                replay = client.post(
                    "/api/workflows/execution-authorizations",
                    json=self._authorization_payload(),
                    headers=self._authorization_headers(
                        "confirmation-used-request"
                    ),
                )

        self.assertEqual(executed.status_code, 200)
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(
            replay.json()["detail"]["code"],
            "EXECUTION_CONFIRMATION_ALREADY_USED",
        )

    def test_new_confirmation_can_execute_the_same_plan_again(self):
        app = create_app()
        with TestClient(app) as client:
            _, first = self._authorized_request(
                client, confirmation_request_id="confirmation-first-run"
            )
            first_task = TaskLogger.load(first["task_id"])
            first_task.log_workflow_terminal("FAILED", error="test failure")
            _, second = self._authorized_request(
                client, confirmation_request_id="confirmation-second-run"
            )

        self.assertNotEqual(second["task_id"], first["task_id"])
        self.assertNotEqual(
            second["execution_attempt_id"], first["execution_attempt_id"]
        )
        self.assertNotEqual(
            second["execution_idempotency_key"],
            first["execution_idempotency_key"],
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
                    headers=self._authorization_headers(
                        "confirmation-plan-changed"
                    ),
                )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["code"], "EXECUTION_PLAN_CHANGED"
        )

    def test_expired_reservation_requires_a_new_confirmation(self):
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
                headers=self._authorization_headers(),
            )
            self.assertEqual(blocked_retry.status_code, 409)

            retry = client.post(
                "/api/workflows/execution-authorizations",
                json=self._authorization_payload(),
                headers=self._authorization_headers(
                    "confirmation-after-expiration"
                ),
            )
            self.assertEqual(retry.status_code, 200, retry.text)
            self.assertNotEqual(retry.json()["task_id"], first_identity["task_id"])
            self.assertNotEqual(
                retry.json()["execution_attempt_id"],
                first_identity["execution_attempt_id"],
            )


if __name__ == "__main__":
    unittest.main()
