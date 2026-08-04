from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
import pytest
import threading
from types import SimpleNamespace

import src.service.web_app as web_app
import src.orchestration.reconciliation as reconciliation_module

from src.orchestration.completion import (
    PersistentReceiptStore,
    idempotency_key,
    normalize_input,
    validate_receipt,
)
from src.orchestration.reconciliation import (
    ReconciliationStore,
    get_reconciliation_store,
)
from src.orchestration.governance import record_governance_event
from src.robust.task_logger import TaskLogger
from src.security.approval import get_approval_store
from src.service.web_app import create_app


@pytest.fixture(autouse=True)
def _configured_governance_identity(monkeypatch):
    monkeypatch.setattr(web_app, "GOVERNANCE_ADMIN_ACTOR_ID", "admin")


def _client() -> TestClient:
    return TestClient(create_app())


def _approval():
    return get_approval_store().create(
        user_id="u1",
        workflow_id="wf-1",
        task_id="task-1",
        resume_step=2,
        node_name="A",
        step_id="s1",
        subject={"subject_type": "user", "id": "u1", "attributes": {}},
        object={"object_type": "agent", "id": "A", "attributes": {}},
        scenario={},
        action={"verb": "dispatch", "attributes": {"action_type": "write"}},
        policy_result={
            "decision": "REVIEW_REQUIRED",
            "reason": "approval needed",
        },
    )


