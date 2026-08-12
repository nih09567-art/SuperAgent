import asyncio
import hashlib
import json
from uuid import uuid4

import httpx


BASE_URL = "http://127.0.0.1:8001"
HEADERS = {"X-Authenticated-User": "admin"}


async def stream(client, payload):
    events = []
    async with client.stream(
        "POST", f"{BASE_URL}/api/workflows/run", headers=HEADERS, json=payload
    ) as response:
        response.raise_for_status()
        event_name = "message"
        data_lines = []
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                event_name = line.partition(":")[2].strip()
            elif line.startswith("data:"):
                data_lines.append(line.partition(":")[2].lstrip())
            elif not line and data_lines:
                value = json.loads("\n".join(data_lines))
                if isinstance(value, dict):
                    value.setdefault("event", event_name)
                    events.append(value)
                event_name = "message"
                data_lines = []
    return events


def event_data(event):
    value = event.get("data")
    return value if isinstance(value, dict) else {}


def plan_hash(workflow_id, steps):
    canonical = {
        "workflowId": workflow_id,
        "steps": [
            {
                "title": step.get("title") or "",
                "description": step.get("description") or "",
                "agent_name": step.get("agent_name") or "",
                "note": step.get("note") or "",
            }
            for step in steps
        ],
    }
    encoded = json.dumps(
        canonical, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def main():
    session_id = f"clarification-salary-{uuid4().hex}"
    messages = [
        {"role": "user", "content": "查询工资"},
        {
            "role": "assistant",
            "content": "请提供员工姓名，或说明用于查询员工的岗位、机构等条件。",
        },
        {"role": "user", "content": "员工李娜"},
    ]
    common = {
        "user_id": "admin",
        "lang": "zh",
        "instruction_history": ["查询工资", "员工李娜"],
        "original_user_query": "查询工资",
        "session_id": session_id,
        "memory_session_id": session_id,
        "memory_enabled": False,
        "skill_reuse_enabled": False,
        "debug": False,
        "deep_thinking_mode": True,
        "search_before_planning": False,
        "coor_agents": None,
    }
    async with httpx.AsyncClient(timeout=180.0) as client:
        plan_events = await stream(
            client,
            {
                **common,
                "workmode": "launch",
                "stop_after_planner": True,
                "instruction": "员工李娜",
                "turn_type": "clarification_answer",
                "clarification_context": {
                    "base_query": "查询工资",
                    "resolved_message": "查询工资",
                    "missing_fields": ["employee_or_criteria"],
                    "entities": {},
                },
                "context_entities": {},
                "context_artifacts": [],
                "messages": messages,
            },
        )
        start = next(
            (event_data(e) for e in plan_events if e.get("event") == "start_of_workflow"),
            {},
        )
        routing = next(
            (event_data(e) for e in plan_events if e.get("event") == "routing_decision"),
            {},
        )
        workflow_id = str(start.get("workflow_id") or "")
        if not workflow_id:
            raise RuntimeError("planning did not return a workflow_id")
        workflow_response = await client.get(
            f"{BASE_URL}/api/workflows/{workflow_id}", headers=HEADERS
        )
        workflow_response.raise_for_status()
        workflow = workflow_response.json()
        steps = workflow.get("planning_steps") or []
        if not steps:
            diagnostic = {
                "events": [event.get("event") for event in plan_events],
                "routing": routing,
                "terminal": next(
                    (
                        event_data(e)
                        for e in reversed(plan_events)
                        if e.get("event") == "end_of_workflow"
                    ),
                    {},
                ),
                "workflow": workflow,
            }
            print(json.dumps(diagnostic, ensure_ascii=False, indent=2))
            raise RuntimeError("planning did not persist executable steps")

        request_id = f"clarification-salary-{uuid4().hex}"
        authorization = await client.post(
            f"{BASE_URL}/api/workflows/execution-authorizations",
            headers={**HEADERS, "Idempotency-Key": request_id},
            json={
                "workflow_id": workflow_id,
                "plan_hash": plan_hash(workflow_id, steps),
                "user_query": "查询工资",
            },
        )
        authorization.raise_for_status()
        identity = authorization.json()
        profile = routing.get("task_profile") or {}

        execution_events = await stream(
            client,
            {
                **common,
                "workmode": "production",
                "stop_after_planner": False,
                "instruction": None,
                "workflow_id": workflow_id,
                "resolved_request": "查询工资",
                "current_request_entities": profile.get("entities") or {},
                "context_references": profile.get("context_references") or [],
                "context_entities": profile.get("entities") or {},
                "context_artifacts": [],
                "messages": messages
                + [
                    {
                        "role": "user",
                        "content": "Execute the confirmed plan.",
                        "message_id": f"{session_id}:execute-confirmed-plan:{workflow_id}",
                    }
                ],
                "execution_task_id": identity["task_id"],
                "execution_attempt_id": identity["execution_attempt_id"],
                "execution_idempotency_key": identity["execution_idempotency_key"],
                "execution_plan_hash": identity["execution_plan_hash"],
                "execution_authorization_token": identity[
                    "execution_authorization_token"
                ],
            },
        )
        terminal = next(
            (
                event_data(e)
                for e in reversed(execution_events)
                if e.get("event") == "end_of_workflow"
            ),
            {},
        )
        step_results = [
            event_data(e)
            for e in execution_events
            if e.get("event") in {"step_result", "end_of_agent"}
        ]
        summary = {
            "workflow_id": workflow_id,
            "task_id": terminal.get("task_id"),
            "entities": profile.get("entities"),
            "missing_fields": profile.get("missing_fields"),
            "needs_clarification": profile.get("needs_clarification"),
            "routing_decision": (routing.get("routing_decision") or {}).get("decision"),
            "selected_agent": (routing.get("routing_decision") or {}).get("selected_agent"),
            "planned_expected_outputs": [step.get("expected_outputs") for step in steps],
            "terminal_status": terminal.get("status"),
            "failed_steps": terminal.get("failed_steps"),
            "failures": terminal.get("failures"),
            "recovery_review_required": terminal.get("recovery_review_required"),
            "step_results": step_results,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if terminal.get("status") != "SUCCEEDED":
            raise SystemExit(2)
        serialized = json.dumps(step_results, ensure_ascii=False)
        if "employee.salary" not in serialized:
            raise SystemExit("SUCCEEDED without employee.salary evidence")


asyncio.run(main())
