from __future__ import annotations

import pytest

from src.contracts.agent_schema_catalog import register_agent_schemas
from src.orchestration.schema_registry import SchemaRegistry


def _policy_v2_payload(**overrides):
    payload = {
        "query": "报销",
        "answer": "请提交报销单",
        "knowledge_items_count": 1,
        "policy_scope": "company",
        "sources": [
            {
                "id": "reimbursement_001",
                "category": "公司制度-费用报销",
                "source": "演示公司财务报销制度（模拟）",
                "effective_date": "2026-01-01",
                "is_demo": True,
                "policy_scope": "company",
            }
        ],
        "matched_items": ["reimbursement_001"],
        "not_found": False,
    }
    payload.update(overrides)
    return payload


def test_catalog_registers_and_validates_business_schemas() -> None:
    registry = register_agent_schemas(SchemaRegistry())

    valid, errors = registry.validate(
        {
            "query": "年假",
            "answer": "依据国家法规执行",
            "knowledge_items_count": 2,
            "policy_scope": "statutory",
        },
        "policy.info@v1",
    )

    assert valid
    assert errors == []
    assert registry.has("employee.info@v1")
    assert registry.has("policy.info@v2")
    assert registry.has("report.sources@v1")
    assert registry.has("report.markdown@v1")
    assert registry.has("document.content@v1")


def test_document_content_accepts_a_validated_markdown_source() -> None:
    registry = register_agent_schemas(SchemaRegistry())

    valid, errors = registry.validate(
        {
            "title": "公司年假制度说明文档",
            "instruction": "将 Markdown 摘要转换为正式 Word 说明文档",
            "sources": [
                {
                    "logical_name": "report.markdown",
                    "schema_ref": "report.markdown@v1",
                    "payload": {
                        "title": "年假制度摘要",
                        "markdown": "# 年假制度",
                        "source_count": 1,
                    },
                }
            ],
        },
        "document.content@v1",
    )

    assert valid is True
    assert errors == []


def test_policy_scope_enum_fails_closed() -> None:
    registry = register_agent_schemas(SchemaRegistry())

    valid, errors = registry.validate(
        {
            "query": "年假",
            "answer": "回答",
            "knowledge_items_count": 1,
            "policy_scope": "internal-ish",
        },
        "policy.info@v1",
    )

    assert not valid
    assert "expected one of" in errors[0]


def test_policy_sources_and_not_found_metadata_are_consistent() -> None:
    registry = register_agent_schemas(SchemaRegistry())
    valid, errors = registry.validate(
        {
            "query": "报销",
            "answer": "请提交报销单",
            "knowledge_items_count": 1,
            "policy_scope": "company",
            "sources": [
                {
                    "id": "reimbursement_001",
                    "category": "公司制度-费用报销",
                    "source": "演示公司财务报销制度（模拟）",
                    "effective_date": "2026-01-01",
                    "policy_scope": "company",
                }
            ],
            "matched_items": ["reimbursement_001"],
            "not_found": False,
        },
        "policy.info@v1",
    )

    assert valid, errors


def test_policy_not_found_metadata_requires_empty_provenance() -> None:
    registry = register_agent_schemas(SchemaRegistry())
    valid, errors = registry.validate(
        {
            "query": "不存在的制度",
            "answer": "未找到相关制度",
            "knowledge_items_count": 0,
            "policy_scope": "unknown",
            "sources": [],
            "matched_items": [],
            "not_found": True,
        },
        "policy.info@v1",
    )

    assert valid, errors


@pytest.mark.parametrize(
    "partial_metadata",
    [
        {"sources": []},
        {"matched_items": []},
        {"not_found": True},
    ],
)
def test_policy_provenance_fields_are_an_atomic_optional_group(
    partial_metadata: dict[str, object],
) -> None:
    registry = register_agent_schemas(SchemaRegistry())
    payload = {
        "query": "报销",
        "answer": "请提交报销单",
        "knowledge_items_count": 1,
        "policy_scope": "company",
    }
    payload.update(partial_metadata)

    valid, errors = registry.validate(payload, "policy.info@v1")

    assert not valid
    assert any("provenance field" in error for error in errors)


