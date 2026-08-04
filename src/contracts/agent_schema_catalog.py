from __future__ import annotations

from datetime import date, datetime
from typing import Any

from src.orchestration.schema_registry import (
    SchemaRegistry,
    get_schema_registry,
    register_default_semantic_validators,
)

_POLICY_SCOPES = {"company", "statutory", "mixed", "unknown"}


def _is_iso_date_or_timestamp(value: str) -> bool:
    """Return whether a provenance date is an ISO date or timestamp."""

    normalized = value.strip()
    if not normalized:
        return False
    try:
        date.fromisoformat(normalized)
        return True
    except ValueError:
        pass
    try:
        datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _policy_scope_from_sources(sources: list[dict[str, Any]]) -> str | None:
    source_scopes = [source.get("policy_scope") for source in sources]
    if not all(
        isinstance(scope, str) and scope in _POLICY_SCOPES
        for scope in source_scopes
    ):
        return None
    scopes = set(source_scopes)
    if not scopes:
        return "unknown"
    if len(scopes) == 1:
        return next(iter(scopes))
    return "mixed"


def _validate_policy_info_provenance(value: Any, path: str) -> list[str]:
    if not isinstance(value, dict):
        return []

    provenance_fields = ("sources", "matched_items", "not_found")
    present_fields = [field for field in provenance_fields if field in value]
    if not present_fields:
        return []

    errors = [
        (
            f"{path}: provenance field {field!r} is required "
            "when provenance metadata is present"
        )
        for field in provenance_fields
        if field not in value
    ]
    if errors:
        return errors

    count = value.get("knowledge_items_count")
    sources = value.get("sources")
    matched_items = value.get("matched_items")
    not_found = value.get("not_found")
    if (
        type(count) is not int
        or not isinstance(sources, list)
        or not isinstance(matched_items, list)
        or not isinstance(not_found, bool)
    ):
        return []

    if not_found:
        if count != 0:
            errors.append(
                f"{path}.knowledge_items_count: expected 0 when "
                f"not_found is true, got {count}"
            )
        if sources:
            errors.append(
                f"{path}.sources: expected an empty array when not_found is true"
            )
        if matched_items:
            errors.append(
                f"{path}.matched_items: expected an empty array when not_found is true"
            )
        if value.get("policy_scope") != "unknown":
            errors.append(
                f"{path}.policy_scope: expected 'unknown' when not_found is true, "
                f"got {value.get('policy_scope')!r}"
            )
        return errors

    if count <= 0:
        errors.append(
            f"{path}.knowledge_items_count: expected a positive value when "
            f"not_found is false, got {count}"
        )
    if len(sources) != count:
        errors.append(
            f"{path}.sources: expected {count} entries, got {len(sources)}"
        )
    if len(matched_items) != count:
        errors.append(
            f"{path}.matched_items: expected {count} entries, got {len(matched_items)}"
        )

    if all(isinstance(source, dict) for source in sources) and all(
        isinstance(item, str) for item in matched_items
    ):
        source_ids = [source.get("id") for source in sources]
        if all(isinstance(source_id, str) for source_id in source_ids):
            if any(not source_id.strip() for source_id in source_ids):
                errors.append(f"{path}.sources: source ids must be non-empty")
            if any(not item.strip() for item in matched_items):
                errors.append(f"{path}.matched_items: item ids must be non-empty")
            if len(set(source_ids)) != len(source_ids):
                errors.append(f"{path}.sources: source ids must be unique")
            if len(set(matched_items)) != len(matched_items):
                errors.append(f"{path}.matched_items: item ids must be unique")
            if set(source_ids) != set(matched_items):
                errors.append(
                    f"{path}: source ids must match matched_items exactly"
                )

        source_names = [source.get("source") for source in sources]
        if all(isinstance(source_name, str) for source_name in source_names) and any(
            not source_name.strip() for source_name in source_names
        ):
            errors.append(f"{path}.sources: source names must be non-empty")

        expected_scope = _policy_scope_from_sources(sources)
        actual_scope = value.get("policy_scope")
        if expected_scope is not None and actual_scope != expected_scope:
            errors.append(
                f"{path}.policy_scope: expected {expected_scope!r} derived from "
                f"sources, got {actual_scope!r}"
            )
    return errors


