"""Generic, deterministic MCP tool discovery and selection control plane.

The primary ``select_tools`` API accepts candidates from ``ToolRegistry``.
``select_office_mcp_tool`` remains as an isolated compatibility wrapper for the
existing Office evaluation.  No function in this module executes a tool.
"""

from __future__ import annotations

import logging
import os
import re
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Mapping, Optional

from src.manager.registry.tool_semantics import (
    load_mcp_tool_semantics,
)


logger = logging.getLogger(__name__)
SELECTION_MODES = frozenset({"off", "audit", "enforce"})

SCENARIO_MARKERS = {
    "personnel": ("员工", "人员", "部门", "领导", "档案", "employee", "manager", "org"),
    "calendar": ("日程", "日历", "空档", "calendar", "schedule"),
    "course": ("课程", "培训", "学习", "course", "training"),
    "travel": ("差旅", "出差", "行程", "travel", "trip"),
    "meeting": ("会议", "参会", "meeting"),
    "messaging": ("邮件", "消息", "收件人", "联系人", "模板", "投递", "email", "message", "contact"),
    "spreadsheet": ("excel", "工作簿", "工作表", "单元格", "表格", "公式", "图表", "透视表"),
}

OPERATION_MARKERS = {
    "unmerge": ("取消合并", "拆分合并", "unmerge"),
    "merge": ("合并单元格", "merge cells"),
    "cancel": ("取消", "撤销", "cancel"),
    "delete": ("删除", "移除", "清除", "delete"),
    "update": ("更新", "修改", "调整", "重命名", "rename", "update"),
    "enroll": ("报名", "参加培训", "enroll"),
    "send": ("发送", "发邮件", "send"),
    "copy": ("复制", "拷贝", "copy"),
    "validate": ("校验", "验证", "检查", "validate"),
    "format": ("格式化", "设置格式", "format"),
    "apply": ("应用", "写入公式", "apply"),
    "write": ("写入数据", "填充数据", "write data"),
    "create": ("创建", "新增", "添加", "安排", "预定", "提交", "生成", "新建", "create"),
    "query": (
        "查询", "搜索", "查找", "查看", "获取", "读取", "列出", "详情", "状态",
        "记录", "政策", "估算", "解析", "空闲", "纪要", "看看", "哪些", "只查",
        "search", "find", "get", "read", "list", "resolve", "estimate", "show",
    ),
}

NEGATION_MARKERS = (
    "不要",
    "不是",
    "无需",
    "不必",
    "别",
    "勿",
    "禁止",
    "do not",
    "don't",
    "not",
    "without",
)


def selection_mode() -> str:
    mode = str(
        os.getenv("MCP_TOOL_SELECTION_MODE")
        or os.getenv("OFFICE_MCP_TOOL_SELECTION_MODE")
        or "off"
    ).strip().lower()
    if mode not in SELECTION_MODES:
        raise ValueError("MCP_TOOL_SELECTION_MODE must be one of: off, audit, enforce")
    return mode


def _present(value: Any) -> bool:
    return value not in (None, "", [], {})


def _schema_properties(schema: Mapping[str, Any]) -> Dict[str, Any]:
    properties = schema.get("properties")
    if isinstance(properties, dict):
        return properties
    return {
        name: {"type": expected}
        for name, expected in schema.items()
        if isinstance(expected, str)
    }


def _type_matches(value: Any, expected: str) -> bool:
    checks = {
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "array": lambda item: isinstance(item, list),
        "object": lambda item: isinstance(item, dict),
        "boolean": lambda item: isinstance(item, bool),
    }
    return checks.get(expected, lambda _item: True)(value)