@pytest.mark.parametrize(
    "source_scopes, expected_scope",
    [
        (["company"], "company"),
        (["statutory"], "statutory"),
        (["mixed"], "mixed"),
        (["unknown"], "unknown"),
        (["company", "statutory"], "mixed"),
        (["company", "unknown"], "mixed"),
    ],
)
def test_policy_scope_is_derived_from_source_scopes(
    source_scopes: list[str],
    expected_scope: str,
) -> None:
    registry = register_agent_schemas(SchemaRegistry())
    source_ids = [f"source_{index}" for index in range(len(source_scopes))]
    valid, errors = registry.validate(
        {
            "query": "制度查询",
            "answer": "查询结果",
            "knowledge_items_count": len(source_scopes),
            "policy_scope": expected_scope,
            "sources": [
                {
                    "id": source_id,
                    "category": "制度",
                    "source": "知识库",
                    "policy_scope": scope,
                }
                for source_id, scope in zip(source_ids, source_scopes)
            ],
            "matched_items": source_ids,
            "not_found": False,
        },
        "policy.info@v1",
    )

    assert valid, errors


def test_policy_scope_rejects_value_inconsistent_with_sources() -> None:
    registry = register_agent_schemas(SchemaRegistry())
    valid, errors = registry.validate(
        {
            "query": "报销",
            "answer": "公司报销流程",
            "knowledge_items_count": 1,
            "policy_scope": "statutory",
            "sources": [
                {
                    "id": "reimbursement_001",
                    "category": "公司制度-费用报销",
                    "source": "演示公司财务报销制度（模拟）",
                    "policy_scope": "company",
                }
            ],
            "matched_items": ["reimbursement_001"],
            "not_found": False,
        },
        "policy.info@v1",
    )

    assert not valid
    assert any(
        "expected 'company' derived from sources" in error for error in errors
    )


def test_policy_scope_type_error_does_not_crash_semantic_validation() -> None:
    registry = register_agent_schemas(SchemaRegistry())
    valid, errors = registry.validate(
        {
            "query": "报销",
            "answer": "公司报销流程",
            "knowledge_items_count": 1,
            "policy_scope": "company",
            "sources": [
                {
                    "id": "reimbursement_001",
                    "category": "公司制度-费用报销",
                    "source": "演示公司财务报销制度（模拟）",
                    "policy_scope": ["company"],
                }
            ],
            "matched_items": ["reimbursement_001"],
            "not_found": False,
        },
        "policy.info@v1",
    )

    assert not valid
    assert any("expected string, got list" in error for error in errors)


@pytest.mark.parametrize(
    "source_id, matched_id, source_name, expected_error",
    [
        ("", "", "知识库", "source ids must be non-empty"),
        ("source_0", "", "知识库", "item ids must be non-empty"),
        ("source_0", "source_0", " ", "source names must be non-empty"),
    ],
)
def test_policy_provenance_requires_non_empty_traceability_fields(
    source_id: str,
    matched_id: str,
    source_name: str,
    expected_error: str,
) -> None:
    registry = register_agent_schemas(SchemaRegistry())
    valid, errors = registry.validate(
        {
            "query": "报销",
            "answer": "公司报销流程",
            "knowledge_items_count": 1,
            "policy_scope": "company",
            "sources": [
                {
                    "id": source_id,
                    "category": "公司制度-费用报销",
                    "source": source_name,
                    "policy_scope": "company",
                }
            ],
            "matched_items": [matched_id],
            "not_found": False,
        },
        "policy.info@v1",
    )

    assert not valid
    assert any(expected_error in error for error in errors)


def test_policy_not_found_rejects_non_unknown_scope() -> None:
    registry = register_agent_schemas(SchemaRegistry())
    valid, errors = registry.validate(
        {
            "query": "不存在的制度",
            "answer": "未找到相关制度",
            "knowledge_items_count": 0,
            "policy_scope": "statutory",
            "sources": [],
            "matched_items": [],
            "not_found": True,
        },
        "policy.info@v1",
    )

    assert not valid
    assert any("expected 'unknown' when not_found is true" in error for error in errors)


