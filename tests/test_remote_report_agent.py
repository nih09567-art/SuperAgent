from __future__ import annotations

import asyncio
import json

from remote_agents.report_agent import RemoteReportAgent


class FakeExtractor:
    async def extract(self, **kwargs):
        return {"title": "人事情况汇总", "data": [{"employee": "李娜"}]}


def test_report_agent_uses_separate_inner_and_outer_timeouts(monkeypatch) -> None:
    monkeypatch.setenv("REMOTE_REPORT_LLM_TIMEOUT", "80")
    monkeypatch.setenv("REMOTE_REPORT_TOOL_TIMEOUT", "100")
    captured = {}
    agent = RemoteReportAgent()

    async def fake_call_tool(*, tool_name, arguments, timeout):
        captured.update(
            {"tool_name": tool_name, "arguments": arguments, "timeout": timeout}
        )
        return {"status": "success", "markdown": "# 人事情况汇总"}

    monkeypatch.setattr(agent, "call_tool", fake_call_tool)
    result = asyncio.run(
        agent.execute(
            tools=[{"name": "remote_report_builder_tool"}],
            messages=[],
            context={},
            parameter_extractor=FakeExtractor(),
        )
    )

    assert result["status"] == "success"
    assert result["outputs"]["report.markdown"]["markdown"] == "# 人事情况汇总"
    assert result["outputs"]["report.markdown"]["source_count"] == 1
    assert captured["arguments"]["llm_timeout_sec"] == 80
    assert captured["timeout"] == 100


def test_report_agent_locks_tool_data_to_scheduler_fan_in(monkeypatch) -> None:
    monkeypatch.setenv("REMOTE_REPORT_LLM_TIMEOUT", "80")
    monkeypatch.setenv("REMOTE_REPORT_TOOL_TIMEOUT", "100")
    captured = {}
    agent = RemoteReportAgent()
    sources = [
        {
            "logical_name": "employee.info",
            "schema_ref": "employee.info@v1",
            "payload": {"records": [{"name": "王强"}]},
        },
        {
            "logical_name": "policy.info",
            "schema_ref": "policy.info@v2",
            "payload": {"answer": "五天"},
        },
    ]
    brief = {
        "resolved_inputs": {
            "report.sources": {
                "sources": sources,
                "title": "真实汇总",
                "instruction": "严格使用两个来源",
            }
        }
    }

    class EmptyExtractor:
        async def extract(self, **kwargs):
            raise AssertionError("structured fan-in must bypass LLM extraction")

    async def fake_call_tool(*, tool_name, arguments, timeout):
        captured.update(arguments)
        return {"status": "success", "markdown": "# 真实汇总"}

    monkeypatch.setattr(agent, "call_tool", fake_call_tool)
    result = asyncio.run(
        agent.execute(
            tools=[{"name": "remote_report_builder_tool"}],
            messages=[
                {
                    "role": "user",
                    "content": "EXECUTION_CONTEXT\n"
                    + json.dumps(brief, ensure_ascii=False),
                }
            ],
            context={},
            parameter_extractor=EmptyExtractor(),
        )
    )

    assert captured["data"] == sources
    assert captured["title"] == "真实汇总"
    assert captured["instruction"] == "严格使用两个来源"
    assert result["outputs"]["report.markdown"]["source_count"] == 2


def test_report_agent_rejects_duplicate_employee_source_without_policy(
    monkeypatch,
) -> None:
    agent = RemoteReportAgent()
    employee = {
        "logical_name": "employee.info",
        "schema_ref": "employee.info@v1",
        "payload": {"records": [{"name": "王强"}]},
    }
    brief = {
        "resolved_inputs": {
            "report.sources": {
                "sources": [employee, dict(employee)],
                "title": "年度假汇总",
                "instruction": "依据政策判断年假天数",
            }
        }
    }

    async def unexpected_call(**_kwargs):
        raise AssertionError("invalid annual-leave sources must not call the tool")

    monkeypatch.setattr(agent, "call_tool", unexpected_call)
    result = asyncio.run(
        agent.execute(
            tools=[{"name": "remote_report_builder_tool"}],
            messages=[
                {
                    "role": "user",
                    "content": "EXECUTION_CONTEXT\n"
                    + json.dumps(brief, ensure_ascii=False),
                }
            ],
            context={},
            parameter_extractor=FakeExtractor(),
        )
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "REPORT_TOOL_ERROR"
