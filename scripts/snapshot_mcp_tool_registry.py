"""Capture live tools/list and the resulting unified ToolRegistry view."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from langchain_mcp_adapters.client import MultiServerMCPClient

from src.manager.mcp import mcp_client_config
from src.manager.registry.tool_loader import ToolLoader
from src.manager.registry.tool_registry import ToolRegistry
from src.manager.registry.tool_semantics import (
    load_mcp_tool_semantics,
    selection_candidate,
    validate_semantics_against_discovery,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_JSON = ROOT / "docs" / "mcp_tool_registry_baseline.json"
DEFAULT_MARKDOWN = ROOT / "docs" / "mcp_tool_registry_baseline.md"


def _agent_selected_tools() -> Dict[str, list[str]]:
    selected: Dict[str, list[str]] = {}
    registry_path = ROOT / "mock_remote_registry.json"
    payload = json.loads(registry_path.read_text(encoding="utf-8-sig"))
    for resource in payload.get("resources", []):
        if resource.get("type") != "agent":
            continue
        names = [
            str(item.get("name") or "")
            for item in (resource.get("metadata") or {}).get("selected_tools", [])
            if isinstance(item, dict) and str(item.get("name") or "")
        ]
        selected[str(resource.get("name") or "")] = names
    return selected


async def snapshot() -> Dict[str, Any]:
    config = mcp_client_config()
    live_tools = []
    for server_name in sorted(config):
        client = MultiServerMCPClient({server_name: config[server_name]})
        tools = await client.get_tools(server_name=server_name)
        for tool in tools:
            live_tools.append(
                {
                    "server_name": server_name,
                    "name": tool.name,
                    "description": tool.description or "",
                    "input_schema": dict(tool.args_schema or {}),
                    "registration_scope": "global",
                }
            )

    registry = ToolRegistry()
    loader = ToolLoader(registry=registry, load_timeout=20)
    loaded_count = await loader.load_mcp_tools()
    registry_metadata = list((await registry.mcp_metadata_view()).values())
    registry_candidates = [selection_candidate(item) for item in registry_metadata]
    registry_by_key = {
        (item["server_name"], item["name"]): item for item in registry_candidates
    }
    live_keys = {(item["server_name"], item["name"]) for item in live_tools}
    registry_keys = set(registry_by_key)
    semantics = load_mcp_tool_semantics()
    semantic_diff = validate_semantics_against_discovery(live_keys, semantics)
    agent_tools = _agent_selected_tools()
    selected_names = {
        name for names in agent_tools.values() for name in names
    }
    runtime_names = {name for _server, name in live_keys}
    legacy_names = {
        str(value.get("legacy_tool_name") or "")
        for value in semantics.values()
        if str(value.get("legacy_tool_name") or "")
    }

    rows = []
    for live in sorted(live_tools, key=lambda item: (item["server_name"], item["name"])):
        key = (live["server_name"], live["name"])
        candidate = registry_by_key.get(key, {})
        legacy = candidate.get("legacy_tool_name")
        referenced = sorted(
            agent
            for agent, names in agent_tools.items()
            if live["name"] in names or (legacy and legacy in names)
        )
        rows.append(
            {
                **live,
                **{k: v for k, v in candidate.items() if k not in {"description", "input_schema"}},
                "entered_registry": key in registry_keys,
                "referenced_by_agents": referenced,
            }
        )

    def labels(keys):
        return [f"{server}:{name}" for server, name in sorted(keys)]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "live MCP tools/list",
        "configured_servers": sorted(config),
        "counts": {
            "server_tools": len(live_keys),
            "registry_tools": len(registry_keys),
            "semantic_tools": len(semantics),
            "loader_reported": loaded_count,
            "by_server": {
                server: sum(item["server_name"] == server for item in live_tools)
                for server in sorted(config)
            },
        },
        "sets": {
            "server_tools": labels(live_keys),
            "registry_tools": labels(registry_keys),
            "agent_selected_tools": sorted(selected_names),
        },
        "diffs": {
            "server_minus_registry": labels(live_keys - registry_keys),
            "registry_minus_server": labels(registry_keys - live_keys),
            "server_minus_semantics": labels(set(semantic_diff["missing_semantics"])),
            "semantics_minus_server": labels(set(semantic_diff["orphan_semantics"])),
            "agent_selected_minus_registry_or_legacy": sorted(
                selected_names - runtime_names - legacy_names
            ),
        },
        "agent_selected_tools_by_agent": agent_tools,
        "tools": rows,
    }


def _markdown(report: Dict[str, Any]) -> str:
    counts = report["counts"]
    lines = [
        "# MCP ToolRegistry 基线快照",
        "",
        f"生成时间：`{report['generated_at']}`",
        "",
        "数据源：每个已配置 MCP Server 的真实 `tools/list`，随后由 ToolLoader 注册到独立 ToolRegistry。",
        "",
        "| Server | tools/list |",
        "| --- | ---: |",
    ]
    for server, count in counts["by_server"].items():
        lines.append(f"| {server} | {count} |")
    lines.extend(
        [
            f"| **合计** | **{counts['server_tools']}** |",
            "",
            "## 集合差异",
            "",
            *[
                f"- `{name}`：{value or '[]'}"
                for name, value in report["diffs"].items()
            ],
            "",
            "## 工具明细",
            "",
            "| Server | 工具 | 场景 | 操作 | Agent引用 |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for item in report["tools"]:
        lines.append(
            f"| {item['server_name']} | {item['name']} | {item['scenario']} | "
            f"{item['operation_mode']} | {', '.join(item['referenced_by_agents']) or '-'} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-output", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--markdown-output", type=Path, default=DEFAULT_MARKDOWN)
    args = parser.parse_args()
    report = asyncio.run(snapshot())
    args.json_output.resolve().write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.markdown_output.resolve().write_text(_markdown(report), encoding="utf-8")
    print(json.dumps(report["counts"], ensure_ascii=False, indent=2))
    print(json.dumps(report["diffs"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
