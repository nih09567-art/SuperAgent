"""Safe task-level projection for the orchestration run UI.

The projection deliberately excludes conversation bodies, tool arguments,
Artifact payloads, authorization tokens, and raw remote responses.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _plan_hash(steps: list[dict[str, Any]]) -> str:
    payload = json.dumps(steps, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _safe_binding(binding: Any) -> dict[str, Any]:
    if not isinstance(binding, Mapping):
        return {}
    safe: dict[str, Any] = {}
    for key in ("param", "parameter", "source_step", "source_output", "schema_ref"):
        value = binding.get(key)
        if value not in (None, ""):
            safe[key] = str(value)
    sources = binding.get("source_artifacts")
    if isinstance(sources, list):
        safe["source_artifacts"] = [
            {
                key: str(source[key])
                for key in ("source_step", "source_output", "schema_ref")
                if isinstance(source, Mapping) and source.get(key) not in (None, "")
            }
            for source in sources
            if isinstance(source, Mapping)
        ]
    return safe


def _safe_graph(raw_graph: Any, planning_steps: list[dict[str, Any]]) -> dict[str, Any]:
    graph = raw_graph if isinstance(raw_graph, Mapping) else {}
    raw_steps = graph.get("steps") if isinstance(graph.get("steps"), list) else []
    if not raw_steps:
        raw_steps = planning_steps

    steps: list[dict[str, Any]] = []
    dependency_edges: list[dict[str, str]] = []
    artifact_edges: list[dict[str, str]] = []
    for index, raw in enumerate(raw_steps):
        if not isinstance(raw, Mapping):
            continue
        step_id = str(
            raw.get("step_id")
            or raw.get("subtask_id")
            or raw.get("agent_name")
            or f"step_{index + 1}"
        )
        dependencies = raw.get("depends_on")
        if isinstance(dependencies, str):
            dependencies = [dependencies]
        if not isinstance(dependencies, list):
            dependencies = []
        bindings = raw.get("input_bindings")
        if not isinstance(bindings, list):
            bindings = raw.get("inputs") if isinstance(raw.get("inputs"), list) else []
        safe_bindings = [_safe_binding(item) for item in bindings]
        safe_bindings = [item for item in safe_bindings if item]
        expected_outputs = raw.get("expected_outputs") or raw.get("produces") or []
        if isinstance(expected_outputs, str):
            expected_outputs = [expected_outputs]
        step = {
            "step_id": step_id,
            "title": str(raw.get("title") or raw.get("description") or step_id),
            "agent_name": str(
                raw.get("agent_name") or raw.get("preferred_resource_id") or ""
            ),
            "depends_on": [str(value) for value in dependencies if str(value)],
            "input_bindings": safe_bindings,
            "expected_outputs": [str(value) for value in expected_outputs if str(value)],
            "operation_mode": str(raw.get("operation_mode") or "read"),
            "risk_level": str(raw.get("risk_level") or "LOW"),
        }
        steps.append(step)
        for source in step["depends_on"]:
            dependency_edges.append(
                {"source": source, "target": step_id, "kind": "dependency"}
            )
        for binding in safe_bindings:
            sources = binding.get("source_artifacts")
            if not isinstance(sources, list):
                sources = [binding] if binding.get("source_step") else []
            for source in sources:
                source_step = str(source.get("source_step") or "")
                if not source_step:
                    continue
                artifact_edges.append(
                    {
                        "source": source_step,
                        "target": step_id,
                        "kind": "artifact",
                        "output": str(source.get("source_output") or ""),
                        "schema_ref": str(source.get("schema_ref") or ""),
                    }
                )
    return {
        "available": bool(steps),
        "steps": steps,
        "dependency_edges": dependency_edges,
        "artifact_edges": artifact_edges,
    }


def _safe_tool_candidate(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {}
    return {
        key: _json_copy(raw[key])
        for key in (
            "tool_key",
            "name",
            "server_name",
            "score",
            "reasons",
            "legacy_tool_name",
            "operation_mode",
            "reason",
        )
        if key in raw
    }


def _safe_tool_decisions(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {}
    result: dict[str, Any] = {}
    scalar_keys = (
        "requested_mode",
        "mode",
        "enforce_blocked",
        "agent_name",
        "server_name",
        "selected_tool",
        "selected_tool_key",
        "recommended_mcp_tool",
        "compatible_legacy_tool",
        "actual_legacy_tool",
        "actual_tool_names",
        "recommendation_matches_actual",
        "candidate_count_before_filter",
        "candidate_count_after_filter",
        "candidate_count",
        "error",
    )
    for step_id, decision in raw.items():
        if not isinstance(decision, Mapping):
            continue
        safe = {key: _json_copy(decision[key]) for key in scalar_keys if key in decision}
        safe["candidates"] = [
            candidate
            for candidate in (
                _safe_tool_candidate(item) for item in decision.get("candidates", [])
            )
            if candidate
        ]
        safe["excluded"] = [
            candidate
            for candidate in (
                _safe_tool_candidate(item) for item in decision.get("excluded", [])
            )
            if candidate
        ]
        result[str(step_id)] = safe
    return result


def _attempt_intervals(events: Any) -> list[dict[str, Any]]:
    if not isinstance(events, list):
        return []
    pending: dict[tuple[str, int, str], dict[str, Any]] = {}
    intervals: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        key = (
            str(event.get("step_id") or ""),
            int(event.get("attempt") or 1),
            str(event.get("phase") or "primary"),
        )
        if event.get("event") == "start":
            pending[key] = dict(event)
            continue
        if event.get("event") != "end":
            continue
        start = pending.pop(key, {})
        started_ns = int(start.get("monotonic_ns") or 0)
        finished_ns = int(event.get("monotonic_ns") or 0)
        intervals.append(
            {
                "step_id": key[0],
                "attempt": key[1],
                "phase": key[2],
                "planned_agent": str(
                    event.get("planned_agent") or start.get("planned_agent") or ""
                ),
                "executed_agent": str(
                    event.get("executed_agent") or start.get("executed_agent") or ""
                ),
                "sequence_start": start.get("sequence"),
                "sequence_end": event.get("sequence"),
                "started_at": start.get("timestamp"),
                "finished_at": event.get("timestamp"),
                "started_monotonic_ns": started_ns or None,
                "finished_monotonic_ns": finished_ns or None,
                "duration_ms": (
                    round((finished_ns - started_ns) / 1_000_000, 3)
                    if started_ns and finished_ns >= started_ns
                    else None
                ),
                "status": str(event.get("status") or ""),
            }
        )
    for key, start in pending.items():
        intervals.append(
            {
                "step_id": key[0],
                "attempt": key[1],
                "phase": key[2],
                "planned_agent": str(start.get("planned_agent") or ""),
                "executed_agent": str(start.get("executed_agent") or ""),
                "sequence_start": start.get("sequence"),
                "started_at": start.get("timestamp"),
                "started_monotonic_ns": start.get("monotonic_ns"),
                "status": "RUNNING",
            }
        )
    return sorted(
        intervals,
        key=lambda item: (item.get("sequence_start") or 0, item["step_id"]),
    )


def _safe_artifacts(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    allowed = {
        "producer_step_id",
        "output_name",
        "artifact_id",
        "version",
        "logical_name",
        "schema_ref",
        "derived_from",
        "sensitivity",
        "schema_valid",
    }
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        safe = {key: _json_copy(value) for key, value in item.items() if key in allowed}
        artifact_id = str(safe.get("artifact_id") or "")
        if artifact_id:
            safe["artifact_id"] = f"{artifact_id[:16]}…"
        derived = safe.get("derived_from")
        if isinstance(derived, list):
            safe["derived_from"] = [
                {
                    key: (
                        f"{str(value)[:16]}…" if key == "artifact_id" and value else value
                    )
                    for key, value in ref.items()
                    if key in {"artifact_id", "version", "expected_schema_ref"}
                }
                for ref in derived
                if isinstance(ref, Mapping)
            ]
        result.append(safe)
    return result


def _safe_governance(events: Iterable[Any]) -> list[dict[str, Any]]:
    allowed = {
        "event_type",
        "timestamp",
        "step_id",
        "agent",
        "decision",
        "reason_code",
        "operation_mode",
        "risk_level",
    }
    return [
        {key: _json_copy(value) for key, value in event.items() if key in allowed}
        for event in events
        if isinstance(event, Mapping)
    ]


def _legacy_step_results(
    history: Any, graph_steps: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Infer coarse replay state from older logs without claiming timing evidence."""

    if not isinstance(history, list):
        return {}
    aliases: dict[str, set[str]] = {}
    for step in graph_steps:
        step_id = str(step.get("step_id") or "")
        if not step_id:
            continue
        aliases[step_id] = {
            value.casefold()
            for value in (step_id, str(step.get("agent_name") or ""))
            if value
        }

    inferred: dict[str, dict[str, Any]] = {}
    precedence = {"RUNNING": 1, "COMPLETED": 2, "FAILED": 3}
    for entry in history:
        if not isinstance(entry, Mapping):
            continue
        event = str(entry.get("event") or "")
        if event not in {"start_of_agent", "end_of_agent", "error", "step_failure"}:
            continue
        candidates = {
            str(entry.get(key) or "").casefold()
            for key in (
                "step_id",
                "sub_agent_name",
                "planned_agent",
                "selected_agent",
                "executed_agent",
                "node_name",
            )
            if entry.get(key)
        }
        if not candidates:
            continue
        status = (
            "FAILED"
            if event in {"error", "step_failure"}
            else "COMPLETED" if event == "end_of_agent" else "RUNNING"
        )
        for step_id, step_aliases in aliases.items():
            if not candidates.intersection(step_aliases):
                continue
            current = str(inferred.get(step_id, {}).get("status") or "")
            if precedence.get(status, 0) >= precedence.get(current, 0):
                inferred[step_id] = {
                    "status": status,
                    "evidence": "legacy_history",
                }
    return inferred


