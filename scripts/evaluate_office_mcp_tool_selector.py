"""Evaluate the unified MCP selector against a live ToolRegistry snapshot."""

from __future__ import annotations

import argparse
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter_ns
from typing import Any, Dict

from src.tools.office_mcp.selector import select_tools, validate_known_inputs


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "experiments" / "office_mcp_tool_selection_cases.json"
DEFAULT_REGISTRY = ROOT / "docs" / "mcp_tool_registry_baseline.json"
DEFAULT_JSON = ROOT / "docs" / "office_mcp_tool_selection_evaluation.json"
DEFAULT_MARKDOWN = ROOT / "docs" / "office_mcp_tool_selection_evaluation.md"


def _percentage(numerator: int, denominator: int) -> float:
    return round(100.0 * numerator / denominator, 3) if denominator else 0.0


def evaluate(cases_path: Path, registry_path: Path) -> Dict[str, Any]:
    dataset = json.loads(cases_path.read_text(encoding="utf-8-sig"))
    baseline = json.loads(registry_path.read_text(encoding="utf-8-sig"))
    cases = dataset["cases"]
    candidates = baseline["tools"]
    by_key = {(item["server_name"], item["name"]): item for item in candidates}
    rows = []

    for case in cases:
        started = perf_counter_ns()
        decision = select_tools(
            task_text=case["query"],
            target_agent=case.get("agent_name"),
            known_inputs=case["known_inputs"],
            candidates=candidates,
            top_k=3,
            mode="audit",
        )
        elapsed_ms = (perf_counter_ns() - started) / 1_000_000
        expected_tool = case.get("expected_tool")
        expected_server = (
            case.get("expected_server")
            or ("office-mcp" if expected_tool else None)
        )
        expected_key = (
            (expected_server, expected_tool) if expected_server and expected_tool else None
        )
        selected_key = (
            (decision["server_name"], decision["selected_tool"])
            if decision["server_name"] and decision["selected_tool"]
            else None
        )
        top_k_keys = [
            (item["server_name"], item["name"]) for item in decision["candidates"]
        ]
        selected_meta = by_key.get(selected_key) if selected_key else None
        schema_valid = (
            validate_known_inputs(selected_meta, case["known_inputs"])["valid"]
            if selected_meta
            else None
        )
        actual_contract_tool = (
            by_key[expected_key].get("legacy_tool_name")
            or by_key[expected_key]["name"]
            if expected_key in by_key
            else None
        )
        recommended_contract_tool = (
            selected_meta.get("legacy_tool_name") or selected_meta["name"]
            if selected_meta
            else None
        )
        rows.append(
            {
                "id": case["id"],
                "kind": case["kind"],
                "query": case["query"],
                "expected_server": expected_server,
                "expected_tool": expected_tool,
                "selected_server": decision["server_name"],
                "selected_tool": decision["selected_tool"],
                "top_k": [f"{server}:{name}" for server, name in top_k_keys],
                "top_1_correct": bool(expected_key and selected_key == expected_key),
                "top_3_correct": bool(expected_key and expected_key in top_k_keys),
                "server_correct": bool(
                    expected_server and decision["server_name"] == expected_server
                ),
                "schema_valid": schema_valid,
                "candidate_count_before_filter": decision[
                    "candidate_count_before_filter"
                ],
                "candidate_count_after_filter": decision[
                    "candidate_count_after_filter"
                ],
                "elapsed_ms": round(elapsed_ms, 6),
                "actual_contract_tool": actual_contract_tool,
                "recommended_contract_tool": recommended_contract_tool,
                "audit_matches_actual": bool(
                    actual_contract_tool
                    and recommended_contract_tool == actual_contract_tool
                ),
                "inference": decision["inference"],
                "abstention_reason": decision["abstention_reason"],
            }
        )

    normal = [case for case in cases if case["kind"] == "normal"]
    scored = [row for row in rows if row["expected_tool"]]
    unknown = [row for row in rows if row["kind"] == "unknown"]
    selected_rows = [row for row in rows if row["selected_tool"]]
    covered = {
        (case.get("expected_server") or "office-mcp", case["expected_tool"])
        for case in normal
    }
    metrics = {
        "catalog_tool_count": len(candidates),
        "case_count": len(cases),
        "normal_case_count": len(normal),
        "total_tool_coverage_rate_pct": _percentage(len(covered), len(candidates)),
        "server_recognition_accuracy_pct": _percentage(
            sum(row["server_correct"] for row in scored), len(scored)
        ),
        "tool_top_1_pct": _percentage(
            sum(row["top_1_correct"] for row in scored), len(scored)
        ),
        "tool_top_3_pct": _percentage(
            sum(row["top_3_correct"] for row in scored), len(scored)
        ),
        "parameter_schema_valid_rate_pct": _percentage(
            sum(row["schema_valid"] is True for row in selected_rows),
            len(selected_rows),
        ),
        "nonexistent_tool_hallucination_rate_pct": _percentage(
            sum(row["selected_tool"] is not None for row in unknown), len(unknown)
        ),
        "average_candidate_count_before_filter": round(
            sum(row["candidate_count_before_filter"] for row in rows) / len(rows), 3
        ),
        "average_candidate_count_after_filter": round(
            sum(row["candidate_count_after_filter"] for row in rows) / len(rows), 3
        ),
        "average_selection_time_ms": round(
            sum(row["elapsed_ms"] for row in rows) / len(rows), 6
        ),
        "audit_recommendation_actual_match_pct": _percentage(
            sum(row["audit_matches_actual"] for row in scored), len(scored)
        ),
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset_version": dataset["dataset_version"],
        "registry_snapshot_generated_at": baseline["generated_at"],
        "algorithm": "generic deterministic layered MCP selector v2",
        "environment": {"python": sys.version.split()[0], "platform": platform.platform()},
        "metric_definitions": {
            "total_tool_coverage_rate_pct": "unique expected (server, tool) pairs in normal cases / discovered registry tools",
            "server_recognition_accuracy_pct": "labeled cases selecting the expected MCP Server / labeled cases",
            "tool_top_1_pct": "labeled cases selecting the expected (server, tool) at rank 1 / labeled cases",
            "tool_top_3_pct": "labeled cases containing the expected (server, tool) in Top-3 / labeled cases",
            "parameter_schema_valid_rate_pct": "selected outcomes satisfying live tools/list Schema / selected outcomes",
            "nonexistent_tool_hallucination_rate_pct": "unknown cases returning a selected tool / unknown cases",
            "average_candidate_count_before_filter": "mean ToolRegistry candidates before layered filters",
            "average_candidate_count_after_filter": "mean Schema-valid candidates after layered filters",
            "average_selection_time_ms": "mean in-process wall-clock selection latency per case",
            "audit_recommendation_actual_match_pct": "labeled cases whose recommendation maps to the expected current concrete/legacy contract / labeled cases",
        },
        "metrics": metrics,
        "cases": rows,
    }


