import asyncio
import json
from pathlib import Path

import pytest

from src.tools.office_mcp.selector import (
    finalize_top_k_with_agent,
    govern_preselected_tool,
    load_tool_catalog,
    select_office_mcp_tool,
    validate_known_inputs,
)
from src.tools.office_mcp.server import mcp


ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads(
    (ROOT / "experiments" / "office_mcp_tool_selection_cases.json").read_text(
        encoding="utf-8-sig"
    )
)["cases"]
REQUIRED_METADATA = {
    "name",
    "scenario",
    "owner_agents",
    "intents",
    "aliases",
    "operation_mode",
    "required_inputs",
    "input_schema",
    "produces",
    "description",
    "side_effect",
    "risk_level",
    "legacy_tool_name",
}


def test_catalog_exactly_matches_registered_server_tools():
    catalog = load_tool_catalog()
    catalog_names = [item["name"] for item in catalog]
    registered = {tool.name: tool for tool in mcp._tool_manager.list_tools()}

    assert len(catalog) == 30
    assert len(catalog_names) == len(set(catalog_names))
    assert set(catalog_names) == set(registered)
    assert all(REQUIRED_METADATA <= set(item) for item in catalog)
    assert all("grants_required" not in item for item in catalog)
    assert all("requires_approval" not in item for item in catalog)

    for item in catalog:
        server_schema = registered[item["name"]].parameters
        properties = server_schema.get("properties", {})
        assert item["input_schema"] == server_schema, item["name"]
        assert item["required_inputs"] == server_schema.get("required", []), item["name"]
        assert {
            name: definition.get("type") for name, definition in properties.items()
        } == {
            name: definition.get("type")
            for name, definition in item["input_schema"]["properties"].items()
        }, item["name"]


def test_fixed_evaluation_cases_cover_all_tools_and_match_expectations():
    catalog_names = {item["name"] for item in load_tool_catalog()}
    normal_cases = [
        item
        for item in CASES
        if item["kind"] == "normal" and not item.get("expected_server")
    ]
    assert len(normal_cases) == 30
    assert {item["expected_tool"] for item in normal_cases} == catalog_names

    for case in [item for item in CASES if not item.get("expected_server")]:
        decision = select_office_mcp_tool(
            query=case["query"],
            known_inputs=case["known_inputs"],
            agent_name=case.get("agent_name"),
            top_k=3,
        )
        assert decision["selected_tool"] == case.get("expected_tool"), case["id"]
        if case.get("expected_tool"):
            assert case["expected_tool"] in {
                item["name"] for item in decision["candidates"]
            }, case["id"]
            selected = next(
                item
                for item in load_tool_catalog()
                if item["name"] == decision["selected_tool"]
            )
            assert validate_known_inputs(selected, case["known_inputs"])["valid"]
        if case.get("expected_excluded_tool"):
            excluded = {
                item["name"]: item["reasons"] for item in decision["excluded"]
            }
            assert case["expected_excluded_tool"] in excluded
            assert any(
                reason["reason"] in {"missing_required_inputs", "invalid_input_types"}
                for reason in excluded[case["expected_excluded_tool"]]
            )


def test_selector_explains_inference_scoring_and_schema_exclusion():
    decision = select_office_mcp_tool(
        query="创建会议",
        known_inputs={"title": "项目会", "meeting_date": "2026-08-18"},
        agent_name="RemoteMeetingManagerAgent",
        top_k=3,
    )
    assert decision["inference"]["scenario"] == "meeting"
    assert decision["inference"]["operation_mode"] == "create"
    assert decision["selected_tool"] is None
    create_meeting = next(
        item for item in decision["excluded"] if item["name"] == "create_meeting"
    )
    assert any(
        reason["reason"] == "missing_required_inputs"
        and set(reason["inputs"]) == {"meeting_time", "participants"}
        for reason in create_meeting["reasons"]
    )


def test_unknown_task_abstains_instead_of_hallucinating_a_tool():
    decision = select_office_mcp_tool(
        query="生成增值税发票", known_inputs={}, top_k=3
    )
    assert decision["selected_tool"] is None
    assert decision["abstention_reason"]
    assert all(item["name"] in {tool["name"] for tool in load_tool_catalog()} for item in decision["candidates"])


def test_off_audit_enforce_modes_are_local_to_selector(monkeypatch):
    common = {
        "preferred_tool": "get_employee_profile",
        "query": "搜索员工",
        "known_inputs": {"keyword": "王强"},
        "agent_name": "RemoteHRAssistantAgent",
    }
    monkeypatch.delenv("MCP_TOOL_SELECTION_MODE", raising=False)
    monkeypatch.delenv("OFFICE_MCP_TOOL_SELECTION_MODE", raising=False)
    off = govern_preselected_tool(**common)
    assert off == {
        "mode": "off",
        "effective_tool": "get_employee_profile",
        "decision": None,
    }

    monkeypatch.setenv("OFFICE_MCP_TOOL_SELECTION_MODE", "audit")
    audit = govern_preselected_tool(**common)
    assert audit["effective_tool"] == "get_employee_profile"
    assert audit["decision"]["selected_tool"] == "search_employees"

    monkeypatch.setenv("OFFICE_MCP_TOOL_SELECTION_MODE", "enforce")
    enforce = govern_preselected_tool(**common)
    assert enforce["effective_tool"] == "search_employees"


class _TopKExtractorDouble:
    def __init__(self):
        self.received_tools = []

    async def select_tool_and_extract(self, *, tools, **_kwargs):
        self.received_tools = tools
        selected = next(item for item in tools if item["name"] == "search_employees")
        return selected, {"keyword": "王强", "limit": 3}


def test_top_k_can_be_handed_to_agent_compatible_parameter_extractor():
    decision = select_office_mcp_tool(
        query="查询员工信息",
        known_inputs={"keyword": "王强"},
        agent_name="RemoteHRAssistantAgent",
        top_k=3,
    )
    extractor = _TopKExtractorDouble()
    result = asyncio.run(
        finalize_top_k_with_agent(
            decision=decision,
            query="查询员工信息",
            agent_name="RemoteHRAssistantAgent",
            agent_prompt="选择员工查询工具",
            parameter_extractor=extractor,
        )
    )
    assert [item["name"] for item in extractor.received_tools] == result["selector_top_k"]
    assert result == {
        "selector_top_k": result["selector_top_k"],
        "agent_selected_tool": "search_employees",
        "arguments": {"keyword": "王强", "limit": 3},
        "executed": False,
    }
    assert "search_employees" in result["selector_top_k"]


def test_agent_finalizer_rejects_tool_outside_top_k():
    class BadExtractor:
        async def select_tool_and_extract(self, **_kwargs):
            return {"name": "invented_office_tool"}, {}

    decision = select_office_mcp_tool(
        query="搜索课程",
        known_inputs={"query": "大模型"},
        agent_name="RemoteKnowledgeAgent",
        top_k=3,
    )
    with pytest.raises(ValueError, match="outside selector Top-K"):
        asyncio.run(
            finalize_top_k_with_agent(
                decision=decision,
                query="搜索课程",
                agent_name="RemoteKnowledgeAgent",
                agent_prompt="选择课程工具",
                parameter_extractor=BadExtractor(),
            )
        )
