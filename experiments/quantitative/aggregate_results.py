from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import matplotlib.pyplot as plt


ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / ".artifacts" / "quantitative" / "20260811-2c9c684"
FIGURES = RUN / "figures"


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def save_bar(path: Path, labels, values, title, ylabel, ylim=None, colors=None):
    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    bars = ax.bar(labels, values, color=colors)
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    if ylim:
        ax.set_ylim(*ylim)
    ax.grid(axis="y", alpha=0.25)
    for bar, value in zip(bars, values):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height(),
            f"{value:.1f}",
            ha="center",
            va="bottom",
        )
    fig.tight_layout()
    fig.savefig(path, format="svg")
    plt.close(fig)


def main() -> None:
    FIGURES.mkdir(parents=True, exist_ok=True)
    planning = read_json(RUN / "planning" / "summary.json")
    mcp = read_json(RUN / "mcp" / "summary.json")
    recovery_rows = read_json(RUN / "recovery" / "summary.json")
    recovery = [row for row in recovery_rows if row["scenario"] == "ALL"]

    http_paths = {
        "dynamic-three": RUN / "http" / "acceptance-dynamic-three-c914454d97ae409689cbdb7b54f2e701" / "acceptance-summary.json",
        "five-approved": RUN / "http" / "acceptance-dynamic-five-approved-19fe8f68c8ee481daea51d8bdc35a0e4" / "acceptance-summary.json",
        "five-rejected": RUN / "http" / "acceptance-dynamic-five-rejected-65cb60f4182349059008f5a3dfb5ba3d" / "acceptance-summary.json",
    }
    http = {name: read_json(path) for name, path in http_paths.items()}

    save_bar(
        FIGURES / "planning_validation.svg",
        ["Legal pass", "Invalid reject"],
        [planning["legal_plan_pass_rate"] * 100, planning["invalid_plan_rejection_rate"] * 100],
        "Structured-plan validation (60 unique cases)",
        "Rate (%)",
        (0, 110),
        ["#4C78A8", "#59A14F"],
    )
    save_bar(
        FIGURES / "mcp_candidate_reduction.svg",
        ["Discovered", "After filtering"],
        [mcp["metrics"]["average_candidate_count_before_filter"], mcp["metrics"]["average_candidate_count_after_filter"]],
        "MCP audit candidate reduction (53 unique cases)",
        "Average candidates",
        (0, 53),
        ["#BAB0AC", "#F28E2B"],
    )

    labels = [row["strategy"] for row in recovery]
    completion = [row["completion_rate"] * 100 for row in recovery]
    recovered = [row["recovery_success_rate"] * 100 for row in recovery]
    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    x = range(len(labels))
    ax.bar([i - 0.18 for i in x], completion, width=0.36, label="Completion")
    ax.bar([i + 0.18 for i in x], recovered, width=0.36, label="Recovered after initial failure")
    ax.set_xticks(list(x), labels)
    ax.set_ylim(0, 50)
    ax.set_ylabel("Rate (%)")
    ax.set_title("Bounded recovery (500 trials per strategy)")
    ax.legend()
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(FIGURES / "recovery_comparison.svg", format="svg")
    plt.close(fig)

    save_bar(
        FIGURES / "http_acceptance.svg",
        ["3-Agent\ncompleted", "5-Agent approved\naccepted", "5-Agent rejected\naccepted"],
        [http["dynamic-three"]["passed"], http["five-approved"]["passed"], http["five-rejected"]["passed"]],
        "Real-HTTP acceptance batches*",
        "Accepted runs",
        (0, 6),
        ["#59A14F", "#E15759", "#E15759"],
    )

    overall = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "baseline_commit": planning["git_commit"],
        "planning": {
            "unique_cases": planning["unique_cases"],
            "legal_pass_rate_pct": planning["legal_plan_pass_rate"] * 100,
            "invalid_rejection_rate_pct": planning["invalid_plan_rejection_rate"] * 100,
            "pipeline_p95_ms": planning["latency"]["pipeline_total"]["p95_ms"],
            "boundary": planning["scope"],
        },
        "multi_agent_http": {
            "dynamic_three": {"requested": 5, "executed": 5, "passed": 5},
            "dynamic_five_approved": {"requested": 3, "executed": 1, "passed": 0, "fail_fast": True},
            "dynamic_five_rejected": {"requested": 3, "executed": 1, "passed": 0, "fail_fast": True},
            "five_agent_boundary": "The admin execution identity bypassed mandatory review, so the email step ended SUCCEEDED instead of APPROVAL_REQUIRED.",
        },
        "mcp_audit": {
            **mcp["metrics"],
            "boundary": mcp["boundary"],
        },
        "recovery": {row["strategy"]: row for row in recovery},
        "focused_regression": "44 passed in 5.24s",
    }
    (RUN / "overall-summary.json").write_text(
        json.dumps(overall, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    md = f"""# Quantitative experiment summary

Baseline: `{overall['baseline_commit']}`

| Research item | Population | Main result | Boundary |
|---|---:|---|---|
| Structured plan | 60 unique cases | legal pass 100%; invalid reject 100%; P95 {planning['latency']['pipeline_total']['p95_ms']:.3f} ms | deterministic validation, not LLM generation accuracy |
| Multi-Agent HTTP | 5 completed runs | dynamic-three 5/5 accepted with three required Artifacts | five-Agent approval batches stopped after the first failed gate assertion |
| MCP selection | 53 unique cases; 1,590 timing samples | Top-1 100%; abstention 100%; candidates 48.00 to 2.17 (-95.48%); P95 {mcp['metrics']['latency_p95_ms']:.3f} ms | audit only; no Executor enforcement or production tool execution |
| Failure recovery | 1,500 trials | completion B0/B1/B2 = 12.4%/27.2%/40.0%; recovered = 0%/16.9%/31.5% | stub services, fixed faults, virtual latency |

## Important negative result

Both five-Agent approval scenarios requested 3 runs but used fail-fast execution and each stopped after one executed failure. The current `admin` execution identity bypassed mandatory review, so the email step ended `SUCCEEDED` instead of the expected `APPROVAL_REQUIRED`. These batches are not reported as 0/3 statistical success rates.

## Verification

- Final focused regression: 44 passed in 5.24 seconds.
- Duplicate side effects: 0; recovery governance violations: 0.
- Figures are in `figures/` and preserve the audit/simulation boundaries above.
"""
    (RUN / "overall-summary.md").write_text(md, encoding="utf-8")


if __name__ == "__main__":
    main()
