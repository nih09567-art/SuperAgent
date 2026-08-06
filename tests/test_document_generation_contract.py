from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
import pytest

import mock_remote_tool_skill as tool_server
from mock_remote_tool_skill import _parse_optional_amount, app
from remote_agents.document_generator_agent import RemoteDocumentGeneratorAgent
from remote_agents.document_generator_agent import _resolved_upstream_content
from remote_agents.email_dispatch_agent import RemoteEmailDispatchAgent
from remote_agents.report_agent import RemoteReportAgent


ROOT = Path(__file__).resolve().parents[1]


def test_document_amount_parser_accepts_business_placeholders() -> None:
    for placeholder in ("待补充", "待确认", "未提供", "暂无", "未知"):
        assert _parse_optional_amount(placeholder) is None


@pytest.mark.parametrize(
    ("arguments", "expected_error"),
    [
        ({"body": "test body"}, "to is required"),
        ({"to": "recipient@example.com"}, "body is required"),
    ],
)
def test_email_rejects_missing_recipient_or_body_without_persisting(
    monkeypatch, tmp_path, arguments, expected_error
) -> None:
    monkeypatch.setattr(tool_server, "_EMAIL_CACHE", None)
    email_path = tmp_path / "emails.json"
    monkeypatch.setattr(tool_server, "_email_path", lambda: email_path)
    response = TestClient(app).post(
        "/tool",
        json={
            "tool": "remote_email_tool",
            "arguments": arguments,
        },
    )

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["status"] == "failed"
    assert result["error"] == expected_error
    assert result["failure_phase"] == "validation"
    assert result["safe_to_retry"] is True
    assert "external_operation_id" not in result
    assert not email_path.exists()


@pytest.mark.parametrize(
    ("tool_name", "loader_name"),
    [
        ("query_leave_record", "_load_leave_applications"),
        ("query_travel_record", "_load_travel_applications"),
    ],
)
def test_high_sensitivity_record_query_rejects_missing_employee_scope(
    monkeypatch, tool_name, loader_name
) -> None:
    monkeypatch.setattr(tool_server, loader_name, lambda: [{"employee_id": "E001"}])
    response = TestClient(app).post(
        "/tool",
        json={"tool": tool_name, "arguments": {}},
    )

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["status"] == "failed"
    assert result["error"] == "employee_id or employee_name is required"
    assert "records" not in result


@pytest.mark.parametrize(
    ("tool_name", "loader_name"),
    [
        ("query_leave_record", "_load_leave_applications"),
        ("query_travel_record", "_load_travel_applications"),
    ],
)
def test_administrator_marker_cannot_query_unscoped_records(
    monkeypatch, tool_name, loader_name
) -> None:
    records = [{"employee_id": "E001", "employee_name": "李娜"}]
    monkeypatch.setattr(tool_server, loader_name, lambda: records)

    response = TestClient(app).post(
        "/tool",
        json={
            "tool": tool_name,
            "arguments": {"__trusted_administrator": True},
        },
    )

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["status"] == "failed"
    assert result["error"] == "employee_id or employee_name is required"
    assert "records" not in result


def test_document_save_failure_is_not_reported_as_success(
    monkeypatch, tmp_path
) -> None:
    import docx.document

    attempted = {"save": False}

    def fail_save(_document, _path):
        attempted["save"] = True
        raise OSError("simulated disk failure")

    monkeypatch.setattr(docx.document.Document, "save", fail_save)
    response = TestClient(app).post(
        "/tool",
        json={
            "tool": "remote_docx_generator_tool",
            "arguments": {
                "template_name": "",
                "data": {"title": "test document"},
                "output_filename": str(tmp_path / "cannot-write"),
            },
        },
    )

    assert response.status_code == 200
    assert attempted["save"] is True
    result = response.json()["result"]
    assert result["status"] == "failed"
    assert result["file_path"] == ""
    assert result["partial_result"]["content"]["title"] == "test document"
    assert result["failure_phase"] == "external_operation"
    assert result["safe_to_retry"] is False
    assert "external_operation_id" not in result


def test_weather_tool_uses_the_remote_agent_result_contract() -> None:
    response = TestClient(app).post(
        "/tool",
        json={
            "tool": "remote_weather_tool",
            "arguments": {"location": "北京", "date": "明天"},
        },
    )

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["status"] == "success"
    assert result["location"] == "北京"
    assert result["date"] == "明天"
    assert result["weather"] == "晴"


class FakeParameterExtractor:
    async def extract(self, **_: Any) -> dict[str, Any]:
        # 模拟参数模型选错旧模板；Agent 应以 TaskProfile 契约为准纠正模板。
        return {
            "template_name": "recommendation_letter",
            "data": {},
            "output_filename": "annual_leave_policy",
        }


class EmptyParameterExtractor:
    async def extract(self, **_: Any) -> dict[str, Any]:
        return {}


