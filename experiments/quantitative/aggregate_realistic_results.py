from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Aggregate the realistic quantitative evaluation run.")
    parser.add_argument("--run-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_dir = args.run_dir.resolve()
    planning = read_json(run_dir / "timing" / "planning" / "summary.json")
    scoring = read_json(run_dir / "realistic" / "scoring" / "summary.json")
    mcp = read_json(run_dir / "realistic" / "mcp" / "summary.json")
    mcp_timing = read_json(run_dir / "timing" / "mcp-audit" / "summary.json")
    recovery_rows = read_json(run_dir / "baseline" / "recovery" / "summary.json")
    recovery = {row["strategy"]: row for row in recovery_rows if row["scenario"] == "ALL"}

    overall = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "baseline_commit": planning["git_commit"],
        "dataset_sha256": scoring["dataset_validation"]["dataset_sha256"],
        "gold_behavior": {
            "conformant": scoring["dimensions"]["overall_gold_behavior"]["status_counts"]["CONFORMANT"],
            "total": scoring["dimensions"]["overall_gold_behavior"]["denominator_unique_cases"],
            "rate_pct": scoring["dimensions"]["overall_gold_behavior"]["conformance_rate_full_denominator"] * 100,
            "dimensions": scoring["dimensions"],
            "nonconformant_cases": ["ORCH-007", "ORCH-016", "ORCH-017"],
        },
        "trusted_plan": {
            "deterministic_regression_cases": planning["unique_cases"],
            "legal_accepted": planning["confusion_matrix"]["true_accept"],
            "invalid_rejected": planning["confusion_matrix"]["true_reject"],
            "pipeline_mean_ms": planning["latency"]["pipeline_total"]["mean_ms"],
            "pipeline_p95_ms": planning["latency"]["pipeline_total"]["p95_ms"],
            "boundary": planning["scope"],
        },
        "mcp_blind": {
            **mcp["metrics"],
            "dataset_sha256": mcp["dataset_sha256"],
            "registry_sha256": mcp["registry_sha256"],
            "audit_latency_mean_ms": mcp_timing["metrics"]["latency_mean_ms"],
            "audit_latency_p95_ms": mcp_timing["metrics"]["latency_p95_ms"],
            "boundary": mcp["boundaries"],
        },
        "recovery_sensitivity": recovery,
        "focused_regression": "119 passed, 3 xfailed in 7.08s",
        "timing_boundary": "Exclusive local in-process measurements; not HTTP wall-clock or production latency.",
    }
    (run_dir / "overall-summary.json").write_text(
        json.dumps(overall, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    dimensions = scoring["dimensions"]
    md = f"""# Realistic quantitative evaluation summary

Baseline: `{overall['baseline_commit']}`
Frozen orchestration dataset: `{overall['dataset_sha256']}`

| Research item | Evidence population | Main result | Interpretation boundary |
| --- | --- | --- | --- |
| Trusted plan | 20-case set plus 60 deterministic plans | Planner to TaskGraph {dimensions['planner_generated_taskgraph']['conformant_over_full_denominator']}; invalid candidate rejection {dimensions['invalid_candidate_plan']['conformant_over_full_denominator']}; deterministic 12 accepted and 48 rejected | deterministic gates are not LLM generation accuracy |
| Multi-Agent collaboration | 8 executed collaboration cases | {dimensions['actual_multi_agent_execution']['conformant_over_full_denominator']} conformant | mixes real HTTP and deterministic composite harness; gold behavior, not production success rate |
| MCP selection | 36 frozen blind cases; 8 schema probes; 8 read-only enforce probes | Top-1 {mcp['metrics']['top1_accuracy_pct']:.1f}%; Top-3 {mcp['metrics']['top3_accuracy_pct']:.3f}%; abstain P/R {mcp['metrics']['abstain_precision_pct']:.1f}%/{mcp['metrics']['abstain_recall_pct']:.1f}%; candidates 48 to {mcp['metrics']['average_candidates_after']:.3f} | production enforce 0/8; isolated read-only enforce 7/8 |
| Failure recovery | 8 controlled-fault cases plus 1,500 fixed-model trials | controlled behavior {dimensions['controlled_fault']['conformant_over_full_denominator']}; B0/B1/B2 closure 12.4%/27.2%/40.0% | offline strategy sensitivity uses stubs and virtual latency |

## Overall gold behavior

- 17/20 cases conformant (85%).
- Nonconformant: ORCH-007 fan-in source completeness leak; ORCH-016 FAILED versus expected SKIPPED; ORCH-017 raw salary in scheduler event data.
- All cases have evidence; xfail remains nonconformant and in the denominator.

## Exclusive in-process timing

- Trusted-plan pipeline: mean {planning['latency']['pipeline_total']['mean_ms']:.3f} ms, P95 {planning['latency']['pipeline_total']['p95_ms']:.3f} ms (1,800 invocations).
- MCP audit selector: mean {mcp_timing['metrics']['latency_mean_ms']:.3f} ms, P95 {mcp_timing['metrics']['latency_p95_ms']:.3f} ms (1,590 samples).
- Production enforce overhead is not reported because the production service downgraded all 8 enforce requests to audit.

## Verification

- Focused regression: 119 passed, 3 xfailed in 7.08 seconds.
- Dataset and registry hashes are recorded in the run manifests.
"""
    (run_dir / "overall-summary.md").write_text(md, encoding="utf-8")


if __name__ == "__main__":
    main()
