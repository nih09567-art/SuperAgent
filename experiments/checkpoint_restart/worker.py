from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any


CRASH_EXIT_CODE = 86


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _append_jsonl(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _latest_checkpoint(checkpoint_dir: Path, task_id: str) -> Path:
    files = list((checkpoint_dir / task_id).glob("*.json"))
    if not files:
        raise FileNotFoundError(f"no checkpoint for {task_id}")
    return max(files, key=lambda item: int(item.name.split("_", 1)[0]))


def _configure_stores(run_dir: Path) -> None:
    os.environ["ARTIFACT_PAYLOAD_STORE_DIR"] = str(run_dir / "artifacts")
    os.environ["RECEIPT_STORE_DIR"] = str(run_dir / "receipts")
    os.environ["TASK_CONTROL_STORE_DIR"] = str(run_dir / "task_controls")
    os.environ["RECONCILIATION_STORE_DIR"] = str(run_dir / "reconciliations")
    os.environ["GOVERNANCE_AUDIT_DIR"] = str(run_dir / "governance")


def _build_state(case: dict[str, Any], task_id: str) -> dict[str, Any]:
    from src.contracts.agent_contract import AgentContract, DataContractRef
    from src.interface.task_graph import TaskGraph, TaskSpec, TaskStep

    steps = []
    for item in case["steps"]:
        schema_ref = item.get("schema_ref")
        contract = (
            AgentContract(
                produces=[DataContractRef(name=item["output"], schema_ref=schema_ref)]
            )
            if schema_ref and item.get("output")
            else None
        )
        steps.append(
            TaskStep(
                step_id=item["step_id"],
                agent_name=item["agent"],
                preferred_resource_id=item["agent"],
                operation_mode=item.get("mode", "read"),
                external_side_effect=item.get("mode") == "send",
                depends_on=item.get("depends_on", []),
                expected_outputs=[item["output"]] if item.get("output") else [],
                expected_schema_refs=(
                    {item["output"]: schema_ref}
                    if schema_ref and item.get("output")
                    else {}
                ),
                agent_contract=contract,
                input_bindings=item.get("bind", []),
                behavior=item.get("behavior", "success"),
            )
        )
    return {
        "workflow_id": f"checkpoint-restart-{case['id'].lower()}",
        "task_id": task_id,
        "user_id": "admin",
        "messages": [{"role": "user", "content": case["name"]}],
        "task_graph": TaskGraph(
            spec=TaskSpec(task_id=task_id, subject="admin"),
            steps=steps,
        ),
    }


class CrashAfterDurableCheckpoint:
    def __init__(self, base_dir: Path, run_dir: Path, crash_when: dict[str, Any]):
        from src.robust.checkpoint import CheckpointManager

        self._delegate = CheckpointManager(base_dir)
        self.run_dir = run_dir
        self.crash_when = crash_when

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    def save_checkpoint(self, **kwargs):
        checkpoint_id = self._delegate.save_checkpoint(**kwargs)
        state = kwargs["state"]
        completed = set(state.get("completed_steps") or [])
        required = set(self.crash_when.get("completed_contains") or [])
        result_step = self.crash_when.get("step_result_present")
        should_crash = bool(required and required.issubset(completed)) or bool(
            result_step and result_step in (state.get("step_results") or {})
        )
        if should_crash:
            _write_json(
                self.run_dir / "crash-marker.json",
                {
                    "checkpoint_id": checkpoint_id,
                    "checkpoint_step": kwargs["step"],
                    "completed_steps": list(state.get("completed_steps") or []),
                    "step_result_ids": sorted((state.get("step_results") or {}).keys()),
                    "artifact_index": state.get("artifacts") or {},
                    "pid": os.getpid(),
                    "exit_mode": "os._exit",
                },
            )
            os._exit(CRASH_EXIT_CODE)
        return checkpoint_id


async def _execute_factory(case: dict[str, Any], run_dir: Path, task_id: str):
    step_cfg = {item["step_id"]: item for item in case["steps"]}

    async def execute(*, step, selected_agent, inputs, context):
        from src.manager.executor.base import ExecuteResult, ExecutionStatus

        cfg = step_cfg[step.step_id]
        behavior = cfg.get("behavior", "success")
        _append_jsonl(
            run_dir / "step-calls.jsonl",
            {
                "task_id": task_id,
                "pid": os.getpid(),
                "step_id": step.step_id,
                "behavior": behavior,
                "input_names": sorted(inputs.keys()),
                "at_ns": time.time_ns(),
            },
        )
        if behavior in {"confirmed_side_effect", "uncertain_side_effect"}:
            _append_jsonl(
                run_dir / "external-side-effects.jsonl",
                {
                    "task_id": task_id,
                    "pid": os.getpid(),
                    "step_id": step.step_id,
                    "idempotency_key": context.get("idempotency_key"),
                    "at_ns": time.time_ns(),
                },
            )
        if behavior == "confirmed_side_effect":
            return ExecuteResult(
                status=ExecutionStatus.SUCCESS,
                result={
                    "contract_version": "1.0",
                    "status": "success",
                    "outputs": {
                        "email.dispatch.receipt": {
                            "dispatch_mode": "simulated",
                            "provider_message_id": f"mail-{task_id}",
                            "status": "simulated",
                            "sent_at": "2026-08-12T00:00:00+08:00",
                            "approval_id": "checkpoint-eval-approved",
                            "idempotency_key": context.get("idempotency_key") or ""
                        }
                    },
                    "metadata": {"producer_agent": selected_agent, "schema_version": "1.0"},
                },
            )
        if behavior == "uncertain_side_effect":
            return ExecuteResult(
                status=ExecutionStatus.SUCCESS,
                result={"status": "sent", "sent": {"accepted": True}},
            )
        output_name = cfg.get("output")
        payload = {
            "case_id": case["id"],
            "step_id": step.step_id,
            "input_names": sorted(inputs.keys()),
        }
        return ExecuteResult(
            status=ExecutionStatus.SUCCESS,
            result=payload if output_name else {"ok": True},
        )

    return execute


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    from src.orchestration.providers import StubRoutingProvider
    from src.orchestration.runtime import run_scheduler_workflow
    from src.robust.checkpoint import CheckpointManager

    # Repository startup loads .env during imports. Re-apply task-local stores
    # afterwards so every subprocess uses only its own durable evidence tree.
    _configure_stores(args.run_dir)
    import src.service.env as service_env

    # This benchmark isolates Checkpoint recovery, not S-ABAC policy coverage.
    # Ownership is still stable (admin before/after restart), while disabling
    # the policy engine prevents local .env policy choices from changing the
    # frozen recovery dataset.
    service_env.S_ABAC_ENABLED = False

    dataset = json.loads(args.cases.read_text(encoding="utf-8"))
    case = next(item for item in dataset["cases"] if item["id"] == args.case_id)
    checkpoint_dir = args.run_dir / "checkpoints"
    if args.phase == "resume":
        checkpoint_file = _latest_checkpoint(checkpoint_dir, args.task_id)
        state = json.loads(checkpoint_file.read_text(encoding="utf-8"))["state"]
        checkpoint_manager = CheckpointManager(checkpoint_dir)
    else:
        state = _build_state(case, args.task_id)
        checkpoint_file = None
        checkpoint_manager = (
            CrashAfterDurableCheckpoint(checkpoint_dir, args.run_dir, case["crash_when"])
            if args.phase == "crash"
            else CheckpointManager(checkpoint_dir)
        )

    async def authorize_step(**_kwargs):
        return {"allowed": True, "decision": "ALLOW"}

    execute = await _execute_factory(case, args.run_dir, args.task_id)
    started = time.perf_counter_ns()
    events = [
        event
        async for event in run_scheduler_workflow(
            state,
            task_id=args.task_id,
            checkpoint_manager=checkpoint_manager,
            execute_step=execute,
            authorize_step=authorize_step,
            routing_provider=StubRoutingProvider(),
            retry_delay_seconds=0,
            redispatch_enabled=False,
        )
    ]
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    terminal = next(event["data"] for event in reversed(events) if event["event"] == "end_of_workflow")
    final_checkpoint = _latest_checkpoint(checkpoint_dir, args.task_id)
    final_state = json.loads(final_checkpoint.read_text(encoding="utf-8"))["state"]
    result = {
        "case_id": case["id"],
        "phase": args.phase,
        "pid": os.getpid(),
        "loaded_checkpoint": str(checkpoint_file) if checkpoint_file else None,
        "final_checkpoint": str(final_checkpoint),
        "terminal": terminal,
        "completed_steps": final_state.get("completed_steps") or [],
        "step_result_ids": sorted((final_state.get("step_results") or {}).keys()),
        "artifact_index": final_state.get("artifacts") or {},
        "elapsed_ms": elapsed_ms,
    }
    _write_json(args.run_dir / f"{args.phase}-result.json", result)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--phase", choices=("baseline", "crash", "resume"), required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.run_dir = args.run_dir.resolve()
    args.run_dir.mkdir(parents=True, exist_ok=True)
    _configure_stores(args.run_dir)
    result = asyncio.run(_run(args))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
