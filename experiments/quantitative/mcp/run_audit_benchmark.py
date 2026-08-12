"""Run the fixed MCP selector audit benchmark without executing any tool.

Functional metrics are computed once over the 53 unique labeled cases. Latency
metrics are computed over 30 complete repetitions (53 * 30 = 1590 samples).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from scripts.evaluate_office_mcp_tool_selector import evaluate  # noqa: E402


CASES = ROOT / "experiments" / "office_mcp_tool_selection_cases.json"
REGISTRY = ROOT / "docs" / "mcp_tool_registry_baseline.json"
DEFAULT_OUTPUT = ROOT / ".artifacts" / "quantitative" / "20260811-2c9c684" / "mcp"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _git(args: list[str]) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=ROOT, text=True, encoding="utf-8"
    ).strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--rounds", type=int, default=30)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)

    reports = [evaluate(CASES, REGISTRY) for _ in range(args.rounds)]
    functional = reports[0]
    cases = functional["cases"]
    normal = [row for row in cases if row["kind"] == "normal"]
    ambiguous = [row for row in cases if row["kind"] == "ambiguous"]
    insufficient = [row for row in cases if row["kind"] == "insufficient"]
    unknown = [row for row in cases if row["kind"] == "unknown"]
    abstention_expected = [row for row in cases if not row["expected_tool"]]

    latency_rows = [
        {
            "round": round_number,
            "case_id": row["id"],
            "kind": row["kind"],
            "elapsed_ms": row["elapsed_ms"],
        }
        for round_number, report in enumerate(reports, start=1)
        for row in report["cases"]
    ]
    latencies = [float(row["elapsed_ms"]) for row in latency_rows]
    before = statistics.fmean(
        row["candidate_count_before_filter"] for row in cases
    )
    after = statistics.fmean(row["candidate_count_after_filter"] for row in cases)
    compression = 100.0 * (1.0 - after / before)

    metrics = {
        "catalog_tool_count": functional["metrics"]["catalog_tool_count"],
        "unique_case_count": len(cases),
        "normal_case_count": len(normal),
        "ambiguous_case_count": len(ambiguous),
        "insufficient_input_case_count": len(insufficient),
        "unknown_case_count": len(unknown),
        "total_tool_coverage_rate_pct": functional["metrics"][
            "total_tool_coverage_rate_pct"
        ],
        "server_recognition_accuracy_pct": functional["metrics"][
            "server_recognition_accuracy_pct"
        ],
        "tool_top_1_pct": functional["metrics"]["tool_top_1_pct"],
        "tool_top_3_pct": functional["metrics"]["tool_top_3_pct"],
        "parameter_schema_valid_rate_pct": functional["metrics"][
            "parameter_schema_valid_rate_pct"
        ],
        "unknown_task_abstention_rate_pct": round(
            100.0 * sum(row["selected_tool"] is None for row in unknown) / len(unknown),
            3,
        ),
        "all_expected_abstention_rate_pct": round(
            100.0
            * sum(row["selected_tool"] is None for row in abstention_expected)
            / len(abstention_expected),
            3,
        ),
        "nonexistent_tool_hallucination_rate_pct": functional["metrics"][
            "nonexistent_tool_hallucination_rate_pct"
        ],
        "average_candidate_count_before_filter": round(before, 3),
        "average_candidate_count_after_filter": round(after, 3),
        "candidate_compression_rate_pct": round(compression, 3),
        "audit_recommendation_actual_match_pct": functional["metrics"][
            "audit_recommendation_actual_match_pct"
        ],
        "latency_rounds": args.rounds,
        "latency_sample_count": len(latencies),
        "latency_mean_ms": round(statistics.fmean(latencies), 6),
        "latency_p50_ms": round(_percentile(latencies, 0.50), 6),
        "latency_p95_ms": round(_percentile(latencies, 0.95), 6),
        "latency_max_ms": round(max(latencies), 6),
    }
    boundary = (
        "Offline audit/control-plane evaluation only. The selector did not execute "
        "tools, did not alter Executor-supplied tools, and enforce mode was not enabled. "
        "Results measure filtering, ranking, Top-K, Schema validation, and abstention; "
        "they do not measure production tool-execution success or online misselection."
    )
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git(["rev-parse", "HEAD"]),
        "git_status_short": _git(["status", "--short"]),
        "dataset_path": str(CASES.relative_to(ROOT)),
        "dataset_sha256": _sha256(CASES),
        "registry_path": str(REGISTRY.relative_to(ROOT)),
        "registry_sha256": _sha256(REGISTRY),
        "mode": "audit",
        "boundary": boundary,
        "metric_population": {
            "functional": (
                "53 unique cases: 48 normal coverage cases, 1 ambiguous labeled "
                "case, 2 insufficient-input cases, and 2 unknown-task cases"
            ),
            "latency": "30 repetitions x 53 cases = 1590 in-process samples",
        },
        "metrics": metrics,
    }

    (output / "functional.json").write_text(
        json.dumps(functional, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    _write_csv(output / "cases.csv", cases)
    _write_csv(output / "latency.csv", latency_rows)
    _write_csv(output / "summary.csv", [{"metric": key, "value": value} for key, value in metrics.items()])

    lines = [
        "# MCP 工具选择控制面离线审计实验",
        "",
        f"生成时间：`{summary['generated_at']}`",
        f"代码提交：`{summary['git_commit']}`",
        "",
        "## 结果",
        "",
        "| 指标 | 结果 |",
        "| --- | ---: |",
        *[f"| `{key}` | {value} |" for key, value in metrics.items()],
        "",
        "## 样本口径",
        "",
        "- 功能指标：53个唯一案例，包括48个正常覆盖案例、1个歧义标注案例、2个输入不足案例、2个未知任务案例。",
        "- Top-1/Top-3与Server准确率的分母是49个存在期望工具的标注案例；工具覆盖率对应48个正常案例覆盖48个注册工具。",
        "- 未知任务弃选率的分母是2个unknown案例；全部应弃选率的分母是2个unknown加2个insufficient案例。",
        "- 时延指标：53个案例完整重复30轮，共1590个进程内选择时延样本。",
        "- 正确率没有把重复轮次扩充为新的独立案例。",
        "",
        "## 能力边界",
        "",
        boundary,
        "",
    ]
    (output / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