@pytest.mark.parametrize(
    "overrides, expected_error",
    [
        (
            {"sources": [], "matched_items": []},
            "expected 1 entries",
        ),
        (
            {
                "sources": [
                    {
                        "id": "reimbursement_001",
                        "category": "公司制度-费用报销",
                        "source": "演示公司财务报销制度（模拟）",
                        "policy_scope": "company",
                    }
                ],
                "matched_items": ["leave_001"],
            },
            "source ids must match matched_items exactly",
        ),
        (
            {"not_found": True},
            "expected 0 when not_found is true",
        ),
    ],
)
def test_policy_provenance_rejects_internally_inconsistent_results(
    overrides: dict[str, object],
    expected_error: str,
) -> None:
    registry = register_agent_schemas(SchemaRegistry())
    payload = {
        "query": "报销",
        "answer": "请提交报销单",
        "knowledge_items_count": 1,
        "policy_scope": "company",
        "sources": [
            {
                "id": "reimbursement_001",
                "category": "公司制度-费用报销",
                "source": "演示公司财务报销制度（模拟）",
                "policy_scope": "company",
            }
        ],
        "matched_items": ["reimbursement_001"],
        "not_found": False,
    }
    payload.update(overrides)

    valid, errors = registry.validate(payload, "policy.info@v1")

    assert not valid
    assert any(expected_error in error for error in errors)


def test_policy_source_snapshot_date_does_not_require_effective_date() -> None:
    registry = register_agent_schemas(SchemaRegistry())
    valid, errors = registry.validate(
        {
            "query": "养老金",
            "answer": "演示答案",
            "knowledge_items_count": 1,
            "policy_scope": "statutory",
            "sources": [
                {
                    "id": "pension_001",
                    "category": "社保-养老金",
                    "source": "公开办事说明（演示摘录）",
                    "source_updated_at": "2026-01-01",
                    "policy_scope": "statutory",
                    "is_demo": True,
                }
            ],
            "matched_items": ["pension_001"],
            "not_found": False,
        },
        "policy.info@v1",
    )

    assert valid, errors


def test_policy_v2_accepts_consistent_match_provenance() -> None:
    registry = register_agent_schemas(SchemaRegistry())

    valid, errors = registry.validate(_policy_v2_payload(), "policy.info@v2")

    assert valid, errors


def test_policy_v2_accepts_iso_timestamp_for_source_snapshot() -> None:
    registry = register_agent_schemas(SchemaRegistry())
    payload = _policy_v2_payload(
        sources=[
            {
                **_policy_v2_payload()["sources"][0],
                "effective_date": "2026-01-01T12:30:00Z",
            }
        ]
    )

    valid, errors = registry.validate(payload, "policy.info@v2")

    assert valid, errors


@pytest.mark.parametrize(
    ("date_overrides", "error_fragment"),
    [
        (
            {"effective_date": " "},
            "must be a non-empty ISO date or timestamp",
        ),
        (
            {"source_updated_at": "not-a-date"},
            "must be an ISO date or timestamp",
        ),
    ],
)
def test_policy_v2_rejects_blank_or_invalid_provenance_dates(
    date_overrides: dict[str, str],
    error_fragment: str,
) -> None:
    registry = register_agent_schemas(SchemaRegistry())
    source = {
        key: value
        for key, value in _policy_v2_payload()["sources"][0].items()
        if key not in {"effective_date", "source_updated_at"}
    }
    source.update(date_overrides)
    payload = _policy_v2_payload(sources=[source])

    valid, errors = registry.validate(payload, "policy.info@v2")

    assert not valid
    assert any(error_fragment in error for error in errors), errors


def test_policy_v2_allows_source_ids_in_a_different_display_order() -> None:
    registry = register_agent_schemas(SchemaRegistry())
    payload = _policy_v2_payload()
    second_source = {
        "id": "travel_001",
        "category": "公司制度-差旅",
        "source": "演示公司差旅制度（模拟）",
        "effective_date": "2026-01-01",
        "is_demo": True,
        "policy_scope": "company",
    }
    payload.update(
        knowledge_items_count=2,
        sources=[payload["sources"][0], second_source],
        matched_items=["travel_001", "reimbursement_001"],
    )

    valid, errors = registry.validate(payload, "policy.info@v2")

    assert valid, errors