AGENT_SCHEMA_CATALOG: dict[str, dict[str, Any]] = {
    "employee.info@v1": {
        "required": ["records"],
        "properties": {
            "records": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "employee_id": {"type": "string"},
                        "name": {"type": "string"},
                        "department": {"type": "string"},
                        "position": {"type": "string"},
                    },
                },
            },
            "query": {"type": "string"},
            "matched_count": {"type": "integer"},
        },
    },
    "employee.salary@v1": {
        "required": ["records"],
        "properties": {
            "records": {"type": "array"},
            "matched_count": {"type": "integer"},
        },
    },
    "policy.info@v1": {
        "required": ["query", "answer", "knowledge_items_count", "policy_scope"],
        "semantic_validator": _validate_policy_info_provenance,
        "properties": {
            "query": {"type": "string"},
            "answer": {"type": "string"},
            "knowledge_items_count": {"type": "integer"},
            "policy_scope": {
                "type": "string",
                "enum": ["company", "statutory", "mixed", "unknown"],
            },
            "sources": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": [
                        "id",
                        "category",
                        "source",
                        "policy_scope",
                    ],
                    "properties": {
                        "id": {"type": "string"},
                        "category": {"type": "string"},
                        "source": {"type": "string"},
                        "effective_date": {"type": "string"},
                        "source_updated_at": {"type": "string"},
                        "is_demo": {"type": "boolean"},
                        "policy_scope": {
                            "type": "string",
                            "enum": ["company", "statutory", "mixed", "unknown"],
                        },
                    },
                },
            },
            "matched_items": {
                "type": "array",
                "items": {"type": "string"},
            },
            "not_found": {"type": "boolean"},
        },
    },
    "policy.info@v2": {
        "required": [
            "query",
            "answer",
            "knowledge_items_count",
            "policy_scope",
            "sources",
            "matched_items",
            "not_found",
        ],
        "properties": {
            "query": {"type": "string"},
            "answer": {"type": "string"},
            "knowledge_items_count": {"type": "integer"},
            "policy_scope": {
                "type": "string",
                "enum": ["company", "statutory", "mixed", "unknown"],
            },
            "sources": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": [
                        "id",
                        "category",
                        "source",
                        "is_demo",
                        "policy_scope",
                    ],
                    "properties": {
                        "id": {"type": "string"},
                        "category": {"type": "string"},
                        "source": {"type": "string"},
                        "effective_date": {"type": "string"},
                        "source_updated_at": {"type": "string"},
                        "is_demo": {"type": "boolean"},
                        "policy_scope": {
                            "type": "string",
                            "enum": ["company", "statutory", "mixed", "unknown"],
                        },
                    },
                },
            },
            "matched_items": {
                "type": "array",
                "items": {"type": "string"},
            },
            "not_found": {"type": "boolean"},
        },
    },
    "report.sources@v1": {
        "required": ["sources", "instruction", "title"],
        "properties": {
            "sources": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["logical_name", "schema_ref", "payload"],
                    "properties": {
                        "logical_name": {"type": "string"},
                        "schema_ref": {"type": "string"},
                        "payload": {"type": "object"},
                    },
                },
            },
            "instruction": {"type": "string"},
            "title": {"type": "string"},
        },
    },
    "report.markdown@v1": {
        "required": ["title", "markdown", "source_count"],
        "properties": {
            "title": {"type": "string"},
            "markdown": {"type": "string"},
            "source_count": {"type": "integer"},
        },
    },
}


