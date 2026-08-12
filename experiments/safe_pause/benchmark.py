from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
import statistics
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from src.interface.task_graph import TaskGraph, TaskSpec, TaskStep
from src.manager.executor.base import ExecuteResult, ExecutionStatus
from src.orchestration.providers import StubRoutingProvider
from src.orchestration.runtime import run_scheduler_workflow
from src.robust.checkpoint import CheckpointManager
from src.robust.task_control import TaskControlStore


HERE = Path(__file__).resolve().parent
CASES_PATH = HERE / "cases.json"
STORE_ENV = {
    "ARTIFACT_PAYLOAD_STORE_DIR": "artifacts",
    "RECEIPT_STORE_DIR": "receipts",
    "TASK_CONTROL_STORE_DIR": "task_controls",
    "GOVERNANCE_EVENT_STORE_DIR": "governance",
    "RECONCILIATION_STORE_DIR": "reconciliation",
}


def _load_cases() -> tuple[dict[str, Any], str]:
    raw = CASES_PATH.read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest()


@contextmanager
def _isolated_stores(root: Path) -> Iterator[None]:
    import src.service.env as service_env

    previous = {name: os.environ.get(name) for name in STORE_ENV}
    previous_s_abac = service_env.S_ABAC_ENABLED
    try:
        # Authorization is a separate research dimension. Freeze it off here so
        # a developer's local .env cannot prevent the pause mechanism from
        # reaching its first safe point.
        service_env.S_ABAC_ENABLED = False
        for name, child in STORE_ENV.items():
            path = root / child
            path.mkdir(parents=True, exist_ok=True)
            os.environ[name] = str(path)
        yield
    finally:
        service_env.S_ABAC_ENABLED = previous_s_abac
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _graph(task_id: str, scenario_id: str) -> TaskGraph:
    if scenario_id == "serial_safe_point":
        steps = [
            TaskStep(step_id="collect", preferred_resource_id="CollectAgent"),
            TaskStep(
                step_id="analyze",
                depends_on=["collect"],
                preferred_resource_id="AnalysisAgent",
            ),
            TaskStep(
                step_id="report",
                depends_on=["analyze"],
                preferred_resource_id="ReportAgent",
            ),
        ]
    elif scenario_id == "parallel_batch_safe_point":
        steps = [
            TaskStep(step_id="hr_read", preferred_resource_id="HRAgent"),
            TaskStep(
                step_id="knowledge_read",
                preferred_resource_id="KnowledgeAgent",
            ),
            TaskStep(
                step_id="report",
                depends_on=["hr_read", "knowledge_read"],
                preferred_resource_id="ReportAgent",
            ),
        ]
    elif scenario_id == "side_effect_before_pause":
        steps = [
            TaskStep(step_id="prepare", preferred_resource_id="PrepareAgent"),
            TaskStep(
                step_id="send_notice",
                depends_on=["prepare"],
                operation_mode="write",
                resource_locks=["mailbox"],
                preferred_resource_id="MessagingAgent",
            ),
        ]
    else:
        raise ValueError(f"unknown scenario: {scenario_id}")
    return TaskGraph(spec=TaskSpec(task_id=task_id), steps=steps).validate_dag()


async def _collect(
    state: dict[str, Any],
    *,
    task_id: str,
    checkpoints: CheckpointManager,
    execute_step: Any,
) -> list[dict[str, Any]]:
    return [
        event
        async for event in run_scheduler_workflow(
            state,
            task_id=task_id,
            checkpoint_manager=checkpoints,
            execute_step=execute_step,
            routing_provider=StubRoutingProvider(),
        )
    ]


def _terminal(events: list[dict[str, Any]]) -> dict[str, Any]:
    return next(event["data"] for event in events if event["event"] == "end_of_workflow")


