"""Bounded audit traces for Step/Agent Skill evidence.

The trace is an observability record, not a replay prompt. It keeps the
observable plan/dispatch/tool/result envelope while dropping hidden model
chain-of-thought and applying secret redaction before persistence.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.memory.utils import redact_secrets


MAX_TRACE_EVENTS = 200
MAX_EVENT_CHARS = 24_000
MAX_TRACE_CHARS = 256_000
TRACE_RETENTION_DAYS = 30
_HIDDEN_KEYS = {
    "chain_of_thought",
    "hidden_reasoning",
    "internal_reasoning",
    "thought_trace",
    "scratchpad",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sanitize(value: Any, *, flags: list[str]) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if normalized in _HIDDEN_KEYS:
                flags.append("hidden_reasoning_omitted")
                continue
            result[str(key)] = _sanitize(item, flags=flags)
        return result
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, flags=flags) for item in value]
    if isinstance(value, str):
        redacted = redact_secrets(value)
        if redacted != value:
            flags.append("secret_redacted")
        return redacted
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_secrets(str(value))


def _bounded(value: Any, *, limit: int, flags: list[str]) -> Any:
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    if len(encoded) <= limit:
        return value
    flags.append("truncated")
    if isinstance(value, str):
        return value[:limit]
    marker = {"truncated": True, "preview": encoded[: max(0, limit - 64)]}
    return marker


def make_trace_event(
    *,
    kind: str,
    request: Any = None,
    response: Any = None,
    status: str = "unknown",
    node_name: str = "",
    agent_name: str = "",
    step_id: str = "",
    sequence: int = 0,
    source_refs: Sequence[str] = (),
    timestamp: str | None = None,
) -> dict[str, Any]:
    flags: list[str] = []
    safe_request = _bounded(_sanitize(request, flags=flags), limit=MAX_EVENT_CHARS, flags=flags)
    safe_response = _bounded(_sanitize(response, flags=flags), limit=MAX_EVENT_CHARS, flags=flags)
    payload = {"request": safe_request, "response": safe_response}
    return {
        "event_id": f"trace_evt_{uuid.uuid4().hex}",
        "sequence": int(sequence),
        "kind": str(kind),
        "node_name": str(node_name),
        "agent_name": str(agent_name),
        "step_id": str(step_id),
        "status": str(status),
        "timestamp": timestamp or _now(),
        "source_refs": [str(item) for item in source_refs if item],
        "request": safe_request,
        "response": safe_response,
        "payload_hash": _hash(payload),
        "redaction_applied": bool(flags),
        "redaction_flags": sorted(set(flags)),
    }


def build_execution_trace(
    *,
    history: Sequence[Mapping[str, Any]] = (),
    runtime_events: Sequence[Mapping[str, Any]] = (),
    planning_steps: Sequence[Mapping[str, Any]] = (),
    task_profile: Mapping[str, Any] | None = None,
    task_id: str = "",
    workflow_id: str = "",
) -> dict[str, Any]:
    """Build one bounded, audit-only trace from observable execution records."""

    events: list[dict[str, Any]] = []
    sequence = 0
    plan = make_trace_event(
        kind="planner_decision",
        request={"task_profile": dict(task_profile or {}), "task_id": task_id},
        response={"planning_steps": list(planning_steps)},
        status="completed",
        node_name="planner",
        sequence=sequence,
    )
    events.append(plan)
    sequence += 1

    for item in runtime_events:
        if not isinstance(item, Mapping):
            continue
        event = make_trace_event(
            kind=str(item.get("kind") or "runtime_event"),
            request=item.get("request"),
            response=item.get("response"),
            status=str(item.get("status") or "unknown"),
            node_name=str(item.get("node_name") or ""),
            agent_name=str(item.get("agent_name") or ""),
            step_id=str(item.get("step_id") or ""),
            sequence=sequence,
            source_refs=[str(ref) for ref in item.get("source_refs") or []],
            timestamp=str(item.get("timestamp") or "") or None,
        )
        events.append(event)
        sequence += 1

    for item in history:
        if not isinstance(item, Mapping):
            continue
        node = str(item.get("node_name") or "")
        kind = str(item.get("event") or "")
        if node not in {"coordinator", "planner", "agent_proxy", "publisher", "scheduler"}:
            continue
        # Runtime events contain structured request/response envelopes; history
        # adds the exact observable internal sequence and visible messages.
        event = make_trace_event(
            kind=f"history_{kind or 'event'}",
            request={
                key: item.get(key)
                for key in ("sub_agent_name", "planned_agent", "executed_agent", "attempt", "phase")
                if item.get(key) is not None
            },
            response={"content": item.get("content", "")},
            status="completed" if kind != "error" else "failed",
            node_name=node,
            agent_name=str(item.get("sub_agent_name") or item.get("executed_agent") or ""),
            sequence=sequence,
            source_refs=[str(item.get("message_id") or "")],
            timestamp=str(item.get("timestamp") or "") or None,
        )
        events.append(event)
        sequence += 1

    truncated = False
    if len(events) > MAX_TRACE_EVENTS:
        events = events[:MAX_TRACE_EVENTS]
        truncated = True
    payload = {
        "trace_id": f"skill_trace_{uuid.uuid4().hex}",
        "schema_version": 1,
        "task_id": str(task_id),
        "workflow_id": str(workflow_id),
        "audit_only": True,
        "created_at": _now(),
        "retention_expires_at": (
            datetime.now(UTC) + timedelta(days=TRACE_RETENTION_DAYS)
        ).isoformat(),
        "events": events,
        "event_count": len(events),
        "event_kinds": sorted({str(item.get("kind")) for item in events}),
        "truncated": truncated,
    }
    encoded = json.dumps(payload, ensure_ascii=False, default=str)
    while len(encoded) > MAX_TRACE_CHARS and len(payload["events"]) > 1:
        payload["events"] = payload["events"][:-1]
        payload["truncated"] = True
        payload["event_count"] = len(payload["events"])
        payload["event_kinds"] = sorted(
            {str(item.get("kind")) for item in payload["events"]}
        )
        encoded = json.dumps(payload, ensure_ascii=False, default=str)
    payload["trace_hash"] = _hash(payload)
    return payload


def normalize_execution_trace(trace: Mapping[str, Any]) -> dict[str, Any]:
    """Revalidate caller-provided traces before writing an audit payload."""

    return build_execution_trace(
        runtime_events=[
            item
            for item in trace.get("events") or []
            if isinstance(item, Mapping)
            and str(item.get("kind") or "") != "planner_decision"
        ],
        planning_steps=(
            next(
                (
                    item.get("response", {}).get("planning_steps", [])
                    for item in trace.get("events") or []
                    if isinstance(item, Mapping)
                    and item.get("kind") == "planner_decision"
                    and isinstance(item.get("response"), Mapping)
                ),
                [],
            )
        ),
        task_profile=(
            next(
                (
                    item.get("request", {}).get("task_profile", {})
                    for item in trace.get("events") or []
                    if isinstance(item, Mapping)
                    and item.get("kind") == "planner_decision"
                    and isinstance(item.get("request"), Mapping)
                ),
                {},
            )
        ),
        task_id=str(trace.get("task_id") or ""),
        workflow_id=str(trace.get("workflow_id") or ""),
    )


def trace_summary(trace: Mapping[str, Any], audit_ref: str) -> dict[str, Any]:
    """Return the small reference stored in Skill SQLite payloads."""

    return {
        "trace_id": str(trace.get("trace_id") or ""),
        "audit_ref": str(audit_ref),
        "audit_only": True,
        "event_count": int(trace.get("event_count") or 0),
        "event_kinds": [str(item) for item in trace.get("event_kinds") or []],
        "trace_hash": str(trace.get("trace_hash") or ""),
        "truncated": bool(trace.get("truncated")),
        "retention_expires_at": str(trace.get("retention_expires_at") or ""),
    }


__all__ = [
    "MAX_EVENT_CHARS",
    "MAX_TRACE_CHARS",
    "build_execution_trace",
    "make_trace_event",
    "normalize_execution_trace",
    "trace_summary",
]
