"""Real HTTP harness for the annual-leave defense workflow.

The harness deliberately starts the same four local services used by the demo
runbook and talks to the Web API.  It does not construct a TaskGraph or call
the Scheduler directly.  The only local reads after execution are evidence
collection from the task/checkpoint APIs and the protected Artifact payload
store created by that execution.
"""

from __future__ import annotations

import json
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

ANNUAL_LEAVE_QUERY = (
    "请查询员工王强的在职状态、岗位和累计工龄，并依据国务院关于职工带薪年休假的规定，"
    "判断其年假天数，生成一份 Markdown 汇总。"
)
EXPECTED_AGENTS = {
    "RemoteHRAssistantAgent",
    "RemoteKnowledgeAgent",
    "RemoteReportAgent",
}
SENSITIVE_SAMPLE_VALUES = (
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
        redacted = re.sub(r"(?i)(api[_-]?key|token|secret)\s*[=:]\s*[^\s,;]+", r"\1=[REDACTED]", redacted)
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
    timeout: float = 30.0,
) -> Any:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - local demo endpoint
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
        raise AnnualLeaveE2EError(f"HTTP request failed for {url}: {type(exc).__name__}") from exc


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
    ) -> None:
        self.project_root = (project_root or Path(__file__).resolve().parents[2]).resolve()
        self.log_dir = (log_dir or self.project_root / "artifacts" / "annual-leave-service-logs").resolve()
        self.python_executable = python_executable or sys.executable
        self.fault_mode = fault_mode
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
                (python, "-u", "cli.py", "web", "--host", "127.0.0.1", "--port", str(WEB_PORT)),
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
            raise AnnualLeaveE2EError(f"refusing to share occupied demo port(s): {details}")

        self.log_dir.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env.update(
            {
                "APP_ENV": "production",
                "ORCHESTRATION_SCHEDULER_ENABLED": "1",
                "S_ABAC_ENABLED": "0",
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
                    timeout_seconds=max(1.0, timeout_seconds - (time.monotonic() - started_at)),
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
                if spec.name != "web" or (isinstance(payload, dict) and payload.get("ready") is True):
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
                        redact_evidence(source.read_text(encoding="utf-8", errors="replace")),
                        encoding="utf-8",
                    )


def _request_body(workflow_id: str, *, workmode: str, stop_after_planner: bool) -> dict[str, Any]:
    message = ANNUAL_LEAVE_QUERY if workmode == "launch" else "执行已确认计划"
    # The Web/API currently derives its routing/query context from
    # ``instruction`` before it considers ``original_user_query``.  Keep the
    # original goal in that field during production confirmation so the
    # fail-closed PlanSnapshot gate re-derives the same graph approved at
    # launch; ``workmode=production`` remains the confirmation signal.
    instruction = ANNUAL_LEAVE_QUERY
    return {
        "user_id": "admin",
        "lang": "zh",
        "messages": [{"role": "user", "content": message}],
        "debug": False,
        "deep_thinking_mode": True,
        "search_before_planning": False,
        "coor_agents": sorted(EXPECTED_AGENTS),
        "workmode": workmode,
        "workflow_id": workflow_id,
        "stop_after_planner": stop_after_planner,
        "memory_enabled": False,
        "skill_reuse_enabled": False,
        "original_user_query": ANNUAL_LEAVE_QUERY,
        "instruction": instruction,
    }


def consume_workflow_sse(
    body: dict[str, Any],
    *,
    on_event: Callable[[dict[str, Any], int], None] | None = None,
    timeout: float = 900.0,
) -> list[dict[str, Any]]:
    """POST one Web API request and consume the entire SSE stream."""

    request = Request(
        f"{WEB_BASE_URL}/api/workflows/run",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )
    events: list[dict[str, Any]] = []
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - local demo endpoint
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
        if event.get("event") == "end_of_workflow" and isinstance(event.get("data"), dict)
    ]
    if not terminal:
        raise AnnualLeaveE2EError("workflow SSE did not expose a terminal result")
    return terminal[-1]


def _collect_task_log(task_id: str) -> dict[str, Any]:
    payload = _http_json(f"{WEB_BASE_URL}/api/tasks/{task_id}/log", timeout=30)
    if not isinstance(payload, dict):
        raise AnnualLeaveE2EError("task log response was not an object")
    return payload


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


def _load_artifacts_from_checkpoint(task_id: str, checkpoint: dict[str, Any]) -> dict[str, Artifact]:
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