def validate_known_inputs(
    tool: Mapping[str, Any], known_inputs: Optional[Mapping[str, Any]]
) -> Dict[str, Any]:
    inputs = dict(known_inputs or {})
    schema = tool.get("input_schema") or {}
    required = list(tool.get("required_inputs") or schema.get("required") or [])
    properties = _schema_properties(schema)
    missing = [name for name in required if not _present(inputs.get(name))]
    invalid_types = []
    for name, definition in properties.items():
        if name not in inputs or not _present(inputs[name]):
            continue
        expected = definition.get("type") if isinstance(definition, dict) else None
        if expected and not _type_matches(inputs[name], str(expected)):
            invalid_types.append(
                {
                    "input": name,
                    "expected": expected,
                    "actual": type(inputs[name]).__name__,
                }
            )
    return {
        "valid": not missing and not invalid_types,
        "missing_required_inputs": missing,
        "invalid_input_types": invalid_types,
    }


def _normalize(value: Any) -> str:
    text = str(value or "").casefold().replace("_", " ")
    return " ".join(text.split())


def _tokens(value: Any) -> set[str]:
    normalized = _normalize(value)
    tokens = {
        token
        for token in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]{2,}", normalized)
        if token
    }
    for span in re.findall(r"[\u4e00-\u9fff]{2,}", normalized):
        tokens.update(span[index : index + 2] for index in range(len(span) - 1))
        tokens.update(span[index : index + 3] for index in range(len(span) - 2))
    return tokens


def _marker_is_negated(query: str, marker: str) -> bool:
    start = 0
    found = False
    while True:
        index = query.find(marker, start)
        if index < 0:
            return found
        found = True
        prefix = query[max(0, index - 12) : index].rstrip()
        if not any(prefix.endswith(negation) for negation in NEGATION_MARKERS):
            return False
        start = index + max(1, len(marker))


def _infer_label(
    query: str,
    markers: Mapping[str, Iterable[str]],
    *,
    ignore_negated: bool = False,
) -> Dict[str, Any]:
    normalized_query = _normalize(query)
    matches = []
    for label, values in markers.items():
        hits = []
        for value in values:
            normalized_value = _normalize(value)
            if not normalized_value or normalized_value not in normalized_query:
                continue
            if ignore_negated and _marker_is_negated(
                normalized_query, normalized_value
            ):
                continue
            hits.append(value)
        if hits:
            matches.append((label, sum(len(_normalize(hit)) for hit in hits), hits))
    matches.sort(key=lambda item: (-item[1], item[0]))
    ambiguous = len(matches) > 1 and matches[0][1] == matches[1][1]
    return {
        "value": matches[0][0] if matches and not ambiguous else None,
        "evidence": matches[0][2] if matches else [],
        "ambiguous": ambiguous,
        "candidates": [
            {"value": label, "score": score, "evidence": evidence}
            for label, score, evidence in matches
        ],
    }


def infer_scenario(query: str) -> Dict[str, Any]:
    return _infer_label(query, SCENARIO_MARKERS)


def infer_operation(query: str) -> Dict[str, Any]:
    return _infer_label(query, OPERATION_MARKERS, ignore_negated=True)


