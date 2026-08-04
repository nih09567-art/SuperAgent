"""
TaskLogger: Structured logging for each task execution instance.
Log format is inspired by the Agents_Failure_Attribution dataset (Who&When),
designed to support post-hoc failure attribution and step-level rollback.

Log structure per task (stored as JSON):
{
    "task_id": "...",
    "workflow_id": "...",
    "user_query": "...",
    "created_at": "...",
    "finished_at": "...",
    "status": "running|completed|failed|<canonical scheduler terminal status>",
    "history": [
        {
            "step": 0,
            "node_name": "coordinator",
            "role": "coordinator",
            "content": "...",
            "timestamp": "...",
            "event": "start_of_agent|end_of_agent|message|error"
        },
        ...
    ],
    "error": null | "error message if failed"
}
"""

import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.global_variables import checkpoints_dir
from src.utils.file_lock import FileLock

logger = logging.getLogger(__name__)

RESERVATION_EXPIRED_CODE = "RESERVATION_EXPIRED"
DEFAULT_RESERVATION_LEASE_SECONDS = 120

SCHEDULER_TERMINAL_STATUSES = {
    "SUCCEEDED",
    "FAILED",
    "PARTIAL_FAILED",
    "CLARIFY_REQUIRED",
    "APPROVAL_REQUIRED",
    "REJECTED",
    "NEEDS_RECONCILIATION",
}


# Task logs are stored alongside checkpoints in a sibling directory
def _get_task_logs_dir() -> Path:
    logs_dir = checkpoints_dir.parent / "task_logs"
    if not logs_dir.exists():
        logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir


