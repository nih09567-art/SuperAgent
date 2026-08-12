import pytest

from src.orchestration.recovery_review import (
    RecoveryReviewStore,
    failures_need_recovery_review,
)


def test_failed_task_recovery_review_is_persistent_and_deduplicated(tmp_path):
    store = RecoveryReviewStore(tmp_path / "reviews")

    first = store.create(
        user_id="u1",
        workflow_id="wf1",
        task_id="task1",
        terminal_status="FAILED",
        failed_steps=["salary"],
        blocked_steps=["report"],
        failure_codes=["AGENT_TIMEOUT"],
    )
    second = store.create(
        user_id="u1",
        workflow_id="wf1",
        task_id="task1",
        terminal_status="FAILED",
    )

    assert first.review_id == second.review_id
    assert store.list(task_id="task1")[0]["status"] == "pending"

    opened = store.start_review(first.review_id, operator="admin")
    assert opened.status == "in_review"
    assert opened.decision["operator"] == "admin"

    assert store.delete(task_id="task1") == 1
    assert store.list(task_id="task1") == []


def test_contract_failure_cannot_start_recovery_review(tmp_path):
    store = RecoveryReviewStore(tmp_path / "reviews")
    review = store.create(
        user_id="u1",
        workflow_id="wf1",
        task_id="task1",
        terminal_status="FAILED",
        failure_codes=["MISSING_REQUIRED_OUTPUT"],
    )

    with pytest.raises(ValueError, match="cannot be cleared"):
        store.start_review(review.review_id, operator="admin")


def test_only_retryable_failures_need_recovery_review():
    assert failures_need_recovery_review([
        {"code": "AGENT_TIMEOUT", "retryable": True},
    ]) is True
    assert failures_need_recovery_review([
        {"code": "MISSING_REQUIRED_OUTPUT", "retryable": False},
    ]) is False


def test_recovery_reviews_can_be_deleted_by_conversation_scope(tmp_path):
    store = RecoveryReviewStore(tmp_path / "reviews")
    store.create(
        user_id="u1",
        workflow_id="u1:w1",
        task_id="task1",
        terminal_status="FAILED",
    )
    store.create(
        user_id="u1",
        workflow_id="u1:w2",
        task_id="task2",
        terminal_status="FAILED",
    )

    assert store.delete(workflow_id="u1:w1") == 1
    assert [item["task_id"] for item in store.list()] == ["task2"]
    assert store.delete(user_id="u1") == 1