def _assert_and_build_graph(plan: list[dict[str, Any]], task_id: str):
    from remote_agents.hr_assistant_agent import RemoteHRAssistantAgent
    from remote_agents.knowledge_agent import RemoteKnowledgeAgent
    from remote_agents.report_agent import RemoteReportAgent

    if len(plan) != 3:
        raise AnnualLeaveE2EError(f"real Planner returned {len(plan)} steps, expected 3")
    by_id = {str(step.get("step_id")): step for step in plan}
    if set(by_id) != {"hr_query", "policy_query", "generate_report"}:
        raise AnnualLeaveE2EError(f"unexpected annual-leave step ids: {sorted(by_id)}")
    if by_id["hr_query"].get("agent_name") != "RemoteHRAssistantAgent":
        raise AnnualLeaveE2EError("hr_query was not assigned to RemoteHRAssistantAgent")
    if by_id["policy_query"].get("agent_name") != "RemoteKnowledgeAgent":
        raise AnnualLeaveE2EError("policy_query was not assigned to RemoteKnowledgeAgent")
    if by_id["generate_report"].get("agent_name") != "RemoteReportAgent":
        raise AnnualLeaveE2EError("generate_report was not assigned to RemoteReportAgent")
    if by_id["hr_query"].get("depends_on") or by_id["policy_query"].get("depends_on"):
        raise AnnualLeaveE2EError("HR and Knowledge steps must be independent")
    if set(by_id["generate_report"].get("depends_on") or []) != {"hr_query", "policy_query"}:
        raise AnnualLeaveE2EError("Report step must depend on both upstream steps")

    inputs = by_id["generate_report"].get("inputs") or []
    if len(inputs) != 1 or inputs[0].get("parameter_name") != "report.sources":
        raise AnnualLeaveE2EError("Report must have one report.sources input binding")
    source_artifacts = inputs[0].get("source_artifacts")
    source_pairs = {
        (item.get("source_step"), item.get("source_output"))
        for item in source_artifacts or []
        if isinstance(item, dict)
    }
    if source_pairs != {
        ("hr_query", "employee.info"),
        ("policy_query", "policy.info"),
    }:
        raise AnnualLeaveE2EError(f"Report fan-in sources were {source_pairs!r}")
    assembly = inputs[0].get("assembly") or {}
    if assembly.get("schema_ref") != "report.sources@v1":
        raise AnnualLeaveE2EError("Report fan-in assembly schema is not report.sources@v1")

    contracts = {
        agent.name: agent.contract
        for agent in (
            RemoteHRAssistantAgent(),
            RemoteKnowledgeAgent(),
            RemoteReportAgent(),
        )
    }
    return plan_to_task_graph(
        plan,
        task_id=task_id,
        subject="admin",
        goal=ANNUAL_LEAVE_QUERY,
        agent_contracts=contracts,
    )


def _timeline_bounds(timeline: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
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
    require_complete: bool = True,
) -> None:
    starts, ends = _timeline_bounds(timeline)
    if not {"hr_query", "policy_query"} <= set(starts):
        raise AnnualLeaveE2EError("attempt timeline is missing an upstream start event")
    if not {"hr_query", "policy_query"} <= set(ends):
        if require_complete:
            raise AnnualLeaveE2EError("attempt timeline is missing an upstream end event")
        # A transport failure can terminate the read-only Agent before the
        # runtime emits its attempt-end lifecycle event.  Keep the observed
        # start boundary in failure evidence, but do not invent an end time or
        # claim a complete overlap proof for that failed attempt.
        return
    hr_start = starts["hr_query"]["monotonic_ns"]
    policy_start = starts["policy_query"]["monotonic_ns"]
    hr_end = ends["hr_query"]["monotonic_ns"]
    policy_end = ends["policy_query"]["monotonic_ns"]
    if not (hr_start < policy_end and policy_start < hr_end):
        raise AnnualLeaveE2EError("HR and Knowledge attempts were not observed in parallel")


