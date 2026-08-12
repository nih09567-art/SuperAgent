"""Independent MCP selector blind benchmark and read-only enforce boundary probe."""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from time import perf_counter_ns
from typing import Any, Iterable, Mapping

from src.manager.registry.tool_identifier import ToolIdentifier
from src.manager.registry.tool_registry import ToolMetadata
from src.manager.registry.tool_selection_service import ToolSelectionService
from src.tools.office_mcp.selector import select_tools


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES = ROOT / "experiments" / "mcp_realistic" / "blind_cases.json"
DEFAULT_REGISTRY = ROOT / "docs" / "mcp_tool_registry_baseline.json"
DEFAULT_OUTPUT = (
    ROOT
    / ".artifacts"
    / "quantitative"
    / "20260811-2225-realistic-2c9c684"
    / "realistic"
    / "mcp"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pct(numerator: int | float, denominator: int | float) -> float:
    return round(100.0 * numerator / denominator, 3) if denominator else 0.0


def _key(server: str | None, tool: str | None) -> str | None:
    return f"{server}:{tool}" if server and tool else None


def _write_csv(path: Path, rows: list[Mapping[str, Any]]) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def evaluate_blind_cases(dataset: Mapping[str, Any], candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    registry_keys = {_key(item["server_name"], item["name"]) for item in candidates}
    for case in dataset["cases"]:
        started = perf_counter_ns()
        decision = select_tools(
            task_text=case["query"],
            target_agent=case.get("agent_name"),
            known_inputs=case.get("arguments") or {},
            candidates=candidates,
            top_k=3,
            mode="audit",
        )
        elapsed_ms = (perf_counter_ns() - started) / 1_000_000
        expected = _key(case.get("expected_server"), case.get("expected_tool"))
        selected = decision.get("selected_tool_key")
        top_k = [item["tool_key"] for item in decision["candidates"]]
        predicted_abstain = selected is None
        expected_abstain = expected is None
        rows.append(
            {
                "id": case["id"],
                "category": case["category"],
                "family": case["family"],
                "query": case["query"],
                "expected": expected or "ABSTAIN",
                "selected": selected or "ABSTAIN",
                "top_k": "|".join(top_k),
                "top1_correct": int(expected is not None and selected == expected),
                "top3_correct": int(expected is not None and expected in top_k),
                "expected_abstain": int(expected_abstain),
                "predicted_abstain": int(predicted_abstain),
                "hallucinated_tool": int(expected_abstain and selected is not None),
                "selected_exists_in_registry": int(selected is None or selected in registry_keys),
                "before": decision["candidate_count_before_filter"],
                "after": decision["candidate_count_after_filter"],
                "elapsed_ms": round(elapsed_ms, 6),
                "abstention_reason": decision.get("abstention_reason") or "",
            }
        )

    labeled = [row for row in rows if not row["expected_abstain"]]
    abstain_expected = [row for row in rows if row["expected_abstain"]]
    tp = sum(row["expected_abstain"] and row["predicted_abstain"] for row in rows)
    fp = sum(not row["expected_abstain"] and row["predicted_abstain"] for row in rows)
    family_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in labeled:
        family_rows[row["family"]].append(row)
    family_top1 = {
        family: _pct(sum(row["top1_correct"] for row in group), len(group))
        for family, group in sorted(family_rows.items())
    }
    before = mean(row["before"] for row in rows)
    after = mean(row["after"] for row in rows)
    metrics = {
        "registry_tool_count": len(candidates),
        "case_count": len(rows),
        "natural_positive_count": sum(row["category"] == "natural_positive" for row in rows),
        "confusable_count": sum(row["category"] == "confusable" for row in rows),
        "abstain_count": len(abstain_expected),
        "top1_accuracy_pct": _pct(sum(row["top1_correct"] for row in labeled), len(labeled)),
        "top3_accuracy_pct": _pct(sum(row["top3_correct"] for row in labeled), len(labeled)),
        "family_top1_accuracy_pct": family_top1,
        "family_macro_top1_accuracy_pct": round(mean(family_top1.values()), 3),
        "abstain_precision_pct": _pct(tp, tp + fp),
        "abstain_recall_pct": _pct(tp, len(abstain_expected)),
        "unknown_tool_hallucination_rate_pct": _pct(sum(row["hallucinated_tool"] for row in abstain_expected), len(abstain_expected)),
        "selected_tool_registry_validity_pct": _pct(sum(row["selected_exists_in_registry"] for row in rows), len(rows)),
        "average_candidates_before": round(before, 3),
        "average_candidates_after": round(after, 3),
        "candidate_compression_pct": round(100.0 * (1.0 - after / before), 3),
        "selection_latency_mean_ms": round(mean(row["elapsed_ms"] for row in rows), 6),
    }
    return rows, metrics


def evaluate_schema_probes(dataset: Mapping[str, Any], candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_id = {case["id"]: case for case in dataset["cases"]}
    rows: list[dict[str, Any]] = []
    for probe in dataset["schema_probes"]:
        source = by_id[probe["source_case"]]
        arguments = dict(source["arguments"])
        for name in probe["remove_inputs"]:
            arguments.pop(name, None)
        decision = select_tools(
            task_text=source["query"],
            target_agent=source.get("agent_name"),
            known_inputs=arguments,
            candidates=candidates,
            top_k=3,
            mode="audit",
        )
        expected_key = _key(source["expected_server"], probe["expected_tool"])
        excluded = next(
            (
                item for item in decision["excluded"]
                if _key(item.get("server_name"), item.get("name")) == expected_key
            ),
            None,
        )
        schema_reasons = [
            reason for reason in (excluded or {}).get("reasons", [])
            if reason.get("stage") == "input_schema_filter"
        ]
        passed = bool(schema_reasons) and decision.get("selected_tool_key") != expected_key
        rows.append(
            {
                "id": probe["id"],
                "source_case": probe["source_case"],
                "expected_tool_key": expected_key,
                "removed_inputs": "|".join(probe["remove_inputs"]),
                "selected_tool_key": decision.get("selected_tool_key") or "ABSTAIN",
                "expected_tool_schema_blocked": int(bool(schema_reasons)),
                "probe_passed": int(passed),
                "abstention_reason": decision.get("abstention_reason") or "",
            }
        )
    return rows, {
        "schema_probe_count": len(rows),
        "schema_block_rate_pct": _pct(sum(row["probe_passed"] for row in rows), len(rows)),
    }


@dataclass
class _Context:
    metadata: dict[str, Any] = field(default_factory=dict)


class _Registry:
    def __init__(self, candidates: Iterable[Mapping[str, Any]]) -> None:
        values = {}
        for item in candidates:
            value = dict(item)
            identifier = ToolIdentifier(
                scope="global",
                server=str(value["server_name"]),
                name=str(value["name"]),
            )
            values[(identifier.server, identifier.name)] = ToolMetadata(
                identifier=identifier,
                tool=None,
                description=str(value.get("description") or ""),
                server_name=identifier.server,
                runtime_tool_name=identifier.name,
                input_schema=dict(value.get("input_schema") or {}),
                scenario=str(value.get("scenario") or ""),
                owner_agents=list(value.get("owner_agents") or []),
                intents=list(value.get("intents") or []),
                aliases=list(value.get("aliases") or []),
                operation_mode=str(value.get("operation_mode") or ""),
                required_inputs=list(value.get("required_inputs") or []),
                produces=list(value.get("produces") or []),
                side_effect=bool(value.get("side_effect")),
                risk_level=str(value.get("risk_level") or "low"),
                legacy_tool_name=value.get("legacy_tool_name"),
            )
        self._values = values

    async def mcp_metadata_view(self) -> dict[str, dict[str, Any]]:
        return self._values


@contextmanager
def _enforce_environment() -> Iterable[None]:
    old_mode = os.environ.get("MCP_TOOL_SELECTION_MODE")
    old_top_k = os.environ.get("MCP_TOOL_SELECTION_TOP_K")
    os.environ["MCP_TOOL_SELECTION_MODE"] = "enforce"
    os.environ["MCP_TOOL_SELECTION_TOP_K"] = "3"
    try:
        yield
    finally:
        if old_mode is None:
            os.environ.pop("MCP_TOOL_SELECTION_MODE", None)
        else:
            os.environ["MCP_TOOL_SELECTION_MODE"] = old_mode
        if old_top_k is None:
            os.environ.pop("MCP_TOOL_SELECTION_TOP_K", None)
        else:
            os.environ["MCP_TOOL_SELECTION_TOP_K"] = old_top_k


async def evaluate_readonly_enforce(dataset: Mapping[str, Any], candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_id = {case["id"]: case for case in dataset["cases"]}
    by_key = {_key(item["server_name"], item["name"]): item for item in candidates}
    read_only = [item for item in candidates if not bool(item.get("side_effect"))]
    service = ToolSelectionService(registry=_Registry(candidates))
    rows: list[dict[str, Any]] = []
    for case_id in dataset["readonly_enforce_cases"]:
        case = by_id[case_id]
        context = _Context(metadata={"known_inputs": dict(case["arguments"])})
        actual_names_before = [item["name"] for item in read_only]
        with _enforce_environment():
            production_report = await service.audit(
                agent_name=case.get("agent_name") or "SpreadsheetReadOnlyAgent",
                messages=[{"role": "user", "content": case["query"]}],
                context=context,
                actual_tool_names=actual_names_before,
            )

        decision = select_tools(
            task_text=case["query"],
            target_agent=case.get("agent_name"),
            known_inputs=case["arguments"],
            candidates=candidates,
            top_k=3,
            mode="enforce",
        )
        allowed_keys = [
            item["tool_key"]
            for item in decision["candidates"]
            if item["tool_key"] in by_key and not bool(by_key[item["tool_key"]].get("side_effect"))
        ]
        invoked_key = decision.get("selected_tool_key")
        isolated_constrained = 0 < len(allowed_keys) < len(read_only)
        isolated_call_within = invoked_key in allowed_keys
        expected_key = _key(case["expected_server"], case["expected_tool"])
        rows.append(
            {
                "id": case_id,
                "expected_tool_key": expected_key,
                "production_requested_mode": (production_report or {}).get("requested_mode"),
                "production_effective_mode": (production_report or {}).get("mode"),
                "production_enforce_blocked": bool((production_report or {}).get("enforce_blocked")),
                "production_actual_tool_count_before": len(actual_names_before),
                "production_actual_tool_count_after": len((production_report or {}).get("actual_tool_names") or []),
                "production_tool_set_constrained": len((production_report or {}).get("actual_tool_names") or []) < len(actual_names_before),
                "isolated_allowed_tool_keys": allowed_keys,
                "isolated_actual_tool_count_before": len(read_only),
                "isolated_actual_tool_count_after": len(allowed_keys),
                "isolated_tool_set_constrained": isolated_constrained,
                "isolated_invoked_tool_key": invoked_key,
                "isolated_call_within_candidates": isolated_call_within,
                "isolated_expected_tool_invoked": invoked_key == expected_key,
            }
        )
    metrics = {
        "readonly_enforce_case_count": len(rows),
        "production_enforce_effective_rate_pct": _pct(
            sum(row["production_tool_set_constrained"] and not row["production_enforce_blocked"] for row in rows),
            len(rows),
        ),
        "production_enforce_blocked_rate_pct": _pct(sum(row["production_enforce_blocked"] for row in rows), len(rows)),
        "isolated_readonly_tool_set_constraint_rate_pct": _pct(sum(row["isolated_tool_set_constrained"] for row in rows), len(rows)),
        "isolated_call_within_candidates_rate_pct": _pct(sum(row["isolated_call_within_candidates"] for row in rows), len(rows)),
        "isolated_expected_tool_invocation_rate_pct": _pct(sum(row["isolated_expected_tool_invoked"] for row in rows), len(rows)),
    }
    return rows, metrics


def _markdown(report: Mapping[str, Any]) -> str:
    m = report["metrics"]
    lines = [
        "# MCP 独立盲测与只读 enforce 边界实验",
        "",
        f"- 盲测集 SHA-256：`{report['dataset_sha256']}`",
        f"- Registry SHA-256：`{report['registry_sha256']}`",
        f"- 36 条冻结案例：24 正例、8 易混淆、4 应弃选。",
        "",
        "| 指标 | 结果 |",
        "| --- | ---: |",
        f"| Top-1 | {m['top1_accuracy_pct']:.3f}% |",
        f"| Top-3 | {m['top3_accuracy_pct']:.3f}% |",
        f"| 工具族宏平均 Top-1 | {m['family_macro_top1_accuracy_pct']:.3f}% |",
        f"| 弃选 Precision | {m['abstain_precision_pct']:.3f}% |",
        f"| 弃选 Recall | {m['abstain_recall_pct']:.3f}% |",
        f"| 未知工具幻觉率 | {m['unknown_tool_hallucination_rate_pct']:.3f}% |",
        f"| Schema 阻断率（8 探针） | {m['schema_block_rate_pct']:.3f}% |",
        f"| 平均候选数 | {m['average_candidates_before']:.3f} → {m['average_candidates_after']:.3f} |",
        f"| 候选压缩率 | {m['candidate_compression_pct']:.3f}% |",
        f"| 生产只读 enforce 生效率 | {m['production_enforce_effective_rate_pct']:.3f}% |",
        f"| 隔离只读 enforce 约束率 | {m['isolated_readonly_tool_set_constraint_rate_pct']:.3f}% |",
        f"| 隔离调用属于候选率 | {m['isolated_call_within_candidates_rate_pct']:.3f}% |",
        "",
        "## 边界",
        "",
        "生产 `ToolSelectionService` 会把 `enforce` 请求降级为 `audit`，因此不会改变 Executor 工具集合；该失败被原样保留。隔离 enforce 仅证明只读 Top-K 约束算法可行，不代表生产链路已接入。",
        "",
    ]
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> dict[str, Any]:
    dataset = json.loads(args.cases.read_text(encoding="utf-8-sig"))
    registry = json.loads(args.registry.read_text(encoding="utf-8-sig"))
    candidates = list(registry["tools"])
    if len(dataset["cases"]) != 36:
        raise ValueError("blind dataset must contain exactly 36 cases")
    if len(candidates) != 48:
        raise ValueError("registry snapshot must contain exactly 48 tools")
    blind_rows, blind_metrics = evaluate_blind_cases(dataset, candidates)
    schema_rows, schema_metrics = evaluate_schema_probes(dataset, candidates)
    enforce_rows, enforce_metrics = await evaluate_readonly_enforce(dataset, candidates)
    metrics = {**blind_metrics, **schema_metrics, **enforce_metrics}
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset_id": dataset["dataset_id"],
        "dataset_sha256": _sha256(args.cases),
        "registry_sha256": _sha256(args.registry),
        "metrics": metrics,
        "family_top1_accuracy_pct": metrics["family_top1_accuracy_pct"],
        "boundaries": {
            "selector_evaluation": "offline deterministic audit over a frozen registry snapshot",
            "production_enforce": "blocked and downgraded to audit by ToolSelectionService",
            "isolated_enforce": "recording executor; no MCP server, LLM, network, or real tool side effect",
        },
        "cases": blind_rows,
        "schema_probes": schema_rows,
        "readonly_enforce": enforce_rows,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(args.output_dir / "cases.csv", blind_rows)
    _write_csv(args.output_dir / "schema_probes.csv", schema_rows)
    (args.output_dir / "enforce.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in enforce_rows),
        encoding="utf-8",
    )
    (args.output_dir / "summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (args.output_dir / "summary.md").write_text(_markdown(report), encoding="utf-8")
    summary_rows = [
        {"metric": key, "value": json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value}
        for key, value in metrics.items()
    ]
    _write_csv(args.output_dir / "summary.csv", summary_rows)
    (args.output_dir / "manifest.json").write_text(
        json.dumps(
            {
                "generated_at": report["generated_at"],
                "dataset_path": str(args.cases.resolve()),
                "dataset_sha256": report["dataset_sha256"],
                "registry_path": str(args.registry.resolve()),
                "registry_sha256": report["registry_sha256"],
                "output_dir": str(args.output_dir.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> None:
    report = asyncio.run(run(parse_args()))
    print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
