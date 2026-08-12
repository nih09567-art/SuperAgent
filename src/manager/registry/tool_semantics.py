"""Semantic overlays for dynamically discovered MCP tools.

Descriptions and JSON Schema always come from MCP ``tools/list``.  The JSON
files loaded here contain only control-plane semantics and cannot create a
runtime tool by themselves.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Tuple

from src.utils import get_project_root


SemanticKey = Tuple[str, str]
SEMANTIC_FIELDS = (
    "scenario",
    "owner_agents",
    "intents",
    "aliases",
    "operation_mode",
    "required_inputs",
    "produces",
    "side_effect",
    "risk_level",
    "legacy_tool_name",
)


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


@lru_cache(maxsize=1)
def load_mcp_tool_semantics() -> Dict[SemanticKey, Dict[str, Any]]:
    root = get_project_root()
    sources = (
        ("office-mcp", root / "config" / "office_mcp_tools.json"),
        (None, root / "config" / "mcp_tool_semantics.json"),
    )
    semantics: Dict[SemanticKey, Dict[str, Any]] = {}
    for default_server, path in sources:
        payload = _read_json(path)
        tools = payload.get("tools")
        if not isinstance(tools, list):
            raise ValueError(f"Semantic catalog must contain a tools list: {path}")
        for raw in tools:
            if not isinstance(raw, dict):
                raise ValueError(f"Semantic catalog contains a non-object tool: {path}")
            server_name = str(raw.get("server_name") or default_server or "").strip()
            tool_name = str(raw.get("name") or "").strip()
            if not server_name or not tool_name:
                raise ValueError(f"Semantic catalog tool is missing server/name: {path}")
            key = (server_name, tool_name)
            if key in semantics:
                raise ValueError(f"Duplicate MCP tool semantics: {server_name}/{tool_name}")
            missing = [field for field in SEMANTIC_FIELDS if field not in raw]
            if missing:
                raise ValueError(
                    f"MCP tool semantics missing {missing}: {server_name}/{tool_name}"
                )
            if "description" in raw or "input_schema" in raw:
                raise ValueError(
                    f"Dynamic description/schema must not be duplicated: {server_name}/{tool_name}"
                )
            semantics[key] = {field: raw[field] for field in SEMANTIC_FIELDS}
    return semantics


def extract_tool_schema(tool: Any) -> Dict[str, Any]:
    schema = getattr(tool, "args_schema", None)
    if isinstance(schema, dict):
        return dict(schema)
    if schema is not None and hasattr(schema, "model_json_schema"):
        value = schema.model_json_schema()
        return dict(value) if isinstance(value, dict) else {}
    parameters = getattr(tool, "parameters", None)
    return dict(parameters) if isinstance(parameters, dict) else {}


def validate_semantics_against_discovery(
    discovered: Iterable[SemanticKey],
    semantics: Mapping[SemanticKey, Mapping[str, Any]],
) -> Dict[str, list[SemanticKey]]:
    discovered_keys = set(discovered)
    semantic_keys = set(semantics)
    return {
        "missing_semantics": sorted(discovered_keys - semantic_keys),
        "orphan_semantics": sorted(semantic_keys - discovered_keys),
    }


def selection_candidate(metadata: Any) -> Dict[str, Any]:
    return {
        "server_name": metadata.server_name or metadata.identifier.server,
        "name": metadata.runtime_tool_name or metadata.identifier.name,
        "description": metadata.description,
        "input_schema": dict(metadata.input_schema),
        "scenario": metadata.scenario,
        "owner_agents": list(metadata.owner_agents),
        "intents": list(metadata.intents),
        "aliases": list(metadata.aliases),
        "operation_mode": metadata.operation_mode,
        "required_inputs": list(metadata.required_inputs),
        "produces": list(metadata.produces),
        "side_effect": metadata.side_effect,
        "risk_level": metadata.risk_level,
        "legacy_tool_name": metadata.legacy_tool_name,
    }