def test_policy_v2_accepts_consistent_not_found_result() -> None:
    registry = register_agent_schemas(SchemaRegistry())
    payload = _policy_v2_payload(
        answer="知识库暂未收录相关内容",
        knowledge_items_count=0,
        policy_scope="unknown",
        sources=[],
        matched_items=[],
        not_found=True,
    )

    valid, errors = registry.validate(payload, "policy.info@v2")

    assert valid, errors


@pytest.mark.parametrize(
    ("payload", "error_fragment"),
    [
        (
            _policy_v2_payload(sources=[], matched_items=[]),
            "must be non-empty when not_found is false",
        ),
        (
            _policy_v2_payload(not_found=True),
            "must be 0 when not_found is true",
        ),
        (
            _policy_v2_payload(knowledge_items_count=2),
            "must equal the number of sources",
        ),
        (
            _policy_v2_payload(matched_items=["different_001"]),
            "must match source ids",
        ),
        (
            _policy_v2_payload(policy_scope="statutory"),
            "must summarize the source policy scopes",
        ),
        (
            _policy_v2_payload(
                not_found=True,
                knowledge_items_count=0,
                policy_scope="company",
                sources=[],
                matched_items=[],
            ),
            "must summarize the source policy scopes",
        ),
        (
            _policy_v2_payload(
                sources=[
                    {
                        **_policy_v2_payload()["sources"][0],
                        "category": "",
                    }
                ]
            ),
            "sources[0].category: must be non-empty",
        ),
        (
            _policy_v2_payload(
                sources=[
                    {
                        key: value
                        for key, value in _policy_v2_payload()["sources"][0].items()
                        if key not in {"effective_date", "source_updated_at"}
                    }
                ]
            ),
            "requires effective_date or source_updated_at",
        ),
        (
            _policy_v2_payload(
                sources=[
                    {
                        key: value
                        for key, value in _policy_v2_payload()["sources"][0].items()
                        if key != "is_demo"
                    }
                ]
            ),
            "missing required field: 'is_demo'",
        ),
        (
            _policy_v2_payload(
                knowledge_items_count=2,
                sources=[
                    _policy_v2_payload()["sources"][0],
                    _policy_v2_payload()["sources"][0],
                ],
                matched_items=["reimbursement_001", "reimbursement_001"],
            ),
            "ids must be unique",
        ),
    ],
)
def test_policy_v2_rejects_inconsistent_provenance(payload, error_fragment) -> None:
    registry = register_agent_schemas(SchemaRegistry())

    valid, errors = registry.validate(payload, "policy.info@v2")

    assert not valid
    assert any(error_fragment in error for error in errors), errors


def test_policy_v2_requires_provenance_fields_without_tightening_v1() -> None:
    registry = register_agent_schemas(SchemaRegistry())
    legacy_payload = {
        "query": "年假",
        "answer": "依据国家法规执行",
        "knowledge_items_count": 1,
        "policy_scope": "statutory",
    }

    v1_valid, v1_errors = registry.validate(legacy_payload, "policy.info@v1")
    v2_valid, v2_errors = registry.validate(legacy_payload, "policy.info@v2")

    assert v1_valid, v1_errors
    assert not v2_valid
    assert any("sources" in error for error in v2_errors)
    assert any("matched_items" in error for error in v2_errors)
    assert any("not_found" in error for error in v2_errors)


def test_report_source_items_require_logical_name_schema_and_payload() -> None:
    registry = register_agent_schemas(SchemaRegistry())

    valid, errors = registry.validate(
        {
            "sources": [{"logical_name": "employee.info"}],
            "instruction": "汇总",
            "title": "报告",
        },
        "report.sources@v1",
    )

    assert not valid
    assert any("schema_ref" in error for error in errors)
    assert any("payload" in error for error in errors)


def test_report_sources_accept_schema_valid_markdown_payload() -> None:
    registry = register_agent_schemas(SchemaRegistry())

    valid, errors = registry.validate(
        {
            "sources": [
                {
                    "logical_name": "research.markdown",
                    "schema_ref": "markdown_text_result@v1",
                    "payload": "# 李娜公开信息\n\n已核验来源摘要。",
                }
            ],
            "instruction": "整理为简短报告",
            "title": "李娜公开信息报告",
        },
        "report.sources@v1",
    )

    assert valid, errors