async def _run_trial(
    scenario: dict[str, Any], round_no: int, trial_root: Path
) -> dict[str, Any]:
    scenario_id = str(scenario["scenario_id"])
    task_id = f"safe-pause-{scenario_id}-r{round_no}"
    workflow_id = f"wf-{scenario_id}"
    user_id = "benchmark-user"
    checkpoints = CheckpointManager(trial_root / "checkpoints")
    # Use the same environment-selected store as run_scheduler_workflow so the
    # executor's pause request is visible to the runtime control plane.
    control = TaskControlStore()
    calls: list[str] = []
    active = 0
    peak_concurrency = 0
    side_effect_invocations = 0

    async def execute(*, step: TaskStep, selected_agent: Any, inputs: Any, context: Any):
        nonlocal active, peak_concurrency, side_effect_invocations
        calls.append(step.step_id)
        active += 1
        peak_concurrency = max(peak_concurrency, active)
        try:
            if step.step_id == scenario["pause_during"]:
                control.request_pause(
                    task_id,
                    workflow_id=workflow_id,
                    user_id=user_id,
                    reason="benchmark_active_pause",
                )
            if scenario_id == "parallel_batch_safe_point" and step.step_id in {
                "hr_read",
                "knowledge_read",
            }:
                # Both reads overlap, while staggered completion makes the
                # persisted batch snapshot deterministic for this small eval.
                await asyncio.sleep(0.015 if step.step_id == "hr_read" else 0.035)
            else:
                await asyncio.sleep(0)
            metadata: dict[str, Any] = {}
            if step.step_id == "send_notice":
                side_effect_invocations += 1
                metadata["external_op_id"] = f"notice-{task_id}-{side_effect_invocations}"
            return ExecuteResult(
                status=ExecutionStatus.SUCCESS,
                # The notification contract needs only its durable provider
                # receipt. Avoid inventing an output Artifact unrelated to the
                # side-effect idempotency assertion.
                result=None if step.step_id == "send_notice" else {"ok": step.step_id},
                metadata=metadata,
            )
        finally:
            active -= 1

    state = {
        "workflow_id": workflow_id,
        "user_id": user_id,
        "task_graph": _graph(task_id, scenario_id),
        "messages": [{"role": "user", "content": f"benchmark {scenario_id}"}],
    }

    pause_started = time.perf_counter()
    pause_events = await _collect(
        state, task_id=task_id, checkpoints=checkpoints, execute_step=execute
    )
    pause_ms = (time.perf_counter() - pause_started) * 1000
    pause_terminal = _terminal(pause_events)
    calls_at_pause = list(calls)
    control_at_pause = control.get(task_id) or {}
    completed_at_pause = sorted(control_at_pause.get("completed_steps") or [])
    checkpoint_step = int(control_at_pause.get("checkpoint_step", -1))
    checkpoint = checkpoints.load_checkpoint(task_id=task_id, step=checkpoint_step)

    control.resume(task_id, user_id=user_id)
    resume_started = time.perf_counter()
    resume_events = await _collect(
        dict(checkpoint.state),
        task_id=task_id,
        checkpoints=checkpoints,
        execute_step=execute,
    )
    resume_ms = (time.perf_counter() - resume_started) * 1000
    resume_terminal = _terminal(resume_events)
    calls_after_resume = list(calls[len(calls_at_pause) :])

    replay_status = "NOT_APPLICABLE"
    receipt_reused = False
    replay_automatic_resend_blocked = False
    if scenario.get("replay_same_checkpoint"):
        calls_before_replay = len(calls)
        replay_state = dict(checkpoint.state)
        replay_events = await _collect(
            replay_state,
            task_id=task_id,
            checkpoints=checkpoints,
            execute_step=execute,
        )
        replay_terminal = _terminal(replay_events)
        replay_status = str(replay_terminal.get("status"))
        replay_step = (replay_state.get("step_results") or {}).get("send_notice") or {}
        replay_metrics = replay_step.get("metrics") or {}
        receipt_reused = bool(replay_metrics.get("idempotent_reuse")) and (
            len(calls) == calls_before_replay
        )
        replay_automatic_resend_blocked = len(calls) == calls_before_replay

    expected_completed = sorted(scenario["expected_completed_at_pause"])
    expected_not_started = set(scenario["expected_not_started_at_pause"])
    completed_step_duplicates = sum(
        max(0, calls.count(step_id) - 1) for step_id in expected_completed
    )
    next_batch_blocked = not expected_not_started.intersection(calls_at_pause)
    parallel_batch_completed = (
        scenario_id != "parallel_batch_safe_point"
        or set(expected_completed).issubset(calls_at_pause)
    )
    parallelism_observed = (
        scenario_id != "parallel_batch_safe_point" or peak_concurrency >= 2
    )
    expected_side_effects = int(scenario.get("expected_side_effect_invocations", 0))
    side_effect_ok = (
        expected_side_effects == 0
        or (
            side_effect_invocations == expected_side_effects
            and replay_automatic_resend_blocked
            and replay_status in {"SUCCEEDED", "NEEDS_RECONCILIATION"}
        )
    )
    paused_at_safe_point = (
        pause_terminal.get("status") == "PAUSED"
        and checkpoint is not None
        and completed_at_pause == expected_completed
        and next_batch_blocked
    )
    checks = {
        "pause_entered": pause_terminal.get("status") == "PAUSED",
        "paused_at_safe_point": paused_at_safe_point,
        "checkpoint_loaded": checkpoint is not None,
        "completed_at_pause_matches": completed_at_pause == expected_completed,
        "next_batch_blocked": next_batch_blocked,
        "resume_completed": resume_terminal.get("status") == "SUCCEEDED",
        "completed_steps_not_reexecuted": completed_step_duplicates == 0,
        "parallel_batch_completed": parallel_batch_completed,
        "parallelism_observed": parallelism_observed,
        "side_effect_safe": side_effect_ok,
    }
    return {
        "case_id": f"{scenario_id}-r{round_no}",
        "scenario": scenario_id,
        "round": round_no,
        "pause_status": pause_terminal.get("status"),
        "paused_at_safe_point": paused_at_safe_point,
        "completed_at_pause": completed_at_pause,
        "calls_at_pause": calls_at_pause,
        "next_batch_blocked": next_batch_blocked,
        "checkpoint_loaded": checkpoint is not None,
        "resume_status": resume_terminal.get("status"),
        "calls_after_resume": calls_after_resume,
        "completed_step_duplicate_invocations": completed_step_duplicates,
        "peak_concurrency": peak_concurrency,
        "side_effect_invocations": side_effect_invocations,
        "duplicate_side_effects": max(0, side_effect_invocations - expected_side_effects),
        "same_checkpoint_replay_status": replay_status,
        "receipt_reused_on_replay": receipt_reused,
        "replay_automatic_resend_blocked": replay_automatic_resend_blocked,
        "pause_wall_ms": round(pause_ms, 3),
        "resume_wall_ms": round(resume_ms, 3),
        "checks": checks,
        "conformant": all(checks.values()),
    }


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = (len(ordered) - 1) * percentile
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _write_outputs(
    output_dir: Path, trials: list[dict[str, Any]], dataset_sha256: str
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    pause_times = [float(row["pause_wall_ms"]) for row in trials]
    resume_times = [float(row["resume_wall_ms"]) for row in trials]
    side_effect_trials = [row for row in trials if row["scenario"] == "side_effect_before_pause"]
    parallel_trials = [row for row in trials if row["scenario"] == "parallel_batch_safe_point"]
    summary = {
        "dataset_sha256": dataset_sha256,
        "trial_count": len(trials),
        "scenario_count": len({row["scenario"] for row in trials}),
        "conformant": sum(bool(row["conformant"]) for row in trials),
        "pause_entered": sum(row["pause_status"] == "PAUSED" for row in trials),
        "safe_point_pause": sum(bool(row["paused_at_safe_point"]) for row in trials),
        "checkpoint_loaded": sum(bool(row["checkpoint_loaded"]) for row in trials),
        "resume_completed": sum(row["resume_status"] == "SUCCEEDED" for row in trials),
        "next_batch_blocked": sum(bool(row["next_batch_blocked"]) for row in trials),
        "completed_step_duplicate_invocations": sum(
            int(row["completed_step_duplicate_invocations"]) for row in trials
        ),
        "parallel_trials_with_concurrency": sum(
            int(row["peak_concurrency"]) >= 2 for row in parallel_trials
        ),
        "parallel_trial_count": len(parallel_trials),
        "side_effect_invocations": sum(int(row["side_effect_invocations"]) for row in side_effect_trials),
        "duplicate_side_effects": sum(int(row["duplicate_side_effects"]) for row in side_effect_trials),
        "receipt_reused_on_same_checkpoint_replay": sum(
            bool(row["receipt_reused_on_replay"]) for row in side_effect_trials
        ),
        "automatic_resend_blocked_on_same_checkpoint_replay": sum(
            bool(row["replay_automatic_resend_blocked"]) for row in side_effect_trials
        ),
        "side_effect_trial_count": len(side_effect_trials),
        "pause_wall_ms_mean": round(statistics.fmean(pause_times), 3),
        "pause_wall_ms_p95": round(_percentile(pause_times, 0.95), 3),
        "resume_wall_ms_mean": round(statistics.fmean(resume_times), 3),
        "resume_wall_ms_p95": round(_percentile(resume_times, 0.95), 3),
        "evidence_boundary": (
            "Deterministic in-process executor with production Scheduler, persistent "
            "Checkpoint, TaskControlStore and ReceiptStore; not a real HTTP or cross-process test."
        ),
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    with (output_dir / "trials.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        fields = [key for key in trials[0] if key != "checks"]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in trials:
            writer.writerow(
                {
                    key: json.dumps(value, ensure_ascii=False)
                    if isinstance(value, (list, dict))
                    else value
                    for key, value in row.items()
                    if key != "checks"
                }
            )
    with (output_dir / "summary.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["metric", "value"])
        writer.writerows(summary.items())
    markdown = f"""# 主动安全暂停与恢复实验\n\n- 场景与轮次：3 类 × 2 轮 = {summary['trial_count']} 次\n- 金标准行为符合：{summary['conformant']}/{summary['trial_count']}\n- 进入安全暂停：{summary['safe_point_pause']}/{summary['trial_count']}\n- 检查点成功载入：{summary['checkpoint_loaded']}/{summary['trial_count']}\n- 恢复后完成：{summary['resume_completed']}/{summary['trial_count']}\n- 暂停期间下一批次未启动：{summary['next_batch_blocked']}/{summary['trial_count']}\n- 已完成步骤重复执行：{summary['completed_step_duplicate_invocations']} 次\n- 并行批次确有重叠：{summary['parallel_trials_with_concurrency']}/{summary['parallel_trial_count']}\n- 副作用调用：{summary['side_effect_invocations']} 次；重复副作用：{summary['duplicate_side_effects']} 次\n- 同一检查点重放时自动重发阻断：{summary['automatic_resend_blocked_on_same_checkpoint_replay']}/{summary['side_effect_trial_count']}\n- 同一检查点重放时直接复用 Receipt：{summary['receipt_reused_on_same_checkpoint_replay']}/{summary['side_effect_trial_count']}（未复用时安全进入人工对账）\n- 暂停墙钟耗时：平均 {summary['pause_wall_ms_mean']} ms，P95 {summary['pause_wall_ms_p95']} ms\n- 恢复墙钟耗时：平均 {summary['resume_wall_ms_mean']} ms，P95 {summary['resume_wall_ms_p95']} ms\n\n边界：{summary['evidence_boundary']}\n"""
    (output_dir / "summary.md").write_text(markdown, encoding="utf-8")
    manifest = {
        "dataset": str(CASES_PATH),
        "dataset_sha256": dataset_sha256,
        "files": ["trials.csv", "summary.csv", "summary.json", "summary.md"],
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return summary


async def run(output_dir: Path) -> dict[str, Any]:
    dataset, dataset_sha256 = _load_cases()
    trials: list[dict[str, Any]] = []
    work_root = output_dir / "trial-state"
    with _isolated_stores(work_root):
        for scenario in dataset["scenarios"]:
            for round_no in range(1, int(dataset["rounds_per_scenario"]) + 1):
                trial_root = work_root / f"{scenario['scenario_id']}-r{round_no}"
                trial_root.mkdir(parents=True, exist_ok=True)
                trials.append(await _run_trial(scenario, round_no, trial_root))
    return _write_outputs(output_dir, trials, dataset_sha256)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    summary = asyncio.run(run(args.output_dir.resolve()))
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if summary["conformant"] == summary["trial_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
