from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES = Path(__file__).with_name("cases.json")
WORKER = Path(__file__).with_name("worker.py")
CRASH_EXIT_CODE = 86


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _artifact_files(run_dir: Path) -> dict[str, str]:
    root = run_dir / "artifacts"
    return {
        str(path.relative_to(root)): _sha256(path)
        for path in sorted(root.rglob("*.json"))
    } if root.exists() else {}


def _run_worker(*, cases: Path, case_id: str, task_id: str, run_dir: Path, phase: str, timeout: int) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        str(WORKER),
        "--cases", str(cases),
        "--case-id", case_id,
        "--task-id", task_id,
        "--run-dir", str(run_dir),
        "--phase", phase,
    ]
    return subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _expected_calls(case: dict[str, Any]) -> dict[str, int]:
    if case["expected_terminal"] == "NEEDS_RECONCILIATION":
        return {
            step["step_id"]: (0 if step["step_id"] == "archive" else 1)
            for step in case["steps"]
        }
    return {step["step_id"]: 1 for step in case["steps"]}


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _markdown(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    lines = [
        "# Cross-process Checkpoint recovery benchmark",
        "",
        f"- Frozen dataset SHA-256: `{report['dataset_sha256']}`",
        f"- Cases: {report['case_count']}; interrupted trials: {report['trial_count']}",
        "- Interruption method: `os._exit(86)` immediately after the target checkpoint was durably written; resume occurs in a fresh Python process.",
        "",
        "| Metric | Result |",
        "| --- | ---: |",
        f"| Checkpoint load success | {metrics['checkpoint_load_success']}/{report['trial_count']} |",
        f"| Fresh resume process verified | {metrics['fresh_process_success']}/{report['trial_count']} |",
        f"| Gold behavior conformance | {metrics['gold_conformant']}/{report['trial_count']} |",
        f"| Completed-step re-executions | {metrics['completed_step_reexecution_count']} |",
        f"| Artifact files unchanged across restart | {metrics['artifact_unchanged']}/{metrics['artifact_checked']} |",
        f"| Duplicate external side effects | {metrics['duplicate_side_effect_count']} |",
        f"| Confirmed side-effect trials without duplicate | {metrics['confirmed_side_effect_safe']}/{metrics['confirmed_side_effect_trials']} |",
        f"| Uncertain side-effect trials reconciled without resend | {metrics['uncertain_reconciled']}/{metrics['uncertain_trials']} |",
        f"| Resume wall time mean / P95 | {metrics['resume_wall_mean_ms']:.3f} / {metrics['resume_wall_p95_ms']:.3f} ms |",
        "",
        "## Boundary",
        "",
        "The Scheduler, CheckpointManager, protected Artifact payload store, persistent receipt store and reconciliation logic are real project code. Agent/tool results are deterministic test doubles; the process crash is real, but no production remote Agent or external email service is invoked.",
        "",
    ]
    return "\n".join(lines)


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((0.95 * len(ordered)) + 0.999999) - 1))
    return ordered[index]


