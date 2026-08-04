from __future__ import annotations

import asyncio
import json

import pytest

from remote_agents.hr_assistant_agent import RemoteHRAssistantAgent
from remote_agents.knowledge_agent import RemoteKnowledgeAgent
from remote_agents.report_agent import RemoteReportAgent
from src.contracts.agent_schema_catalog import register_agent_schemas
from src.interface.task_graph import TaskGraphValidationError
from src.orchestration.plan_to_task_graph import (
    canonicalize_annual_leave_plan,
    plan_to_task_graph,
)
from src.orchestration.schema_registry import SchemaRegistry
from tests.integration.annual_leave_e2e_harness import _timeline_event


def _contracts():
    return {
        "RemoteHRAssistantAgent": RemoteHRAssistantAgent().contract,
        "RemoteKnowledgeAgent": RemoteKnowledgeAgent().contract,
        "RemoteReportAgent": RemoteReportAgent().contract,
    }


def _annual_leave_plan() -> list[dict]:
    return [
        {
            "step_id": "hr_query",
            "agent_name": "RemoteHRAssistantAgent",
            "title": "查询王强员工基础信息",
            "depends_on": [],
        },
        {
            "step_id": "policy_query",
            "agent_name": "RemoteKnowledgeAgent",
            "title": "查询年假政策依据",
            "depends_on": [],
        },
        {
            "step_id": "generate_report",
            "agent_name": "RemoteReportAgent",
            "title": "生成王强年假 Markdown 汇总",
            "depends_on": ["hr_query", "policy_query"],
            "inputs": [
                {
                    "parameter_name": "report.sources",
                    "source_artifacts": [
                        {
                            "source_step": "hr_query",
                            "source_output": "employee.info",
                        },
                        {
                            "source_step": "policy_query",
                            "source_output": "policy.info",
                        },
                    ],
                    "assembly": {"schema_ref": "report.sources@v1"},
                }
            ],
        },
    ]


def test_annual_leave_plan_has_exact_three_step_contract_fan_in():
    graph = plan_to_task_graph(
        _annual_leave_plan(),
        task_id="annual-leave-plan",
        subject="demo_hr_manager",
        agent_contracts=_contracts(),
    )

    assert [step.step_id for step in graph.steps] == [
        "hr_query",
        "policy_query",
        "generate_report",
    ]
    by_id = graph.step_map()
    assert by_id["hr_query"].depends_on == []
    assert by_id["policy_query"].depends_on == []
    assert set(by_id["generate_report"].depends_on) == {
        "hr_query",
        "policy_query",
    }
    assert by_id["generate_report"].expected_outputs == ["report.markdown"]
    assert by_id["generate_report"].expected_schema_refs == {
        "report.markdown": "report.markdown@v1"
    }
    assert len(by_id["generate_report"].input_bindings) == 1
    assert {
        (item["source_step"], item["source_output"])
        for item in by_id["generate_report"].input_bindings[0][
            "source_artifacts"
        ]
    } == {
        ("hr_query", "employee.info"),
        ("policy_query", "policy.info"),
    }
    assert not any(
        token in json.dumps(_annual_leave_plan(), ensure_ascii=False)
        for token in ("工资", "薪资", "联系方式", "身份证")
    )


def test_real_planner_positional_ids_are_canonicalized_without_inventing_edges():
    positional = [
        {
            "step_id": "step_1",
            "agent_name": "RemoteHRAssistantAgent",
            "depends_on": [],
        },
        {
            "step_id": "step_2",
            "agent_name": "RemoteKnowledgeAgent",
            "depends_on": [],
        },
        {
            "step_id": "step_3",
            "agent_name": "RemoteReportAgent",
            "depends_on": ["step_1", "step_2"],
            "inputs": [
                {
                    "parameter_name": "report.sources",
                    "source_artifacts": [
                        {
                            "source_step": "RemoteHRAssistantAgent",
                            "source_output": "employee.info",
                        },
                        {
                            "source_step": "RemoteKnowledgeAgent",
                            "source_output": "policy.info",
                        },
                    ],
                }
            ],
        },
    ]

    normalized = canonicalize_annual_leave_plan(
        positional,
        user_query=ANNUAL_QUERY,
    )

    assert [step["step_id"] for step in normalized] == [
        "hr_query",
        "policy_query",
        "generate_report",
    ]
    assert normalized[2]["depends_on"] == ["hr_query", "policy_query"]
    assert [
        source["source_step"]
        for source in normalized[2]["inputs"][0]["source_artifacts"]
    ] == ["hr_query", "policy_query"]
    assert positional[0]["step_id"] == "step_1"