def _markdown(report: Dict[str, Any]) -> str:
    metrics = report["metrics"]
    metric_rows = [
        ("总工具覆盖率", "total_tool_coverage_rate_pct", "%"),
        ("Server 识别准确率", "server_recognition_accuracy_pct", "%"),
        ("Tool Top-1", "tool_top_1_pct", "%"),
        ("Tool Top-3", "tool_top_3_pct", "%"),
        ("参数/Schema 有效率", "parameter_schema_valid_rate_pct", "%"),
        ("不存在工具幻觉率", "nonexistent_tool_hallucination_rate_pct", "%"),
        ("平均过滤前候选数", "average_candidate_count_before_filter", ""),
        ("平均过滤后候选数", "average_candidate_count_after_filter", ""),
        ("平均选择耗时", "average_selection_time_ms", " ms"),
        ("audit 建议与实际契约一致率", "audit_recommendation_actual_match_pct", "%"),
    ]
    lines = [
        "# 统一 MCP 工具选择评估",
        "",
        f"生成时间：`{report['generated_at']}`",
        "",
        "候选来自真实 tools/list 生成的 ToolRegistry 基线快照；本评估不执行工具。",
        "",
        "| 指标 | 实际结果 |",
        "| --- | ---: |",
        *[
            f"| {label} | {metrics[key]:.6f}{suffix} |"
            for label, key, suffix in metric_rows
        ],
        "",
        "## 用例结果",
        "",
        "| ID | 期望 | Top-1 | Top-3 | 过滤前/后 | 耗时(ms) |",
        "| --- | --- | --- | --- | ---: | ---: |",
    ]
    for row in report["cases"]:
        expected = (
            f"{row['expected_server']}:{row['expected_tool']}"
            if row["expected_tool"]
            else "-"
        )
        selected = (
            f"{row['selected_server']}:{row['selected_tool']}"
            if row["selected_tool"]
            else "ABSTAIN"
        )
        lines.append(
            f"| {row['id']} | {expected} | {selected} | "
            f"{' / '.join(row['top_k']) or '-'} | "
            f"{row['candidate_count_before_filter']}/{row['candidate_count_after_filter']} | "
            f"{row['elapsed_ms']:.6f} |"
        )
    lines.extend(
        [
            "",
            "## 口径",
            "",
            *[f"- `{name}`：{definition}" for name, definition in report["metric_definitions"].items()],
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--json-output", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--markdown-output", type=Path, default=DEFAULT_MARKDOWN)
    args = parser.parse_args()
    report = evaluate(args.cases.resolve(), args.registry.resolve())
    args.json_output.resolve().write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.markdown_output.resolve().write_text(_markdown(report), encoding="utf-8")
    print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