def _score_tool(
    tool: Mapping[str, Any],
    query: str,
    known_inputs: Mapping[str, Any],
    inferred_scenario: Optional[str],
    inferred_operation: Optional[str],
) -> Dict[str, Any]:
    normalized_query = _normalize(query)
    query_tokens = _tokens(normalized_query)
    reasons: List[Dict[str, Any]] = []
    score = 0.0
    semantic_score = 0.0

    name = _normalize(tool.get("name"))
    if (
        name
        and (name in normalized_query or name.replace(" ", "") in normalized_query.replace(" ", ""))
        and not _marker_is_negated(normalized_query, name)
    ):
        score += 120.0
        semantic_score += 120.0
        reasons.append({"signal": "tool_name", "value": tool.get("name"), "points": 120.0})

    for kind, weight in (("aliases", 70.0), ("intents", 45.0)):
        best = None
        for value in tool.get(kind, []):
            normalized = _normalize(value)
            if (
                normalized
                and normalized in normalized_query
                and not _marker_is_negated(normalized_query, normalized)
            ):
                points = weight + min(len(normalized), 20) / 10
                if best is None or points > best[1]:
                    best = (value, points)
        if best is not None:
            score += best[1]
            semantic_score += best[1]
            reasons.append({"signal": kind[:-1], "value": best[0], "points": best[1]})

    description_overlap = sorted(query_tokens & _tokens(tool.get("description")))
    if description_overlap:
        points = float(min(len(description_overlap), 5) * 6)
        score += points
        semantic_score += points
        reasons.append({"signal": "description", "value": description_overlap, "points": points})

    metadata_tokens = _tokens(
        " ".join(
            [str(tool.get("name") or "")]
            + [str(item) for item in tool.get("intents", [])]
            + [str(item) for item in tool.get("aliases", [])]
        )
    )
    overlap = sorted(query_tokens & metadata_tokens)
    if overlap:
        points = float(min(len(overlap), 5) * 5)
        score += points
        semantic_score += points
        reasons.append({"signal": "token_overlap", "value": overlap, "points": points})

    if inferred_scenario and _normalize(tool.get("scenario")) == inferred_scenario:
        score += 12.0
        reasons.append({"signal": "scenario", "value": inferred_scenario, "points": 12.0})

    if inferred_operation and tool.get("operation_mode") == inferred_operation:
        score += 15.0
        reasons.append({"signal": "operation_mode", "value": inferred_operation, "points": 15.0})

    schema_keys = set(_schema_properties(tool.get("input_schema") or {}))
    matching_inputs = sorted(key for key in known_inputs if key in schema_keys)
    if matching_inputs:
        points = float(min(len(matching_inputs), 6) * 2)
        score += points
        reasons.append({"signal": "known_inputs", "value": matching_inputs, "points": points})

    return {
        "score": round(score, 3),
        "semantic_score": round(semantic_score, 3),
        "score_reasons": reasons,
    }