ANNUAL_QUERY = (
    "请查询员工王强的在职状态、岗位和累计工龄，并依据国务院关于职工带薪年休假的规定，"
    "判断其年假天数，生成一份 Markdown 汇总。"
)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda plan: plan[2]["inputs"][0]["source_artifacts"].pop(),
        lambda plan: plan[2]["inputs"][0]["source_artifacts"][0].update(
            source_step="missing_step"
        ),
        lambda plan: plan[2]["inputs"][0]["source_artifacts"][0].update(
            source_output="employee.unknown"
        ),
    ],
)
def test_annual_leave_invalid_fan_in_fails_during_task_graph_conversion(mutation):
    plan = _annual_leave_plan()
    mutation(plan)

    with pytest.raises(TaskGraphValidationError):
        plan_to_task_graph(
            plan,
            task_id="annual-leave-invalid",
            agent_contracts=_contracts(),
        )


def test_annual_leave_assembly_schema_mismatch_fails_closed():
    plan = _annual_leave_plan()
    plan[2]["inputs"][0]["assembly"]["schema_ref"] = "policy.info@v2"

    with pytest.raises(TaskGraphValidationError, match="assembly schema"):
        plan_to_task_graph(
            plan,
            task_id="annual-leave-schema-mismatch",
            agent_contracts=_contracts(),
        )


class _Extractor:
    async def extract(self, **kwargs):
        if kwargs["tool"]["name"] == "remote_salary_info_tool":
            return {"employee_name": "王强"}
        return {"keyword": "王强"}


def _messages(text: str) -> list[dict]:
    brief = {
        "original_user_query": text,
        "step": {"title": text, "description": text},
    }
    return [
        {
            "role": "user",
            "content": "EXECUTION_CONTEXT\n"
            + json.dumps(brief, ensure_ascii=False),
        }
    ]


def test_hr_agent_projects_person_tool_rows_before_employee_artifact():
    agent = RemoteHRAssistantAgent()
    calls: list[str] = []

    async def fake_call_tool(*, tool_name, arguments, **_kwargs):
        calls.append(tool_name)
        return {
            "status": "success",
            "detail": {
                "personInfoList": [
                    {
                        "adtEmpeNm": "王强",
                        "empeStdsc": "在岗",
                        "holdposInstNm": "营业部",
                        "tcoPostNm": "客户经理",
                        "pcsTrdYrlmt": 20,
                        "idvId": "EMP-DO-NOT-LEAK",
                        "officePhone": "020-60000003",
                        "internalMaiBox": "wangqiang@ccb.com",
                        "brthDt": "1982-11-05",
                    }
                ]
            },
        }

    agent.call_tool = fake_call_tool
    result = asyncio.run(
        agent.execute(
            [
                {"name": "remote_person_info_tool"},
                {"name": "remote_salary_info_tool"},
            ],
            _messages("查询王强的年假信息"),
            {},
            _Extractor(),
        )
    )

    assert calls == ["remote_person_info_tool"]
    records = result["outputs"]["employee.info"]["records"]
    assert records == [
        {
            "adtEmpeNm": "王强",
            "empeStdsc": "在岗",
            "holdposInstNm": "营业部",
            "tcoPostNm": "客户经理",
            "pcsTrdYrlmt": 20,
        }
    ]
    serialized = json.dumps(result["outputs"]["employee.info"], ensure_ascii=False)
    for forbidden in (
        "EMP-DO-NOT-LEAK",
        "020-60000003",
        "wangqiang@ccb.com",
        "1982-11-05",
        "idvId",
        "officePhone",
        "internalMaiBox",
        "brthDt",
    ):
        assert forbidden not in serialized

    registry = register_agent_schemas(SchemaRegistry())
    valid, errors = registry.validate(
        result["outputs"]["employee.info"], "employee.info@v1"
    )
    assert valid, errors