class TaskLogger:
    """
    Records the complete interaction history of a single task execution.
    Each task run gets its own JSON log file, keyed by task_id.
    The log format mirrors the Agents_Failure_Attribution dataset to support
    failure attribution and step-level replay/rollback.
    """

    def __init__(self, task_id: str, workflow_id: str, user_query: str = ""):
        self.task_id = task_id
        self.workflow_id = workflow_id
        self.user_query = user_query
        self.created_at = datetime.now().isoformat()
        self.finished_at: Optional[str] = None
        self.history: List[Dict[str, Any]] = []
        self.status = "running"
        self.error: Optional[str] = None
        self.execution_phase: str = "initial_planning"  # 新增: 执行阶段
        self.planning_steps: List[Dict[str, Any]] = []
        self.task_profile: Dict[str, Any] = {}
        self.agent_contract_fingerprints: Dict[str, str] = {}
        self.agent_capability_bindings: Dict[str, List[str]] = {}
        self.skill_execution_evidence: Dict[str, Any] = {}
        self.failures: List[Dict[str, Any]] = []
        self.execution_attempt_id: str = ""
        self.execution_idempotency_key: str = ""
        self.execution_plan_hash: str = ""
        self.execution_user_id: str = ""
        self.execution_authorization_token_hash: str = ""
        self.execution_authorization_claimed_at: str = ""
        self.reservation_expires_at: str = ""
        self.reservation_failure_code: str = ""
        self._step_counter: Dict[str, int] = {}  # track per-node step

        self._logs_dir = _get_task_logs_dir()
        self._log_file = self._logs_dir / f"{task_id}.json"
        logger.info(f"TaskLogger initialized: {self._log_file}")

    @classmethod
    def reserve_execution(
        cls,
        *,
        task_id: str,
        workflow_id: str,
        user_query: str,
        attempt_id: str,
        idempotency_key: str,
        plan_hash: str,
        user_id: str = "",
        authorization_token_hash: str = "",
        lease_seconds: int = DEFAULT_RESERVATION_LEASE_SECONDS,
    ) -> tuple[bool, Optional["TaskLogger"]]:
        """Atomically reserve a production task before its SSE stream starts."""

        task = cls(task_id=task_id, workflow_id=workflow_id, user_query=user_query)
        task.status = "reserved"
        task.execution_phase = "execution"
        task.execution_attempt_id = attempt_id
        task.execution_idempotency_key = idempotency_key
        task.execution_plan_hash = plan_hash
        task.execution_user_id = user_id
        task.execution_authorization_token_hash = authorization_token_hash
        task.reservation_expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=max(1, lease_seconds))
        ).isoformat()
        try:
            with open(task._log_file, "x", encoding="utf-8") as stream:
                json.dump(task.to_dict(), stream, indent=2, ensure_ascii=False, default=str)
            return True, task
        except FileExistsError:
            return False, cls.load(task_id)

    @staticmethod
    def hash_execution_authorization_token(token: str) -> str:
        return hashlib.sha256(str(token).encode("utf-8")).hexdigest()

    def reservation_is_expired(self, now: Optional[datetime] = None) -> bool:
        if self.status != "reserved" or not self.reservation_expires_at:
            return False
        try:
            expires_at = datetime.fromisoformat(self.reservation_expires_at)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
        except ValueError:
            return True
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current >= expires_at

    def expire_reservation_if_stale(self, now: Optional[datetime] = None) -> bool:
        if not self.reservation_is_expired(now):
            return False
        self.status = "FAILED"
        self.finished_at = (now or datetime.now(timezone.utc)).isoformat()
        self.error = "production execution reservation lease expired before startup"
        self.reservation_failure_code = RESERVATION_EXPIRED_CODE
        self.execution_authorization_token_hash = ""
        self._flush()
        return True

    @classmethod
    def expire_stale_reservation(
        cls, task_id: str, now: Optional[datetime] = None
    ) -> Optional["TaskLogger"]:
        log_file = _get_task_logs_dir() / f"{task_id}.json"
        with FileLock(log_file):
            task = cls.load(task_id)
            if task is not None:
                task.expire_reservation_if_stale(now)
            return task

    @classmethod
    def claim_execution_authorization(
        cls,
        *,
        task_id: str,
        authorization_token: str,
        user_id: str,
        workflow_id: str,
        plan_hash: str,
    ) -> tuple[bool, Optional["TaskLogger"], str]:
        logs_dir = _get_task_logs_dir()
        log_file = logs_dir / f"{task_id}.json"
        with FileLock(log_file):
            task = cls.load(task_id)
            if task is None:
                return False, None, "EXECUTION_AUTHORIZATION_NOT_FOUND"
            if task.expire_reservation_if_stale():
                return False, task, RESERVATION_EXPIRED_CODE
            if task.status != "reserved":
                return False, task, "EXECUTION_AUTHORIZATION_NOT_RESERVED"
            supplied_hash = cls.hash_execution_authorization_token(authorization_token)
            identity_matches = (
                bool(task.execution_authorization_token_hash)
                and hmac.compare_digest(
                    supplied_hash, task.execution_authorization_token_hash
                )
                and hmac.compare_digest(task.execution_user_id, user_id)
                and hmac.compare_digest(task.workflow_id, workflow_id)
                and hmac.compare_digest(task.execution_plan_hash, plan_hash)
            )
            if not identity_matches:
                return False, task, "EXECUTION_AUTHORIZATION_MISMATCH"
            if task.execution_authorization_claimed_at:
                return False, task, "EXECUTION_AUTHORIZATION_ALREADY_CLAIMED"
            task.execution_authorization_claimed_at = datetime.now(timezone.utc).isoformat()
            task._flush()
            return True, task, ""

    @classmethod
    def renew_expired_execution_reservation(
        cls,
        *,
        task_id: str,
        user_query: str,
        attempt_id: str,
        idempotency_key: str,
        plan_hash: str,
        user_id: str,
        authorization_token_hash: str,
        lease_seconds: int = DEFAULT_RESERVATION_LEASE_SECONDS,
    ) -> tuple[bool, Optional["TaskLogger"]]:
        logs_dir = _get_task_logs_dir()
        log_file = logs_dir / f"{task_id}.json"
        with FileLock(log_file):
            task = cls.load(task_id)
            if (
                task is None
                or task.status != "FAILED"
                or task.reservation_failure_code != RESERVATION_EXPIRED_CODE
            ):
                return False, task
            task.user_query = user_query
            task.status = "reserved"
            task.finished_at = None
            task.error = None
            task.execution_attempt_id = attempt_id
            task.execution_idempotency_key = idempotency_key
            task.execution_plan_hash = plan_hash
            task.execution_user_id = user_id
            task.execution_authorization_token_hash = authorization_token_hash
            task.execution_authorization_claimed_at = ""
            task.reservation_expires_at = (
                datetime.now(timezone.utc) + timedelta(seconds=max(1, lease_seconds))
            ).isoformat()
            task.reservation_failure_code = ""
            task._flush()
            return True, task

    @classmethod
    def expire_stale_reservations(cls) -> int:
        expired = 0
        logs_dir = _get_task_logs_dir()
        for log_file in logs_dir.glob("*.json"):
            with FileLock(log_file):
                task = cls.load(log_file.stem)
                if task is not None and task.expire_reservation_if_stale():
                    expired += 1
        return expired

    def activate_reserved_execution(self) -> None:
        if self.status != "reserved":
            raise RuntimeError(f"task {self.task_id} is not reserved")
        if self.expire_reservation_if_stale():
            raise RuntimeError(f"task {self.task_id} reservation lease expired")
        if not self.execution_authorization_claimed_at:
            raise RuntimeError(f"task {self.task_id} authorization was not claimed")
        self.status = "running"
        self.finished_at = None
        self.error = None
        self.reservation_expires_at = ""
        self.execution_authorization_token_hash = ""
        self._flush()

    def _next_step(self, node_name: str) -> int:
        """Return the current global step count (shared across nodes)."""
        count = self._step_counter.get("__global__", -1) + 1
        self._step_counter["__global__"] = count
        return count

    def truncate_for_resume(self, resume_step: int) -> None:
        """Roll the log back to just before ``resume_step`` for a re-run.

        Removes history entries from ``resume_step`` onwards (and any
        ``workflow_end``), rebuilds :attr:`failures` from the retained
        history so a successful re-run no longer reports the previous
        attempt's failures, and resets the terminal fields.
        """

        self.history = [
            entry for entry in self.history
            if entry.get("step", 0) < resume_step and entry.get("event") != "workflow_end"
        ]
        self.failures = [
            entry.get("failure")
            for entry in self.history
            if entry.get("event") == "step_failure" and entry.get("failure")
        ]
        self.status = "running"
        self.finished_at = None
        self.error = None
        self._step_counter = {"__global__": resume_step - 1}

    def log_event(
        self,
        node_name: str,
        event: str,
        content: str = "",
        step: Optional[int] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Append a log entry.

        Args:
            node_name: The agent/node name
            event: Event type: start_of_agent | end_of_agent | message | error | workflow_start | workflow_end
            content: Text content of the event
            step: Explicit step number (auto-incremented if not provided)
            extra: Optional extra fields merged into the entry
        """
        if step is None:
            step = self._next_step(node_name)
        else:
            # Update step counter when explicit step is provided
            # to keep it in sync for subsequent auto-increment calls
            self._step_counter["__global__"] = step

        entry: Dict[str, Any] = {
            "step": step,
            "node_name": node_name,
            "role": node_name,
            "event": event,
            "content": content,
            "timestamp": datetime.now().isoformat(),
        }
        if extra:
            entry.update(extra)
        self.history.append(entry)
        self._flush()

    def log_message(self, node_name: str, content: str, step: Optional[int] = None) -> None:
        """Log an agent output message."""
        self.log_event(node_name=node_name, event="message", content=content, step=step)

    def log_agent_start(
        self,
        node_name: str,
        step: Optional[int] = None,
        sub_agent_name: Optional[str] = None,
        *,
        attempt: Optional[int] = None,
        phase: Optional[str] = None,
        planned_agent: Optional[str] = None,
        executed_agent: Optional[str] = None,
    ) -> None:
        """Log the start of an agent node."""
        display_name = f"{node_name}【{sub_agent_name}】" if sub_agent_name else node_name
        extra: Dict[str, Any] = {}
        if sub_agent_name:
            extra["sub_agent_name"] = sub_agent_name
        if attempt is not None:
            extra["attempt"] = attempt
        if phase:
            extra["phase"] = phase
        if planned_agent:
            extra["planned_agent"] = planned_agent
        if executed_agent:
            extra["selected_agent"] = executed_agent
            extra["executed_agent"] = executed_agent
        self.log_event(
            node_name=node_name,
            event="start_of_agent",
            content=f"Agent {display_name} started",
            step=step,
            extra=extra or None,
        )

    def log_agent_end(
        self,
        node_name: str,
        next_node: Optional[str] = None,
        step: Optional[int] = None,
        sub_agent_name: Optional[str] = None,
        *,
        attempt: Optional[int] = None,
        phase: Optional[str] = None,
        planned_agent: Optional[str] = None,
        executed_agent: Optional[str] = None,
    ) -> None:
        """Log the end of an agent node."""
        display_name = f"{node_name}【{sub_agent_name}】" if sub_agent_name else node_name
        content = f"Agent {display_name} finished"
        if next_node:
            content += f" -> {next_node}"
        extra = {"next_node": next_node}
        if sub_agent_name:
            extra["sub_agent_name"] = sub_agent_name
        if attempt is not None:
            extra["attempt"] = attempt
        if phase:
            extra["phase"] = phase
        if planned_agent:
            extra["planned_agent"] = planned_agent
        if executed_agent:
            extra["selected_agent"] = executed_agent
            extra["executed_agent"] = executed_agent
        self.log_event(node_name=node_name, event="end_of_agent", content=content, step=step, extra=extra)

    def log_workflow_start(self, user_query: str = "") -> None:
        """Log workflow start."""
        if user_query:
            self.user_query = user_query
        self.log_event(node_name="system", event="workflow_start",
                       content=f"Workflow started. Query: {user_query}", step=0)

    def log_workflow_end(self) -> None:
        """Log workflow successful completion."""
        if self.status not in {"running", "reserved"}:
            return
        self.status = "completed"
        self.finished_at = datetime.now().isoformat()
        self.log_event(node_name="system", event="workflow_end", content="Workflow completed successfully.")

    def log_workflow_terminal(self, status: Any, error: Optional[str] = None) -> None:
        """Persist one canonical Scheduler terminal status exactly once."""
        normalized = str(getattr(status, "value", status) or "").upper()
        if normalized not in SCHEDULER_TERMINAL_STATUSES:
            raise ValueError(f"unsupported scheduler terminal status: {normalized!r}")
        if self.status not in {"running", "reserved"}:
            return

        self.status = normalized
        self.finished_at = datetime.now().isoformat()
        self.error = error
        content = f"Scheduler workflow ended with status {normalized}."
        if error:
            content += f" {error}"
        self.log_event(
            node_name="scheduler",
            event="workflow_end",
            content=content,
            extra={"terminal_status": normalized},
        )

    def log_error(self, error: str, node_name: str = "system", step: Optional[int] = None) -> None:
        """Log an error event."""
        self.status = "failed"
        self.finished_at = datetime.now().isoformat()
        self.error = error
        self.log_event(node_name=node_name, event="error", content=error, step=step)

    def log_failure(
        self,
        failure: Dict[str, Any],
        *,
        node_name: str = "scheduler",
        step: Optional[int] = None,
    ) -> None:
        """Persist a payload-free, structured step failure.

        Unlike :meth:`log_error`, a step failure does not finalize the task:
        independent DAG branches may still complete before the Scheduler emits
        the authoritative workflow terminal status.
        """

        safe_failure = dict(failure or {})
        if not safe_failure:
            return
        self.failures.append(safe_failure)
        self.log_event(
            node_name=node_name,
            event="step_failure",
            content=str(safe_failure.get("message") or safe_failure.get("code") or "step failed"),
            step=step,
            extra={
                "failure_code": safe_failure.get("code"),
                "failure_category": safe_failure.get("category"),
                "retryable": bool(safe_failure.get("retryable", False)),
                "failure": safe_failure,
            },
        )

    def set_execution_phase(self, execution_phase: str) -> None:
        """设置执行阶段"""
        self.execution_phase = execution_phase
        self._flush()

    def set_workflow_snapshot(
        self,
        planning_steps: List[Dict[str, Any]],
        task_profile: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Persist the approved plan and scenario used by this execution."""

        self.planning_steps = [dict(step) for step in planning_steps if isinstance(step, dict)]
        self.task_profile = dict(task_profile or {})
        self._flush()

    def set_agent_contract_fingerprints(self, fingerprints: Dict[str, str]) -> None:
        """Persist versioned Agent contracts used by the approved plan."""

        self.agent_contract_fingerprints = {
            str(name): str(value)
            for name, value in fingerprints.items()
            if name and value
        }
        self._flush()

    def set_agent_capability_bindings(
        self, bindings: Dict[str, List[str]]
    ) -> None:
        """Persist non-sensitive Agent capability declarations for compilation."""

        self.agent_capability_bindings = {
            str(name): [str(item) for item in capabilities if str(item)]
            for name, capabilities in bindings.items()
            if name
        }
        self._flush()

    def set_skill_execution_evidence(self, evidence: Dict[str, Any]) -> None:
        """Persist payload-free evidence used by workflow-skill distillation."""

        self.skill_execution_evidence = dict(evidence or {})
        self._flush()

    @staticmethod
    def determine_execution_phase(workmode: str, instruction_history: List[str]) -> str:
        """
        判断执行阶段（静态方法，解耦主流程）

        Args:
            workmode: 工作模式 ("launch" 或 "production")
            instruction_history: 指令历史列表

        Returns:
            执行阶段: "initial_planning" | "re_planning" | "execution"
        """
        # 优先级1: workmode="production" → 确认执行
        if workmode == "production":
            return "execution"

        # 优先级2: instruction_history长度判断
        if len(instruction_history) <= 1:
            return "initial_planning"
        else:
            return "re_planning"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "workflow_id": self.workflow_id,
            "user_query": self.user_query,
            "execution_phase": self.execution_phase,  # 新增
            "planning_steps": self.planning_steps,
            "task_profile": self.task_profile,
            "agent_contract_fingerprints": self.agent_contract_fingerprints,
            "agent_capability_bindings": self.agent_capability_bindings,
            "skill_execution_evidence": self.skill_execution_evidence,
            "failures": self.failures,
            "execution_attempt_id": self.execution_attempt_id,
            "execution_idempotency_key": self.execution_idempotency_key,
            "execution_plan_hash": self.execution_plan_hash,
            "execution_user_id": self.execution_user_id,
            "execution_authorization_token_hash": self.execution_authorization_token_hash,
            "execution_authorization_claimed_at": self.execution_authorization_claimed_at,
            "reservation_expires_at": self.reservation_expires_at,
            "reservation_failure_code": self.reservation_failure_code,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "history": self.history,
            "error": self.error,
        }

    def _flush(self) -> None:
        """Write current log state to disk."""
        try:
            with open(self._log_file, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, indent=2, ensure_ascii=False, default=str)
        except Exception as e:
            logger.error(f"Failed to flush task log: {e}")

    @classmethod
    def load(cls, task_id: str) -> Optional["TaskLogger"]:
        """Load an existing task log from disk."""
        logs_dir = _get_task_logs_dir()
        log_file = logs_dir / f"{task_id}.json"
        if not log_file.exists():
            return None
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            inst = cls.__new__(cls)
            inst.task_id = data.get("task_id", task_id)
            inst.workflow_id = data.get("workflow_id", "")
            inst.user_query = data.get("user_query", "")
            inst.created_at = data.get("created_at", "")
            inst.finished_at = data.get("finished_at")
            inst.history = data.get("history", [])
            inst.status = data.get("status", "unknown")
            inst.error = data.get("error")

            # 兼容性处理：如果缺少新字段，设置默认值
            inst.execution_phase = data.get("execution_phase", "initial_planning")
            inst.planning_steps = data.get("planning_steps", [])
            inst.task_profile = data.get("task_profile", {})
            inst.agent_contract_fingerprints = data.get(
                "agent_contract_fingerprints", {}
            )
            inst.agent_capability_bindings = data.get(
                "agent_capability_bindings", {}
            )
            inst.skill_execution_evidence = data.get(
                "skill_execution_evidence", {}
            )
            inst.failures = data.get("failures", [])
            inst.execution_attempt_id = data.get("execution_attempt_id", "")
            inst.execution_idempotency_key = data.get("execution_idempotency_key", "")
            inst.execution_plan_hash = data.get("execution_plan_hash", "")
            inst.execution_user_id = data.get("execution_user_id", "")
            inst.execution_authorization_token_hash = data.get(
                "execution_authorization_token_hash", ""
            )
            inst.execution_authorization_claimed_at = data.get(
                "execution_authorization_claimed_at", ""
            )
            inst.reservation_expires_at = data.get("reservation_expires_at", "")
            inst.reservation_failure_code = data.get("reservation_failure_code", "")

            inst._step_counter = {"__global__": len(inst.history)}
            inst._logs_dir = logs_dir
            inst._log_file = log_file
            return inst
        except Exception as e:
            logger.error(f"Failed to load task log {task_id}: {e}")
            return None

    @classmethod
    def list_tasks(cls, workflow_id: Optional[str] = None, execution_phase: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        List all task log summaries (without full history).
        Optionally filter by workflow_id and execution_phase.
        Returns list sorted newest first.
        """
        cls.expire_stale_reservations()
        logs_dir = _get_task_logs_dir()
        results = []
        if not logs_dir.exists():
            return results
        for log_file in logs_dir.glob("*.json"):
            try:
                with open(log_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if workflow_id and data.get("workflow_id") != workflow_id:
                    continue
                if execution_phase and data.get("execution_phase") != execution_phase:
                    continue

                # 兼容性处理：如果缺少新字段，设置默认值
                task_execution_phase = data.get("execution_phase", "initial_planning")

                results.append({
                    "task_id": data.get("task_id", log_file.stem),
                    "workflow_id": data.get("workflow_id", ""),
                    "user_query": data.get("user_query", ""),
                    "execution_phase": task_execution_phase,
                    "created_at": data.get("created_at", ""),
                    "finished_at": data.get("finished_at"),
                    "status": data.get("status", "unknown"),
                    "step_count": len(data.get("history", [])),
                    "error": data.get("error"),
                    "failure_count": len(data.get("failures", [])),
                    "execution_attempt_id": data.get("execution_attempt_id", ""),
                    "execution_plan_hash": data.get("execution_plan_hash", ""),
                    "reservation_expires_at": data.get("reservation_expires_at", ""),
                    "reservation_failure_code": data.get("reservation_failure_code", ""),
                })
            except Exception:
                continue
        results.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return results