def select_tools(
    *,
    task_text: str,
    target_agent: Optional[str],
    known_inputs: Optional[Mapping[str, Any]],
    candidates: Iterable[Mapping[str, Any]],
    top_k: int = 3,
    server_name: Optional[str] = None,
    scenario: Optional[str] = None,
    operation_mode: Optional[str] = None,
    mode: str = "audit",
) -> Dict[str, Any]:
    """Select only from a supplied ToolRegistry candidate snapshot."""

    if top_k <= 0:
        raise ValueError("top_k must be greater than zero")
    if mode not in SELECTION_MODES:
        raise ValueError("mode must be one of: off, audit, enforce")
    candidate_list = [dict(item) for item in candidates]
    inputs = dict(known_inputs or {})
    scenario_inference = infer_scenario(task_text)
    operation_inference = infer_operation(task_text)
    explicit_scenario = _normalize(scenario)
    explicit_operation = _normalize(operation_mode)
    inferred_scenario = _normalize(scenario_inference["value"])
    inferred_operation = _normalize(operation_inference["value"])
    expected_scenario = explicit_scenario or inferred_scenario
    expected_operation = explicit_operation or inferred_operation
    expected_agent = str(target_agent or "").strip()

    matching_servers = sorted(
        {
            str(item.get("server_name") or "")
            for item in candidate_list
            if expected_scenario and _normalize(item.get("scenario")) == expected_scenario
        }
        - {""}
    )
    inferred_server = matching_servers[0] if len(matching_servers) == 1 else None
    expected_server = str(server_name or inferred_server or "").strip()
    ranked_candidates = []
    excluded = []

    for tool in candidate_list:
        name = str(tool.get("name") or "")
        tool_server = str(tool.get("server_name") or "")
        reasons = []
        scored = _score_tool(
            tool,
            task_text,
            inputs,
            explicit_scenario or inferred_scenario or None,
            explicit_operation or inferred_operation or None,
        )
        if expected_server and tool_server != expected_server:
            reasons.append({"stage": "server_domain_filter", "reason": "server_mismatch", "expected": expected_server, "actual": tool_server})
        if explicit_scenario and _normalize(tool.get("scenario")) != explicit_scenario:
            reasons.append({"stage": "scenario_operation_filter", "reason": "scenario_mismatch", "expected": expected_scenario, "actual": tool.get("scenario")})
        if explicit_operation and _normalize(tool.get("operation_mode")) != explicit_operation:
            reasons.append({"stage": "scenario_operation_filter", "reason": "operation_mismatch", "expected": expected_operation, "actual": tool.get("operation_mode")})
        owners = list(tool.get("owner_agents") or [])
        if expected_agent and owners and expected_agent not in owners:
            reasons.append({"stage": "agent_filter", "reason": "agent_mismatch", "expected": expected_agent, "actual": owners})

        validation = validate_known_inputs(tool, inputs)
        if validation["missing_required_inputs"]:
            reasons.append({"stage": "input_schema_filter", "reason": "missing_required_inputs", "inputs": validation["missing_required_inputs"]})
        if validation["invalid_input_types"]:
            reasons.append({"stage": "input_schema_filter", "reason": "invalid_input_types", "inputs": validation["invalid_input_types"]})
        if reasons:
            excluded.append(
                {
                    "server_name": tool_server,
                    "name": name,
                    "score": scored["score"],
                    "semantic_score": scored["semantic_score"],
                    "reasons": reasons,
                }
            )
            continue

        ranked_candidates.append(
            {
                "server_name": tool_server,
                "name": name,
                "tool_key": f"{tool_server}:{name}",
                "score": scored["score"],
                "semantic_score": scored["semantic_score"],
                "score_reasons": scored["score_reasons"],
                "scenario": tool.get("scenario"),
                "operation_mode": tool.get("operation_mode"),
                "required_inputs": list(tool.get("required_inputs") or []),
                "legacy_tool_name": tool.get("legacy_tool_name"),
                "schema_valid": True,
            }
        )

    ranked_candidates.sort(
        key=lambda item: (-item["score"], item["server_name"], item["name"])
    )
    ranked = ranked_candidates[:top_k]
    schema_blocked_scores = [
        item["score"]
        for item in excluded
        if item["score"] > 0
        and item["reasons"]
        and all(reason["stage"] == "input_schema_filter" for reason in item["reasons"])
    ]
    best_blocked_score = max(schema_blocked_scores, default=0.0)
    selected = (
        ranked[0]
        if ranked
        and ranked[0]["semantic_score"] > 0
        and ranked[0]["score"] > best_blocked_score
        else None
    )
    abstention_reason = None
    if selected is None:
        abstention_reason = (
            "best_semantic_candidate_failed_schema"
            if best_blocked_score > 0
            else "no_semantic_match_or_schema_valid_candidate"
        )
    return {
        "selected_tool": selected["name"] if selected else None,
        "selected_tool_key": selected["tool_key"] if selected else None,
        "server_name": selected["server_name"] if selected else expected_server or None,
        "candidates": ranked,
        "excluded": sorted(excluded, key=lambda item: (item["server_name"], item["name"])),
        "candidate_count_before_filter": len(candidate_list),
        "candidate_count_after_filter": len(ranked_candidates),
        "candidate_count": len(ranked_candidates),
        "mode": mode,
        "abstention_reason": abstention_reason,
        "inference": {
            "server_name": expected_server or None,
            "scenario": scenario or scenario_inference["value"],
            "scenario_evidence": [] if scenario else scenario_inference["evidence"],
            "scenario_ambiguous": False if scenario else scenario_inference["ambiguous"],
            "scenario_candidates": [] if scenario else scenario_inference["candidates"],
            "operation_mode": operation_mode or operation_inference["value"],
            "operation_evidence": [] if operation_mode else operation_inference["evidence"],
            "operation_ambiguous": False if operation_mode else operation_inference["ambiguous"],
            "operation_candidates": [] if operation_mode else operation_inference["candidates"],
        },
        "filters": {
            "target_agent": target_agent,
            "known_inputs": inputs,
            "authorization_enforced": False,
        },
    }


