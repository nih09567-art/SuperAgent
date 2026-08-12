from __future__ import annotations

import asyncio
import json

from remote_agents.hr_assistant_agent import RemoteHRAssistantAgent


class FakeExtractor:
    async def extract(self, **kwargs):
        tool_name = kwargs["tool"]["name"]
        if tool_name == "remote_salary_info_tool":
            return {"employee_name": "王强"}
        return {"keyword": "王强"}


def _execution_messages(query: str) -> list[dict]:
    brief = {
        "original_user_query": query,
        "step": {"title": query, "description": query},
    }
    return [
        {
            "role": "user",
            "content": "EXECUTION_CONTEXT\n"
            + json.dumps(brief, ensure_ascii=False),
        }
    ]


def _clarified_salary_messages() -> list[dict]:
    brief = {
        "original_user_query": "李娜",
        "assigned_steps": [
            {
                "title": "查询员工信息",
                "description": "按追问补充的姓名查询员工",
                "expected_outputs": ["employee.salary"],
            }
        ],
    }
    return [
        {"role": "user", "content": "查询工资"},
        {"role": "assistant", "content": "请提供员工姓名。"},
        {"role": "user", "content": "李娜"},
        {
            "role": "user",
            "content": "EXECUTION_CONTEXT\n"
            + json.dumps(brief, ensure_ascii=False),
        },
    ]


def _tools() -> list[dict]:
    return [
        {"name": "remote_person_info_tool"},
        {"name": "remote_salary_info_tool"},
    ]


def test_basic_employee_query_does_not_call_salary_tool():
    agent = RemoteHRAssistantAgent()
    calls = []

    async def fake_call_tool(*, tool_name, arguments, **_kwargs):
        calls.append((tool_name, arguments))
        return {
            "detail": {
                "personInfoList": [
                    {"idvId": "employee-1", "name": "王强", "department": "营业部"}
                ]
            }
        }

    agent.call_tool = fake_call_tool
    result = asyncio.run(
        agent.execute(
            _tools(),
            _execution_messages("查询员工王强的基本信息"),
            {},
            FakeExtractor(),
        )
    )

    assert [name for name, _arguments in calls] == ["remote_person_info_tool"]
    assert result["status"] == "success"
    assert result["outputs"]["employee.info"]["records"] == [
        {"adtEmpeNm": "王强", "holdposInstNm": "营业部"}
    ]
    assert "employee.salary" not in result["outputs"]


def test_explicit_salary_query_calls_salary_tool_and_merges_result():
    agent = RemoteHRAssistantAgent()
    calls = []

    async def fake_call_tool(*, tool_name, arguments, **_kwargs):
        calls.append((tool_name, arguments))
        if tool_name == "remote_person_info_tool":
            return {
                "detail": {
                    "personInfoList": [{"idvId": "employee-1", "name": "王强"}]
                }
            }
        return {
            "salary_records": [
                {
                    "employee_id": "employee-1",
                    "monthly_salary": 100,
                    "annual_salary": 1200,
                    "currency": "CNY",
                }
            ]
        }

    agent.call_tool = fake_call_tool
    result = asyncio.run(
        agent.execute(
            _tools(),
            _execution_messages("查询员工王强的薪资信息"),
            {},
            FakeExtractor(),
        )
    )

    assert [name for name, _arguments in calls] == [
        "remote_person_info_tool",
        "remote_salary_info_tool",
    ]
    assert (
        result["outputs"]["employee.salary"]["records"][0]["monthly_salary"]
        == 100
    )


def test_clarified_employee_name_keeps_salary_step_scope():
    agent = RemoteHRAssistantAgent()
    calls = []

    async def fake_call_tool(*, tool_name, arguments, **_kwargs):
        calls.append((tool_name, arguments))
        if tool_name == "remote_person_info_tool":
            return {
                "detail": {"personInfoList": [{"idvId": "employee-2", "name": "李娜"}]}
            }
        return {
            "salary_records": [
                {
                    "employee_id": "employee-2",
                    "monthly_salary": 18000,
                    "annual_salary": 216000,
                    "currency": "CNY",
                }
            ]
        }

    agent.call_tool = fake_call_tool
    result = asyncio.run(
        agent.execute(
            _tools(),
            _clarified_salary_messages(),
            {},
            FakeExtractor(),
        )
    )

    assert [name for name, _arguments in calls] == [
        "remote_person_info_tool",
        "remote_salary_info_tool",
    ]
    assert result["status"] == "success"
    assert result["outputs"]["employee.salary"]["matched_count"] == 1
