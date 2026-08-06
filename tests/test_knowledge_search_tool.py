from __future__ import annotations

import asyncio
import json
from pathlib import Path

import mock_remote_tool_skill as tool_skill
import pytest


class _FakeResponse:
    content = "已根据命中条目生成演示答案。"


class _FakeLLM:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def invoke(self, prompt: str) -> _FakeResponse:
        self.prompts.append(prompt)
        return _FakeResponse()


def _knowledge_items() -> list[dict]:
    path = Path(__file__).resolve().parents[1] / "assets" / "knowledge_base.json"
    return json.loads(path.read_text(encoding="utf-8"))["knowledge_items"]


def test_knowledge_base_items_have_unique_ids_and_required_metadata() -> None:
    items = _knowledge_items()
    required = {
        "id",
        "category",
        "question",
        "keywords",
        "content",
        "source",
        "policy_scope",
        "is_demo",
    }

    assert len(items) == 12
    assert len({item["id"] for item in items}) == len(items)
    for item in items:
        assert required <= item.keys(), item.get("id")
        assert item["policy_scope"] in {"company", "statutory"}
        assert item["is_demo"] is True
        assert item.get("effective_date") or item.get("source_updated_at")


def test_knowledge_search_ranks_curated_keywords() -> None:
    ranked = tool_skill._rank_knowledge_items(_knowledge_items(), "工作十二年休几天")

    assert [item[0]["id"] for item in ranked] == ["annual_leave_001"]
    assert "十二年" in ranked[0][1]


def test_annual_leave_demo_item_has_correct_statutory_tiers_and_provenance() -> None:
    item = next(
        item for item in _knowledge_items() if item["id"] == "annual_leave_001"
    )

    assert "已满1年不满10年的，年休假5天" in item["content"]
    assert "已满10年不满20年的，年休假10天" in item["content"]
    assert "已满20年的，年休假15天" in item["content"]
    assert item["source"] == "国务院令第514号第三条"
    assert item["source_note"] == "演示知识库中的官方法规摘录"
    assert item["effective_date"] == "2008-01-01"
    assert item["policy_scope"] == "statutory"
    assert item["is_demo"] is True


def test_specific_query_filters_weak_generic_matches() -> None:
    ranked = tool_skill._rank_knowledge_items(
        _knowledge_items(),
        "个人养老金怎么缴存",
    )

    assert [item[0]["id"] for item in ranked] == ["pension_001"]


def test_ambiguous_query_can_keep_multiple_relevant_matches() -> None:
    ranked = tool_skill._rank_knowledge_items(_knowledge_items(), "账户怎么缴存")

    assert [item[0]["id"] for item in ranked] == [
        "pension_001",
        "housing_fund_001",
    ]


def test_knowledge_sources_reject_invalid_traceability_metadata() -> None:
    item = dict(_knowledge_items()[0])
    item["is_demo"] = False
    assert tool_skill._knowledge_sources([(item, [])])[0]["is_demo"] is False

    item["is_demo"] = "false"

    with pytest.raises(TypeError, match="expected bool, got str"):
        tool_skill._knowledge_sources([(item, [])])

    item["is_demo"] = True
    item["id"] = ""
    with pytest.raises(ValueError, match="id must be a non-empty string"):
        tool_skill._knowledge_sources([(item, [])])


def test_knowledge_search_limits_context_and_returns_sources(monkeypatch) -> None:
    fake_llm = _FakeLLM()
    monkeypatch.setattr(
        tool_skill,
        "_KNOWLEDGE_CACHE",
        {"knowledge_items": _knowledge_items()},
    )
    monkeypatch.setattr(tool_skill, "get_llm_by_type", lambda _name: fake_llm)

    response = asyncio.run(
        tool_skill.tool(
            tool_skill.ToolRequest(
                tool="knowledge_search_tool",
                arguments={"query": "费用报销需要什么材料"},
            )
        )
    )
    result = response["result"]

    assert result["status"] == "success"
    assert result["knowledge_items_count"] == 1
    assert result["matched_items"] == ["reimbursement_001"]
    assert result["sources"][0]["source"] == "演示公司财务报销制度（模拟）"
    assert result["sources"][0]["policy_scope"] == "company"
    assert result["sources"][0]["is_demo"] is True
    assert result["not_found"] is False
    assert len(fake_llm.prompts) == 1
    assert "reimbursement_001" in fake_llm.prompts[0]
    assert "annual_leave_001" not in fake_llm.prompts[0]


def test_annual_leave_search_returns_15_days_and_provenance(monkeypatch) -> None:
    class _AnnualLeaveLLM:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def invoke(self, prompt: str) -> _FakeResponse:
            self.prompts.append(prompt)
            assert "已满20年的，年休假15天" in prompt
            response = _FakeResponse()
            response.content = "累计工作满20年可享受15天带薪年休假。"
            return response

    fake_llm = _AnnualLeaveLLM()
    monkeypatch.setattr(
        tool_skill,
        "_KNOWLEDGE_CACHE",
        {"knowledge_items": _knowledge_items()},
    )
    monkeypatch.setattr(tool_skill, "get_llm_by_type", lambda _name: fake_llm)

    response = asyncio.run(
        tool_skill.tool(
            tool_skill.ToolRequest(
                tool="knowledge_search_tool",
                arguments={"query": "20年工龄年假"},
            )
        )
    )
    result = response["result"]

    assert result["status"] == "success"
    assert "15天" in result["answer"]
    assert result["policy_scope"] == "statutory"
    assert result["matched_items"] == ["annual_leave_001"]
    assert result["not_found"] is False
    assert len(fake_llm.prompts) == 1
    source = result["sources"][0]
    assert source["source"] == "国务院令第514号第三条"
    assert source["source_note"] == "演示知识库中的官方法规摘录"
    assert source["effective_date"] == "2008-01-01"
    assert source["policy_scope"] == "statutory"
    assert source["is_demo"] is True


def test_knowledge_search_returns_not_found_without_llm(monkeypatch) -> None:
    monkeypatch.setattr(
        tool_skill,
        "_KNOWLEDGE_CACHE",
        {"knowledge_items": _knowledge_items()},
    )

    def fail_if_called(_name):
        raise AssertionError("LLM must not be called for an unmatched query")

    monkeypatch.setattr(tool_skill, "get_llm_by_type", fail_if_called)
    response = asyncio.run(
        tool_skill.tool(
            tool_skill.ToolRequest(
                tool="knowledge_search_tool",
                arguments={"query": "火星基地如何申请"},
            )
        )
    )
    result = response["result"]

    assert result["status"] == "success"
    assert result["knowledge_items_count"] == 0
    assert result["policy_scope"] == "unknown"
    assert result["sources"] == []
    assert result["matched_items"] == []
    assert result["not_found"] is True
    assert "暂未收录" in result["answer"]
