"""Local reproduction: iterate run_agent_workflow and capture the exact exception."""
import asyncio
import json
import traceback

from src.workflow.process import run_agent_workflow

instruction = "查询王强的在职状态、岗位和累计工龄，并依据国务院关于职工带薪年休假的规定，判断其年假天数，生成一份 Markdown 汇总。"


async def main():
    event_count = 0
    try:
        async for event in run_agent_workflow(
            user_id="test",
            user_input_messages=[{"role": "user", "content": instruction}],
            debug=True,
            deep_thinking_mode=False,
            search_before_planning=False,
            coor_agents=None,
            workmode="launch",
            workflow_id=None,
            stop_after_planner=True,
            instruction=instruction,
            instruction_history=[],
            original_user_query=instruction,
            memory_session_id="debug-session",
            memory_context={},
            request_input_messages=[{"role": "user", "content": instruction}],
            current_request=instruction,
            raw_request=instruction,
            entity_overrides={},
            context_references=[],
            context_artifacts=[],
            conversation_context={"entities": {}, "artifacts": []},
        ):
            event_count += 1
            ev = event.get("event", "?")
            data = event.get("data") or {}
            print(f"[{event_count}] event={ev}", flush=True)
            text = json.dumps(data, ensure_ascii=False, default=str)
            print(text[:1500], flush=True)
            if event_count >= 40:
                print("...stopping after 40 events", flush=True)
                break
    except Exception as exc:
        print(f"\nEXCEPTION after {event_count} events:", flush=True)
        print(f"{type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()
        return 1
    print(f"\nSTREAM ENDED NORMALLY after {event_count} events")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
