"""Debug script: capture the full SSE stream from /api/workflows/run."""
import json
import sys
import httpx

BASE = "http://127.0.0.1:8001"
instruction = "查询王强的在职状态、岗位和累计工龄，并依据国务院关于职工带薪年休假的规定，判断其年假天数，生成一份 Markdown 汇总。"

payload = {
    "user_id": "test",
    "lang": "zh",
    "workmode": "launch",
    "stop_after_planner": True,
    "instruction": instruction,
    "instruction_history": [],
    "original_user_query": instruction,
    "turn_type": "request",
    "clarification_context": {},
    "context_entities": {},
    "context_artifacts": [],
    "messages": [{"role": "user", "content": instruction, "timestamp": "2026-08-07T00:00:00"}],
    "debug": True,
    "deep_thinking_mode": False,
    "search_before_planning": False,
    "coor_agents": None,
    "workflow_id": None,
    "session_id": "debug-session",
    "memory_session_id": "debug-session",
}

headers = {
    "Content-Type": "application/json",
    "Accept": "text/event-stream",
    "X-Authenticated-User": "test",
}

event_name = "message"
lines_buf = []


def flush_event():
    global event_name, lines_buf
    data = "\n".join(lines_buf)
    out = {"event": event_name, "data": data}
    try:
        parsed = json.loads(data)
        out["parsed"] = parsed
    except Exception:
        pass
    print(json.dumps(out, ensure_ascii=False)[:2000])
    print("-" * 80)
    event_name = "message"
    lines_buf = []


try:
    with httpx.stream(
        "POST",
        f"{BASE}/api/workflows/run",
        json=payload,
        headers=headers,
        timeout=httpx.Timeout(180.0, connect=10.0),
    ) as response:
        print(f"HTTP status: {response.status_code}", flush=True)
        if response.status_code != 200:
            print(response.text[:2000])
            sys.exit(1)
        for line in response.iter_lines():
            if line == "":
                flush_event()
                continue
            if line.startswith("event:"):
                event_name = line.partition(":")[2].strip()
            elif line.startswith("data:"):
                lines_buf.append(line.partition(":")[2].strip())
        if lines_buf:
            flush_event()
        print("STREAM_ENDED_NORMALLY")
except Exception as exc:
    print(f"STREAM_FAILED: {type(exc).__name__}: {exc}")
    print("Last event seen:", event_name)
    if lines_buf:
        print("Last data lines:", lines_buf[-3:])



