"""Run the eight admin demo instructions through the real Web/SSE workflow.

This is an executable evaluation harness, not a mocked unit test.  It first
asks the live planner to persist an approved plan and then executes that exact
workflow through the production TaskGraph scheduler.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx


CASES = [
    "查询员工李娜的基本信息，生成收入证明，然后发给王经理",
    "查询员工张三的工资，生成收入证明，发邮件通知 HR",
    "搜索李娜的公开信息，整理成一份简短报告",
    "查询王经理下周的日程，安排一次和李娜的会议，并通知参会人",
    "查询公司年假制度，整理成摘要，并生成一份说明文档",
    "查询客户授信风险，生成风险分析报告，并发送给合规负责人",
    "查询员工李娜的基本信息和请假记录，生成一份人事情况汇总",
    "查询北京明天天气，结合出差行程给出提醒",
]


async def _stream_events(
    client: httpx.AsyncClient,
    base_url: str,
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    async with client.stream(
        "POST", f"{base_url}/api/workflows/run", json=payload
    ) as response:
        response.raise_for_status()
        event_name = "message"
        data_lines: list[str] = []
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                event_name = line.partition(":")[2].strip()
            elif line.startswith("data:"):
                data_lines.append(line.partition(":")[2].lstrip())
            elif not line and data_lines:
                raw = json.loads("\n".join(data_lines))
                if isinstance(raw, dict):
                    raw.setdefault("event", event_name)
                    events.append(raw)
                event_name = "message"
                data_lines = []
    return events


def _event_data(event: dict[str, Any]) -> dict[str, Any]:
    value = event.get("data")
    return value if isinstance(value, dict) else {}


def _terminal(events: list[dict[str, Any]]) -> dict[str, Any]:
    matches = [event for event in events if event.get("event") == "end_of_workflow"]
    return _event_data(matches[-1]) if matches else {}


async def run_case(
    client: httpx.AsyncClient,
    base_url: str,
    case_number: int,
    instruction: str,
) -> dict[str, Any]:
    session_id = f"admin-eight-{case_number}-{uuid4().hex}"
    common = {
        "user_id": "admin",
        "lang": "zh",
        "instruction_history": [instruction],
        "original_user_query": instruction,
        "debug": False,
        "deep_thinking_mode": True,
        "search_before_planning": False,
        "coor_agents": None,
        "session_id": session_id,
        "memory_session_id": session_id,
        "memory_enabled": False,
        "skill_reuse_enabled": False,
    }
    print(f"[{case_number}/8] PLAN {instruction}", flush=True)
    plan_events = await _stream_events(
        client,
        base_url,
        {
            **common,
            "workmode": "launch",
            "stop_after_planner": True,
            "instruction": instruction,
            "messages": [{"role": "user", "content": instruction}],
        },
    )
    plan_start = next(
        (_event_data(event) for event in plan_events if event.get("event") == "start_of_workflow"),
        {},
    )
    workflow_id = str(plan_start.get("workflow_id") or "")
    plan_terminal = _terminal(plan_events)
    planning_failures = [
        event
        for event in plan_events
        if event.get("event") in {"permission_denied", "workflow_error", "planning_failed"}
    ]
    if not workflow_id or planning_failures:
        return {
            "case": case_number,
            "instruction": instruction,
            "workflow_id": workflow_id,
            "status": "PLANNING_FAILED",
            "terminal": plan_terminal,
            "failure_events": planning_failures,
        }

    print(f"[{case_number}/8] EXEC {workflow_id}", flush=True)
    execution_events = await _stream_events(
        client,
        base_url,
        {
            **common,
            "workmode": "production",
            "stop_after_planner": False,
            "instruction": None,
            "workflow_id": workflow_id,
            "resolved_request": instruction,
            "messages": [
                {"role": "user", "content": instruction},
                {"role": "user", "content": "Execute the confirmed plan."},
            ],
        },
    )
    terminal = _terminal(execution_events)
    step_events = [
        _event_data(event)
        for event in execution_events
        if event.get("event") == "end_of_agent"
    ]
    failure_events = [
        event
        for event in execution_events
        if event.get("event")
        in {"permission_denied", "approval_required", "reconciliation_required"}
    ]
    status = str(terminal.get("status") or "MISSING_TERMINAL").upper()
    result = {
        "case": case_number,
        "instruction": instruction,
        "workflow_id": workflow_id,
        "task_id": terminal.get("task_id"),
        "status": status,
        "failed_steps": terminal.get("failed_steps") or [],
        "blocked_steps": terminal.get("blocked_steps") or [],
        "steps": [
            {
                "step_id": item.get("step_id"),
                "agent": item.get("agent_name") or item.get("agent_id"),
                "status": item.get("status"),
                "error": item.get("error"),
                "failure": item.get("failure"),
            }
            for item in step_events
        ],
        "governance_failure_events": failure_events,
        "terminal": terminal,
    }
    print(
        f"[{case_number}/8] RESULT {status} "
        f"failed={result['failed_steps']} blocked={result['blocked_steps']}",
        flush=True,
    )
    return result


async def main_async(args: argparse.Namespace) -> int:
    selected = (
        [(args.case, CASES[args.case - 1])]
        if args.case
        else list(enumerate(CASES, start=1))
    )
    timeout = httpx.Timeout(args.timeout, connect=20.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        results = [
            await run_case(client, args.base_url.rstrip("/"), number, instruction)
            for number, instruction in selected
        ]
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    passed = sum(item["status"] == "SUCCEEDED" for item in results)
    print(f"SUMMARY {passed}/{len(results)} SUCCEEDED -> {output}", flush=True)
    return 0 if passed == len(results) else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--case", type=int, choices=range(1, 9))
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--output", default="output/admin-eight-e2e.json")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