@pytest.mark.parametrize(
    "tool_result",
    [
        None,
        {"status": "error", "error": "https://internal.example/key=secret"},
        {"status": "success"},
        {"status": "success", "markdown": 123},
        {"status": "success", "markdown": ""},
        {"status": "success", "markdown": "   \n\t"},
    ],
)
def test_report_agent_rejects_invalid_tool_results_without_sensitive_error(tool_result):
    agent = RemoteReportAgent()

    async def fake_call_tool(**_kwargs):
        return tool_result

    agent.call_tool = fake_call_tool
    result = asyncio.run(
        agent.execute(
            [{"name": "remote_report_builder_tool"}],
            [],
            {},
            _Extractor(),
        )
    )

    assert result["status"] == "error"
    assert result["outputs"] == {}
    assert result["error"]["message"] in {
        "Report tool returned invalid output",
        "Report generation failed",
    }
    assert result["error"]["details"] == {"tool": "remote_report_builder_tool"}
    assert "secret" not in json.dumps(result, ensure_ascii=False).lower()
    assert "https://" not in json.dumps(result, ensure_ascii=False).lower()


def test_report_agent_requires_success_string_markdown():
    agent = RemoteReportAgent()

    async def fake_call_tool(**_kwargs):
        return {"status": "success", "markdown": "# 年假汇总"}

    agent.call_tool = fake_call_tool
    result = asyncio.run(
        agent.execute(
            [{"name": "remote_report_builder_tool"}],
            [],
            {},
            _Extractor(),
        )
    )

    assert result["status"] == "success"
    assert result["outputs"]["report.markdown"]["markdown"] == "# 年假汇总"
    assert result["outputs"]["report.markdown"]["external_op_id"].startswith(
        "report-"
    )


def test_report_agent_normalizes_factual_markdown_spacing():
    agent = RemoteReportAgent()

    async def fake_call_tool(**_kwargs):
        return {
            "status": "success",
            "markdown": "累计工龄 20 年；年休假 15 天；国务院令第 514 号。",
        }

    agent.call_tool = fake_call_tool
    result = asyncio.run(
        agent.execute(
            [{"name": "remote_report_builder_tool"}],
            [],
            {},
            _Extractor(),
        )
    )

    assert result["outputs"]["report.markdown"]["markdown"] == (
        "累计工龄 20年；年休假 15天；国务院令第514号。"
    )


def test_report_agent_handles_policy_not_found_deterministically():
    agent = RemoteReportAgent()

    async def unexpected_tool_call(**_kwargs):
        raise AssertionError("not_found must not be delegated to the report LLM")

    agent.call_tool = unexpected_tool_call
    sources = [
        {
            "logical_name": "employee.info",
            "schema_ref": "employee.info@v1",
            "payload": {
                "records": [{"adtEmpeNm": "王强", "pcsTrdYrlmt": 20}],
                "matched_count": 1,
            },
        },
        {
            "logical_name": "policy.info",
            "schema_ref": "policy.info@v2",
            "payload": {
                "query": "年假",
                "answer": "",
                "knowledge_items_count": 0,
                "policy_scope": "unknown",
                "sources": [],
                "matched_items": [],
                "not_found": True,
            },
        },
    ]
    messages = [
        {
            "role": "user",
            "content": "EXECUTION_CONTEXT\n"
            + json.dumps(
                {
                    "resolved_inputs": {
                        "report.sources": {
                            "sources": sources,
                            "title": "王强年假汇总",
                            "instruction": "使用全部来源",
                        }
                    }
                },
                ensure_ascii=False,
            ),
        }
    ]

    result = asyncio.run(
        agent.execute(
            [{"name": "remote_report_builder_tool"}],
            messages,
            {},
            _Extractor(),
        )
    )

    output = result["outputs"]["report.markdown"]
    assert result["status"] == "success"
    assert output["source_count"] == 2
    assert "无法据此判断可休年假天数" in output["markdown"]
    assert "20天" not in output["markdown"]
    assert "15天" not in output["markdown"]


def test_e2e_timeline_accepts_non_read_scheduler_attempt_boundaries():
    start = _timeline_event(
        {
            "event": "start_of_agent",
            "data": {
                "step_id": "generate_report",
                "sub_agent_name": "RemoteReportAgent",
            },
        },
        10,
    )
    end = _timeline_event(
        {
            "event": "end_of_agent",
            "data": {
                "step_id": "generate_report",
                "planned_agent": "RemoteReportAgent",
                "executed_agent": "RemoteReportAgent",
                "status": "SUCCEEDED",
            },
        },
        11,
    )

    assert start is not None
    assert start["attempt"] == 1
    assert start["phase"] == "primary"
    assert start["executed_agent"] == "RemoteReportAgent"
    assert end is not None
    assert end["status"] == "SUCCEEDED"
    assert _timeline_event(
        {"event": "start_of_agent", "data": {"agent_name": "planner"}},
        12,
    ) is None
