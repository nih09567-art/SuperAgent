"""Detached production execution and replayable SSE event storage.

The browser is an observer of a production task, not its owner.  A producer
therefore consumes the workflow generator independently and persists every
public SSE event before any subscriber sees it.  Subscribers may disappear and
later replay events from their last durable sequence without cancelling the
producer.

The same journal also carries a renewable worker lease.  A replacement process
can fence an expired producer, mark the task as recovery-required, and expose a
safe checkpoint resume position without replaying uncertain side effects.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from config.global_variables import checkpoints_dir
from src.robust.task_logger import TaskLogger
from src.robust.checkpoint import CheckpointManager
from src.robust.task_control import TaskControlStore
from src.utils.file_lock import FileLock


logger = logging.getLogger(__name__)


class LeaseOwnershipLost(RuntimeError):
    """Raised when a stale producer tries to write after lease takeover."""


def _event_store_dir() -> Path:
    configured = os.getenv("TASK_EVENT_STORE_DIR")
    path = Path(configured) if configured else checkpoints_dir.parent / "task_events"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, indent=2, default=str)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class TaskEventStore:
    """File-backed, append-only public event journal for production tasks."""

    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = Path(base_dir) if base_dir is not None else _event_store_dir()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe_task_id(task_id: str) -> str:
        normalized = str(task_id or "").strip()
        if not normalized:
            raise ValueError("task_id is required")
        safe = "".join(
            character if character.isalnum() or character in ("-", "_") else "_"
            for character in normalized
        )
        if safe != normalized:
            raise ValueError("task_id is invalid")
        return safe

    def _events_path(self, task_id: str) -> Path:
        return self.base_dir / f"{self._safe_task_id(task_id)}.jsonl"

    def _meta_path(self, task_id: str) -> Path:
        return self.base_dir / f"{self._safe_task_id(task_id)}.meta.json"

    def exists(self, task_id: str) -> bool:
        return self._events_path(task_id).exists() or self._meta_path(task_id).exists()

    def delete(self, task_id: str) -> int:
        events_path = self._events_path(task_id)
        deleted = 0
        with FileLock(events_path):
            for path in (events_path, self._meta_path(task_id)):
                if path.exists():
                    path.unlink()
                    deleted = 1
        return deleted

    def _read_meta_unlocked(self, task_id: str) -> dict[str, Any]:
        path = self._meta_path(task_id)
        if not path.exists():
            return {
                "task_id": task_id,
                "last_sequence": 0,
                "producer_done": False,
                "terminal_status": "",
            }
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {
                "task_id": task_id,
                "last_sequence": 0,
                "producer_done": False,
                "terminal_status": "",
            }
        return data if isinstance(data, dict) else {}

    def reset(self, task_id: str) -> None:
        events_path = self._events_path(task_id)
        with FileLock(events_path):
            events_path.unlink(missing_ok=True)
            _atomic_write_json(
                self._meta_path(task_id),
                {
                    "task_id": task_id,
                    "last_sequence": 0,
                    "producer_done": False,
                    "terminal_status": "",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )

    def append(
        self,
        task_id: str,
        event: dict[str, Any],
        *,
        worker_id: str = "",
    ) -> dict[str, Any]:
        events_path = self._events_path(task_id)
        with FileLock(events_path):
            meta = self._read_meta_unlocked(task_id)
            if worker_id and (
                meta.get("producer_done")
                or str(meta.get("worker_id") or "") != worker_id
            ):
                raise LeaseOwnershipLost(
                    f"task {task_id} producer lease is no longer owned by {worker_id}"
                )
            sequence = int(meta.get("last_sequence") or 0) + 1
            record = {
                **dict(event),
                "sequence": sequence,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
            events_path.parent.mkdir(parents=True, exist_ok=True)
            with events_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False, default=str))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            meta.update(
                {
                    "task_id": task_id,
                    "last_sequence": sequence,
                    "updated_at": record["recorded_at"],
                }
            )
            if str(record.get("event") or "") == "end_of_workflow":
                meta["terminal_status"] = str(
                    (record.get("data") or {}).get("status") or ""
                ).upper()
            _atomic_write_json(self._meta_path(task_id), meta)
            return record

    def mark_producer_done(
        self,
        task_id: str,
        *,
        error: str = "",
        worker_id: str = "",
    ) -> bool:
        events_path = self._events_path(task_id)
        with FileLock(events_path):
            meta = self._read_meta_unlocked(task_id)
            if worker_id and str(meta.get("worker_id") or "") != worker_id:
                return False
            meta.update(
                {
                    "task_id": task_id,
                    "producer_done": True,
                    "producer_error": str(error or "")[:1024],
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            _atomic_write_json(self._meta_path(task_id), meta)
            return True

    def mark_producer_started(
        self,
        task_id: str,
        *,
        worker_id: str = "",
        lease_seconds: float = 30.0,
    ) -> None:
        """Open a new producer generation while retaining its event history."""

        events_path = self._events_path(task_id)
        with FileLock(events_path):
            meta = self._read_meta_unlocked(task_id)
            meta.update(
                {
                    "task_id": task_id,
                    "producer_done": False,
                    "producer_error": "",
                    "terminal_status": "",
                    "worker_id": str(worker_id),
                    "last_heartbeat_at": datetime.now(timezone.utc).isoformat(),
                    "lease_expires_at": (
                        datetime.now(timezone.utc)
                        + timedelta(seconds=max(1.0, float(lease_seconds)))
                    ).isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            _atomic_write_json(self._meta_path(task_id), meta)

    def renew_lease(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_seconds: float,
    ) -> bool:
        events_path = self._events_path(task_id)
        with FileLock(events_path):
            meta = self._read_meta_unlocked(task_id)
            if meta.get("producer_done") or str(meta.get("worker_id") or "") != worker_id:
                return False
            now = datetime.now(timezone.utc)
            meta["last_heartbeat_at"] = now.isoformat()
            meta["lease_expires_at"] = (
                now + timedelta(seconds=max(1.0, float(lease_seconds)))
            ).isoformat()
            meta["updated_at"] = now.isoformat()
            _atomic_write_json(self._meta_path(task_id), meta)
            return True

    def claim_expired_lease(
        self,
        task_id: str,
        *,
        expected_worker_id: str,
        recovery_worker_id: str,
        now: datetime,
    ) -> bool:
        """Atomically claim one expired producer generation for recovery."""

        events_path = self._events_path(task_id)
        with FileLock(events_path):
            meta = self._read_meta_unlocked(task_id)
            if meta.get("producer_done"):
                return False
            if str(meta.get("worker_id") or "") != expected_worker_id:
                return False
            try:
                expiry = datetime.fromisoformat(
                    str(meta.get("lease_expires_at") or "")
                )
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                return False
            if now < expiry:
                return False
            meta.update(
                {
                    "worker_id": recovery_worker_id,
                    "recovery_claimed_at": now.isoformat(),
                    "updated_at": now.isoformat(),
                }
            )
            _atomic_write_json(self._meta_path(task_id), meta)
            return True

    def list_task_ids(self) -> list[str]:
        suffix = ".meta.json"
        return sorted(
            path.name[: -len(suffix)]
            for path in self.base_dir.glob(f"*{suffix}")
        )

    def metadata(self, task_id: str) -> dict[str, Any]:
        events_path = self._events_path(task_id)
        with FileLock(events_path):
            return dict(self._read_meta_unlocked(task_id))

    def list_after(
        self,
        task_id: str,
        after_sequence: int = 0,
        *,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        events_path = self._events_path(task_id)
        if not events_path.exists():
            return []
        cursor = max(0, int(after_sequence or 0))
        result: list[dict[str, Any]] = []
        with FileLock(events_path):
            try:
                lines = events_path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return []
        for line in lines:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Skipping corrupt task event line for %s", task_id)
                continue
            if int(item.get("sequence") or 0) <= cursor:
                continue
            result.append(item)
            if len(result) >= max(1, int(limit)):
                break
        return result


class DetachedExecutionRunner:
    """Own workflow producers independently from their SSE subscribers."""

    def __init__(self, event_store: Optional[TaskEventStore] = None):
        self.event_store = event_store or TaskEventStore()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self.worker_id = f"worker-{os.getpid()}-{uuid.uuid4().hex[:12]}"
        self.lease_seconds = max(
            3.0, float(os.getenv("DETACHED_WORKER_LEASE_SECONDS", "30"))
        )

    def is_running(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        return bool(task is not None and not task.done())

    def start(
        self,
        task_id: str,
        producer_factory: Callable[[], AsyncIterator[dict[str, Any]]],
        *,
        reset_events: bool = True,
    ) -> bool:
        if self.is_running(task_id):
            return False
        if reset_events:
            self.event_store.reset(task_id)
        self.event_store.mark_producer_started(
            task_id,
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
        )
        task = asyncio.create_task(
            self._drive(task_id, producer_factory),
            name=f"detached-execution:{task_id}",
        )
        self._tasks[task_id] = task
        task.add_done_callback(lambda _task: self._tasks.pop(task_id, None))
        return True

    async def _drive(
        self,
        task_id: str,
        producer_factory: Callable[[], AsyncIterator[dict[str, Any]]],
    ) -> None:
        error = ""
        producer_finished = False
        owner_task = asyncio.current_task()
        heartbeat = asyncio.create_task(self._heartbeat(task_id, owner_task))
        try:
            async for event in producer_factory():
                self.event_store.append(
                    task_id,
                    event,
                    worker_id=self.worker_id,
                )
            producer_finished = True
        except LeaseOwnershipLost:
            error = "detached execution worker lost its lease"
        except asyncio.CancelledError:
            # Preserve the open lease generation.  A replacement process will
            # recover it after expiry; cancellation must not forge FAILED.
            error = "detached execution worker was interrupted"
            raise
        except Exception as exc:  # noqa: BLE001 - producer must publish a terminal
            error = f"detached execution worker failed: {exc}"
            logger.exception("Detached execution failed for task %s", task_id)
            self._record_failure(task_id, error)
            producer_finished = True
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            if producer_finished:
                self.event_store.mark_producer_done(
                    task_id,
                    error=error,
                    worker_id=self.worker_id,
                )

    async def _heartbeat(
        self,
        task_id: str,
        owner_task: Optional[asyncio.Task[Any]],
    ) -> None:
        interval = max(1.0, self.lease_seconds / 3.0)
        while True:
            await asyncio.sleep(interval)
            if not self.event_store.renew_lease(
                task_id,
                worker_id=self.worker_id,
                lease_seconds=self.lease_seconds,
            ):
                if owner_task is not None and not owner_task.done():
                    owner_task.cancel()
                return

    def recover_expired_tasks(self, *, now: Optional[datetime] = None) -> list[dict[str, Any]]:
        """Convert expired process-local workers into durable recovery records."""

        current = now or datetime.now(timezone.utc)
        recovered: list[dict[str, Any]] = []
        checkpoints = CheckpointManager()
        controls = TaskControlStore()
        for task_id in self.event_store.list_task_ids():
            meta = self.event_store.metadata(task_id)
            if meta.get("producer_done"):
                continue
            try:
                expiry = datetime.fromisoformat(str(meta.get("lease_expires_at") or ""))
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
            if current < expiry or self.is_running(task_id):
                continue
            task = TaskLogger.load(task_id)
            if task is None or str(task.status).upper() != "RUNNING":
                continue
            recovery_worker_id = f"recovery:{self.worker_id}"
            if not self.event_store.claim_expired_lease(
                task_id,
                expected_worker_id=str(meta.get("worker_id") or ""),
                recovery_worker_id=recovery_worker_id,
                now=current,
            ):
                continue
            listed = checkpoints.list_checkpoints(task_id=task_id)
            checkpoint_step = max(
                (int(item.get("step")) for item in listed if item.get("step") is not None),
                default=-1,
            )
            completed_steps: list[str] = []
            if checkpoint_step >= 0:
                try:
                    checkpoint = checkpoints.load_checkpoint(
                        task_id=task_id, step=checkpoint_step
                    )
                    completed_steps = list(checkpoint.state.get("completed_steps") or [])
                except Exception:  # noqa: BLE001 - recovery must fail closed
                    checkpoint_step = -1

            open_steps: set[str] = set()
            for attempt in task.orchestration_attempts or []:
                step_id = str(attempt.get("step_id") or "")
                if attempt.get("event") == "start":
                    open_steps.add(step_id)
                elif attempt.get("event") == "end":
                    open_steps.discard(step_id)
            graph_steps = {
                str(step.get("step_id") or ""): step
                for step in (task.task_graph or {}).get("steps", [])
                if isinstance(step, dict)
            }
            uncertain_side_effects = sorted(
                step_id
                for step_id in open_steps
                if (
                    str(graph_steps.get(step_id, {}).get("operation_mode") or "read").lower()
                    != "read"
                    or bool(graph_steps.get(step_id, {}).get("external_side_effect"))
                )
            )
            requires_review = checkpoint_step < 0 or bool(uncertain_side_effects)
            resume_step = checkpoint_step + 1 if checkpoint_step >= 0 else 0
            control = controls.mark_recovery_required(
                task_id,
                checkpoint_step=checkpoint_step,
                resume_step=resume_step,
                completed_steps=completed_steps,
                requires_review=requires_review,
                uncertain_side_effect_steps=uncertain_side_effects,
                reason="worker_lease_expired",
            )
            task.mark_recovery_required("detached worker lease expired")
            self.event_store.append(
                task_id,
                {"event": "workflow_interrupted", "data": dict(control)},
                worker_id=recovery_worker_id,
            )
            self.event_store.mark_producer_done(
                task_id,
                error="detached worker lease expired",
                worker_id=recovery_worker_id,
            )
            recovered.append(control)
        return recovered

    def _record_failure(self, task_id: str, reason: str) -> None:
        if not self.event_store.renew_lease(
            task_id,
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
        ):
            return
        task_log = TaskLogger.load(task_id)
        if task_log is not None and task_log.status in {"running", "reserved"}:
            task_log.log_workflow_terminal("FAILED", error=reason)
        meta = self.event_store.metadata(task_id)
        if meta.get("terminal_status"):
            return
        try:
            self.event_store.append(
                task_id,
                {
                    "event": "error",
                    "data": {"task_id": task_id, "error": reason},
                },
                worker_id=self.worker_id,
            )
            self.event_store.append(
                task_id,
                {
                    "event": "end_of_workflow",
                    "data": {"task_id": task_id, "status": "FAILED"},
                },
                worker_id=self.worker_id,
            )
        except LeaseOwnershipLost:
            logger.warning("Failure event lease was lost for task %s", task_id)