def test_approval_api_lists_approves_and_exposes_resume_contract(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("APPROVAL_STORE_DIR", str(tmp_path / "approvals"))
    monkeypatch.setenv(
        "GOVERNANCE_EVENT_STORE_DIR", str(tmp_path / "governance")
    )
    approval = _approval()
    client = _client()

    listed = client.get(
        "/api/security/approvals",
        params={"requester_id": "admin", "status": "pending"},
    )
    assert listed.status_code == 200
    assert [item["approval_id"] for item in listed.json()] == [
        approval.approval_id
    ]

    approved = client.post(
        f"/api/security/approvals/{approval.approval_id}/approve",
        json={"approver": "admin", "comment": "ok"},
    )
    assert approved.status_code == 200
    body = approved.json()
    assert body["status"] == "approved"
    assert body["resume_endpoint"] == "/api/tasks/resume"
    assert body["resume_request"] == {
        "task_id": "task-1",
        "resume_step": 2,
        "user_id": "u1",
        "workmode": "production",
    }

    timeline = client.get("/api/tasks/task-1/governance")
    assert timeline.status_code == 200
    assert timeline.json()[-1]["event_type"] == "APPROVAL_GRANTED"


def test_approval_api_rejects_request(tmp_path, monkeypatch):
    monkeypatch.setenv("APPROVAL_STORE_DIR", str(tmp_path / "approvals"))
    monkeypatch.setenv(
        "GOVERNANCE_EVENT_STORE_DIR", str(tmp_path / "governance")
    )
    approval = _approval()
    client = _client()

    rejected = client.post(
        f"/api/security/approvals/{approval.approval_id}/reject",
        json={"approver": "admin", "comment": "not allowed"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"

    again = client.post(
        f"/api/security/approvals/{approval.approval_id}/approve",
        json={"approver": "admin", "comment": ""},
    )
    assert again.status_code == 409


def _reconciliation(
    tmp_path,
    monkeypatch,
    *,
    task_id="task-recon",
    expected_schema_refs=None,
):
    monkeypatch.setenv(
        "RECONCILIATION_STORE_DIR", str(tmp_path / "reconciliations")
    )
    monkeypatch.setenv("RECEIPT_STORE_DIR", str(tmp_path / "receipts"))
    monkeypatch.setenv(
        "GOVERNANCE_EVENT_STORE_DIR", str(tmp_path / "governance")
    )
    inputs = {"employee": "李娜"}
    key = idempotency_key(task_id, "write-1", inputs)
    receipts = PersistentReceiptStore(task_id)
    claim = receipts.claim_if_absent(
        key,
        {
            "idempotency_key": key,
            "task_id": task_id,
            "step_id": "write-1",
            "agent": "DocumentAgent",
            "status": "STARTED",
            "normalized_input": normalize_input(inputs),
            "external_op_id": None,
            "expected_outputs": ["document_id"],
            "expected_schema_refs": dict(expected_schema_refs or {}),
            "timestamp": 1.0,
        },
    )
    reconciliation = get_reconciliation_store().create(
        user_id="u1",
        workflow_id="wf-recon",
        task_id=task_id,
        step_id="write-1",
        resume_step=2,
        agent_name="DocumentAgent",
        error="external result unknown",
        idempotency_key=key,
        claim_id=claim.claim_id or "",
        receipt=claim.receipt,
        expected_outputs=["document_id"],
        expected_schema_refs=dict(expected_schema_refs or {}),
    )
    return reconciliation, key


def test_reconciliation_api_releases_receipt_then_exposes_resume(
    tmp_path, monkeypatch
):
    reconciliation, key = _reconciliation(tmp_path, monkeypatch)
    client = _client()

    listed = client.get(
        "/api/security/reconciliations",
        params={"requester_id": "admin", "status": "pending"},
    )
    assert listed.status_code == 200
    assert [item["reconciliation_id"] for item in listed.json()] == [
        reconciliation.reconciliation_id
    ]

    retried = client.post(
        (
            "/api/security/reconciliations/"
            f"{reconciliation.reconciliation_id}/retry"
        ),
        json={"operator": "admin", "comment": "外部目录中没有新文件"},
    )
    assert retried.status_code == 200
    body = retried.json()
    assert body["status"] == "retry_ready"
    assert body["resume_request"]["resume_step"] == 2
    assert PersistentReceiptStore("task-recon").get(key) is None

    timeline = client.get("/api/tasks/task-recon/governance").json()
    assert timeline[-1]["decision"] == "SAFE_TO_RETRY"


def test_reconciliation_resume_claim_is_single_use_and_consumed_on_success(
    tmp_path, monkeypatch
):
    reconciliation, _ = _reconciliation(tmp_path, monkeypatch)
    store = get_reconciliation_store()
    store.resolve(
        reconciliation.reconciliation_id,
        status="retry_ready",
        operator="admin",
        comment="verified not executed",
    )

    claimed = store.claim_for_resume(
        task_id=reconciliation.task_id,
        resume_step=reconciliation.resume_step,
        operator="admin",
    )
    assert claimed is not None
    assert claimed.status == "resuming"
    with pytest.raises(ValueError, match="already in progress"):
        store.claim_for_resume(
            task_id=reconciliation.task_id,
            resume_step=reconciliation.resume_step,
            operator="admin",
        )

    completed = store.finish_resume(
        reconciliation.reconciliation_id,
        resume_claim_id=claimed.resolution["resume_claim_id"],
        succeeded=True,
    )
    assert completed.status == "consumed"
    assert completed.resolution["resume_succeeded"] is True
    assert (
        store.claim_for_resume(
            task_id=reconciliation.task_id,
            resume_step=reconciliation.resume_step,
            operator="admin",
        )
        is None
    )


def test_reconciliation_resume_claim_is_atomic_across_store_instances(
    tmp_path,
) -> None:
    base_dir = tmp_path / "reconciliations"
    setup_store = ReconciliationStore(base_dir)
    request = setup_store.create(
        user_id="u1",
        workflow_id="wf-recon",
        task_id="task-cross-store",
        step_id="write-1",
        resume_step=2,
        agent_name="DocumentAgent",
        error="external result unknown",
    )
    setup_store.resolve(
        request.reconciliation_id,
        status="retry_ready",
        operator="admin",
    )
    store_a = ReconciliationStore(base_dir)
    store_b = ReconciliationStore(base_dir)
    barrier = threading.Barrier(2)

    def claim(store):
        barrier.wait()
        try:
            result = store.claim_for_resume(
                task_id="task-cross-store",
                resume_step=2,
                operator="admin",
            )
            return result.status if result else "none"
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(claim, (store_a, store_b)))

    assert outcomes.count("resuming") == 1
    assert sum("already in progress" in value for value in outcomes) == 1


def test_failed_reconciliation_resume_returns_to_ready_state(tmp_path, monkeypatch):
    reconciliation, _ = _reconciliation(tmp_path, monkeypatch)
    store = get_reconciliation_store()
    store.resolve(
        reconciliation.reconciliation_id,
        status="retry_ready",
        operator="admin",
    )
    claimed = store.claim_for_resume(
        task_id=reconciliation.task_id,
        resume_step=reconciliation.resume_step,
        operator="admin",
    )

    restored = store.finish_resume(
        reconciliation.reconciliation_id,
        resume_claim_id=claimed.resolution["resume_claim_id"],
        succeeded=False,
    )

    assert restored.status == "retry_ready"
    assert restored.resolution["resume_succeeded"] is False


def test_expired_resume_claim_is_reclaimed_after_process_restart(
    tmp_path, monkeypatch
) -> None:
    base_dir = tmp_path / "reconciliations"
    clock = SimpleNamespace(now=1_800_000_000.0)
    monkeypatch.setattr(
        reconciliation_module,
        "time",
        SimpleNamespace(time=lambda: clock.now),
    )
    setup_store = ReconciliationStore(base_dir)
    request = setup_store.create(
        user_id="u1",
        workflow_id="wf-recon",
        task_id="task-restart",
        step_id="write-1",
        resume_step=2,
        agent_name="DocumentAgent",
        error="external result unknown",
    )
    setup_store.resolve(
        request.reconciliation_id,
        status="retry_ready",
        operator="admin",
    )
    first_claim = setup_store.claim_for_resume(
        task_id=request.task_id,
        resume_step=request.resume_step,
        operator="worker-a",
        lease_seconds=30,
    )

    restarted_store = ReconciliationStore(base_dir)
    with pytest.raises(ValueError, match="already in progress"):
        restarted_store.claim_for_resume(
            task_id=request.task_id,
            resume_step=request.resume_step,
            operator="worker-b",
            lease_seconds=30,
        )

    clock.now += 31
    second_claim = restarted_store.claim_for_resume(
        task_id=request.task_id,
        resume_step=request.resume_step,
        operator="worker-b",
        lease_seconds=30,
    )

    assert second_claim.status == "resuming"
    assert (
        second_claim.resolution["resume_claim_id"]
        != first_claim.resolution["resume_claim_id"]
    )
    assert (
        second_claim.resolution["resume_reclaimed_claim_id"]
        == first_claim.resolution["resume_claim_id"]
    )
    with pytest.raises(ValueError, match="claim id mismatch"):
        setup_store.finish_resume(
            request.reconciliation_id,
            resume_claim_id=first_claim.resolution["resume_claim_id"],
            succeeded=True,
        )

    completed = restarted_store.finish_resume(
        request.reconciliation_id,
        resume_claim_id=second_claim.resolution["resume_claim_id"],
        succeeded=True,
    )
    assert completed.status == "consumed"


def test_governance_queue_reaps_expired_resume_claim_and_audits_it(
    tmp_path, monkeypatch
) -> None:
    reconciliation, _ = _reconciliation(
        tmp_path, monkeypatch, task_id="task-expired-resume"
    )
    clock = SimpleNamespace(now=1_800_000_000.0)
    monkeypatch.setattr(
        reconciliation_module,
        "time",
        SimpleNamespace(time=lambda: clock.now),
    )
    store = get_reconciliation_store()
    store.resolve(
        reconciliation.reconciliation_id,
        status="retry_ready",
        operator="admin",
    )
    claimed = store.claim_for_resume(
        task_id=reconciliation.task_id,
        resume_step=reconciliation.resume_step,
        operator="worker-a",
        lease_seconds=30,
    )
    clock.now += 31

    response = _client().get(
        "/api/security/reconciliations",
        params={"task_id": reconciliation.task_id},
    )

    assert response.status_code == 200
    assert response.json()[0]["status"] == "retry_ready"
    restored = store.get(reconciliation.reconciliation_id)
    assert restored.resolution["resume_reaped_claim_id"] == (
        claimed.resolution["resume_claim_id"]
    )
    timeline = _client().get(
        f"/api/tasks/{reconciliation.task_id}/governance"
    ).json()
    assert timeline[-1]["event_type"] == "RECONCILIATION_RESUME_LEASE_EXPIRED"
    assert timeline[-1]["decision"] == "RESTORED_TO_READY"


def test_resume_heartbeat_renews_lease_across_store_instances(
    tmp_path, monkeypatch
) -> None:
    base_dir = tmp_path / "reconciliations"
    clock = SimpleNamespace(now=1_800_000_000.0)
    monkeypatch.setattr(
        reconciliation_module,
        "time",
        SimpleNamespace(time=lambda: clock.now),
    )
    worker_store = ReconciliationStore(base_dir)
    request = worker_store.create(
        user_id="u1",
        workflow_id="wf-recon",
        task_id="task-heartbeat",
        step_id="write-1",
        resume_step=2,
        agent_name="DocumentAgent",
        error="external result unknown",
    )
    worker_store.resolve(
        request.reconciliation_id,
        status="retry_ready",
        operator="admin",
    )
    claim = worker_store.claim_for_resume(
        task_id=request.task_id,
        resume_step=request.resume_step,
        operator="worker-a",
        lease_seconds=30,
    )
    claim_id = claim.resolution["resume_claim_id"]

    clock.now += 20
    renewed = worker_store.renew_resume_claim(
        request.reconciliation_id,
        resume_claim_id=claim_id,
        lease_seconds=30,
    )
    assert renewed.resolution["resume_lease_expires_at"] == clock.now + 30

    clock.now += 11
    restarted_store = ReconciliationStore(base_dir)
    with pytest.raises(ValueError, match="already in progress"):
        restarted_store.claim_for_resume(
            task_id=request.task_id,
            resume_step=request.resume_step,
            operator="worker-b",
            lease_seconds=30,
        )

    clock.now += 20
    reclaimed = restarted_store.claim_for_resume(
        task_id=request.task_id,
        resume_step=request.resume_step,
        operator="worker-b",
        lease_seconds=30,
    )
    assert reclaimed.resolution["resume_claim_id"] != claim_id


def _patch_resume_dependencies(monkeypatch, events):
    import src.robust.checkpoint as checkpoint_module

    checkpoint = SimpleNamespace(
        workflow_id="wf-recon",
        state={
            "messages": [{"role": "user", "content": "resume"}],
            "workflow_mode": "production",
            "coor_agents": [],
        },
    )

    class FakeCheckpointManager:
        def load_checkpoint(self, **_kwargs):
            return checkpoint

    class FakeServer:
        async def _run_agent_workflow_with_resume(self, *_args, **_kwargs):
            for event in events:
                if isinstance(event, Exception):
                    raise event
                yield event

    monkeypatch.setattr(
        checkpoint_module, "CheckpointManager", FakeCheckpointManager
    )
    monkeypatch.setattr(web_app, "Server", FakeServer)


@pytest.mark.parametrize("terminal_status", ["FAILED", "NEEDS_RECONCILIATION"])
def test_resume_api_restores_reconciliation_after_failed_terminal(
    tmp_path, monkeypatch, terminal_status
) -> None:
    reconciliation, _ = _reconciliation(
        tmp_path, monkeypatch, task_id=f"task-{terminal_status.lower()}"
    )
    store = get_reconciliation_store()
    store.resolve(
        reconciliation.reconciliation_id,
        status="confirmed_succeeded",
        operator="admin",
    )
    _patch_resume_dependencies(
        monkeypatch,
        [
            {
                "event": "end_of_workflow",
                "data": {
                    "task_id": reconciliation.task_id,
                    "status": terminal_status,
                },
            }
        ],
    )

    response = _client().post(
        "/api/tasks/resume",
        json={
            "task_id": reconciliation.task_id,
            "resume_step": reconciliation.resume_step,
            "user_id": "admin",
        },
    )

    assert response.status_code == 200
    restored = store.get(reconciliation.reconciliation_id)
    assert restored.status == "confirmed_succeeded"
    assert restored.resolution["resume_succeeded"] is False


def test_resume_api_consumes_reconciliation_only_after_successful_terminal(
    tmp_path, monkeypatch
) -> None:
    reconciliation, _ = _reconciliation(
        tmp_path, monkeypatch, task_id="task-resume-success"
    )
    store = get_reconciliation_store()
    store.resolve(
        reconciliation.reconciliation_id,
        status="confirmed_succeeded",
        operator="admin",
    )
    _patch_resume_dependencies(
        monkeypatch,
        [
            {
                "event": "end_of_workflow",
                "data": {
                    "task_id": reconciliation.task_id,
                    "status": "SUCCEEDED",
                },
            }
        ],
    )

    response = _client().post(
        "/api/tasks/resume",
        json={
            "task_id": reconciliation.task_id,
            "resume_step": reconciliation.resume_step,
            "user_id": "admin",
        },
    )

    assert response.status_code == 200
    assert store.get(reconciliation.reconciliation_id).status == "consumed"


def test_resume_api_restores_reconciliation_when_stream_raises(
    tmp_path, monkeypatch
) -> None:
    reconciliation, _ = _reconciliation(
        tmp_path, monkeypatch, task_id="task-resume-error"
    )
    store = get_reconciliation_store()
    store.resolve(
        reconciliation.reconciliation_id,
        status="confirmed_succeeded",
        operator="admin",
    )
    _patch_resume_dependencies(monkeypatch, [RuntimeError("resume failed")])

    with pytest.raises(Exception, match="resume failed"):
        _client().post(
            "/api/tasks/resume",
            json={
                "task_id": reconciliation.task_id,
                "resume_step": reconciliation.resume_step,
                "user_id": "admin",
            },
        )

    restored = store.get(reconciliation.reconciliation_id)
    assert restored.status == "confirmed_succeeded"
    assert restored.resolution["resume_succeeded"] is False


def test_resume_api_restores_reconciliation_when_client_disconnects(
    tmp_path, monkeypatch
) -> None:
    reconciliation, _ = _reconciliation(
        tmp_path, monkeypatch, task_id="task-resume-disconnect"
    )
    store = get_reconciliation_store()
    store.resolve(
        reconciliation.reconciliation_id,
        status="confirmed_succeeded",
        operator="admin",
    )
    _patch_resume_dependencies(
        monkeypatch,
        [{"event": "message", "data": {"task_id": reconciliation.task_id}}],
    )

    async def disconnected(_request):
        return True

    monkeypatch.setattr(web_app.Request, "is_disconnected", disconnected)
    response = _client().post(
        "/api/tasks/resume",
        json={
            "task_id": reconciliation.task_id,
            "resume_step": reconciliation.resume_step,
            "user_id": "admin",
        },
    )

    assert response.status_code == 200
    restored = store.get(reconciliation.reconciliation_id)
    assert restored.status == "confirmed_succeeded"
    assert restored.resolution["resume_succeeded"] is False


def test_reconciliation_api_confirms_success_with_external_operation_id(
    tmp_path, monkeypatch
):
    reconciliation, key = _reconciliation(tmp_path, monkeypatch)
    client = _client()

    missing_id = client.post(
        (
            "/api/security/reconciliations/"
            f"{reconciliation.reconciliation_id}/succeeded"
        ),
        json={"operator": "admin"},
    )
    assert missing_id.status_code == 422

    succeeded = client.post(
        (
            "/api/security/reconciliations/"
            f"{reconciliation.reconciliation_id}/succeeded"
        ),
        json={
            "operator": "admin",
            "comment": "已在文档平台找到文件",
            "external_operation_id": "doc-20260729-001",
            "outputs": {"document_id": "doc-20260729-001"},
        },
    )
    assert succeeded.status_code == 200
    assert succeeded.json()["status"] == "confirmed_succeeded"
    receipt = PersistentReceiptStore("task-recon").get(key)
    assert validate_receipt(receipt, key=key)
    assert receipt["confirmed_by"] == "admin"


def test_reconciliation_api_rejects_success_without_contract_outputs(
    tmp_path, monkeypatch
):
    reconciliation, key = _reconciliation(tmp_path, monkeypatch)

    response = _client().post(
        (
            "/api/security/reconciliations/"
            f"{reconciliation.reconciliation_id}/succeeded"
        ),
        json={"external_operation_id": "doc-1", "outputs": {}},
    )

    assert response.status_code == 409
    assert "document_id" in response.json()["detail"]
    assert PersistentReceiptStore("task-recon").get(key)["status"] == "STARTED"


def test_reconciliation_api_validates_confirmed_output_schema(
    tmp_path, monkeypatch
):
    reconciliation, key = _reconciliation(
        tmp_path,
        monkeypatch,
        expected_schema_refs={"document_id": "markdown_text_result@v1"},
    )

    response = _client().post(
        (
            "/api/security/reconciliations/"
            f"{reconciliation.reconciliation_id}/succeeded"
        ),
        json={
            "external_operation_id": "doc-1",
            "outputs": {"document_id": {"not": "a string"}},
        },
    )

    assert response.status_code == 409
    assert "failed schema" in response.json()["detail"]
    assert PersistentReceiptStore("task-recon").get(key)["status"] == "STARTED"


def test_reconciliation_api_repairs_succeeded_or_legacy_receipt_outputs(
    tmp_path, monkeypatch
):
    reconciliation, key = _reconciliation(
        tmp_path,
        monkeypatch,
        expected_schema_refs={"document_id": "markdown_text_result@v1"},
    )
    receipts = PersistentReceiptStore(reconciliation.task_id)
    succeeded = dict(receipts.get(key) or {})
    succeeded.update(
        {
            "status": "SUCCEEDED",
            "external_op_id": "doc-existing",
            # Model both the pre-Artifact-checkpoint crash and the stale
            # pre-outputs_kind format. The trusted reconciliation request must
            # be able to replace these unusable outputs without retrying.
            "outputs": {
                "document_id": {"artifact_id": "missing", "version": 1}
            },
        }
    )
    succeeded.pop("outputs_kind", None)
    succeeded["expected_schema_refs"] = {
        "document_id": "document_generation_result@v1"
    }
    receipts.put(key, succeeded)
    client = _client()

    retry = client.post(
        f"/api/security/reconciliations/{reconciliation.reconciliation_id}/retry",
        json={"comment": "must not resend"},
    )
    assert retry.status_code == 409

    repaired = client.post(
        f"/api/security/reconciliations/{reconciliation.reconciliation_id}/succeeded",
        json={
            "external_operation_id": "doc-existing",
            "outputs": {"document_id": "recovered document id"},
        },
    )

    assert repaired.status_code == 200
    receipt = PersistentReceiptStore(reconciliation.task_id).get(key)
    assert receipt["status"] == "SUCCEEDED"
    assert receipt["outputs_kind"] == "confirmed_payloads"
    assert receipt["outputs"] == {"document_id": "recovered document id"}
    assert receipt["expected_schema_refs"] == {
        "document_id": "markdown_text_result@v1"
    }


def test_reconciliation_api_can_freeze_and_terminate(tmp_path, monkeypatch):
    reconciliation, key = _reconciliation(tmp_path, monkeypatch)
    client = _client()

    frozen = client.post(
        (
            "/api/security/reconciliations/"
            f"{reconciliation.reconciliation_id}/freeze"
        ),
        json={"operator": "admin", "comment": "等待供应商回执"},
    )
    assert frozen.status_code == 200
    assert frozen.json()["status"] == "frozen"

    terminated = client.post(
        (
            "/api/security/reconciliations/"
            f"{reconciliation.reconciliation_id}/terminate"
        ),
        json={"operator": "admin", "comment": "业务取消"},
    )
    assert terminated.status_code == 200
    assert terminated.json()["status"] == "terminated"
    assert PersistentReceiptStore("task-recon").get(key)["status"] == "STARTED"


def test_deleting_conversation_cascades_runtime_and_security_records(
    tmp_path, monkeypatch
):
    import src.robust.checkpoint as checkpoint_module
    import src.robust.task_logger as task_logger_module

    checkpoint_root = tmp_path / "checkpoints"
    monkeypatch.setattr(task_logger_module, "checkpoints_dir", checkpoint_root)
    monkeypatch.setattr(checkpoint_module, "checkpoints_dir", checkpoint_root)
    monkeypatch.setenv(
        "RECONCILIATION_STORE_DIR", str(tmp_path / "reconciliations")
    )
    monkeypatch.setenv("APPROVAL_STORE_DIR", str(tmp_path / "approvals"))
    monkeypatch.setenv("RECEIPT_STORE_DIR", str(tmp_path / "receipts"))
    monkeypatch.setenv(
        "ARTIFACT_PAYLOAD_STORE_DIR", str(tmp_path / "artifacts")
    )
    monkeypatch.setenv(
        "GOVERNANCE_EVENT_STORE_DIR", str(tmp_path / "governance")
    )

    task_id = "u1_demo__20260729_120000"
    workflow_id = "u1:demo"
    task_log = TaskLogger(task_id, workflow_id, "test")
    task_log.log_workflow_start("test")
    checkpoint_dir = checkpoint_root / task_id
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    (checkpoint_dir / "0_scheduler.json").write_text("{}", encoding="utf-8")
    (tmp_path / "receipts").mkdir(parents=True, exist_ok=True)
    receipt = PersistentReceiptStore(task_id)
    receipt.put("key", {"status": "STARTED"})
    record_governance_event(
        "WORKFLOW_STARTED",
        task_id=task_id,
        workflow_id=workflow_id,
    )
    get_reconciliation_store().create(
        user_id="u1",
        workflow_id=workflow_id,
        task_id=task_id,
        step_id="send",
        resume_step=1,
        agent_name="RemoteEmailDispatchAgent",
        error="outcome unknown",
    )
    get_approval_store().create(
        user_id="u1",
        workflow_id=workflow_id,
        task_id=task_id,
        resume_step=1,
        node_name="RemoteEmailDispatchAgent",
        subject={"id": "u1"},
        object={"id": "RemoteEmailDispatchAgent"},
        scenario={},
        action={"verb": "dispatch"},
        policy_result={"decision": "REVIEW_REQUIRED"},
    )

    client = _client()
    response = client.delete(
        f"/api/tasks/{task_id}",
        params={"user_id": "admin"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["business_outputs_preserved"] is True
    assert TaskLogger.load(task_id) is None
    assert not checkpoint_dir.exists()
    assert get_reconciliation_store().list(task_id=task_id) == []
    assert get_approval_store().list(task_id=task_id) == []


def test_deleting_legacy_conversation_removes_orphan_security_records_only(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "RECONCILIATION_STORE_DIR", str(tmp_path / "reconciliations")
    )
    monkeypatch.setenv("APPROVAL_STORE_DIR", str(tmp_path / "approvals"))
    get_reconciliation_store().create(
        user_id="u1",
        workflow_id="u1:demo",
        task_id="legacy-task",
        step_id="send",
        resume_step=1,
        agent_name="RemoteEmailDispatchAgent",
        error="outcome unknown",
    )

    response = _client().delete(
        "/api/conversation-history",
        params={"workflow_id": "u1:demo", "user_id": "u1"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["deleted_tasks"] == 0
    assert body["deleted"]["reconciliations"] == 1
    assert body["business_outputs_preserved"] is True
    assert get_reconciliation_store().list(task_id="legacy-task") == []


def test_workflow_run_requires_no_browser_cleanup_credential(monkeypatch):
    class FakeServer:
        async def _run_agent_workflow(self, _body):
            yield {
                "event": "start_of_workflow",
                "data": {"workflow_id": "u1:wf", "task_id": "task-created"},
            }

    monkeypatch.setattr(web_app, "Server", FakeServer)

    response = TestClient(create_app()).post(
        "/api/workflows/run",
        json={
            "user_id": "u1",
            "lang": "zh",
            "messages": [{"role": "user", "content": "test"}],
            "debug": False,
            "deep_thinking_mode": False,
            "search_before_planning": False,
            "coor_agents": None,
            "workmode": "production",
            "workflow_id": "u1:wf",
        },
    )

    assert response.status_code == 200


def test_deleting_user_history_removes_all_orphan_queues_for_that_user_only(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "RECONCILIATION_STORE_DIR", str(tmp_path / "reconciliations")
    )
    monkeypatch.setenv("APPROVAL_STORE_DIR", str(tmp_path / "approvals"))
    for workflow_id, task_id in (("u1:first", "task-1"), ("u1:old", "task-old")):
        get_reconciliation_store().create(
            user_id="u1",
            workflow_id=workflow_id,
            task_id=task_id,
            step_id="send",
            resume_step=1,
            agent_name="RemoteEmailDispatchAgent",
            error="unknown",
        )
        get_approval_store().create(
            user_id="u1",
            workflow_id=workflow_id,
            task_id=task_id,
            resume_step=1,
            node_name="RemoteEmailDispatchAgent",
            subject={"id": "u1"},
            object={"id": "RemoteEmailDispatchAgent"},
            scenario={},
            action={"verb": "dispatch"},
            policy_result={"decision": "REVIEW_REQUIRED"},
        )
    other = get_reconciliation_store().create(
        user_id="u2",
        workflow_id="u2:keep",
        task_id="task-keep",
        step_id="send",
        resume_step=1,
        agent_name="RemoteEmailDispatchAgent",
        error="unknown",
    )

    response = TestClient(create_app()).delete(
        "/api/conversation-history",
        params={"user_id": "u1"},
    )

    assert response.status_code == 200
    assert response.json()["deleted"] == {"reconciliations": 2, "approvals": 2}
    assert get_reconciliation_store().list(user_id="u1") == []
    assert get_approval_store().list(user_id="u1") == []
    assert get_reconciliation_store().get(other.reconciliation_id) is not None


def test_governance_apis_require_no_client_credential(tmp_path, monkeypatch):
    monkeypatch.setenv("APPROVAL_STORE_DIR", str(tmp_path / "approvals"))
    monkeypatch.setenv(
        "RECONCILIATION_STORE_DIR", str(tmp_path / "reconciliations")
    )
    approval = _approval()
    reconciliation, _ = _reconciliation(tmp_path, monkeypatch)
    client = TestClient(create_app())

    approved = client.post(
        f"/api/security/approvals/{approval.approval_id}/approve",
        json={"approver": "admin", "comment": "spoofed admin"},
    )
    frozen = client.post(
        (
            "/api/security/reconciliations/"
            f"{reconciliation.reconciliation_id}/freeze"
        ),
        json={"operator": "admin", "comment": "spoofed admin"},
    )
    listing = client.get(
        "/api/security/approvals",
        params={"requester_id": "not-a-demo-user"},
    )

    assert approved.status_code == 200
    assert frozen.status_code == 200
    assert listing.status_code == 200
    assert approved.json()["decision"]["approver"] == "admin"
    assert get_approval_store().get(approval.approval_id).status == "approved"
    assert (
        get_reconciliation_store()
        .get(reconciliation.reconciliation_id)
        .status
        == "frozen"
    )


def test_governance_mutations_ignore_body_actor_and_record_server_actor(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("APPROVAL_STORE_DIR", str(tmp_path / "approvals"))
    monkeypatch.setenv(
        "RECONCILIATION_STORE_DIR", str(tmp_path / "reconciliations")
    )
    approval = _approval()
    reconciliation, _ = _reconciliation(tmp_path, monkeypatch)
    client = _client()

    approved = client.post(
        f"/api/security/approvals/{approval.approval_id}/approve",
        json={"approver": "guest", "comment": "body actor is ignored"},
    )
    frozen = client.post(
        f"/api/security/reconciliations/{reconciliation.reconciliation_id}/freeze",
        json={"operator": "guest", "comment": "body actor is ignored"},
    )

    assert approved.status_code == 200
    assert approved.json()["decision"]["approver"] == "admin"
    assert frozen.status_code == 200
    assert frozen.json()["resolution"]["operator"] == "admin"


def test_task_cleanup_requires_no_client_credential(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "RECONCILIATION_STORE_DIR", str(tmp_path / "reconciliations")
    )
    record = get_reconciliation_store().create(
        user_id="hr_manager",
        workflow_id="hr_manager:cleanup-test",
        task_id="task-owned-by-hr",
        step_id="send",
        resume_step=1,
        agent_name="RemoteEmailDispatchAgent",
        error="unknown outcome",
    )
    client = TestClient(create_app())

    deleted = client.delete(
        "/api/tasks/task-owned-by-hr",
    )
    assert deleted.status_code == 200
    assert get_reconciliation_store().get(record.reconciliation_id) is None


def test_governance_reads_require_no_credential_and_ignore_query_identity(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("APPROVAL_STORE_DIR", str(tmp_path / "approvals"))
    monkeypatch.setenv(
        "RECONCILIATION_STORE_DIR", str(tmp_path / "reconciliations")
    )
    monkeypatch.setenv(
        "GOVERNANCE_EVENT_STORE_DIR", str(tmp_path / "governance")
    )
    _approval()
    _reconciliation(tmp_path, monkeypatch)
    record_governance_event("APPROVAL_REQUIRED", task_id="task-1")
    client = TestClient(create_app())

    assert client.get(
        "/api/security/approvals", params={"requester_id": "admin"}
    ).status_code == 200
    assert client.get(
        "/api/security/reconciliations", params={"requester_id": "admin"}
    ).status_code == 200
    assert client.get(
        "/api/tasks/task-1/governance", params={"requester_id": "admin"}
    ).status_code == 200


def test_reconciliation_receipt_transaction_rolls_back_on_record_write_failure(
    tmp_path, monkeypatch
):
    reconciliation, key = _reconciliation(tmp_path, monkeypatch)
    store = get_reconciliation_store()
    original_save = store._save

    def fail_resolution_save(request):
        if request.status == "retry_ready":
            raise OSError("injected reconciliation write failure")
        return original_save(request)

    monkeypatch.setattr(store, "_save", fail_resolution_save)
    response = _client().post(
        f"/api/security/reconciliations/{reconciliation.reconciliation_id}/retry",
        json={"comment": "confirmed absent"},
    )

    assert response.status_code == 409
    assert PersistentReceiptStore(reconciliation.task_id).get(key)["status"] == "STARTED"
    assert store.get(reconciliation.reconciliation_id).status == "pending"


def test_security_precheck_matches_static_policy_constraints(monkeypatch):
    import src.service.web_app as web_app

    monkeypatch.setattr(web_app, "S_ABAC_ENABLED", True)
    client = _client()

    salary_review = client.get(
        "/api/security/tool-check",
        params={"user_id": "hr_manager", "tool_name": "remote_salary_info_tool"},
    )
    salary_denied = client.get(
        "/api/security/tool-check",
        params={"user_id": "engineer", "tool_name": "remote_salary_info_tool"},
    )
    unknown_tool = client.get(
        "/api/security/tool-check",
        params={"user_id": "admin", "tool_name": "not_registered_tool"},
    )
    precheck = client.get("/api/security/users/hr_manager/precheck")

    assert salary_review.status_code == 200
    assert salary_review.json()["decision"] == "REVIEW_REQUIRED"
    assert salary_review.json()["allowed"] is False
    assert salary_review.json()["eligible"] is True

    assert salary_denied.json()["decision"] == "DENY"
    assert salary_denied.json()["grants_match"] is False
    assert unknown_tool.json()["decision"] == "DENY"
    assert unknown_tool.json()["resource_registered"] is False

    salary_summary = precheck.json()["tool_access"]["remote_salary_info_tool"]
    document_summary = precheck.json()["tool_access"]["remote_docx_generator_tool"]
    assert salary_summary["decision"] == "REVIEW_REQUIRED"
    assert document_summary["decision"] == "ALLOW"


def test_demo_static_assets_disable_stale_cache_and_include_resume_fixes():
    client = TestClient(create_app())

    index = client.get("/")
    script = client.get("/static/app.js")

    assert index.status_code == 200
    assert "v=20260803-decision-history-1" in index.text
    assert script.status_code == 200
    assert script.headers["cache-control"] == "no-store"
    assert "const uniqueOutputs = []" in script.text
    assert 'await resumeTask({ inChat: true })' in script.text


def test_governance_queue_items_include_chinese_friendly_task_context(
    monkeypatch,
):
    import src.service.web_app as web_app

    task = SimpleNamespace(
        user_query="查询李娜的基本信息，生成收入证明，然后发给王经理",
        created_at="2026-08-03T17:16:13+08:00",
        execution_phase="execution",
        planning_steps=[
            {
                "step_id": "step_3",
                "title": "将收入证明发送给王经理",
                "intents": ["email.send"],
            }
        ],
    )
    monkeypatch.setattr(
        web_app.TaskLogger,
        "load",
        classmethod(lambda cls, task_id: task),
    )
    monkeypatch.setattr(
        web_app.TaskLogger,
        "list_tasks",
        classmethod(
            lambda cls, **kwargs: [
                {
                    "task_id": "task-previous",
                    "created_at": "2026-08-03T17:00:00+08:00",
                },
                {
                    "task_id": "task-current",
                    "created_at": "2026-08-03T17:16:13+08:00",
                },
            ]
        ),
    )

    result = web_app._enrich_governance_queue_items(
        [
            {
                "task_id": "task-current",
                "workflow_id": "admin:demo",
                "step_id": "step_3",
            }
        ]
    )[0]

    assert result["user_query"] == task.user_query
    assert result["task_created_at"] == task.created_at
    assert result["step_title"] == "将收入证明发送给王经理"
    assert result["step_intents"] == ["email.send"]
    assert result["execution_round"] == 2
    assert result["execution_round_total"] == 2


def test_governance_static_ui_explains_trigger_and_uses_chinese_context():
    client = TestClient(create_app())

    index = client.get("/")
    script = client.get("/static/security.js")

    assert "人工核对队列（外部操作状态不确定）" in index.text
    assert "普通查询失败不会进入这里" in index.text
    assert "触发时间：" in script.text
    assert "所属对话/工作流执行轮次：" in script.text
    assert "用户问题：" in script.text
    assert "已恢复完成" in script.text
    assert "文档生成工具" in script.text
    assert "第 ${numbered[1]} 步" in script.text


def test_agent_precheck_enforces_user_agent_roster(monkeypatch):
    import src.service.web_app as web_app

    monkeypatch.setattr(web_app, "S_ABAC_ENABLED", True)
    client = _client()

    denied = client.get(
        "/api/security/check",
        params={
            "user_id": "hr_manager",
            "agent_name": "RemoteEmailDispatchAgent",
            "action": "send",
        },
    )
    allowed = client.get(
        "/api/security/check",
        params={
            "user_id": "admin",
            "agent_name": "RemoteBusinessRiskAgent",
            "action": "query",
        },
    )

    assert denied.json()["decision"] == "DENY"
    assert denied.json()["available_to_user"] is False
    assert allowed.json()["decision"] == "ALLOW"
    assert allowed.json()["available_to_user"] is True
