"""Real HTTP harness for the annual-leave defense workflow.

The harness deliberately starts the same four local services used by the demo
runbook and talks to the Web API.  It does not construct a TaskGraph or call
the Scheduler directly.  The only local reads after execution are evidence
collection from the task/checkpoint APIs and the protected Artifact payload
store created by that execution.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from src.contracts.agent_schema_catalog import register_agent_schemas
from src.interface.artifact import Artifact
from src.orchestration.markdown_artifact_exporter import export_markdown_artifact
from src.orchestration.plan_to_task_graph import plan_to_task_graph
from src.orchestration.schema_registry import SchemaRegistry

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    # The harness can also be driven by an explicitly configured process
    # environment; dotenv is only a convenience for this local demo.
    pass

WEB_PORT = 8001
REMOTE_AGENT_PORT = 8010
REMOTE_TOOL_PORT = 8011
REMOTE_REGISTRY_PORT = 8012
WEB_BASE_URL = f"http://127.0.0.1:{WEB_PORT}"
EXECUTION_USER_ID = "admin"
EXECUTION_USER_API_KEY = "annual-leave-demo-execution-key"

ANNUAL_LEAVE_QUERY = (
    "请查询员工王强的在职状态、岗位和累计工龄，并依据国务院关于职工带薪年休假的规定，"
    "判断其年假天数，生成一份 Markdown 汇总。"
)
EXPECTED_AGENTS = {
    "RemoteHRAssistantAgent",
    "RemoteKnowledgeAgent",
    "RemoteReportAgent",
}
DYNAMIC_FIVE_QUERY = (
    "查询王强的工龄和年假政策，查询历史请假记录，生成报告，"
    "经确认后发送给 hr@example.test。"
)
DYNAMIC_FIVE_AGENTS = EXPECTED_AGENTS | {
    "RemoteOfficeAssistantAgent",
    "RemoteEmailDispatchAgent",
}
SENSITIVE_SAMPLE_VALUES = (
    EXECUTION_USER_API_KEY,
    "EMP-DO-NOT-LEAK",
    "020-60000003",
    "wangqiang@ccb.com",
    "1982-11-05",
)


class AnnualLeaveE2EError(RuntimeError):
    """Raised when a real demo run cannot satisfy an acceptance invariant."""


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    command: tuple[str, ...]
    port: int
    health_path: str


@dataclass
class RunningService:
    spec: ServiceSpec
    process: subprocess.Popen
    stdout_handle: Any
    stderr_handle: Any
    stdout_path: Path
    stderr_path: Path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_json(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _safe_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_json(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def redact_evidence(value: Any) -> Any:
    """Redact secrets, stack traces and unrelated HR fields from evidence."""

    # Keys are normalized below by removing punctuation, so keep this set in
    # the same normalized form (notably ``internal_url`` -> ``internalurl``).
    sensitive_keys = {
        "authorization",
        "apikey",
        "secret",
        "token",
        "traceback",
        "stacktrace",
        "internalurl",
    }
    forbidden_keys = {
        "idvid",
        "officephone",
        "internalmaibox",
        "brthdt",
        "employee_id",
        "employeeid",
        "empeinfbt lmprbtnc".replace(" ", ""),
        "monthly_salary",
        "annual_salary",
        "salary_breakdown",
    }
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in sensitive_keys or normalized in forbidden_keys:
                continue
            if normalized in {"url", "endpoint", "baseurl", "healthurl"}:
                text = str(item)
                if "127.0.0.1" not in text and "localhost" not in text:
                    output[str(key)] = "[REDACTED_URL]"
                    continue
            output[str(key)] = redact_evidence(item)
        return output
    if isinstance(value, list):
        return [redact_evidence(item) for item in value]
    if isinstance(value, str):
        redacted = value
        for sample in SENSITIVE_SAMPLE_VALUES:
            redacted = redacted.replace(sample, "[REDACTED]")
        redacted = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[REDACTED]", redacted)
        redacted = re.sub(
            r"(?i)(api[_-]?key|token|secret)\s*[=:]\s*[^\s,;]+",
            r"\1=[REDACTED]",
            redacted,
        )
        if "Traceback (most recent call last)" in redacted:
            return "[REDACTED_STACKTRACE]"
        return redacted
    return value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(redact_evidence(_safe_json(value)), ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _http_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> Any:
    body = None
    request_headers = {"Accept": "application/json"}
    request_headers.update(headers or {})
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=request_headers, method=method)
    try:
        with urlopen(
            request, timeout=timeout
        ) as response:  # noqa: S310 - local demo endpoint
            raw = response.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise AnnualLeaveE2EError(
            f"HTTP {exc.code} from {url}: {redact_evidence(raw)}"
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise AnnualLeaveE2EError(
            f"HTTP request failed for {url}: {type(exc).__name__}"
        ) from exc


def _iter_sse(response: Any) -> Iterable[dict[str, Any]]:
    event_name = "message"
    data_lines: list[str] = []
    for raw_line in response:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            if data_lines:
                data_text = "\n".join(data_lines)
                try:
                    payload = json.loads(data_text)
                except json.JSONDecodeError as exc:
                    raise AnnualLeaveE2EError(
                        f"SSE data was not JSON for event {event_name!r}"
                    ) from exc
                if isinstance(payload, dict):
                    yield payload
                data_lines = []
            event_name = "message"
            continue
        if line.startswith("event:"):
            event_name = line.partition(":")[2].strip() or "message"
        elif line.startswith("data:"):
            data_lines.append(line.partition(":")[2].lstrip())
    if data_lines:
        try:
            payload = json.loads("\n".join(data_lines))
        except json.JSONDecodeError as exc:
            raise AnnualLeaveE2EError("final SSE data was not JSON") from exc
        if isinstance(payload, dict):
            yield payload


def _extract_plan_from_text(text: str) -> list[dict[str, Any]]:
    candidates = [text.strip()]
    first_obj, last_obj = text.find("{"), text.rfind("}")
    if first_obj >= 0 and last_obj > first_obj:
        candidates.append(text[first_obj : last_obj + 1])
    first_arr, last_arr = text.find("["), text.rfind("]")
    if first_arr >= 0 and last_arr > first_arr:
        candidates.append(text[first_arr : last_arr + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("steps"), list):
            return [item for item in parsed["steps"] if isinstance(item, dict)]
        if isinstance(parsed, dict) and isinstance(parsed.get("planning_steps"), list):
            return [item for item in parsed["planning_steps"] if isinstance(item, dict)]
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
    return []


def _timeline_event(event: dict[str, Any], sequence: int) -> dict[str, Any] | None:
    event_type = str(event.get("event") or "")
    if event_type not in {"start_of_agent", "end_of_agent"}:
        return None
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    step_id = data.get("step_id")
    planned_agent = data.get("planned_agent") or data.get("sub_agent_name")
    executed_agent = (
        data.get("executed_agent")
        or data.get("selected_agent")
        or data.get("sub_agent_name")
    )
    if not step_id or not (planned_agent or executed_agent):
        # Planner/UI lifecycle cards do not identify a concrete scheduler step
        # and Agent, so they are intentionally excluded from the timing proof.
        return None
    # Read-only steps emit explicit attempt callbacks.  Non-read steps use the
    # scheduler's step-start/end callbacks because they are single-attempt
    # operations; those events are still the actual attempt boundary and carry
    # no explicit attempt field.
    attempt = data.get("attempt")
    if attempt is None:
        attempt = 1
    phase = data.get("phase") or "primary"
    return {
        "sequence": sequence,
        "observed_at_utc": _utc_now(),
        "monotonic_ns": time.monotonic_ns(),
        "step_id": step_id,
        "planned_agent": planned_agent,
        "executed_agent": executed_agent,
        "attempt": attempt,
        "phase": phase,
        "status": "RUNNING" if event_type == "start_of_agent" else data.get("status"),
    }


class AnnualLeaveServiceManager:
    """Own and clean up only the services launched by this harness."""

    def __init__(
        self,
        *,
        project_root: Path | None = None,
        log_dir: Path | None = None,
        python_executable: str | None = None,
        fault_mode: str = "",
        s_abac_enabled: bool = False,
    ) -> None:
        self.project_root = (
            project_root or Path(__file__).resolve().parents[2]
        ).resolve()
        self.log_dir = (
            log_dir or self.project_root / "artifacts" / "annual-leave-service-logs"
        ).resolve()
        self.python_executable = python_executable or sys.executable
        self.fault_mode = fault_mode
        self.s_abac_enabled = s_abac_enabled
        self.running: list[RunningService] = []
        self._closed = False

    @property
    def specs(self) -> tuple[ServiceSpec, ...]:
        python = str(self.python_executable)
        return (
            ServiceSpec(
                "remote-registry",
                (python, "-u", "mock_remote_registry.py"),
                REMOTE_REGISTRY_PORT,
                "/health",
            ),
            ServiceSpec(
                "remote-tool",
                (python, "-u", "mock_remote_tool_skill.py"),
                REMOTE_TOOL_PORT,
                "/health",
            ),
            ServiceSpec(
                "remote-agent",
                (python, "-u", "mock_remote_agent.py"),
                REMOTE_AGENT_PORT,
                "/health",
            ),
            ServiceSpec(
                "web",
                (
                    python,
                    "-u",
                    "cli.py",
                    "web",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(WEB_PORT),
                ),
                WEB_PORT,
                "/api/health/ready",
            ),
        )

    def start(self, *, timeout_seconds: float = 90.0) -> None:
        if self.running:
            return
        occupied = [spec for spec in self.specs if _port_in_use(spec.port)]
        if occupied:
            details = ", ".join(f"{item.name}:{item.port}" for item in occupied)
            raise AnnualLeaveE2EError(
                f"refusing to share occupied demo port(s): {details}"
            )

        self.log_dir.mkdir(parents=True, exist_ok=True)
        mock_email_log = self.log_dir / "mock-email-log.json"
        mock_email_log.write_text('{"emails": []}\n', encoding="utf-8")
        env = dict(os.environ)
        env.update(
            {
                "APP_ENV": "production",
                "ORCHESTRATION_SCHEDULER_ENABLED": "1",
                "S_ABAC_ENABLED": "1" if self.s_abac_enabled else "0",
                "INTENT_RECOGNITION_MODE": "rule",
                "EXECUTION_USER_API_KEYS_JSON": json.dumps(
                    {EXECUTION_USER_ID: EXECUTION_USER_API_KEY}
                ),
                "GOVERNANCE_ADMIN_ACTOR_ID": EXECUTION_USER_ID,
                "APPROVAL_STORE_DIR": str(self.log_dir / "approvals"),
                "GOVERNANCE_EVENT_STORE_DIR": str(self.log_dir / "governance"),
                "RECEIPT_STORE_DIR": str(self.log_dir / "receipts"),
                "MOCK_EMAIL_LOG_PATH": str(mock_email_log),
                "USE_MCP_TOOLS": "0",
                "MEMORY_ENABLED": "0",
                "WORKFLOW_SKILL_ENABLED": "0",
                "WORKFLOW_SKILL_REUSE_ENABLED": "0",
            }
        )
        if self.fault_mode:
            env["ANNUAL_LEAVE_E2E_FAULT"] = self.fault_mode
        else:
            env.pop("ANNUAL_LEAVE_E2E_FAULT", None)

        started_at = time.monotonic()
        try:
            for spec in self.specs:
                stdout_path = self.log_dir / f"{spec.name}-stdout.log"
                stderr_path = self.log_dir / f"{spec.name}-stderr.log"
                stdout_handle = stdout_path.open("w", encoding="utf-8")
                stderr_handle = stderr_path.open("w", encoding="utf-8")
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                process = subprocess.Popen(
                    list(spec.command),
                    cwd=str(self.project_root),
                    env=env,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    creationflags=creationflags,
                )
                self.running.append(
                    RunningService(
                        spec=spec,
                        process=process,
                        stdout_handle=stdout_handle,
                        stderr_handle=stderr_handle,
                        stdout_path=stdout_path,
                        stderr_path=stderr_path,
                    )
                )
                self._wait_for_health(
                    spec,
                    process,
                    timeout_seconds=max(
                        1.0, timeout_seconds - (time.monotonic() - started_at)
                    ),
                )
        except Exception:
            self.close()
            raise

    def _wait_for_health(
        self,
        spec: ServiceSpec,
        process: subprocess.Popen,
        *,
        timeout_seconds: float,
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        url = f"http://127.0.0.1:{spec.port}{spec.health_path}"
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AnnualLeaveE2EError(
                    f"{spec.name} exited during startup with code {process.returncode}"
                )
            try:
                payload = _http_json(url, timeout=1.5)
                if spec.name != "web" or (
                    isinstance(payload, dict) and payload.get("ready") is True
                ):
                    return
            except AnnualLeaveE2EError:
                pass
            # This is a bounded health-poll interval, not a workflow/demo delay.
            threading.Event().wait(0.25)
        raise AnnualLeaveE2EError(f"timed out waiting for {spec.name} health at {url}")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for item in reversed(self.running):
            if item.process.poll() is None:
                item.process.terminate()
                try:
                    item.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    item.process.kill()
                    item.process.wait(timeout=10)
            for handle in (item.stdout_handle, item.stderr_handle):
                try:
                    handle.close()
                except OSError:
                    pass
        self.running.clear()

    def __enter__(self) -> "AnnualLeaveServiceManager":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def log_snapshot(self, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        for service in self.specs:
            for suffix in ("stdout", "stderr"):
                source = self.log_dir / f"{service.name}-{suffix}.log"
                target = destination / source.name
                if source.exists():
                    target.write_text(
                        redact_evidence(
                            source.read_text(encoding="utf-8", errors="replace")
                        ),
                        encoding="utf-8",
                    )


def _request_body(
    workflow_id: str,
    *,
    workmode: str,
    stop_after_planner: bool,
    query: str = ANNUAL_LEAVE_QUERY,
    expected_agents: set[str] = EXPECTED_AGENTS,
) -> dict[str, Any]:
    message = query if workmode == "launch" else "执行已确认计划"
    # The Web/API currently derives its routing/query context from
    # ``instruction`` before it considers ``original_user_query``.  Keep the
    # original goal in that field during production confirmation so the
    # fail-closed PlanSnapshot gate re-derives the same graph approved at
    # launch; ``workmode=production`` remains the confirmation signal.
    instruction = query
    return {
        "user_id": EXECUTION_USER_ID,
        "lang": "zh",
        "messages": [{"role": "user", "content": message}],
        "debug": False,
        "deep_thinking_mode": True,
        "search_before_planning": False,
        "coor_agents": sorted(expected_agents),
        "workmode": workmode,
        "workflow_id": workflow_id,
        "stop_after_planner": stop_after_planner,
        "memory_enabled": False,
        "skill_reuse_enabled": False,
        "original_user_query": query,
        "instruction": instruction,
    }


def consume_workflow_sse(
    body: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    on_event: Callable[[dict[str, Any], int], None] | None = None,
    timeout: float = 900.0,
) -> list[dict[str, Any]]:
    """POST one Web API request and consume the entire SSE stream."""

    request_headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    request_headers.update(headers or {})
    request = Request(
        f"{WEB_BASE_URL}/api/workflows/run",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    events: list[dict[str, Any]] = []
    try:
        with urlopen(
            request, timeout=timeout
        ) as response:  # noqa: S310 - local demo endpoint
            for sequence, event in enumerate(_iter_sse(response), start=1):
                events.append(event)
                if on_event is not None:
                    on_event(event, sequence)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise AnnualLeaveE2EError(
            f"workflow API returned HTTP {exc.code}: {redact_evidence(detail)}"
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise AnnualLeaveE2EError(
            f"workflow SSE request failed: {type(exc).__name__}"
        ) from exc
    if not any(event.get("event") == "end_of_workflow" for event in events):
        raise AnnualLeaveE2EError("SSE stream ended before end_of_workflow")
    return events


def consume_task_resume_sse(
    body: dict[str, Any],
    *,
    on_event: Callable[[dict[str, Any], int], None] | None = None,
    timeout: float = 900.0,
) -> list[dict[str, Any]]:
    request = Request(
        f"{WEB_BASE_URL}/api/tasks/resume",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )
    events: list[dict[str, Any]] = []
    try:
        with urlopen(
            request, timeout=timeout
        ) as response:  # noqa: S310 - local demo endpoint
            for sequence, event in enumerate(_iter_sse(response), start=1):
                events.append(event)
                if on_event is not None:
                    on_event(event, sequence)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise AnnualLeaveE2EError(
            f"resume API returned HTTP {exc.code}: {redact_evidence(detail)}"
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise AnnualLeaveE2EError(
            f"resume SSE request failed: {type(exc).__name__}"
        ) from exc
    if not any(event.get("event") == "end_of_workflow" for event in events):
        raise AnnualLeaveE2EError("resume SSE stream ended before end_of_workflow")
    return events


def _task_id_from_events(events: Iterable[dict[str, Any]]) -> str:
    for event in events:
        data = event.get("data")
        if isinstance(data, dict) and data.get("task_id"):
            return str(data["task_id"])
    raise AnnualLeaveE2EError("workflow SSE did not expose a task_id")


def _workflow_terminal(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    terminal = [
        event.get("data")
        for event in events
        if event.get("event") == "end_of_workflow"
        and isinstance(event.get("data"), dict)
    ]
    if not terminal:
        raise AnnualLeaveE2EError("workflow SSE did not expose a terminal result")
    return terminal[-1]


def _collect_task_log(task_id: str) -> dict[str, Any]:
    payload = _http_json(f"{WEB_BASE_URL}/api/tasks/{task_id}/log", timeout=30)
    if not isinstance(payload, dict):
        raise AnnualLeaveE2EError("task log response was not an object")
    return payload


def _execution_plan_hash(workflow_id: str, plan: list[dict[str, Any]]) -> str:
    """Mirror the public confirmation hash computed by the Web API."""

    canonical = {
        "workflowId": workflow_id,
        "steps": [
            {
                "title": step.get("title") or "",
                "description": step.get("description") or "",
                "agent_name": step.get("agent_name") or "",
                "note": step.get("note") or "",
            }
            for step in plan
            if isinstance(step, dict)
        ],
    }
    encoded = json.dumps(canonical, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _create_execution_authorization(
    *, workflow_id: str, plan: list[dict[str, Any]], user_query: str
) -> dict[str, str]:
    confirmation_request_id = f"annual-leave-{uuid.uuid4().hex}"
    payload = _http_json(
        f"{WEB_BASE_URL}/api/workflows/execution-authorizations",
        method="POST",
        headers={
            "Authorization": f"Bearer {EXECUTION_USER_API_KEY}",
            "Idempotency-Key": confirmation_request_id,
        },
        payload={
            "user_id": EXECUTION_USER_ID,
            "workflow_id": workflow_id,
            "plan_hash": _execution_plan_hash(workflow_id, plan),
            "user_query": user_query,
        },
    )
    if not isinstance(payload, dict):
        raise AnnualLeaveE2EError("execution authorization response was not an object")
    required = {
        "task_id": "execution_task_id",
        "execution_attempt_id": "execution_attempt_id",
        "execution_idempotency_key": "execution_idempotency_key",
        "execution_plan_hash": "execution_plan_hash",
        "execution_authorization_token": "execution_authorization_token",
    }
    identity = {
        body_key: str(payload.get(response_key) or "")
        for response_key, body_key in required.items()
    }
    missing = sorted(key for key, value in identity.items() if not value)
    if missing:
        raise AnnualLeaveE2EError(
            f"execution authorization omitted required fields: {missing}"
        )
    return identity


def _collect_latest_checkpoint(task_id: str) -> dict[str, Any]:
    listed = _http_json(f"{WEB_BASE_URL}/api/tasks/{task_id}/checkpoints", timeout=30)
    if not isinstance(listed, list) or not listed:
        raise AnnualLeaveE2EError("no checkpoints were persisted for the workflow")
    latest = max(
        (item for item in listed if isinstance(item, dict)),
        key=lambda item: int(item.get("step") or -1),
    )
    detail = _http_json(
        f"{WEB_BASE_URL}/api/tasks/{task_id}/checkpoints/{int(latest.get('step'))}",
        timeout=30,
    )
    if not isinstance(detail, dict):
        raise AnnualLeaveE2EError("checkpoint detail response was not an object")
    return detail


def _load_artifacts_from_checkpoint(
    task_id: str, checkpoint: dict[str, Any]
) -> dict[str, Artifact]:
    state = checkpoint.get("state") if isinstance(checkpoint, dict) else None
    index = state.get("artifacts") if isinstance(state, dict) else None
    if not isinstance(index, dict):
        raise AnnualLeaveE2EError("latest checkpoint has no Artifact index")
    from src.orchestration.artifact_payload_store import ArtifactPayloadStore

    payloads = ArtifactPayloadStore(task_id).load_index(index)
    artifacts: dict[str, Artifact] = {}
    for versions in payloads.values():
        if not isinstance(versions, dict):
            continue
        for payload in versions.values():
            artifact = Artifact.model_validate(payload)
            artifacts[artifact.logical_name] = artifact
    return artifacts


def _assert_and_build_graph(
    plan: list[dict[str, Any]], task_id: str
) -> tuple[Any, dict[str, str]]:
    from remote_agents.hr_assistant_agent import RemoteHRAssistantAgent
    from remote_agents.knowledge_agent import RemoteKnowledgeAgent
    from remote_agents.report_agent import RemoteReportAgent

    by_agent: dict[str, dict[str, Any]] = {}
    for step in plan:
        agent_name = str(step.get("agent_name") or "")
        if agent_name in by_agent:
            raise AnnualLeaveE2EError(
                f"Planner returned duplicate Agent {agent_name!r}"
            )
        by_agent[agent_name] = step
    if set(by_agent) != EXPECTED_AGENTS:
        raise AnnualLeaveE2EError(
            f"real Planner selected Agents {sorted(by_agent)}, expected {sorted(EXPECTED_AGENTS)}"
        )
    step_ids = {
        agent_name: str(step.get("step_id") or "")
        for agent_name, step in by_agent.items()
    }
    if (
        any(not value for value in step_ids.values())
        or len(set(step_ids.values())) != 3
    ):
        raise AnnualLeaveE2EError("Planner step IDs must be non-empty and unique")

    hr_step = by_agent["RemoteHRAssistantAgent"]
    knowledge_step = by_agent["RemoteKnowledgeAgent"]
    report_step = by_agent["RemoteReportAgent"]
    if hr_step.get("depends_on") or knowledge_step.get("depends_on"):
        raise AnnualLeaveE2EError("HR and Knowledge steps must be independent")

    inputs = report_step.get("inputs") or []
    if len(inputs) != 1 or inputs[0].get("parameter_name") != "report.sources":
        raise AnnualLeaveE2EError("Report must have one report.sources input binding")
    source_artifacts = inputs[0].get("source_artifacts")
    source_pairs = {
        (item.get("source_step"), item.get("source_output"))
        for item in source_artifacts or []
        if isinstance(item, dict)
    }
    if source_pairs != {
        (step_ids["RemoteHRAssistantAgent"], "employee.info"),
        (step_ids["RemoteKnowledgeAgent"], "policy.info"),
    }:
        raise AnnualLeaveE2EError(f"Report fan-in sources were {source_pairs!r}")
    assembly = inputs[0].get("assembly") or {}
    if assembly.get("schema_ref") != "report.sources@v1":
        raise AnnualLeaveE2EError(
            "Report fan-in assembly schema is not report.sources@v1"
        )

    contracts = {
        agent.name: agent.contract
        for agent in (
            RemoteHRAssistantAgent(),
            RemoteKnowledgeAgent(),
            RemoteReportAgent(),
        )
    }
    graph = plan_to_task_graph(
        plan,
        task_id=task_id,
        subject="admin",
        goal=ANNUAL_LEAVE_QUERY,
        agent_contracts=contracts,
    )
    graph_by_id = {step.step_id: step for step in graph.steps}
    report_step_id = step_ids["RemoteReportAgent"]
    expected_dependencies = {
        step_ids["RemoteHRAssistantAgent"],
        step_ids["RemoteKnowledgeAgent"],
    }
    if set(graph_by_id[report_step_id].depends_on) != expected_dependencies:
        raise AnnualLeaveE2EError(
            "validated TaskGraph did not derive both Report dependencies from source_artifacts"
        )
    return graph, step_ids


def _assert_and_build_dynamic_five_graph(
    plan: list[dict[str, Any]],
    task_id: str,
    task_profile: dict[str, Any],
) -> tuple[Any, dict[str, str]]:
    from remote_agents.email_dispatch_agent import RemoteEmailDispatchAgent
    from remote_agents.hr_assistant_agent import RemoteHRAssistantAgent
    from remote_agents.knowledge_agent import RemoteKnowledgeAgent
    from remote_agents.office_assistant_agent import RemoteOfficeAssistantAgent
    from remote_agents.report_agent import RemoteReportAgent

    by_agent: dict[str, dict[str, Any]] = {}
    for step in plan:
        agent_name = str(step.get("agent_name") or "")
        if agent_name in by_agent:
            raise AnnualLeaveE2EError(
                f"Planner returned duplicate Agent {agent_name!r}"
            )
        by_agent[agent_name] = step
    if set(by_agent) != DYNAMIC_FIVE_AGENTS:
        raise AnnualLeaveE2EError(
            f"real Planner selected Agents {sorted(by_agent)}, expected {sorted(DYNAMIC_FIVE_AGENTS)}"
        )
    step_ids = {
        agent_name: str(step.get("step_id") or "")
        for agent_name, step in by_agent.items()
    }
    if (
        any(not value for value in step_ids.values())
        or len(set(step_ids.values())) != 5
    ):
        raise AnnualLeaveE2EError("Planner step IDs must be non-empty and unique")

    def input_binding(agent_name: str, parameter_name: str) -> dict[str, Any]:
        inputs = by_agent[agent_name].get("inputs") or []
        matches = [
            item
            for item in inputs
            if isinstance(item, dict) and item.get("parameter_name") == parameter_name
        ]
        if len(matches) != 1:
            raise AnnualLeaveE2EError(
                f"{agent_name} must have exactly one {parameter_name} binding"
            )
        return matches[0]

    hr_id = step_ids["RemoteHRAssistantAgent"]
    knowledge_id = step_ids["RemoteKnowledgeAgent"]
    office_id = step_ids["RemoteOfficeAssistantAgent"]
    report_id = step_ids["RemoteReportAgent"]
    email_id = step_ids["RemoteEmailDispatchAgent"]

    office_binding = input_binding("RemoteOfficeAssistantAgent", "employee.info")
    if (
        office_binding.get("source_step"),
        office_binding.get("source_output"),
    ) != (hr_id, "employee.info"):
        raise AnnualLeaveE2EError("Office input is not bound to the HR Artifact")

    report_binding = input_binding("RemoteReportAgent", "report.sources")
    report_sources = {
        (item.get("source_step"), item.get("source_output"))
        for item in report_binding.get("source_artifacts") or []
        if isinstance(item, dict)
    }
    if report_sources != {
        (hr_id, "employee.info"),
        (knowledge_id, "policy.info"),
        (office_id, "employee.leave_records"),
    }:
        raise AnnualLeaveE2EError(f"Report fan-in sources were {report_sources!r}")
    if (report_binding.get("assembly") or {}).get("schema_ref") != (
        "report.sources@v1"
    ):
        raise AnnualLeaveE2EError("Report fan-in schema is not report.sources@v1")

    email_binding = input_binding("RemoteEmailDispatchAgent", "email.dispatch.request")
    email_sources = email_binding.get("source_artifacts") or []
    if email_sources != [
        {"source_step": report_id, "source_output": "report.markdown"}
    ]:
        raise AnnualLeaveE2EError(f"Email request source was {email_sources!r}")
    if (email_binding.get("assembly") or {}).get("schema_ref") != (
        "email.dispatch.request@v1"
    ):
        raise AnnualLeaveE2EError(
            "Email input assembly schema is not email.dispatch.request@v1"
        )

    agents = (
        RemoteHRAssistantAgent(),
        RemoteKnowledgeAgent(),
        RemoteOfficeAssistantAgent(),
        RemoteReportAgent(),
        RemoteEmailDispatchAgent(),
    )
    graph = plan_to_task_graph(
        plan,
        task_id=task_id,
        subject=EXECUTION_USER_ID,
        goal=DYNAMIC_FIVE_QUERY,
        agent_contracts={agent.name: agent.contract for agent in agents},
        subtasks=task_profile.get("subtasks") or [],
    )
    graph_by_id = graph.step_map()
    expected_dependencies = {
        office_id: {hr_id},
        report_id: {hr_id, knowledge_id, office_id},
        email_id: {report_id},
    }
    for step_id, dependencies in expected_dependencies.items():
        if set(graph_by_id[step_id].depends_on) != dependencies:
            raise AnnualLeaveE2EError(
                f"validated TaskGraph dependencies for {step_id} were "
                f"{graph_by_id[step_id].depends_on!r}"
            )
    if graph_by_id[office_id].operation_mode != "read":
        raise AnnualLeaveE2EError(
            "Office leave-history query was not classified read-only"
        )
    email_step = graph_by_id[email_id]
    if email_step.operation_mode != "send" or not email_step.external_side_effect:
        raise AnnualLeaveE2EError(
            "Email step was not classified as a governed side effect"
        )
    return graph, step_ids


def _timeline_bounds(
    timeline: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    starts: dict[str, dict[str, Any]] = {}
    ends: dict[str, dict[str, Any]] = {}
    for item in timeline:
        step_id = str(item.get("step_id") or "")
        if item.get("status") == "RUNNING":
            starts[step_id] = item
        else:
            ends[step_id] = item
    return starts, ends


def _assert_upstream_parallel(
    timeline: list[dict[str, Any]],
    *,
    step_ids: dict[str, str],
    require_complete: bool = True,
) -> None:
    starts, ends = _timeline_bounds(timeline)
    hr_step_id = step_ids["RemoteHRAssistantAgent"]
    knowledge_step_id = step_ids["RemoteKnowledgeAgent"]
    upstream_ids = {hr_step_id, knowledge_step_id}
    if not upstream_ids <= set(starts):
        raise AnnualLeaveE2EError("attempt timeline is missing an upstream start event")
    if not upstream_ids <= set(ends):
        if require_complete:
            raise AnnualLeaveE2EError(
                "attempt timeline is missing an upstream end event"
            )
        # A transport failure can terminate the read-only Agent before the
        # runtime emits its attempt-end lifecycle event.  Keep the observed
        # start boundary in failure evidence, but do not invent an end time or
        # claim a complete overlap proof for that failed attempt.
        return
    hr_start = starts[hr_step_id]["monotonic_ns"]
    policy_start = starts[knowledge_step_id]["monotonic_ns"]
    hr_end = ends[hr_step_id]["monotonic_ns"]
    policy_end = ends[knowledge_step_id]["monotonic_ns"]
    if not (hr_start < policy_end and policy_start < hr_end):
        raise AnnualLeaveE2EError(
            "HR and Knowledge attempts were not observed in parallel"
        )


def _assert_timeline(
    timeline: list[dict[str, Any]], *, step_ids: dict[str, str]
) -> None:
    starts, ends = _timeline_bounds(timeline)
    expected_ids = set(step_ids.values())
    if not expected_ids <= set(starts):
        raise AnnualLeaveE2EError("attempt timeline is missing a scheduler start event")
    if not expected_ids <= set(ends):
        raise AnnualLeaveE2EError("attempt timeline is missing a scheduler end event")
    _assert_upstream_parallel(timeline, step_ids=step_ids)
    report_start = starts[step_ids["RemoteReportAgent"]]["monotonic_ns"]
    hr_end = ends[step_ids["RemoteHRAssistantAgent"]]["monotonic_ns"]
    policy_end = ends[step_ids["RemoteKnowledgeAgent"]]["monotonic_ns"]
    if report_start < hr_end or report_start < policy_end:
        raise AnnualLeaveE2EError(
            "Report attempt started before both upstream attempts completed"
        )


def _assert_dynamic_five_timeline(
    timeline: list[dict[str, Any]], *, step_ids: dict[str, str]
) -> None:
    starts, ends = _timeline_bounds(timeline)
    expected_ids = set(step_ids.values())
    if not expected_ids <= set(starts) or not expected_ids <= set(ends):
        raise AnnualLeaveE2EError(
            "dynamic five-Agent timeline is missing scheduler boundaries"
        )
    _assert_upstream_parallel(timeline, step_ids=step_ids)

    hr_id = step_ids["RemoteHRAssistantAgent"]
    knowledge_id = step_ids["RemoteKnowledgeAgent"]
    office_id = step_ids["RemoteOfficeAssistantAgent"]
    report_id = step_ids["RemoteReportAgent"]
    email_id = step_ids["RemoteEmailDispatchAgent"]
    if starts[office_id]["monotonic_ns"] < ends[hr_id]["monotonic_ns"]:
        raise AnnualLeaveE2EError("Office started before the HR Artifact was ready")
    report_start = starts[report_id]["monotonic_ns"]
    if any(
        report_start < ends[upstream_id]["monotonic_ns"]
        for upstream_id in (hr_id, knowledge_id, office_id)
    ):
        raise AnnualLeaveE2EError("Report started before its three inputs were ready")
    if starts[email_id]["monotonic_ns"] < ends[report_id]["monotonic_ns"]:
        raise AnnualLeaveE2EError("Email started before the Report Artifact was ready")


def _persist_evidence(
    *,
    service_manager: AnnualLeaveServiceManager,
    run_dir: Path,
    workflow_id: str,
    task_id: str,
    scenario: str,
    started_at_utc: str,
    launch_body: dict[str, Any],
    production_body: dict[str, Any],
    plan: list[dict[str, Any]],
    graph: Any,
    all_events: list[dict[str, Any]],
    timeline: list[dict[str, Any]],
    terminal: dict[str, Any],
    task_log: dict[str, Any],
    checkpoint: dict[str, Any],
    artifacts: dict[str, Artifact],
    schema_validation: dict[str, Any],
) -> None:
    _write_json(
        run_dir / "request.json",
        {
            "scenario": scenario,
            "launch": launch_body,
            "production": production_body,
        },
    )
    _write_json(run_dir / "plan.json", plan)
    _write_json(run_dir / "task-graph.json", graph)
    _write_json(run_dir / "schema-validation.json", schema_validation)
    _write_json(run_dir / "artifacts.json", artifacts)
    _write_json(
        run_dir / "lineage.json",
        {
            logical_name: [ref.model_dump(mode="json") for ref in artifact.derived_from]
            for logical_name, artifact in artifacts.items()
        },
    )
    _write_json(run_dir / "execution-timeline.json", timeline)
    _write_json(run_dir / "workflow-result.json", terminal)
    _write_json(run_dir / "task-log.json", task_log)
    _write_json(run_dir / "checkpoints.json", checkpoint)
    (run_dir / "sse-events.jsonl").write_text(
        "".join(
            json.dumps(redact_evidence(event), ensure_ascii=False) + "\n"
            for event in all_events
        ),
        encoding="utf-8",
    )
    service_manager.log_snapshot(run_dir / "service-logs")
    _write_json(
        run_dir / "run-metadata.json",
        {
            "run_id": run_dir.name,
            "workflow_id": workflow_id,
            "task_id": task_id,
            "git_commit": _git_commit(service_manager.project_root),
            "branch": _git_branch(service_manager.project_root),
            "python_version": sys.version,
            "ports": {
                "web": WEB_PORT,
                "remote_agent": REMOTE_AGENT_PORT,
                "remote_tool": REMOTE_TOOL_PORT,
                "remote_registry": REMOTE_REGISTRY_PORT,
            },
            "started_at_utc": started_at_utc,
            "finished_at_utc": _utc_now(),
            "scenario": scenario,
            "test_result": "passed",
        },
    )


def run_annual_leave_workflow(
    service_manager: AnnualLeaveServiceManager,
    *,
    run_dir: Path,
    scenario: str = "success",
) -> dict[str, Any]:
    """Run one launch -> production workflow and persist evidence."""

    run_dir.mkdir(parents=True, exist_ok=False)
    workflow_id = f"{EXECUTION_USER_ID}:annual_leave_{uuid.uuid4().hex}"
    started_at_utc = _utc_now()
    timeline: list[dict[str, Any]] = []
    all_events: list[dict[str, Any]] = []

    def on_event(event: dict[str, Any], sequence: int) -> None:
        item = _timeline_event(event, sequence)
        if item is not None:
            timeline.append(item)

    launch_body = _request_body(
        workflow_id,
        workmode="launch",
        stop_after_planner=True,
    )
    _write_json(run_dir / "request.json", {"scenario": scenario, "launch": launch_body})
    launch_events = consume_workflow_sse(
        launch_body,
        headers={"X-Authenticated-User": EXECUTION_USER_ID},
        on_event=on_event,
    )
    all_events.extend(launch_events)
    task_id = _task_id_from_events(launch_events)
    launch_log = _collect_task_log(task_id)
    plan = launch_log.get("planning_steps") or []
    if not isinstance(plan, list) or not plan:
        planner_text = "".join(
            str((event.get("data") or {}).get("delta", {}).get("content") or "")
            for event in launch_events
            if event.get("event") == "messages"
            and str(event.get("agent_name") or "").startswith("planner")
        )
        plan = _extract_plan_from_text(planner_text)
    if not isinstance(plan, list):
        plan = []
    _write_json(run_dir / "plan.json", plan)
    graph, step_ids = _assert_and_build_graph(plan, task_id)
    _write_json(run_dir / "task-graph.json", graph)

    production_body = _request_body(
        workflow_id,
        workmode="production",
        stop_after_planner=False,
    )
    production_body.update(
        _create_execution_authorization(
            workflow_id=workflow_id,
            plan=plan,
            user_query=ANNUAL_LEAVE_QUERY,
        )
    )
    production_events = consume_workflow_sse(production_body, on_event=on_event)
    all_events.extend(production_events)
    terminal = _workflow_terminal(production_events)
    production_task_id = _task_id_from_events(production_events)
    if production_task_id == task_id:
        raise AnnualLeaveE2EError("production execution did not receive a new task id")
    task_id = production_task_id
    task_log = _collect_task_log(task_id)
    checkpoint = _collect_latest_checkpoint(task_id)

    # The API path is the proof boundary: the remote Agent service must have
    # received all three routed Agents, and the two data-producing Agents must
    # have called the real remote Tool endpoint.  The service logs are read
    # only after the workflow has completed and are redacted before evidence
    # is persisted.
    agent_log = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for service in service_manager.running
        if service.spec.name == "remote-agent"
        for path in (service.stdout_path, service.stderr_path)
        if path.exists()
    )
    expected_routed_agents = {
        "RemoteHRAssistantAgent",
        "RemoteKnowledgeAgent",
    }
    if scenario in {"success", "policy_not_found"}:
        expected_routed_agents.add("RemoteReportAgent")
    for agent_name in sorted(expected_routed_agents):
        if f"Received request for agent: {agent_name}" not in agent_log:
            raise AnnualLeaveE2EError(
                f"remote Agent log did not prove routing to {agent_name}"
            )
    if scenario in {"knowledge_http_error", "knowledge_invalid_date"} and (
        "Received request for agent: RemoteReportAgent" in agent_log
    ):
        raise AnnualLeaveE2EError(
            "Report Agent was called even though an upstream failure should block it"
        )
    tool_log = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for service in service_manager.running
        if service.spec.name == "remote-tool"
        for path in (service.stdout_path, service.stderr_path)
        if path.exists()
    )
    for route_marker in ("Person query parameters", "knowledge_search_tool"):
        if route_marker not in tool_log:
            raise AnnualLeaveE2EError(
                f"remote Tool log did not prove {route_marker} was called"
            )

    artifacts = _load_artifacts_from_checkpoint(task_id, checkpoint)
    schema_registry = register_agent_schemas(SchemaRegistry())
    schema_validation: dict[str, Any] = {}
    for logical_name, artifact in artifacts.items():
        valid, errors = schema_registry.validate(
            artifact.payload, artifact.schema_ref or ""
        )
        schema_validation[logical_name] = {
            "artifact_id": artifact.artifact_id,
            "version": artifact.version,
            "schema_ref": artifact.schema_ref,
            "schema_valid_flag": artifact.schema_valid,
            "valid": valid,
            "errors": errors,
        }
        if not valid or artifact.schema_valid is not True:
            raise AnnualLeaveE2EError(
                f"Artifact {logical_name} failed schema validation"
            )
    expected_artifacts = {"employee.info", "policy.info", "report.markdown"}
    failure_scenarios = {"knowledge_http_error", "knowledge_invalid_date"}
    if scenario in failure_scenarios:
        if terminal.get("status") != "PARTIAL_FAILED":
            raise AnnualLeaveE2EError(
                f"failure scenario terminal status was {terminal.get('status')!r}"
            )
        knowledge_step_id = step_ids["RemoteKnowledgeAgent"]
        report_step_id = step_ids["RemoteReportAgent"]
        if knowledge_step_id not in (terminal.get("failed_steps") or []):
            raise AnnualLeaveE2EError(
                "Knowledge failure was not present in failed_steps"
            )
        if report_step_id not in (terminal.get("blocked_steps") or []):
            raise AnnualLeaveE2EError("Report was not present in blocked_steps")
        if "report.markdown" in artifacts:
            raise AnnualLeaveE2EError(
                "Report Artifact was created after upstream failure"
            )
        if set(artifacts) - {"employee.info"}:
            raise AnnualLeaveE2EError(
                f"unexpected failure-scenario Artifacts: {set(artifacts)!r}"
            )
        step_statuses = {
            str((event.get("data") or {}).get("step_id")): str(
                (event.get("data") or {}).get("status") or ""
            )
            .upper()
            .replace("STEPSTATUS.", "")
            for event in production_events
            if event.get("event") == "step_result"
            and isinstance(event.get("data"), dict)
        }
        hr_step_id = step_ids["RemoteHRAssistantAgent"]
        if step_statuses.get(hr_step_id) != "SUCCEEDED":
            raise AnnualLeaveE2EError(
                f"HR status was {step_statuses.get(hr_step_id)!r}"
            )
        if step_statuses.get(knowledge_step_id) != "FAILED":
            raise AnnualLeaveE2EError(
                f"Knowledge status was {step_statuses.get(knowledge_step_id)!r}"
            )
        if step_statuses.get(report_step_id) != "SKIPPED":
            raise AnnualLeaveE2EError(
                f"Report status was {step_statuses.get(report_step_id)!r}"
            )
        _assert_upstream_parallel(timeline, step_ids=step_ids, require_complete=False)
        _persist_evidence(
            service_manager=service_manager,
            run_dir=run_dir,
            workflow_id=workflow_id,
            task_id=task_id,
            scenario=scenario,
            started_at_utc=started_at_utc,
            launch_body=launch_body,
            production_body=production_body,
            plan=plan,
            graph=graph,
            all_events=all_events,
            timeline=timeline,
            terminal=terminal,
            task_log=task_log,
            checkpoint=checkpoint,
            artifacts=artifacts,
            schema_validation=schema_validation,
        )
        return {
            "run_id": run_dir.name,
            "workflow_id": workflow_id,
            "task_id": task_id,
            "scenario": scenario,
            "status": terminal.get("status"),
            "artifact_ids": {
                name: artifact.artifact_id for name, artifact in artifacts.items()
            },
        }

    if scenario not in {"success", "policy_not_found"}:
        raise AnnualLeaveE2EError(f"unknown annual-leave scenario: {scenario}")
    if set(artifacts) != expected_artifacts:
        raise AnnualLeaveE2EError(f"workflow Artifacts were {set(artifacts)!r}")
    employee = artifacts["employee.info"]
    policy = artifacts["policy.info"]
    report = artifacts["report.markdown"]
    if (
        report.schema_ref != "report.markdown@v1"
        or report.logical_name != "report.markdown"
    ):
        raise AnnualLeaveE2EError("final Artifact is not report.markdown@v1")
    if len(report.derived_from) != 2 or {
        item.artifact_id for item in report.derived_from
    } != {employee.artifact_id, policy.artifact_id}:
        raise AnnualLeaveE2EError(
            "final report lineage is not exactly the two upstream Artifacts"
        )
    if not isinstance(report.payload, dict):
        raise AnnualLeaveE2EError("report Artifact payload is not an object")
    markdown = str(report.payload.get("markdown") or "")
    if report.payload.get("source_count") != 2:
        raise AnnualLeaveE2EError("final Report Artifact source_count was not 2")
    if scenario == "success":
        for required_text in ("王强", "20年", "15天", "国务院令第514号", "第三条"):
            if required_text not in markdown:
                raise AnnualLeaveE2EError(
                    f"report Markdown is missing {required_text!r}"
                )
    elif "无法据此判断可休年假天数" not in markdown:
        raise AnnualLeaveE2EError(
            "not-found report did not provide a cautious explanation"
        )
    if scenario == "policy_not_found" and any(
        forbidden in markdown for forbidden in ("5天", "10天", "15天", "20天")
    ):
        raise AnnualLeaveE2EError("not-found report inferred a leave-day number")
    for forbidden in SENSITIVE_SAMPLE_VALUES + (
        "idvId",
        "officePhone",
        "internalMaiBox",
        "brthDt",
    ):
        if forbidden in json.dumps(
            {"employee": employee.payload, "report": report.payload}, ensure_ascii=False
        ):
            raise AnnualLeaveE2EError(
                f"sensitive sample value leaked into evidence: {forbidden}"
            )

    export_markdown_artifact(report, run_dir)
    _assert_timeline(timeline, step_ids=step_ids)
    if terminal.get("status") != "SUCCEEDED":
        raise AnnualLeaveE2EError(
            f"success scenario terminal status was {terminal.get('status')!r}"
        )

    _persist_evidence(
        service_manager=service_manager,
        run_dir=run_dir,
        workflow_id=workflow_id,
        task_id=task_id,
        scenario=scenario,
        started_at_utc=started_at_utc,
        launch_body=launch_body,
        production_body=production_body,
        plan=plan,
        graph=graph,
        all_events=all_events,
        timeline=timeline,
        terminal=terminal,
        task_log=task_log,
        checkpoint=checkpoint,
        artifacts=artifacts,
        schema_validation=schema_validation,
    )
    return {
        "run_id": run_dir.name,
        "workflow_id": workflow_id,
        "task_id": task_id,
        "scenario": scenario,
        "status": terminal.get("status"),
        "artifact_ids": {
            name: artifact.artifact_id for name, artifact in artifacts.items()
        },
    }


def run_dynamic_five_agent_workflow(
    service_manager: AnnualLeaveServiceManager,
    *,
    run_dir: Path,
    approval_decision: str = "approve",
) -> dict[str, Any]:
    """Run HR + Knowledge -> Office -> Report -> approval -> Email."""

    if not service_manager.s_abac_enabled:
        raise AnnualLeaveE2EError("dynamic five-Agent workflow requires S-ABAC")
    if approval_decision not in {"approve", "reject"}:
        raise AnnualLeaveE2EError(
            f"unsupported dynamic five-Agent approval decision: {approval_decision}"
        )
    run_dir.mkdir(parents=True, exist_ok=False)
    workflow_id = f"{EXECUTION_USER_ID}:annual_leave_five_{uuid.uuid4().hex}"
    started_at_utc = _utc_now()
    timeline: list[dict[str, Any]] = []
    all_events: list[dict[str, Any]] = []

    def on_event(event: dict[str, Any], sequence: int) -> None:
        item = _timeline_event(event, sequence)
        if item is not None:
            timeline.append(item)

    launch_body = _request_body(
        workflow_id,
        workmode="launch",
        stop_after_planner=True,
        query=DYNAMIC_FIVE_QUERY,
        expected_agents=DYNAMIC_FIVE_AGENTS,
    )
    launch_events = consume_workflow_sse(
        launch_body,
        headers={"X-Authenticated-User": EXECUTION_USER_ID},
        on_event=on_event,
    )
    all_events.extend(launch_events)
    launch_task_id = _task_id_from_events(launch_events)
    launch_log = _collect_task_log(launch_task_id)
    launch_checkpoint = _collect_latest_checkpoint(launch_task_id)
    launch_state = launch_checkpoint.get("state") or {}
    plan = launch_log.get("planning_steps") or launch_state.get("planning_steps") or []
    if not isinstance(plan, list) or not plan:
        planner_text = "".join(
            str((event.get("data") or {}).get("delta", {}).get("content") or "")
            for event in launch_events
            if event.get("event") == "messages"
            and str(event.get("agent_name") or "").startswith("planner")
        )
        plan = _extract_plan_from_text(planner_text)
    if not isinstance(plan, list) or not plan:
        raise AnnualLeaveE2EError("launch evidence did not contain the Planner plan")
    task_profile = (
        launch_log.get("task_profile") or launch_state.get("task_profile") or {}
    )
    if not isinstance(task_profile, dict):
        raise AnnualLeaveE2EError("launch task log did not persist the TaskProfile")
    graph, step_ids = _assert_and_build_dynamic_five_graph(
        plan, launch_task_id, task_profile
    )

    production_body = _request_body(
        workflow_id,
        workmode="production",
        stop_after_planner=False,
        query=DYNAMIC_FIVE_QUERY,
        expected_agents=DYNAMIC_FIVE_AGENTS,
    )
    production_body.update(
        _create_execution_authorization(
            workflow_id=workflow_id,
            plan=plan,
            user_query=DYNAMIC_FIVE_QUERY,
        )
    )
    production_events = consume_workflow_sse(production_body, on_event=on_event)
    all_events.extend(production_events)
    approval_terminal = _workflow_terminal(production_events)
    task_id = _task_id_from_events(production_events)
    email_step_id = step_ids["RemoteEmailDispatchAgent"]
    if approval_terminal.get("status") != "APPROVAL_REQUIRED":
        raise AnnualLeaveE2EError(
            f"Email gate ended as {approval_terminal.get('status')!r}, expected APPROVAL_REQUIRED"
        )
    if approval_terminal.get("approval_required_steps") != [email_step_id]:
        raise AnnualLeaveE2EError(
            f"approval-required steps were {approval_terminal.get('approval_required_steps')!r}"
        )

    before_checkpoint = _collect_latest_checkpoint(task_id)
    before_artifacts = _load_artifacts_from_checkpoint(task_id, before_checkpoint)
    if set(before_artifacts) != {
        "employee.info",
        "policy.info",
        "employee.leave_records",
        "report.markdown",
    }:
        raise AnnualLeaveE2EError(
            f"pre-approval Artifacts were {set(before_artifacts)!r}"
        )
    agent_log_before = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for service in service_manager.running
        if service.spec.name == "remote-agent"
        for path in (service.stdout_path, service.stderr_path)
        if path.exists()
    )
    if "Received request for agent: RemoteEmailDispatchAgent" in agent_log_before:
        raise AnnualLeaveE2EError("Email Agent ran before approval")

    pending = _http_json(
        f"{WEB_BASE_URL}/api/security/approvals?status=pending&task_id={task_id}",
        timeout=30,
    )
    if not isinstance(pending, list) or len(pending) != 1:
        count = len(pending) if isinstance(pending, list) else "invalid"
        raise AnnualLeaveE2EError(f"pending approval queue contained {count} items")
    approval = pending[0]
    if str(approval.get("step_id") or "") != email_step_id:
        raise AnnualLeaveE2EError("pending approval does not belong to the Email step")
    approval_id = str(approval.get("approval_id") or "")
    if approval_decision == "reject":
        rejected = _http_json(
            f"{WEB_BASE_URL}/api/security/approvals/{approval_id}/reject",
            method="POST",
            payload={
                "approver": EXECUTION_USER_ID,
                "comment": "dynamic demo rejection",
            },
        )
        if not isinstance(rejected, dict) or rejected.get("status") != "rejected":
            raise AnnualLeaveE2EError(
                "approval API did not reject the Email step"
            )
        rejected_resume_body = {
            "task_id": task_id,
            "resume_step": int(approval.get("resume_step") or 0),
            "user_id": str(approval.get("user_id") or EXECUTION_USER_ID),
            "workmode": "production",
            "lang": "zh",
        }
        rejected_resume_events = consume_task_resume_sse(
            rejected_resume_body, on_event=on_event
        )
        all_events.extend(rejected_resume_events)
        terminal = _workflow_terminal(rejected_resume_events)
        if terminal.get("status") != "PARTIAL_FAILED":
            raise AnnualLeaveE2EError(
                "rejected workflow did not end as completed-but-not-sent "
                f"(status={terminal.get('status')!r})"
            )

        task_log = _collect_task_log(task_id)
        checkpoint = _collect_latest_checkpoint(task_id)
        artifacts = _load_artifacts_from_checkpoint(task_id, checkpoint)
        expected_artifacts = {
            "employee.info",
            "policy.info",
            "employee.leave_records",
            "report.markdown",
        }
        if set(artifacts) != expected_artifacts:
            raise AnnualLeaveE2EError(
                f"rejected dynamic five-Agent Artifacts were {set(artifacts)!r}"
            )

        schema_registry = register_agent_schemas(SchemaRegistry())
        schema_validation: dict[str, Any] = {}
        for logical_name, artifact in artifacts.items():
            valid, errors = schema_registry.validate(
                artifact.payload, artifact.schema_ref or ""
            )
            schema_validation[logical_name] = {
                "artifact_id": artifact.artifact_id,
                "schema_ref": artifact.schema_ref,
                "valid": valid,
                "errors": errors,
            }
            if not valid or artifact.schema_valid is not True:
                raise AnnualLeaveE2EError(
                    f"Artifact {logical_name} failed schema validation"
                )

        agent_log_after = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for service in service_manager.running
            if service.spec.name == "remote-agent"
            for path in (service.stdout_path, service.stderr_path)
            if path.exists()
        )
        if "Received request for agent: RemoteEmailDispatchAgent" in agent_log_after:
            raise AnnualLeaveE2EError("Email Agent ran after approval rejection")
        tool_log = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for service in service_manager.running
            if service.spec.name == "remote-tool"
            for path in (service.stdout_path, service.stderr_path)
            if path.exists()
        )
        if "[TOOL] remote_email_tool called" in tool_log:
            raise AnnualLeaveE2EError("mock Email tool ran after approval rejection")
        mock_email_log = json.loads(
            (service_manager.log_dir / "mock-email-log.json").read_text(
                encoding="utf-8"
            )
        )
        if mock_email_log.get("emails"):
            raise AnnualLeaveE2EError(
                "mock Email provider recorded a send after approval rejection"
            )

        starts, ends = _timeline_bounds(timeline)
        email_step_id = step_ids["RemoteEmailDispatchAgent"]
        if email_step_id in starts:
            raise AnnualLeaveE2EError(
                "Email scheduler attempt started after approval rejection"
            )
        _assert_upstream_parallel(timeline, step_ids=step_ids)
        for agent_name in (
            "RemoteHRAssistantAgent",
            "RemoteKnowledgeAgent",
            "RemoteOfficeAssistantAgent",
            "RemoteReportAgent",
        ):
            step_id = step_ids[agent_name]
            if step_id not in starts or step_id not in ends:
                raise AnnualLeaveE2EError(
                    f"rejected workflow timeline omitted completed step {step_id}"
                )

        governance = _http_json(
            f"{WEB_BASE_URL}/api/tasks/{task_id}/governance", timeout=30
        )
        if not isinstance(governance, list):
            raise AnnualLeaveE2EError(
                "governance timeline response was not a list"
            )
        governance_types = {
            str(item.get("event_type") or "") for item in governance
        }
        if not {"APPROVAL_REQUIRED", "APPROVAL_REJECTED"} <= governance_types:
            raise AnnualLeaveE2EError(
                f"governance timeline events were {sorted(governance_types)}"
            )

        _persist_evidence(
            service_manager=service_manager,
            run_dir=run_dir,
            workflow_id=workflow_id,
            task_id=task_id,
            scenario="dynamic_five_rejected",
            started_at_utc=started_at_utc,
            launch_body=launch_body,
            production_body=production_body,
            plan=plan,
            graph=graph,
            all_events=all_events,
            timeline=timeline,
            terminal=terminal,
            task_log=task_log,
            checkpoint=checkpoint,
            artifacts=artifacts,
            schema_validation=schema_validation,
        )
        _write_json(
            run_dir / "approval.json",
            {
                "request": approval,
                "decision": rejected,
                "pre_approval_terminal": approval_terminal,
                "rejected_resume_terminal": terminal,
            },
        )
        _write_json(run_dir / "governance-events.json", governance)
        return {
            "run_id": run_dir.name,
            "workflow_id": workflow_id,
            "task_id": task_id,
            "scenario": "dynamic_five_rejected",
            "status": terminal.get("status"),
            "approval_id": approval_id,
            "artifact_ids": {
                name: artifact.artifact_id for name, artifact in artifacts.items()
            },
        }

    approved = _http_json(
        f"{WEB_BASE_URL}/api/security/approvals/{approval_id}/approve",
        method="POST",
        payload={"approver": EXECUTION_USER_ID, "comment": "dynamic demo approval"},
    )
    if not isinstance(approved, dict) or approved.get("status") != "approved":
        raise AnnualLeaveE2EError("approval API did not approve the Email step")
    resume_body = approved.get("resume_request") or {}
    if not isinstance(resume_body, dict):
        raise AnnualLeaveE2EError("approval response omitted the resume request")
    duplicate_approved = _http_json(
        f"{WEB_BASE_URL}/api/security/approvals/{approval_id}/approve",
        method="POST",
        payload={
            "approver": EXECUTION_USER_ID,
            "comment": "duplicate dynamic demo approval",
        },
    )
    if (
        not isinstance(duplicate_approved, dict)
        or duplicate_approved.get("status") != "approved"
        or duplicate_approved.get("resume_request") != resume_body
    ):
        raise AnnualLeaveE2EError(
            "duplicate approval did not preserve the original resume contract"
        )
    resume_events = consume_task_resume_sse(
        {**resume_body, "lang": "zh"}, on_event=on_event
    )
    all_events.extend(resume_events)
    terminal = _workflow_terminal(resume_events)
    if terminal.get("status") != "SUCCEEDED":
        raise AnnualLeaveE2EError(
            f"approved workflow terminal status was {terminal.get('status')!r}"
        )
    successful_task_log = _collect_task_log(task_id)
    successful_checkpoint = _collect_latest_checkpoint(task_id)
    successful_artifacts = _load_artifacts_from_checkpoint(
        task_id, successful_checkpoint
    )
    if "email.dispatch.receipt" not in successful_artifacts:
        raise AnnualLeaveE2EError(
            "approved workflow checkpoint omitted the Email receipt"
        )
    duplicate_resume_events = consume_task_resume_sse(
        {**resume_body, "lang": "zh"}, on_event=on_event
    )
    all_events.extend(duplicate_resume_events)
    duplicate_terminal = _workflow_terminal(duplicate_resume_events)
    if duplicate_terminal.get("status") not in {"SUCCEEDED", "NEEDS_RECONCILIATION"}:
        raise AnnualLeaveE2EError(
            "duplicate resume did not preserve the completed Email side effect "
            f"(status={duplicate_terminal.get('status')!r})"
        )

    # A duplicate Resume intentionally reopens the pre-side-effect checkpoint.
    # Preserve the first successful checkpoint as the final success evidence;
    # the duplicate terminal status is recorded separately below.
    task_log = successful_task_log
    checkpoint = successful_checkpoint
    artifacts = successful_artifacts
    expected_artifacts = {
        "employee.info",
        "policy.info",
        "employee.leave_records",
        "report.markdown",
        "email.dispatch.receipt",
    }
    if set(artifacts) != expected_artifacts:
        raise AnnualLeaveE2EError(
            f"dynamic five-Agent Artifacts were {set(artifacts)!r}"
        )

    schema_registry = register_agent_schemas(SchemaRegistry())
    schema_validation: dict[str, Any] = {}
    for logical_name, artifact in artifacts.items():
        valid, errors = schema_registry.validate(
            artifact.payload, artifact.schema_ref or ""
        )
        schema_validation[logical_name] = {
            "artifact_id": artifact.artifact_id,
            "schema_ref": artifact.schema_ref,
            "valid": valid,
            "errors": errors,
        }
        if not valid or artifact.schema_valid is not True:
            raise AnnualLeaveE2EError(
                f"Artifact {logical_name} failed schema validation"
            )

    employee = artifacts["employee.info"]
    policy = artifacts["policy.info"]
    leave_records = artifacts["employee.leave_records"]
    report = artifacts["report.markdown"]
    receipt = artifacts["email.dispatch.receipt"]
    if {ref.artifact_id for ref in report.derived_from} != {
        employee.artifact_id,
        policy.artifact_id,
        leave_records.artifact_id,
    }:
        raise AnnualLeaveE2EError("Report lineage does not cover all three sources")
    if {ref.artifact_id for ref in receipt.derived_from} != {report.artifact_id}:
        raise AnnualLeaveE2EError("Email receipt lineage does not point to the Report")
    if (
        not isinstance(receipt.payload, dict)
        or receipt.payload.get("dispatch_mode") != "simulated"
    ):
        raise AnnualLeaveE2EError("Email receipt was not marked simulated")
    if receipt.payload.get("approval_id") != approval_id:
        raise AnnualLeaveE2EError(
            "Email receipt does not carry the consumed approval ID"
        )
    if not receipt.payload.get("idempotency_key"):
        raise AnnualLeaveE2EError("Email receipt omitted its idempotency key")

    agent_log_after = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for service in service_manager.running
        if service.spec.name == "remote-agent"
        for path in (service.stdout_path, service.stderr_path)
        if path.exists()
    )
    if (
        agent_log_after.count("Received request for agent: RemoteEmailDispatchAgent")
        != 1
    ):
        raise AnnualLeaveE2EError("Email Agent was not invoked exactly once")
    tool_log = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for service in service_manager.running
        if service.spec.name == "remote-tool"
        for path in (service.stdout_path, service.stderr_path)
        if path.exists()
    )
    if tool_log.count("[TOOL] remote_email_tool called") != 1:
        raise AnnualLeaveE2EError("mock Email tool was not invoked exactly once")
    mock_email_log = json.loads(
        (service_manager.log_dir / "mock-email-log.json").read_text(encoding="utf-8")
    )
    if len(mock_email_log.get("emails") or []) != 1:
        raise AnnualLeaveE2EError(
            "mock Email provider did not persist exactly one send"
        )

    governance = _http_json(
        f"{WEB_BASE_URL}/api/tasks/{task_id}/governance", timeout=30
    )
    if not isinstance(governance, list):
        raise AnnualLeaveE2EError("governance timeline response was not a list")
    governance_types = {str(item.get("event_type") or "") for item in governance}
    if not {"APPROVAL_REQUIRED", "APPROVAL_GRANTED"} <= governance_types:
        raise AnnualLeaveE2EError(
            f"governance timeline events were {sorted(governance_types)}"
        )
    _assert_dynamic_five_timeline(timeline, step_ids=step_ids)

    _persist_evidence(
        service_manager=service_manager,
        run_dir=run_dir,
        workflow_id=workflow_id,
        task_id=task_id,
        scenario="dynamic_five_approved",
        started_at_utc=started_at_utc,
        launch_body=launch_body,
        production_body=production_body,
        plan=plan,
        graph=graph,
        all_events=all_events,
        timeline=timeline,
        terminal=terminal,
        task_log=task_log,
        checkpoint=checkpoint,
        artifacts=artifacts,
        schema_validation=schema_validation,
    )
    _write_json(
        run_dir / "approval.json",
        {
            "request": approval,
            "decision": approved,
            "duplicate_decision": duplicate_approved,
            "pre_approval_terminal": approval_terminal,
            "duplicate_resume_terminal": duplicate_terminal,
        },
    )
    _write_json(run_dir / "governance-events.json", governance)
    return {
        "run_id": run_dir.name,
        "workflow_id": workflow_id,
        "task_id": task_id,
        "scenario": "dynamic_five_approved",
        "status": terminal.get("status"),
        "approval_id": approval_id,
        "artifact_ids": {
            name: artifact.artifact_id for name, artifact in artifacts.items()
        },
    }


def _git_commit(project_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _git_branch(project_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "branch", "--show-current"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def integration_prerequisite_reason() -> str | None:
    if not os.getenv("RUN_ANNUAL_LEAVE_HTTP_E2E"):
        return "设置 RUN_ANNUAL_LEAVE_HTTP_E2E=1 后才运行真实 HTTP E2E"
    missing = [
        name
        for name in ("REMOTE_API_KEY", "BASIC_API_KEY", "REASONING_API_KEY")
        if not os.getenv(name)
    ]
    if missing:
        return "缺少真实 Planner/Agent/Tool 所需的密钥: " + ", ".join(missing)
    return None


__all__ = [
    "ANNUAL_LEAVE_QUERY",
    "DYNAMIC_FIVE_QUERY",
    "AnnualLeaveE2EError",
    "AnnualLeaveServiceManager",
    "consume_workflow_sse",
    "consume_task_resume_sse",
    "integration_prerequisite_reason",
    "redact_evidence",
    "run_annual_leave_workflow",
    "run_dynamic_five_agent_workflow",
]
