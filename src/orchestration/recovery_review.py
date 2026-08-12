"""Persistent queue for human review of failed workflow recovery."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.utils.path_utils import get_project_root


_NON_ACTIONABLE_FAILURE_CODES = {
    "AMBIGUOUS_LEGACY_OUTPUT",
    "BUSINESS_RESULT_INCOMPLETE",
    "CONTRACT_NO_OUTPUTS",
    "CONTRACT_VERSION_MISMATCH",
    "MISSING_REQUIRED_OUTPUT",
    "PRODUCER_AGENT_MISMATCH",
    "REROUTED_AGENT_CONTRACT_MISSING",
    "SCHEMA_VALIDATION_FAILED",
    "TASK_GRAPH_INVALID",
    "TASK_GRAPH_MISSING",
    "UNDECLARED_OUTPUT",
    "UNREGISTERED_SCHEMA",
}


def failure_codes_are_reviewable(failure_codes: list[str]) -> bool:
    """Whether an old queue item can meaningfully be resumed by an operator."""

    normalized = {str(code or "").strip().upper() for code in failure_codes}
    return bool(normalized) and not bool(normalized & _NON_ACTIONABLE_FAILURE_CODES)


def failures_need_recovery_review(failures: list[dict[str, Any]]) -> bool:
    """Queue only failures for which a checkpoint retry can change the result."""

    return any(bool(item.get("retryable")) for item in failures)


@dataclass
class RecoveryReviewRequest:
    review_id: str
    status: str
    created_at: str
    updated_at: str
    user_id: str
    workflow_id: str
    task_id: str
    terminal_status: str
    failed_steps: list[str] = field(default_factory=list)
    blocked_steps: list[str] = field(default_factory=list)
    failure_codes: list[str] = field(default_factory=list)
    decision: dict[str, Any] = field(default_factory=dict)


class RecoveryReviewStore:
    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self.base_dir = Path(base_dir or _configured_store_dir())
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def create(
        self,
        *,
        user_id: str,
        workflow_id: str,
        task_id: str,
        terminal_status: str,
        failed_steps: Optional[list[str]] = None,
        blocked_steps: Optional[list[str]] = None,
        failure_codes: Optional[list[str]] = None,
    ) -> RecoveryReviewRequest:
        with self._lock:
            active = self.find_active(task_id=task_id)
            if active is not None:
                return active
            now = datetime.now().isoformat()
            request = RecoveryReviewRequest(
                review_id=f"recovery_{int(datetime.now().timestamp() * 1000)}_{uuid.uuid4().hex[:10]}",
                status="pending",
                created_at=now,
                updated_at=now,
                user_id=str(user_id or ""),
                workflow_id=str(workflow_id or ""),
                task_id=str(task_id),
                terminal_status=str(terminal_status or "FAILED").upper(),
                failed_steps=list(failed_steps or []),
                blocked_steps=list(blocked_steps or []),
                failure_codes=list(dict.fromkeys(failure_codes or [])),
            )
            self._save(request)
            return request

    def list(
        self, *, status: Optional[str] = None, task_id: Optional[str] = None
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for path in self.base_dir.glob("*.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if status and item.get("status") != status:
                continue
            if task_id and item.get("task_id") != task_id:
                continue
            items.append(item)
        return sorted(items, key=lambda item: item.get("created_at", ""), reverse=True)

    def get(self, review_id: str) -> Optional[RecoveryReviewRequest]:
        path = self._path(review_id)
        if not path.exists():
            return None
        return RecoveryReviewRequest(**json.loads(path.read_text(encoding="utf-8")))

    def find_active(self, *, task_id: str) -> Optional[RecoveryReviewRequest]:
        for item in self.list(task_id=task_id):
            if item.get("status") in {"pending", "in_review"}:
                return RecoveryReviewRequest(**item)
        return None

    def start_review(self, review_id: str, *, operator: str) -> RecoveryReviewRequest:
        with self._lock:
            request = self.get(review_id)
            if request is None:
                raise FileNotFoundError(f"recovery review not found: {review_id}")
            if request.status not in {"pending", "in_review"}:
                raise ValueError(f"recovery review is not actionable in status={request.status}")
            if not failure_codes_are_reviewable(request.failure_codes):
                raise ValueError(
                    "failure requires an Agent, Contract, Schema, or plan fix; "
                    "it cannot be cleared by recovery review"
                )
            request.status = "in_review"
            request.updated_at = datetime.now().isoformat()
            request.decision = {
                "operator": operator,
                "opened_at": request.updated_at,
            }
            self._save(request)
            return request

    def delete(
        self,
        *,
        task_id: Optional[str] = None,
        workflow_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> int:
        """Delete queue records within an explicit task/workflow/user scope."""

        if not any((task_id, workflow_id, user_id)):
            raise ValueError("task_id, workflow_id or user_id is required")
        with self._lock:
            removed = 0
            for item in self.list():
                if task_id and item.get("task_id") != task_id:
                    continue
                if workflow_id and item.get("workflow_id") != workflow_id:
                    continue
                if user_id and item.get("user_id") != user_id:
                    continue
                review_id = str(item.get("review_id") or "")
                if not review_id:
                    continue
                try:
                    self._path(review_id).unlink()
                    removed += 1
                except FileNotFoundError:
                    continue
            return removed

    def _save(self, request: RecoveryReviewRequest) -> None:
        path = self._path(request.review_id)
        fd, temporary_path = tempfile.mkstemp(
            dir=str(self.base_dir), prefix=f"{path.stem}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(asdict(request), handle, indent=2, ensure_ascii=False)
            os.replace(temporary_path, path)
        except Exception:
            try:
                os.remove(temporary_path)
            except OSError:
                pass
            raise

    def _path(self, review_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in review_id)
        return self.base_dir / f"{safe}.json"


def _configured_store_dir() -> Path:
    return Path(
        os.getenv(
            "RECOVERY_REVIEW_STORE_DIR",
            str(get_project_root() / "store" / "recovery_reviews"),
        )
    )


_store: Optional[RecoveryReviewStore] = None


def get_recovery_review_store() -> RecoveryReviewStore:
    global _store
    configured = _configured_store_dir()
    if _store is None or _store.base_dir != configured:
        _store = RecoveryReviewStore(configured)
    return _store
