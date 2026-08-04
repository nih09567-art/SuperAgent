"""Persistent manual-reconciliation queue for uncertain side effects."""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.utils.path_utils import get_project_root
from src.utils.file_lock import FileLock
from src.orchestration.completion import ReceiptClaimMismatch, missing_receipt_outputs
from src.orchestration.schema_registry import get_schema_registry


_DEFAULT_RESUME_LEASE_SECONDS = 300.0


@dataclass
class ReconciliationRequest:
    reconciliation_id: str
    status: str
    created_at: str
    updated_at: str
    user_id: str
    workflow_id: str
    task_id: str
    step_id: str
    resume_step: int
    agent_name: str
    error: str
    idempotency_key: str = ""
    claim_id: str = ""
    external_operation_id: str = ""
    receipt: dict[str, Any] = field(default_factory=dict)
    expected_outputs: list[str] = field(default_factory=list)
    expected_schema_refs: dict[str, str] = field(default_factory=dict)
    resolution: dict[str, Any] = field(default_factory=dict)


class ReconciliationStore:
    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self.base_dir = Path(base_dir or _configured_store_dir())
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._file_lock_path = self.base_dir / ".reconciliation-store"
        self._transaction_path = self.base_dir / ".receipt-resolution.txn"
        self._recover_if_needed()

    def create(
        self,
        *,
        user_id: str,
        workflow_id: str,
        task_id: str,
        step_id: str,
        resume_step: int,
        agent_name: str,
        error: str,
        idempotency_key: str = "",
        claim_id: str = "",
        external_operation_id: str = "",
        receipt: Optional[dict[str, Any]] = None,
        expected_outputs: Optional[list[str]] = None,
        expected_schema_refs: Optional[dict[str, str]] = None,
    ) -> ReconciliationRequest:
        with self._lock, FileLock(self._file_lock_path):
            self._recover_transaction_unlocked()
            existing = self.find_active(task_id=task_id, step_id=step_id)
            if existing is not None:
                return existing
            now = datetime.now().isoformat()
            identity = f"{task_id}_{step_id}_{int(datetime.now().timestamp() * 1000)}"
            safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in identity)
            request = ReconciliationRequest(
                reconciliation_id=f"recon_{safe}", status="pending",
                created_at=now, updated_at=now, user_id=user_id,
                workflow_id=workflow_id, task_id=task_id, step_id=step_id,
                resume_step=resume_step, agent_name=agent_name, error=error,
                idempotency_key=idempotency_key, claim_id=claim_id,
                external_operation_id=external_operation_id,
                receipt=dict(receipt or {}),
                expected_outputs=list(expected_outputs or []),
                expected_schema_refs=dict(expected_schema_refs or {}),
            )
            self._save(request)
            return request

    def list(
        self,
        *,
        status: Optional[str] = None,
        task_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        self._recover_if_needed()
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
            if user_id and item.get("user_id") != user_id:
                continue
            items.append(item)
        return sorted(items, key=lambda item: item.get("created_at", ""), reverse=True)

    def get(self, reconciliation_id: str) -> Optional[ReconciliationRequest]:
        self._recover_if_needed()
        path = self._path(reconciliation_id)
        if not path.exists():
            return None
        return ReconciliationRequest(
            **json.loads(path.read_text(encoding="utf-8"))
        )

    def find_active(
        self, *, task_id: str, step_id: str
    ) -> Optional[ReconciliationRequest]:
        # Only unresolved records suppress duplicates. A safe retry can produce
        # a genuinely new uncertain attempt, which must get a new queue item.
        for status in ("pending", "frozen"):
            for item in self.list(status=status, task_id=task_id):
                if item.get("step_id") == step_id:
                    return ReconciliationRequest(**item)
        return None

    def resolve(
        self,
        reconciliation_id: str,
        *,
        status: str,
        operator: str,
        comment: str = "",
        external_operation_id: str = "",
        outputs: Optional[dict[str, Any]] = None,
    ) -> ReconciliationRequest:
        with self._lock, FileLock(self._file_lock_path):
            self._recover_transaction_unlocked()
            request = self._require(reconciliation_id)
            if request.status not in {"pending", "frozen"}:
                raise ValueError(
                    f"reconciliation is not resolvable in status={request.status}"
                )
            self._apply_resolution(
                request, status=status, operator=operator, comment=comment,
                external_operation_id=external_operation_id, outputs=outputs,
            )
            self._save(request)
            return request

    def resolve_with_receipt(
        self,
        reconciliation_id: str,
        *,
        receipt_store: Any,
        decision: str,
        operator: str,
        comment: str = "",
        external_operation_id: str = "",
        outputs: Optional[dict[str, Any]] = None,
    ) -> ReconciliationRequest:
        """Commit the reconciliation decision and receipt change as one transaction.

        A write-ahead rollback journal is persisted before either file changes.
        Ordinary exceptions roll both files back immediately; after a process
        crash the next store instance/read restores the pre-transaction state.
        """

        if decision not in {"retry", "succeeded"}:
            raise ValueError(f"unsupported receipt decision: {decision}")
        with self._lock, FileLock(self._file_lock_path):
            self._recover_transaction_unlocked()
            receipt_store._path.parent.mkdir(parents=True, exist_ok=True)
            with receipt_store._lock, FileLock(receipt_store._path):
                request = self._require(reconciliation_id)
                if request.status not in {"pending", "frozen"}:
                    raise ValueError(
                        f"reconciliation is not resolvable in status={request.status}"
                    )
                key = request.idempotency_key
                if not key:
                    raise ValueError("missing idempotency key")
                receipt_data = receipt_store._read_strict()
                existing = receipt_data.get(key)
                if not isinstance(existing, dict):
                    raise KeyError(f"receipt not found: {key}")
                existing_status = str(existing.get("status") or "")
                repairing_succeeded = (
                    existing_status == "SUCCEEDED" and decision == "succeeded"
                )
                if existing_status != "STARTED" and not repairing_succeeded:
                    raise ValueError(
                        f"receipt is not resolvable in status={existing_status}"
                    )
                owner = existing.get("claim_id")
                if request.claim_id and owner and owner != request.claim_id:
                    raise ReceiptClaimMismatch(
                        f"claim id mismatch for {key}: {owner!r} != {request.claim_id!r}"
                    )

                before_request = asdict(request)
                before_receipts = dict(receipt_data)
                if decision == "retry":
                    if existing_status != "STARTED":
                        raise ValueError(
                            "a confirmed side effect cannot be released for retry"
                        )
                    del receipt_data[key]
                    target_status = "retry_ready"
                else:
                    if not str(external_operation_id).strip():
                        raise ValueError("external_operation_id is required")
                    if repairing_succeeded:
                        # The runtime created this request from the current
                        # trusted actual-Agent contract. It intentionally
                        # supersedes a stale/legacy SUCCEEDED receipt contract.
                        trusted_expected = list(request.expected_outputs)
                    else:
                        trusted_expected = existing.get("expected_outputs")
                        if trusted_expected is None:
                            trusted_expected = request.expected_outputs
                        elif request.expected_outputs and set(trusted_expected) != set(
                            request.expected_outputs
                        ):
                            raise ValueError(
                                "receipt output contract does not match reconciliation request"
                            )
                    missing = missing_receipt_outputs(
                        outputs, trusted_expected or []
                    )
                    if missing:
                        raise ValueError(
                            "confirmed outputs violate the trusted output contract; "
                            f"missing: {', '.join(missing)}"
                        )
                    if repairing_succeeded:
                        trusted_schema_refs = dict(request.expected_schema_refs)
                    else:
                        trusted_schema_refs = existing.get("expected_schema_refs")
                        if trusted_schema_refs is None:
                            trusted_schema_refs = request.expected_schema_refs
                        elif request.expected_schema_refs and trusted_schema_refs != (
                            request.expected_schema_refs
                        ):
                            raise ValueError(
                                "receipt output schemas do not match reconciliation request"
                            )
                    registry = get_schema_registry()
                    for logical_name, schema_ref in dict(
                        trusted_schema_refs or {}
                    ).items():
                        if logical_name not in (outputs or {}):
                            continue
                        valid, errors = registry.validate(outputs[logical_name], schema_ref)
                        if not valid:
                            raise ValueError(
                                f"confirmed output {logical_name!r} failed schema "
                                f"{schema_ref!r}: {'; '.join(errors)}"
                            )
                    receipt_data[key] = {
                        **existing,
                        "status": "SUCCEEDED",
                        "external_op_id": str(external_operation_id).strip(),
                        "outputs": dict(outputs or {}),
                        "outputs_kind": "confirmed_payloads",
                        "expected_outputs": list(trusted_expected or []),
                        "expected_schema_refs": dict(trusted_schema_refs or {}),
                        "confirmed_by": operator,
                        "timestamp": time.time(),
                    }
                    target_status = "confirmed_succeeded"
                self._apply_resolution(
                    request, status=target_status, operator=operator,
                    comment=comment, external_operation_id=external_operation_id,
                    outputs=outputs,
                )
                journal = {
                    "reconciliation_path": str(self._path(reconciliation_id).resolve()),
                    "reconciliation_before": before_request,
                    "receipt_path": str(receipt_store._path.resolve()),
                    "receipts_before": before_receipts,
                }
                self._write_json_atomic(self._transaction_path, journal)
                try:
                    receipt_store._flush_data(receipt_data)
                    receipt_store._receipts = receipt_data
                    self._save(request)
                except Exception:
                    self._recover_transaction_unlocked()
                    receipt_store._receipts = receipt_store._read_strict()
                    raise
                self._transaction_path.unlink(missing_ok=True)
                return request

    def claim_for_resume(
        self,
        *,
        task_id: str,
        resume_step: int,
        operator: str,
        lease_seconds: Optional[float] = None,
    ) -> Optional[ReconciliationRequest]:
        """Atomically lease a manually cleared reconciliation for one resume.

        Persisting an owner-bound, expiring claim makes ``resuming`` recoverable
        after a hard process exit. The cross-process file lock ensures that only
        one worker can reclaim an expired lease.
        """

        with self._lock, FileLock(self._file_lock_path):
            self._recover_transaction_unlocked()
            now = time.time()
            effective_lease_seconds = self._resume_lease_seconds(lease_seconds)
            matches = [
                ReconciliationRequest(**item)
                for item in self.list(task_id=task_id)
                if int(item.get("resume_step") or 0) == int(resume_step)
                and item.get("status") in {
                    "retry_ready",
                    "confirmed_succeeded",
                    "resuming",
                }
            ]
            if not matches:
                return None
            request = sorted(
                matches,
                key=lambda item: item.updated_at,
                reverse=True,
            )[0]
            if request.status == "resuming":
                if not self._resume_lease_expired(
                    request,
                    now=now,
                    fallback_lease_seconds=effective_lease_seconds,
                ):
                    raise ValueError("reconciliation resume is already in progress")
                previous_status = str(
                    request.resolution.get("resume_from_status") or "retry_ready"
                )
                reclaimed = {
                    "resume_reclaimed_at": datetime.now().isoformat(),
                    "resume_reclaimed_claim_id": str(
                        request.resolution.get("resume_claim_id") or ""
                    ),
                    "resume_reclaimed_owner": str(
                        request.resolution.get("resume_owner") or ""
                    ),
                    "resume_reclaim_reason": "lease_expired",
                }
            else:
                previous_status = request.status
                reclaimed = {}
            resume_claim_id = uuid.uuid4().hex
            request.status = "resuming"
            request.updated_at = datetime.now().isoformat()
            request.resolution = {
                **dict(request.resolution or {}),
                **reclaimed,
                "resume_from_status": previous_status,
                "resume_operator": operator,
                "resume_claim_id": resume_claim_id,
                "resume_owner": f"{socket.gethostname()}:{os.getpid()}",
                "resume_started_at": request.updated_at,
                "resume_lease_seconds": effective_lease_seconds,
                "resume_lease_expires_at": now + effective_lease_seconds,
            }
            self._save(request)
            return request

    def renew_resume_claim(
        self,
        reconciliation_id: str,
        *,
        resume_claim_id: str,
        lease_seconds: Optional[float] = None,
    ) -> ReconciliationRequest:
        """Extend an active resume lease owned by ``resume_claim_id``."""

        with self._lock, FileLock(self._file_lock_path):
            self._recover_transaction_unlocked()
            request = self._require(reconciliation_id)
            self._require_resume_claim(request, resume_claim_id)
            now = time.time()
            effective_lease_seconds = self._resume_lease_seconds(lease_seconds)
            if self._resume_lease_expired(
                request,
                now=now,
                fallback_lease_seconds=effective_lease_seconds,
            ):
                raise ValueError("reconciliation resume lease has expired")
            request.updated_at = datetime.now().isoformat()
            request.resolution = {
                **dict(request.resolution or {}),
                "resume_lease_seconds": effective_lease_seconds,
                "resume_lease_expires_at": now + effective_lease_seconds,
                "resume_renewed_at": request.updated_at,
            }
            self._save(request)
            return request

    def finish_resume(
        self,
        reconciliation_id: str,
        *,
        resume_claim_id: str,
        succeeded: bool,
    ) -> ReconciliationRequest:
        """Consume a successful resume or restore its ready state on failure."""

        with self._lock, FileLock(self._file_lock_path):
            self._recover_transaction_unlocked()
            request = self._require(reconciliation_id)
            self._require_resume_claim(request, resume_claim_id)
            if self._resume_lease_expired(
                request,
                now=time.time(),
                fallback_lease_seconds=self._resume_lease_seconds(),
            ):
                raise ValueError("reconciliation resume lease has expired")
            previous_status = str(
                request.resolution.get("resume_from_status") or "retry_ready"
            )
            request.status = "consumed" if succeeded else previous_status
            request.updated_at = datetime.now().isoformat()
            request.resolution = {
                **dict(request.resolution or {}),
                "resume_finished_at": request.updated_at,
                "resume_succeeded": bool(succeeded),
            }
            self._save(request)
            return request

    def reap_expired_resume_claims(self) -> list[ReconciliationRequest]:
        """Restore expired ``resuming`` records to their actionable ready state."""

        with self._lock, FileLock(self._file_lock_path):
            self._recover_transaction_unlocked()
            now = time.time()
            fallback_lease_seconds = self._resume_lease_seconds()
            restored: list[ReconciliationRequest] = []
            for item in self.list(status="resuming"):
                request = ReconciliationRequest(**item)
                if not self._resume_lease_expired(
                    request,
                    now=now,
                    fallback_lease_seconds=fallback_lease_seconds,
                ):
                    continue
                request.status = str(
                    request.resolution.get("resume_from_status") or "retry_ready"
                )
                request.updated_at = datetime.now().isoformat()
                request.resolution = {
                    **dict(request.resolution or {}),
                    "resume_reaped_at": request.updated_at,
                    "resume_reaped_claim_id": str(
                        request.resolution.get("resume_claim_id") or ""
                    ),
                    "resume_reaped_owner": str(
                        request.resolution.get("resume_owner") or ""
                    ),
                    "resume_reap_reason": "lease_expired",
                }
                self._save(request)
                restored.append(request)
            return restored

    @staticmethod
    def _resume_lease_seconds(value: Optional[float] = None) -> float:
        raw_value: Any = value
        if raw_value is None:
            raw_value = os.getenv(
                "RECONCILIATION_RESUME_LEASE_SECONDS",
                str(_DEFAULT_RESUME_LEASE_SECONDS),
            )
        try:
            seconds = float(raw_value)
        except (TypeError, ValueError):
            if value is not None:
                raise ValueError("resume lease seconds must be a number")
            seconds = _DEFAULT_RESUME_LEASE_SECONDS
        if seconds <= 0:
            if value is not None:
                raise ValueError("resume lease seconds must be positive")
            seconds = _DEFAULT_RESUME_LEASE_SECONDS
        return seconds

    @staticmethod
    def _resume_lease_expired(
        request: ReconciliationRequest,
        *,
        now: float,
        fallback_lease_seconds: float,
    ) -> bool:
        expiry = request.resolution.get("resume_lease_expires_at")
        try:
            return float(expiry) <= now
        except (TypeError, ValueError):
            # Backward compatibility for pre-lease ``resuming`` records. A
            # valid start timestamp receives one default lease window; an
            # unreadable/missing timestamp is treated as abandoned.
            started_at = str(
                request.resolution.get("resume_started_at")
                or request.updated_at
                or ""
            )
            try:
                started = datetime.fromisoformat(started_at).timestamp()
            except (TypeError, ValueError):
                return True
            return started + fallback_lease_seconds <= now

    @staticmethod
    def _require_resume_claim(
        request: ReconciliationRequest,
        resume_claim_id: str,
    ) -> None:
        if request.status != "resuming":
            raise ValueError(
                f"reconciliation resume is not active in status={request.status}"
            )
        active_claim_id = str(request.resolution.get("resume_claim_id") or "")
        if not resume_claim_id or active_claim_id != str(resume_claim_id):
            raise ValueError("reconciliation resume claim id mismatch")

    def delete(
        self,
        *,
        task_id: Optional[str] = None,
        workflow_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> int:
        """Delete queue records in an explicitly scoped task/workflow boundary."""

        if not any((task_id, workflow_id, user_id)):
            raise ValueError("task_id, workflow_id or user_id is required")
        with self._lock, FileLock(self._file_lock_path):
            self._recover_transaction_unlocked()
            removed = 0
            for item in self.list():
                if task_id and item.get("task_id") != task_id:
                    continue
                if workflow_id and item.get("workflow_id") != workflow_id:
                    continue
                if user_id and item.get("user_id") != user_id:
                    continue
                reconciliation_id = str(item.get("reconciliation_id") or "")
                if not reconciliation_id:
                    continue
                try:
                    self._path(reconciliation_id).unlink()
                    removed += 1
                except FileNotFoundError:
                    continue
            return removed

    def freeze(
        self, reconciliation_id: str, *, operator: str, comment: str = ""
    ) -> ReconciliationRequest:
        with self._lock, FileLock(self._file_lock_path):
            self._recover_transaction_unlocked()
            request = self._require(reconciliation_id)
            if request.status not in {"pending", "frozen"}:
                raise ValueError(
                    f"reconciliation is not freezable in status={request.status}"
                )
            request.status = "frozen"
            request.updated_at = datetime.now().isoformat()
            request.resolution = {
                "operator": operator, "comment": comment,
                "updated_at": request.updated_at,
            }
            self._save(request)
            return request

    @staticmethod
    def _apply_resolution(
        request: ReconciliationRequest, *, status: str, operator: str,
        comment: str, external_operation_id: str,
        outputs: Optional[dict[str, Any]],
    ) -> None:
        request.status = status
        request.updated_at = datetime.now().isoformat()
        if external_operation_id:
            request.external_operation_id = external_operation_id
        request.resolution = {
            "operator": operator, "comment": comment,
            "resolved_at": request.updated_at,
            "external_operation_id": external_operation_id,
            "outputs": dict(outputs or {}),
        }

    def _recover_if_needed(self) -> None:
        if not self._transaction_path.exists():
            return
        with self._lock, FileLock(self._file_lock_path):
            self._recover_transaction_unlocked()

    def _recover_transaction_unlocked(self) -> None:
        if not self._transaction_path.exists():
            return
        journal = json.loads(self._transaction_path.read_text(encoding="utf-8"))
        reconciliation_path = Path(journal["reconciliation_path"])
        receipt_path = Path(journal["receipt_path"])
        if reconciliation_path.parent.resolve() != self.base_dir.resolve():
            raise ValueError("invalid reconciliation transaction path")
        self._write_json_atomic(
            reconciliation_path, journal["reconciliation_before"]
        )
        self._write_json_atomic(receipt_path, journal["receipts_before"])
        self._transaction_path.unlink(missing_ok=True)

    @staticmethod
    def _write_json_atomic(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_path = tempfile.mkstemp(
            dir=str(path.parent), prefix=f"{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False)
            os.replace(temporary_path, path)
        except Exception:
            try:
                os.remove(temporary_path)
            except OSError:
                pass
            raise

    def _require(self, reconciliation_id: str) -> ReconciliationRequest:
        request = self.get(reconciliation_id)
        if request is None:
            raise FileNotFoundError(
                f"reconciliation not found: {reconciliation_id}"
            )
        return request

    def _save(self, request: ReconciliationRequest) -> None:
        path = self._path(request.reconciliation_id)
        fd, temporary_path = tempfile.mkstemp(
            dir=str(self.base_dir),
            prefix=f"{path.stem}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    asdict(request),
                    handle,
                    indent=2,
                    ensure_ascii=False,
                )
            os.replace(temporary_path, path)
        except Exception:
            try:
                os.remove(temporary_path)
            except OSError:
                pass
            raise

    def _path(self, reconciliation_id: str) -> Path:
        safe = "".join(
            c if c.isalnum() or c in "-_." else "_" for c in reconciliation_id
        )
        return self.base_dir / f"{safe}.json"


def _configured_store_dir() -> Path:
    return Path(
        os.getenv(
            "RECONCILIATION_STORE_DIR",
            str(get_project_root() / "store" / "reconciliations"),
        )
    )


_store: Optional[ReconciliationStore] = None


def get_reconciliation_store() -> ReconciliationStore:
    global _store
    configured = _configured_store_dir()
    if _store is None or _store.base_dir != configured:
        _store = ReconciliationStore(configured)
    return _store