def run(args: argparse.Namespace) -> dict[str, Any]:
    dataset = _read_json(args.cases)
    cases = dataset.get("cases") or []
    if len(cases) != 6 or len({case["id"] for case in cases}) != 6:
        raise ValueError("checkpoint dataset must contain exactly six unique cases")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    baselines: dict[str, dict[str, Any]] = {}
    for case in cases:
        baseline_dir = args.output_dir / "baseline" / case["id"]
        result = _run_worker(
            cases=args.cases,
            case_id=case["id"],
            task_id=f"{case['id'].lower()}-baseline",
            run_dir=baseline_dir,
            phase="baseline",
            timeout=args.timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(f"baseline {case['id']} failed: {result.stderr or result.stdout}")
        baselines[case["id"]] = _read_json(baseline_dir / "baseline-result.json")

    rows: list[dict[str, Any]] = []
    for case in cases:
        expected_calls = _expected_calls(case)
        crash_required = set(case["crash_when"].get("completed_contains") or [])
        for repeat in range(1, args.repeats + 1):
            trial_id = f"{case['id']}-r{repeat:02d}"
            task_id = trial_id.lower()
            trial_dir = args.output_dir / "trials" / trial_id
            crashed = _run_worker(
                cases=args.cases,
                case_id=case["id"],
                task_id=task_id,
                run_dir=trial_dir,
                phase="crash",
                timeout=args.timeout,
            )
            marker_exists = (trial_dir / "crash-marker.json").exists()
            crash_ok = crashed.returncode == CRASH_EXIT_CODE and marker_exists
            marker = _read_json(trial_dir / "crash-marker.json") if marker_exists else {}
            before_hashes = _artifact_files(trial_dir)
            resumed = _run_worker(
                cases=args.cases,
                case_id=case["id"],
                task_id=task_id,
                run_dir=trial_dir,
                phase="resume",
                timeout=args.timeout,
            ) if crash_ok else None
            resume_ok = bool(resumed and resumed.returncode == 0 and (trial_dir / "resume-result.json").exists())
            resume_result = _read_json(trial_dir / "resume-result.json") if resume_ok else {}
            after_hashes = _artifact_files(trial_dir)
            calls = Counter(item["step_id"] for item in _read_jsonl(trial_dir / "step-calls.jsonl"))
            side_effects = _read_jsonl(trial_dir / "external-side-effects.jsonl")
            side_effect_counts = Counter((item["step_id"], item.get("idempotency_key")) for item in side_effects)
            duplicate_effects = sum(max(0, count - 1) for count in side_effect_counts.values())
            precrash_steps = set(marker.get("completed_steps") or [])
            reexecutions = sum(max(0, calls[step] - 1) for step in precrash_steps)
            artifact_checked = len(before_hashes)
            artifact_unchanged = sum(
                1 for path, digest in before_hashes.items() if after_hashes.get(path) == digest
            )
            terminal = (resume_result.get("terminal") or {}).get("status")
            baseline_terminal = (
                baselines[case["id"]].get("terminal") or {}
            ).get("status")
            fresh_process = bool(
                marker.get("pid")
                and resume_result.get("pid")
                and marker["pid"] != resume_result["pid"]
            )
            completed = set(resume_result.get("completed_steps") or [])
            expected_completed = {
                step["step_id"] for step in case["steps"]
                if not (case["expected_terminal"] == "NEEDS_RECONCILIATION" and step["step_id"] in {"send", "archive"})
            }
            calls_match = all(calls[step_id] == count for step_id, count in expected_calls.items())
            gold = all(
                [
                    crash_ok,
                    resume_ok,
                    bool(resume_result.get("loaded_checkpoint")),
                    fresh_process,
                    terminal == case["expected_terminal"],
                    terminal == baseline_terminal,
                    expected_completed.issubset(completed),
                    calls_match,
                    reexecutions == 0,
                    artifact_unchanged == artifact_checked,
                    duplicate_effects == 0,
                ]
            )
            rows.append(
                {
                    "trial_id": trial_id,
                    "case_id": case["id"],
                    "repeat": repeat,
                    "crash_exit_code": crashed.returncode,
                    "crash_marker": marker_exists,
                    "crash_pid": marker.get("pid"),
                    "resume_pid": resume_result.get("pid"),
                    "fresh_process": fresh_process,
                    "checkpoint_loaded": bool(resume_result.get("loaded_checkpoint")),
                    "terminal": terminal,
                    "expected_terminal": case["expected_terminal"],
                    "baseline_terminal": baseline_terminal,
                    "precrash_completed": json.dumps(sorted(precrash_steps), ensure_ascii=False),
                    "final_completed": json.dumps(sorted(completed), ensure_ascii=False),
                    "step_calls": json.dumps(dict(sorted(calls.items())), ensure_ascii=False),
                    "completed_step_reexecutions": reexecutions,
                    "artifact_checked": artifact_checked,
                    "artifact_unchanged": artifact_unchanged,
                    "external_side_effect_calls": len(side_effects),
                    "duplicate_side_effects": duplicate_effects,
                    "resume_wall_ms": float(resume_result.get("elapsed_ms") or 0.0),
                    "gold_conformant": gold,
                }
            )

    resume_times = [row["resume_wall_ms"] for row in rows if row["checkpoint_loaded"]]
    confirmed_rows = [row for row in rows if row["case_id"] in {"CP-04", "CP-05"}]
    uncertain_rows = [row for row in rows if row["case_id"] == "CP-06"]
    metrics = {
        "checkpoint_load_success": sum(row["checkpoint_loaded"] for row in rows),
        "fresh_process_success": sum(row["fresh_process"] for row in rows),
        "gold_conformant": sum(row["gold_conformant"] for row in rows),
        "completed_step_reexecution_count": sum(row["completed_step_reexecutions"] for row in rows),
        "artifact_checked": sum(row["artifact_checked"] for row in rows),
        "artifact_unchanged": sum(row["artifact_unchanged"] for row in rows),
        "duplicate_side_effect_count": sum(row["duplicate_side_effects"] for row in rows),
        "confirmed_side_effect_trials": len(confirmed_rows),
        "confirmed_side_effect_safe": sum(row["external_side_effect_calls"] == 1 and row["duplicate_side_effects"] == 0 for row in confirmed_rows),
        "uncertain_trials": len(uncertain_rows),
        "uncertain_reconciled": sum(row["terminal"] == "NEEDS_RECONCILIATION" and row["external_side_effect_calls"] == 1 for row in uncertain_rows),
        "resume_wall_mean_ms": statistics.mean(resume_times) if resume_times else 0.0,
        "resume_wall_p95_ms": _p95(resume_times),
    }
    report = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "dataset_id": dataset["dataset_id"],
        "dataset_sha256": _sha256(args.cases),
        "case_count": len(cases),
        "repeats": args.repeats,
        "trial_count": len(rows),
        "metrics": metrics,
        "boundaries": {
            "real": "fresh OS process, CheckpointManager, Scheduler, Artifact payload persistence, receipt and reconciliation stores",
            "controlled": "deterministic Agent/tool test doubles and checkpoint-boundary crash injection",
        },
    }
    _write_csv(args.output_dir / "trials.csv", rows)
    (args.output_dir / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.output_dir / "summary.md").write_text(_markdown(report), encoding="utf-8")
    (args.output_dir / "cases.snapshot.json").write_bytes(args.cases.read_bytes())
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=30)
    return parser.parse_args()


def main() -> None:
    report = run(parse_args())
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