@lru_cache(maxsize=1)
def load_tool_catalog() -> tuple[Dict[str, Any], ...]:
    """Compatibility view of Office tools, dynamically enriched from FastMCP."""

    from src.tools.office_mcp.server import mcp

    semantics = load_mcp_tool_semantics()
    catalog = []
    for tool in mcp._tool_manager.list_tools():
        semantic = semantics[("office-mcp", tool.name)]
        catalog.append(
            {
                "server_name": "office-mcp",
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": dict(tool.parameters or {}),
                **semantic,
            }
        )
    return tuple(catalog)


def select_office_mcp_tool(
    *,
    query: str,
    known_inputs: Optional[Mapping[str, Any]] = None,
    arguments: Optional[Mapping[str, Any]] = None,
    scenario: Optional[str] = None,
    operation_mode: Optional[str] = None,
    agent_name: Optional[str] = None,
    top_k: int = 3,
    catalog: Optional[Iterable[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    return select_tools(
        task_text=query,
        target_agent=agent_name,
        known_inputs=known_inputs if known_inputs is not None else arguments,
        candidates=catalog or load_tool_catalog(),
        top_k=top_k,
        server_name="office-mcp",
        scenario=scenario,
        operation_mode=operation_mode,
        mode=selection_mode(),
    )


def _tool_definition(tool: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "name": tool["name"],
        "description": tool["description"],
        "parameters": dict(tool.get("input_schema") or {}),
    }


async def finalize_top_k_with_agent(
    *,
    decision: Mapping[str, Any],
    query: str,
    agent_name: str,
    agent_prompt: str,
    parameter_extractor: Any,
) -> Dict[str, Any]:
    names = [str(item["name"]) for item in decision.get("candidates", [])]
    if not names:
        raise ValueError("Selector returned no Top-K candidates")
    by_name = {item["name"]: item for item in load_tool_catalog()}
    tools = [_tool_definition(by_name[name]) for name in names]
    selected, arguments = await parameter_extractor.select_tool_and_extract(
        agent_name=agent_name,
        agent_prompt=agent_prompt,
        tools=tools,
        messages=[{"role": "user", "content": query}],
    )
    selected_name = str((selected or {}).get("name") or "")
    if selected_name not in names:
        raise ValueError("Agent finalizer selected a tool outside selector Top-K")
    return {
        "selector_top_k": names,
        "agent_selected_tool": selected_name,
        "arguments": dict(arguments or {}),
        "executed": False,
    }


def govern_preselected_tool(
    *,
    preferred_tool: str,
    query: str,
    known_inputs: Mapping[str, Any],
    scenario: Optional[str] = None,
    operation_mode: Optional[str] = None,
    agent_name: Optional[str] = None,
    top_k: int = 3,
) -> Dict[str, Any]:
    mode = selection_mode()
    if mode == "off":
        return {"mode": mode, "effective_tool": preferred_tool, "decision": None}
    decision = select_office_mcp_tool(
        query=query,
        known_inputs=known_inputs,
        scenario=scenario,
        operation_mode=operation_mode,
        agent_name=agent_name,
        top_k=top_k,
    )
    if mode == "audit":
        logger.info(
            "MCP selector audit preferred=%s selected=%s top_k=%s",
            preferred_tool,
            decision["selected_tool"],
            [item["name"] for item in decision["candidates"]],
        )
        effective = preferred_tool
    else:
        effective = decision["selected_tool"]
        if effective is None:
            raise ValueError("MCP selector did not select a schema-valid tool")
    return {"mode": mode, "effective_tool": effective, "decision": decision}