def _validate_policy_info_v2(payload: dict[str, Any]) -> list[str]:
    """Enforce the provenance invariants promised by ``policy.info@v2``."""

    errors: list[str] = []
    for field in (
        "knowledge_items_count",
        "sources",
        "matched_items",
        "not_found",
        "policy_scope",
    ):
        if field not in payload:
            errors.append(f"payload: missing required field: {field!r}")
    if errors:
        return errors

    count = payload["knowledge_items_count"]
    sources = payload["sources"]
    matched_items = payload["matched_items"]
    not_found = payload["not_found"]
    policy_scope = payload["policy_scope"]
    if type(count) is not int:
        errors.append(
            "payload.knowledge_items_count: expected integer, "
            f"got {type(count).__name__}"
        )
    if not isinstance(sources, list):
        errors.append(
            "payload.sources: expected array, "
            f"got {type(sources).__name__}"
        )
    if not isinstance(matched_items, list):
        errors.append(
            "payload.matched_items: expected array, "
            f"got {type(matched_items).__name__}"
        )
    if type(not_found) is not bool:
        errors.append(
            "payload.not_found: expected boolean, "
            f"got {type(not_found).__name__}"
        )
    if not isinstance(policy_scope, str):
        errors.append(
            "payload.policy_scope: expected string, "
            f"got {type(policy_scope).__name__}"
        )
    elif policy_scope not in _POLICY_SCOPES:
        errors.append(
            "payload.policy_scope: expected one of "
            f"{sorted(_POLICY_SCOPES)!r}, got {policy_scope!r}"
        )
    if errors:
        return errors

    for index, source in enumerate(sources):
        source_path = f"payload.sources[{index}]"
        if not isinstance(source, dict):
            errors.append(
                f"{source_path}: expected object, got {type(source).__name__}"
            )
            continue
        for field in ("id", "category", "source", "is_demo", "policy_scope"):
            if field not in source:
                errors.append(f"{source_path}: missing required field: {field!r}")
        for field in ("id", "category", "source"):
            if field not in source:
                continue
            value = source[field]
            if not isinstance(value, str):
                errors.append(
                    f"{source_path}.{field}: expected string, "
                    f"got {type(value).__name__}"
                )
            elif not value.strip():
                errors.append(f"{source_path}.{field}: must be non-empty")
        if "is_demo" in source and type(source["is_demo"]) is not bool:
            errors.append(
                f"{source_path}.is_demo: expected boolean, "
                f"got {type(source['is_demo']).__name__}"
            )
        if "policy_scope" in source:
            source_scope = source["policy_scope"]
            if not isinstance(source_scope, str):
                errors.append(
                    f"{source_path}.policy_scope: expected string, "
                    f"got {type(source_scope).__name__}"
                )
            elif source_scope not in _POLICY_SCOPES:
                errors.append(
                    f"{source_path}.policy_scope: expected one of "
                    f"{sorted(_POLICY_SCOPES)!r}, got {source_scope!r}"
                )
        valid_date_present = False
        date_fields = ("effective_date", "source_updated_at")
        for date_field in date_fields:
            if date_field not in source:
                continue
            value = source[date_field]
            if not isinstance(value, str):
                errors.append(
                    f"{source_path}.{date_field}: expected string, "
                    f"got {type(value).__name__}"
                )
            elif not value.strip():
                errors.append(
                    f"{source_path}.{date_field}: must be a non-empty "
                    "ISO date or timestamp"
                )
            elif not _is_iso_date_or_timestamp(value):
                errors.append(
                    f"{source_path}.{date_field}: must be an ISO date or timestamp"
                )
            else:
                valid_date_present = True
        if not valid_date_present and not any(
            date_field in source for date_field in date_fields
        ):
            errors.append(
                f"{source_path}: requires effective_date or source_updated_at"
            )

    for index, item_id in enumerate(matched_items):
        if not isinstance(item_id, str):
            errors.append(
                f"payload.matched_items[{index}]: expected string, "
                f"got {type(item_id).__name__}"
            )
        elif not item_id.strip():
            errors.append(f"payload.matched_items[{index}]: ids must be non-empty")
    if errors:
        return errors

    if not_found:
        if count != 0:
            errors.append(
                "payload.knowledge_items_count: must be 0 when not_found is true"
            )
        if sources:
            errors.append("payload.sources: must be empty when not_found is true")
        if matched_items:
            errors.append("payload.matched_items: must be empty when not_found is true")
    else:
        if count <= 0:
            errors.append(
                "payload.knowledge_items_count: must be greater than 0 "
                "when not_found is false"
            )
        if not sources:
            errors.append("payload.sources: must be non-empty when not_found is false")
        if not matched_items:
            errors.append(
                "payload.matched_items: must be non-empty when not_found is false"
            )

    source_scopes = {source["policy_scope"] for source in sources}
    if not_found:
        expected_scope = "unknown"
    elif source_scopes == {"company"}:
        expected_scope = "company"
    elif source_scopes == {"statutory"}:
        expected_scope = "statutory"
    elif source_scopes == {"unknown"}:
        expected_scope = "unknown"
    elif source_scopes:
        expected_scope = "mixed"
    else:
        expected_scope = None
    if expected_scope is not None and payload["policy_scope"] != expected_scope:
        errors.append(
            "payload.policy_scope: must summarize the source policy scopes "
            f"as {expected_scope!r}"
        )

    if count != len(sources):
        errors.append("payload.knowledge_items_count: must equal the number of sources")
    if count != len(matched_items):
        errors.append(
            "payload.knowledge_items_count: must equal the number of matched_items"
        )

    source_ids = [source["id"] for source in sources]
    for index, source in enumerate(sources):
        for field in ("id", "category", "source"):
            if not source[field].strip():
                errors.append(f"payload.sources[{index}].{field}: must be non-empty")
        if not (source.get("effective_date") or source.get("source_updated_at")):
            errors.append(
                f"payload.sources[{index}]: requires effective_date or "
                "source_updated_at"
            )
    if any(not source_id.strip() for source_id in source_ids):
        errors.append("payload.sources: source ids must be non-empty")
    if any(not item_id.strip() for item_id in matched_items):
        errors.append("payload.matched_items: ids must be non-empty")
    if len(set(source_ids)) != len(source_ids):
        errors.append("payload.sources: source ids must be unique")
    if len(set(matched_items)) != len(matched_items):
        errors.append("payload.matched_items: ids must be unique")
    if set(source_ids) != set(matched_items):
        errors.append(
            "payload.matched_items: ids must match source ids"
        )

    return errors


AGENT_SCHEMA_VALIDATORS = {
    "policy.info@v2": _validate_policy_info_v2,
}
register_default_semantic_validators(AGENT_SCHEMA_VALIDATORS)


def register_agent_schemas(
    registry: SchemaRegistry | None = None,
) -> SchemaRegistry:
    target = registry or get_schema_registry()
    for schema_ref, schema in AGENT_SCHEMA_CATALOG.items():
        target.register(
            schema_ref,
            schema,
            semantic_validator=AGENT_SCHEMA_VALIDATORS.get(schema_ref),
        )
    return target