def test_report_uses_resolved_artifact_when_optional_model_fields_are_empty() -> None:
    agent = RemoteReportAgent()
    captured: dict[str, Any] = {}

    async def fake_call_tool(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"status": "success", "markdown": "report"}

    agent.call_tool = fake_call_tool  # type: ignore[method-assign]
    messages = [
        {
            "role": "user",
            "content": "EXECUTION_CONTEXT\n"
            + json.dumps(
                {
                    "resolved_inputs": {
                        "upstream_risk": {
                            "status": "success",
                            "records": [{"company_id": "uc-001"}],
                        }
                    }
                },
                ensure_ascii=False,
            ),
        }
    ]

    result = asyncio.run(
        agent.execute(
            tools=[{"name": "remote_report_builder_tool"}],
            messages=messages,
            context={},
            parameter_extractor=EmptyParameterExtractor(),
        )
    )

    assert result["status"] == "success"
    assert captured["arguments"]["data"] == [{"company_id": "uc-001"}]


def test_email_reuses_profile_recipient_and_resolved_report() -> None:
    agent = RemoteEmailDispatchAgent()
    captured: dict[str, Any] = {}

    async def fake_call_tool(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"status": "success"}

    agent.call_tool = fake_call_tool  # type: ignore[method-assign]
    messages = [
        {
            "role": "user",
            "content": "EXECUTION_CONTEXT\n"
            + json.dumps(
                {
                    "task_profile": {
                        "entities": {
                            "recipient": "合规负责人",
                            "document_type": "风险分析报告",
                        }
                    },
                    "resolved_inputs": {
                        "upstream_report": {
                            "status": "success",
                            "markdown": "# 风险分析报告",
                        }
                    },
                },
                ensure_ascii=False,
            ),
        }
    ]

    result = asyncio.run(
        agent.execute(
            tools=[{"name": "remote_email_tool"}],
            messages=messages,
            context={},
            parameter_extractor=EmptyParameterExtractor(),
        )
    )

    assert result["status"] == "success"
    assert captured["arguments"] == {
        "to": "合规负责人",
        "subject": "风险分析报告",
        "body": "# 风险分析报告",
    }


def test_registry_only_advertises_installed_document_templates() -> None:
    registry = json.loads(
        (ROOT / "mock_remote_registry.json").read_text(encoding="utf-8-sig")
    )
    templates = json.loads(
        (ROOT / "assets" / "document_templates.json").read_text(encoding="utf-8")
    )["templates"]
    document_agent = next(
        item
        for item in registry["resources"]
        if item.get("name") == "RemoteDocumentGeneratorAgent"
    )
    tool = document_agent["metadata"]["selected_tools"][0]
    advertised = set(tool["parameters"]["properties"]["template_name"]["enum"])

    assert advertised == set(templates)
    assert "recommendation_letter" not in advertised
    assert "explanation_document" in advertised


def test_explanation_document_uses_profile_contract_and_upstream_report() -> None:
    agent = RemoteDocumentGeneratorAgent()
    captured: dict[str, Any] = {}

    async def fake_call_tool(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"status": "success"}

    agent.call_tool = fake_call_tool  # type: ignore[method-assign]
    messages = [
        {
            "role": "assistant",
            "tool": "RemoteReportAgent",
            "content": "# 年假制度摘要\n\n员工年假按工龄分档执行。",
        },
        {
            "role": "user",
            "content": "EXECUTION_CONTEXT\n"
            + json.dumps(
                {
                    "assigned_steps": [{"title": "生成年假制度说明文档"}],
                    "task_profile": {
                        "entities": {"document_type": "explanation_document"}
                    },
                },
                ensure_ascii=False,
            ),
        },
    ]

    result = asyncio.run(
        agent.execute(
            tools=[{"name": "remote_docx_generator_tool"}],
            messages=messages,
            context={},
            parameter_extractor=FakeParameterExtractor(),
        )
    )

    assert result["status"] == "success"
    arguments = captured["arguments"]
    assert arguments["template_name"] == "explanation_document"
    assert arguments["data"]["title"] == "生成年假制度说明文档"
    assert "员工年假按工龄分档执行" in arguments["data"]["content"]


def test_document_agent_reads_markdown_from_assembled_document_content():
    brief = {
        "resolved_inputs": {
            "document.content": {
                "title": "年假制度说明",
                "instruction": "生成 Word 文档",
                "sources": [
                    {
                        "logical_name": "report.markdown",
                        "schema_ref": "report.markdown@v1",
                        "payload": {
                            "title": "年假制度摘要",
                            "markdown": "# 公司年假制度\n员工依法享有年假。",
                            "source_count": 1,
                        },
                    }
                ],
            }
        }
    }

    assert _resolved_upstream_content(brief) == "# 公司年假制度\n员工依法享有年假。"