def build_orchestration_view(
    task_log: Any,
    *,
    governance_events: Iterable[Any] = (),
    control: Mapping[str, Any] | None = None,
    checkpoints: Iterable[Any] = (),
) -> dict[str, Any]:
    """Return a stable, payload-free view for one task execution."""

    planning_steps = [
        dict(step)
        for step in (getattr(task_log, "planning_steps", []) or [])
        if isinstance(step, Mapping)
    ]
    graph = _safe_graph(getattr(task_log, "task_graph", {}), planning_steps)
    safe_planning_steps = [
        {
            key: _json_copy(step[key])
            for key in (
                "step_id",
                "title",
                "agent_name",
                "depends_on",
                "input_bindings",
                "expected_outputs",
                "operation_mode",
                "risk_level",
            )
            if key in step
        }
        for step in graph["steps"]
    ]
    step_results = _json_copy(
        getattr(task_log, "orchestration_step_results", {}) or {}
    )
    for step_id, result in _legacy_step_results(
        getattr(task_log, "history", []) or [], graph["steps"]
    ).items():
        step_results.setdefault(step_id, result)
    intervals = _attempt_intervals(
        getattr(task_log, "orchestration_attempts", []) or []
    )
    for interval in intervals:
        if interval.get("status") == "RUNNING":
            step_results.setdefault(interval["step_id"], {})["status"] = "RUNNING"
    checkpoint_summaries = []
    for checkpoint in checkpoints:
        if isinstance(checkpoint, Mapping):
            checkpoint_summaries.append(
                {
                    key: _json_copy(checkpoint[key])
                    for key in ("checkpoint_id", "step", "node_name", "next_node", "timestamp")
                    if key in checkpoint
                }
            )
        else:
            checkpoint_summaries.append(
                {
                    key: _json_copy(getattr(checkpoint, key))
                    for key in ("checkpoint_id", "step", "node_name", "next_node", "timestamp")
                    if getattr(checkpoint, key, None) is not None
                }
            )
    return {
        "schema_version": "orchestration-view.v1",
        "task": {
            "task_id": str(getattr(task_log, "task_id", "")),
            "workflow_id": str(getattr(task_log, "workflow_id", "")),
            "user_query": str(getattr(task_log, "user_query", "")),
            "status": str(getattr(task_log, "status", "UNKNOWN")),
            "execution_phase": str(getattr(task_log, "execution_phase", "")),
            "created_at": getattr(task_log, "created_at", None),
            "finished_at": getattr(task_log, "finished_at", None),
        },
        "planning": {
            "steps": safe_planning_steps,
            "step_count": len(planning_steps),
            "plan_hash": str(
                getattr(task_log, "execution_plan_hash", "") or _plan_hash(planning_steps)
            ),
            "validation": {
                "status": "VERIFIED_FOR_EXECUTION" if getattr(task_log, "task_graph", {}) else "NOT_RECORDED",
                "task_graph_recorded": bool(getattr(task_log, "task_graph", {})),
            },
        },
        "graph": graph,
        "runtime": {
            "workflow_status": str(getattr(task_log, "status", "UNKNOWN")),
            "step_states": step_results,
            "attempts": intervals,
        },
        "tool_decisions": _safe_tool_decisions(
            getattr(task_log, "tool_selection_decisions", {}) or {}
        ),
        "artifacts": {"nodes": _safe_artifacts(getattr(task_log, "artifact_lineage", []))},
        "governance": {"events": _safe_governance(governance_events)},
        "control": {
            key: _json_copy(value)
            for key, value in dict(control or {}).items()
            if key
            in {
                "task_id",
                "workflow_id",
                "user_id",
                "state",
                "checkpoint_step",
                "resume_step",
                "requested_at",
                "paused_at",
                "updated_at",
            }
        },
        "checkpoints": checkpoint_summaries,
    }
