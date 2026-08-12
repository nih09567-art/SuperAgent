"""Deterministic quantitative benchmark for trusted plan validation.

The benchmark evaluates candidate-plan validation and conversion mechanisms,
not the quality or accuracy of an LLM Planner.
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.orchestration.plan_snapshot import (  # noqa: E402
    save_plan_snapshot,
    verify_snapshot_for_execution,
)
from src.orchestration.plan_to_task_graph import plan_to_task_graph  # noqa: E402
from src.workflow.coor_task import _validate_plan_against_task_profile  # noqa: E402


VALID_CATEGORY = "valid"
INVALID_CATEGORIES = (
    "missing_subtask_coverage",
    "duplicate_subtask_coverage",
    "dependency_mismatch",
    "intent_mismatch",
    "unknown_dependency",
    "cycle",
    "invalid_output_binding",
    "snapshot_drift",
)


@dataclass(frozen=True)
class Case:
    case_id: str
    category: str
    expected_valid: bool
    profile: dict[str, Any]
    planning_steps: list[dict[str, Any]]
    approved_steps: list[dict[str, Any]] | None = None


def _profile() -> dict[str, Any]:
    return {
        "subtasks": [
            {
                "id": "subtask_hr",
                "intent": "employee_information_query",
                "action": "read",
                "depends_on": [],
            },
            {
                "id": "subtask_knowledge",
                "intent": "knowledge_lookup",
                "action": "read",
                "depends_on": [],
            },
            {
                "id": "subtask_report",
                "intent": "report_generation",
                "action": "generate",
                "depends_on": ["subtask_hr", "subtask_knowledge"],
            },
        ]
    }


def _base_plan(variant: int) -> list[dict[str, Any]]:
    people = ("王强", "李娜", "赵明", "陈晨", "刘洋", "孙悦")
    topics = ("年假", "差旅", "培训", "调休", "报销", "入职")
    person = people[variant % len(people)]
    topic = topics[variant % len(topics)]
    return [
        {
            "step_id": "step_hr",
            "subtask_ids": ["subtask_hr"],
            "intents": ["employee_information_query"],
            "depends_on": [],
            "agent_name": "RemoteHRAssistantAgent",
            "title": f"查询{person}的员工信息（样本{variant + 1}）",
            "produces": ["person_info"],
        },
        {
            "step_id": "step_knowledge",
            "subtask_ids": ["subtask_knowledge"],
            "intents": ["knowledge_lookup"],
            "depends_on": [],
            "agent_name": "RemoteKnowledgeAgent",
            "title": f"查询{topic}制度（样本{variant + 1}）",
            "produces": ["policy_info"],
        },
        {
            "step_id": "step_report",
            "subtask_ids": ["subtask_report"],
            "intents": ["report_generation"],
            "depends_on": ["step_hr", "step_knowledge"],
            "agent_name": "RemoteReportAgent",
            "title": f"汇总{person}{topic}调研报告（样本{variant + 1}）",
            "produces": ["report"],
        },
    ]


def _mutate(category: str, variant: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
    approved = _base_plan(variant)
    steps = copy.deepcopy(approved)
    if category == "missing_subtask_coverage":
        steps.pop(1)
    elif category == "duplicate_subtask_coverage":
        steps[0]["subtask_ids"].append("subtask_knowledge")
        steps[0]["intents"].append("knowledge_lookup")
    elif category == "dependency_mismatch":
        steps[2]["depends_on"] = ["step_hr"]
    elif category == "intent_mismatch":
        steps[2]["intents"] = ["document_generation"]
    elif category == "unknown_dependency":
        steps[2]["depends_on"].append(f"missing_step_{variant + 1}")
    elif category == "cycle":
        steps[0]["depends_on"] = ["step_report"]
    elif category == "invalid_output_binding":
        steps[2]["inputs"] = [
            {
                "parameter_name": "report.source",
                "source_step": "step_hr",
                "source_output": "invented_output",
            }
        ]
    elif category == "snapshot_drift":
        steps[2]["title"] += "（确认后被修改）"
        return steps, approved
    else:  # pragma: no cover - guarded by the fixed category list
        raise ValueError(f"unknown mutation category: {category}")
    return steps, None


def build_cases() -> list[Case]:
    cases: list[Case] = []
    for variant in range(12):
        cases.append(
            Case(
                case_id=f"valid-{variant + 1:02d}",
                category=VALID_CATEGORY,
                expected_valid=True,
                profile=_profile(),
                planning_steps=_base_plan(variant),
            )
        )
    for category in INVALID_CATEGORIES:
        for variant in range(6):
            steps, approved = _mutate(category, variant)
            cases.append(
                Case(
                    case_id=f"{category}-{variant + 1:02d}",
                    category=category,
                    expected_valid=False,
                    profile=_profile(),
                    planning_steps=steps,
                    approved_steps=approved,
                )
            )
    assert len(cases) == 60
    return cases


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = (len(ordered) - 1) * percentile
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    fraction = rank - low
    return ordered[low] + (ordered[high] - ordered[low]) * fraction


def evaluate_case(case: Case, snapshot_dir: Path, run_suffix: str = "") -> dict[str, Any]:
    started = time.perf_counter_ns()
    state = {
        "task_profile": copy.deepcopy(case.profile),
        "_require_trusted_subtask_bindings": True,
    }

    profile_started = time.perf_counter_ns()
    profile_errors = _validate_plan_against_task_profile(case.planning_steps, state)
    profile_ms = (time.perf_counter_ns() - profile_started) / 1_000_000

    graph = None
    converter_error = ""
    converter_started = time.perf_counter_ns()
    try:
        graph = plan_to_task_graph(
            copy.deepcopy(case.planning_steps),
            task_id=f"benchmark-{case.case_id}{run_suffix}",
            subject="benchmark-user",
            goal="生成结构化调研报告",
            subtasks=copy.deepcopy(case.profile["subtasks"]),
        )
    except Exception as exc:  # noqa: BLE001 - rejection is the measured result
        converter_error = f"{type(exc).__name__}: {exc}"
    converter_ms = (time.perf_counter_ns() - converter_started) / 1_000_000

    snapshot_attempted = graph is not None
    snapshot_ok = False
    snapshot_reason = "converter_rejected"
    snapshot_started = time.perf_counter_ns()
    if snapshot_attempted:
        workflow_id = f"benchmark-{case.case_id}{run_suffix}"
        approved_steps = copy.deepcopy(case.approved_steps or case.planning_steps)
        approved_graph = graph
        if case.approved_steps is not None:
            approved_graph = plan_to_task_graph(
                approved_steps,
                task_id=workflow_id,
                subject="benchmark-user",
                goal="生成结构化调研报告",
                subtasks=copy.deepcopy(case.profile["subtasks"]),
            )
        snapshot = save_plan_snapshot(
            workflow_id=workflow_id,
            user_id="benchmark-user",
            planning_steps=approved_steps,
            task_graph=approved_graph.model_dump(),
            base_dir=snapshot_dir,
        )
        verified_graph, snapshot_reason = verify_snapshot_for_execution(
            snapshot,
            workflow_id=workflow_id,
            user_id="benchmark-user",
            planning_steps=copy.deepcopy(case.planning_steps),
            goal="生成结构化调研报告",
            subtasks=copy.deepcopy(case.profile["subtasks"]),
        )
        snapshot_ok = verified_graph is not None
    snapshot_ms = (time.perf_counter_ns() - snapshot_started) / 1_000_000

    accepted = not profile_errors and graph is not None and snapshot_ok
    total_ms = (time.perf_counter_ns() - started) / 1_000_000
    gates_rejecting = []
    if profile_errors:
        gates_rejecting.append("task_profile")
    if graph is None:
        gates_rejecting.append("task_graph_conversion")
    if snapshot_attempted and not snapshot_ok:
        gates_rejecting.append("plan_snapshot")
    return {
        "case_id": case.case_id,
        "category": case.category,
        "expected_valid": case.expected_valid,
        "accepted": accepted,
        "correct": accepted == case.expected_valid,
        "rejecting_gates": gates_rejecting,
        "profile_ok": not profile_errors,
        "profile_errors": profile_errors,
        "converter_ok": graph is not None,
        "converter_error": converter_error,
        "snapshot_attempted": snapshot_attempted,
        "snapshot_ok": snapshot_ok,
        "snapshot_reason": snapshot_reason,
        "latency_ms": {
            "task_profile": profile_ms,
            "task_graph_conversion": converter_ms,
            "plan_snapshot": snapshot_ms,
            "pipeline_total": total_ms,
        },
    }


def _git(command: list[str]) -> str:
    try:
        return subprocess.check_output(
            ["git", *command], cwd=REPO_ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:  # pragma: no cover - metadata only
        return "unavailable"


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timing-repeats", type=int, default=30)
    args = parser.parse_args()
    if args.timing_repeats < 1:
        parser.error("--timing-repeats must be at least 1")

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    cases = build_cases()
    catalog = [
        {
            "case_id": case.case_id,
            "category": case.category,
            "expected_valid": case.expected_valid,
            "task_profile": case.profile,
            "planning_steps": case.planning_steps,
            "approved_steps": case.approved_steps,
        }
        for case in cases
    ]
    _write_jsonl(output_dir / "case_catalog.jsonl", catalog)

    with tempfile.TemporaryDirectory(prefix="trusted-plan-benchmark-") as temp:
        snapshot_dir = Path(temp)
        detailed = [evaluate_case(case, snapshot_dir) for case in cases]
        timing_samples: list[dict[str, Any]] = []
        for repeat in range(args.timing_repeats):
            for case in cases:
                result = evaluate_case(case, snapshot_dir, run_suffix=f"-r{repeat:02d}")
                timing_samples.append(
                    {
                        "case_id": case.case_id,
                        "category": case.category,
                        "repeat": repeat,
                        **result["latency_ms"],
                    }
                )

    _write_jsonl(output_dir / "raw_results.jsonl", detailed)
    _write_jsonl(output_dir / "timing_samples.jsonl", timing_samples)

    valid = [row for row in detailed if row["expected_valid"]]
    invalid = [row for row in detailed if not row["expected_valid"]]
    true_accept = sum(row["accepted"] for row in valid)
    false_reject = len(valid) - true_accept
    true_reject = sum(not row["accepted"] for row in invalid)
    false_accept = len(invalid) - true_reject

    category_rows: list[dict[str, Any]] = []
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in detailed:
        grouped[row["category"]].append(row)
    for category, rows in grouped.items():
        expected_valid = rows[0]["expected_valid"]
        correct = sum(row["correct"] for row in rows)
        category_rows.append(
            {
                "category": category,
                "expected_valid": expected_valid,
                "cases": len(rows),
                "accepted": sum(row["accepted"] for row in rows),
                "rejected": sum(not row["accepted"] for row in rows),
                "correct": correct,
                "correct_rate": correct / len(rows),
                "task_profile_rejections": sum(not row["profile_ok"] for row in rows),
                "converter_rejections": sum(not row["converter_ok"] for row in rows),
                "snapshot_rejections": sum(
                    row["snapshot_attempted"] and not row["snapshot_ok"]
                    for row in rows
                ),
            }
        )

    latency_keys = (
        "task_profile",
        "task_graph_conversion",
        "plan_snapshot",
        "pipeline_total",
    )
    latency_summary: dict[str, dict[str, float | int]] = {}
    for key in latency_keys:
        values = [float(row[key]) for row in timing_samples]
        latency_summary[key] = {
            "n": len(values),
            "mean_ms": statistics.fmean(values),
            "p50_ms": _percentile(values, 0.50),
            "p95_ms": _percentile(values, 0.95),
            "max_ms": max(values),
        }

    gate_counts = Counter(
        gate for row in detailed for gate in row["rejecting_gates"]
    )
    summary = {
        "benchmark": "trusted_plan_validation",
        "scope": (
            "Deterministic TaskProfile, TaskGraph conversion, and PlanSnapshot "
            "validation; not LLM plan-generation accuracy."
        ),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "git_commit": _git(["rev-parse", "HEAD"]),
        "git_status_short": _git(["status", "--short"]),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "unique_cases": len(detailed),
        "valid_cases": len(valid),
        "invalid_cases": len(invalid),
        "invalid_categories": len(INVALID_CATEGORIES),
        "timing_repeats": args.timing_repeats,
        "timing_invocations": len(timing_samples),
        "confusion_matrix": {
            "true_accept": true_accept,
            "false_reject": false_reject,
            "true_reject": true_reject,
            "false_accept": false_accept,
        },
        "legal_plan_pass_rate": true_accept / len(valid),
        "legal_plan_false_reject_rate": false_reject / len(valid),
        "invalid_plan_rejection_rate": true_reject / len(invalid),
        "invalid_plan_leak_rate": false_accept / len(invalid),
        "overall_classification_accuracy": sum(row["correct"] for row in detailed)
        / len(detailed),
        "rejecting_gate_counts_nonexclusive": dict(gate_counts),
        "category_results": category_rows,
        "latency": latency_summary,
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with (output_dir / "summary.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["metric", "value"])
        for key in (
            "unique_cases",
            "valid_cases",
            "invalid_cases",
            "invalid_categories",
            "timing_invocations",
            "legal_plan_pass_rate",
            "legal_plan_false_reject_rate",
            "invalid_plan_rejection_rate",
            "invalid_plan_leak_rate",
            "overall_classification_accuracy",
        ):
            writer.writerow([key, summary[key]])
        for stage, stats in latency_summary.items():
            for stat, value in stats.items():
                writer.writerow([f"latency_{stage}_{stat}", value])

    with (output_dir / "category_summary.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=list(category_rows[0]))
        writer.writeheader()
        writer.writerows(category_rows)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if false_accept == 0 and false_reject == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
