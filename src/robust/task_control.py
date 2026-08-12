"""Persistent cooperative pause control for scheduler task executions.

The control record is deliberately stored separately from ``TaskLogger``.  A
running workflow writes its task log frequently; keeping control state in a
separate atomically-updated file prevents an in-memory logger from overwriting a
pause request submitted by another HTTP request.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from config.global_variables import checkpoints_dir
from src.utils.file_lock import FileLock


ACTIVE_STATES = {"RUNNING", "PAUSE_REQUESTED"}
TERMINAL_STATES = {
    "SUCCEEDED",
    "FAILED",
    "PARTIAL_FAILED",
    "CLARIFY_REQUIRED",
    "APPROVAL_REQUIRED",
    "REJECTED",
    "NEEDS_RECONCILIATION",
    "CANCELLED",
}


def _get_task_controls_dir() -> Path:
    configured = str(os.getenv("TASK_CONTROL_STORE_DIR") or "").strip()
    path = Path(configured) if configured else checkpoints_dir.parent / "task_controls"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
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
            json.dump(payload, stream, indent=2, ensure_ascii=False, default=str)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskControlStore:
    """File-backed control plane for one task's cooperative pause lifecycle."""

    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = Path(base_dir) if base_dir is not None else _get_task_controls_dir()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, task_id: str) -> Path:
        normalized = str(task_id or "").strip()
        if not normalized:
            raise ValueError("task_id is required")
        safe = "".join(
            character if character.isalnum() or character in ("-", "_") else "_"
            for character in normalized
        )
        if not safe:
            raise ValueError("task_id is invalid")
        return self.base_dir / f"{safe}.json"

    def get(self, task_id: str) -> Optional[dict[str, Any]]:
        path = self._path(task_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def ensure_running(
        self,
        task_id: str,
        *,
        workflow_id: str = "",
        user_id: str = "",
    ) -> dict[str, Any]:
        """Create the control record without erasing an early pause request."""

        path = self._path(task_id)
        with FileLock(path):
            current = self.get(task_id)
            if current and current.get("state") in {
                "PAUSE_REQUESTED",
                "PAUSED",
                "RECOVERY_REQUIRED",
            }:
                return current
            now = _now()
            payload = {
                **(current or {}),
                "task_id": task_id,
                "workflow_id": workflow_id or (current or {}).get("workflow_id", ""),
                "user_id": user_id or (current or {}).get("user_id", ""),
                "state": "RUNNING",
                "updated_at": now,
                "started_at": (current or {}).get("started_at") or now,
                "pause_requested_at": "",
                "paused_at": "",
                "reason": "",
            }
            _atomic_write(path, payload)
            return payload

    def request_pause(
        self,
        task_id: str,
        *,
        workflow_id: str = "",
        user_id: str = "",
        reason: str = "user_requested",
    ) -> dict[str, Any]:
        path = self._path(task_id)
        with FileLock(path):
            current = self.get(task_id) or {}
            state = str(current.get("state") or "RUNNING").upper()
            if state == "PAUSED" or state == "PAUSE_REQUESTED":
                return current
            if state == "RECOVERY_REQUIRED":
                raise ValueError("task requires recovery review or checkpoint resume")
            if state in TERMINAL_STATES:
                raise ValueError(f"task cannot be paused in state={state}")
            now = _now()
            payload = {
                **current,
                "task_id": task_id,
                "workflow_id": workflow_id or current.get("workflow_id", ""),
                "user_id": user_id or current.get("user_id", ""),
                "state": "PAUSE_REQUESTED",
                "pause_requested_at": now,
                "updated_at": now,
                "reason": str(reason or "user_requested")[:256],
            }
            _atomic_write(path, payload)
            return payload

    def pause_requested(self, task_id: str) -> bool:
        current = self.get(task_id)
        return bool(current and current.get("state") == "PAUSE_REQUESTED")

    def mark_paused(
        self,
        task_id: str,
        *,
        checkpoint_step: int,
        resume_step: int,
        completed_steps: list[str],
    ) -> dict[str, Any]:
        path = self._path(task_id)
        with FileLock(path):
            current = self.get(task_id) or {"task_id": task_id}
            now = _now()
            payload = {
                **current,
                "state": "PAUSED",
                "checkpoint_step": int(checkpoint_step),
                "resume_step": int(resume_step),
                "completed_steps": list(completed_steps),
                "paused_at": now,
                "updated_at": now,
            }
            _atomic_write(path, payload)
            return payload

    def resume(self, task_id: str, *, user_id: str = "") -> dict[str, Any]:
        path = self._path(task_id)
        with FileLock(path):
            current = self.get(task_id)
            if current is None:
                raise ValueError("task control record not found")
            if str(current.get("state") or "").upper() not in {"PAUSED", "RECOVERY_REQUIRED"}:
                raise ValueError(
                    f"task cannot resume in state={current.get('state') or 'UNKNOWN'}"
                )
            owner = str(current.get("user_id") or "")
            if owner and user_id and owner != user_id:
                raise PermissionError("task control owner mismatch")
            now = _now()
            payload = {
                **current,
                "state": "RUNNING",
                "pause_requested_at": "",
                "paused_at": "",
                "resumed_at": now,
                "updated_at": now,
                "reason": "",
            }
            _atomic_write(path, payload)
            return payload

    def mark_recovery_required(
        self,
        task_id: str,
        *,
        checkpoint_step: int,
        resume_step: int,
        completed_steps: list[str],
        requires_review: bool,
        uncertain_side_effect_steps: list[str],
        reason: str,
    ) -> dict[str, Any]:
        path = self._path(task_id)
        with FileLock(path):
            current = self.get(task_id) or {"task_id": task_id}
            now = _now()
            payload = {
                **current,
                "state": "RECOVERY_REQUIRED",
                "checkpoint_step": int(checkpoint_step),
                "resume_step": int(resume_step),
                "completed_steps": list(completed_steps),
                "requires_review": bool(requires_review),
                "uncertain_side_effect_steps": list(uncertain_side_effect_steps),
                "interrupted_at": now,
                "updated_at": now,
                "reason": str(reason)[:256],
            }
            _atomic_write(path, payload)
            return payload

    def mark_terminal(self, task_id: str, status: Any) -> dict[str, Any]:
        normalized = str(getattr(status, "value", status) or "FAILED").upper()
        path = self._path(task_id)
        with FileLock(path):
            current = self.get(task_id) or {"task_id": task_id}
            now = _now()
            payload = {
                **current,
                "state": normalized,
                "updated_at": now,
                "finished_at": now,
            }
            _atomic_write(path, payload)
            return payload

    def delete(self, task_id: str) -> int:
        path = self._path(task_id)
        try:
            path.unlink()
            return 1
        except FileNotFoundError:
            return 0