def _assert_timeline(timeline: list[dict[str, Any]]) -> None:
    starts, ends = _timeline_bounds(timeline)
    if not {"hr_query", "policy_query", "generate_report"} <= set(starts):
        raise AnnualLeaveE2EError("attempt timeline is missing a scheduler start event")
    if not {"hr_query", "policy_query", "generate_report"} <= set(ends):
        raise AnnualLeaveE2EError("attempt timeline is missing a scheduler end event")
    _assert_upstream_parallel(timeline)
    report_start = starts["generate_report"]["monotonic_ns"]
    hr_end = ends["hr_query"]["monotonic_ns"]
    policy_end = ends["policy_query"]["monotonic_ns"]
    if report_start < hr_end or report_start < policy_end:
        raise AnnualLeaveE2EError("Report attempt started before both upstream attempts completed")


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
    workflow_id = f"admin:annual_leave_{uuid.uuid4().hex}"
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
    launch_events = consume_workflow_sse(launch_body, on_event=on_event)
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
    graph = _assert_and_build_graph(plan, task_id)
    _write_json(run_dir / "task-graph.json", graph)

    production_body = _request_body(
        workflow_id,
        workmode="production",
        stop_after_planner=False,
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
        valid, errors = schema_registry.validate(artifact.payload, artifact.schema_ref or "")
        schema_validation[logical_name] = {
            "artifact_id": artifact.artifact_id,
            "version": artifact.version,
            "schema_ref": artifact.schema_ref,
            "schema_valid_flag": artifact.schema_valid,
            "valid": valid,
            "errors": errors,
        }
        if not valid or artifact.schema_valid is not True:
            raise AnnualLeaveE2EError(f"Artifact {logical_name} failed schema validation")
    expected_artifacts = {"employee.info", "policy.info", "report.markdown"}
    failure_scenarios = {"knowledge_http_error", "knowledge_invalid_date"}
    if scenario in failure_scenarios:
        if terminal.get("status") != "PARTIAL_FAILED":
            raise AnnualLeaveE2EError(
                f"failure scenario terminal status was {terminal.get('status')!r}"
            )
        if "policy_query" not in (terminal.get("failed_steps") or []):
            raise AnnualLeaveE2EError("Knowledge failure was not present in failed_steps")
        if "generate_report" not in (terminal.get("blocked_steps") or []):
            raise AnnualLeaveE2EError("Report was not present in blocked_steps")
        if "report.markdown" in artifacts:
            raise AnnualLeaveE2EError("Report Artifact was created after upstream failure")
        if set(artifacts) - {"employee.info"}:
            raise AnnualLeaveE2EError(
                f"unexpected failure-scenario Artifacts: {set(artifacts)!r}"
            )
        step_statuses = {
            str((event.get("data") or {}).get("step_id")): str(
                (event.get("data") or {}).get("status") or ""
            ).upper().replace("STEPSTATUS.", "")
            for event in production_events
            if event.get("event") == "step_result"
            and isinstance(event.get("data"), dict)
        }
        if step_statuses.get("hr_query") != "SUCCEEDED":
            raise AnnualLeaveE2EError(f"HR status was {step_statuses.get('hr_query')!r}")
        if step_statuses.get("policy_query") != "FAILED":
            raise AnnualLeaveE2EError(
                f"Knowledge status was {step_statuses.get('policy_query')!r}"
            )
        if step_statuses.get("generate_report") != "SKIPPED":
            raise AnnualLeaveE2EError(
                f"Report status was {step_statuses.get('generate_report')!r}"
            )
        _assert_upstream_parallel(timeline, require_complete=False)
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
    if report.schema_ref != "report.markdown@v1" or report.logical_name != "report.markdown":
        raise AnnualLeaveE2EError("final Artifact is not report.markdown@v1")
    if len(report.derived_from) != 2 or {
        item.artifact_id for item in report.derived_from
    } != {employee.artifact_id, policy.artifact_id}:
        raise AnnualLeaveE2EError("final report lineage is not exactly the two upstream Artifacts")
    if not isinstance(report.payload, dict):
        raise AnnualLeaveE2EError("report Artifact payload is not an object")
    markdown = str(report.payload.get("markdown") or "")
    if report.payload.get("source_count") != 2:
        raise AnnualLeaveE2EError("final Report Artifact source_count was not 2")
    if scenario == "success":
        for required_text in ("王强", "20年", "15天", "国务院令第514号", "第三条"):
            if required_text not in markdown:
                raise AnnualLeaveE2EError(f"report Markdown is missing {required_text!r}")
    elif "无法据此判断可休年假天数" not in markdown:
        raise AnnualLeaveE2EError("not-found report did not provide a cautious explanation")
    if scenario == "policy_not_found" and any(
        forbidden in markdown for forbidden in ("5天", "10天", "15天", "20天")
    ):
        raise AnnualLeaveE2EError("not-found report inferred a leave-day number")
    for forbidden in SENSITIVE_SAMPLE_VALUES + ("idvId", "officePhone", "internalMaiBox", "brthDt"):
        if forbidden in json.dumps({"employee": employee.payload, "report": report.payload}, ensure_ascii=False):
            raise AnnualLeaveE2EError(f"sensitive sample value leaked into evidence: {forbidden}")

    export_markdown_artifact(report, run_dir)
    _assert_timeline(timeline)
    if terminal.get("status") != "SUCCEEDED":
        raise AnnualLeaveE2EError(f"success scenario terminal status was {terminal.get('status')!r}")

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
        "artifact_ids": {name: artifact.artifact_id for name, artifact in artifacts.items()},
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
    "AnnualLeaveE2EError",
    "AnnualLeaveServiceManager",
    "consume_workflow_sse",
    "integration_prerequisite_reason",
    "redact_evidence",
    "run_annual_leave_workflow",
]
