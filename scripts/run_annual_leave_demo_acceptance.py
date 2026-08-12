"""Run dynamic annual-leave defense scenarios repeatedly over real HTTP."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from tests.integration.annual_leave_e2e_harness import (
    AnnualLeaveE2EError,
    AnnualLeaveServiceManager,
    integration_prerequisite_reason,
    redact_evidence,
    run_annual_leave_workflow,
    run_dynamic_five_agent_workflow,
)


SCENARIOS = (
    "dynamic-three",
    "dynamic-five-approved",
    "dynamic-five-rejected",
)


def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass


def _run_scenario(
    services,
    *,
    run_dir: Path,
    scenario: str,
    query: str | None = None,
) -> dict:
    if scenario == "dynamic-three":
        return run_annual_leave_workflow(
            services,
            run_dir=run_dir,
            scenario="success",
            **({"query": query} if query else {}),
        )
    if query:
        raise ValueError("--query is currently supported only for dynamic-three")
    if scenario == "dynamic-five-approved":
        return run_dynamic_five_agent_workflow(
            services,
            run_dir=run_dir,
            approval_decision="approve",
        )
    if scenario == "dynamic-five-rejected":
        return run_dynamic_five_agent_workflow(
            services,
            run_dir=run_dir,
            approval_decision="reject",
        )
    raise ValueError(f"unsupported acceptance scenario: {scenario}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument(
        "--scenario",
        choices=SCENARIOS,
        default="dynamic-three",
        help=(
            "dynamic-three runs HR + Knowledge -> Report; "
            "dynamic-five-approved/rejected run "
            "HR + Knowledge -> Office -> Report -> Approval -> Email"
        ),
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("artifacts/demo-runs/annual-leave"),
    )
    parser.add_argument(
        "--query",
        help="Override the natural-language request for dynamic-three evaluations.",
    )
    args = parser.parse_args()
    _load_env()

    if args.runs <= 0:
        print("--runs must be positive", file=sys.stderr)
        return 2
    if not os.getenv("RUN_ANNUAL_LEAVE_HTTP_E2E"):
        print(
            "Set RUN_ANNUAL_LEAVE_HTTP_E2E=1 to explicitly authorize the real demo run.",
            file=sys.stderr,
        )
        return 2
    reason = integration_prerequisite_reason()
    if reason and "RUN_ANNUAL_LEAVE_HTTP_E2E" not in reason:
        print(reason, file=sys.stderr)
        return 2

    project_root = Path(__file__).resolve().parents[1]
    output_root = (project_root / args.output_root).resolve() if not args.output_root.is_absolute() else args.output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    batch_id = uuid.uuid4().hex
    batch_root = output_root / f"acceptance-{args.scenario}-{batch_id}"
    batch_root.mkdir(parents=True, exist_ok=False)
    service_log_dir = batch_root / ".service-logs"
    results: list[dict] = []
    passed = 0

    try:
        # One service lifetime covers every independent workflow run.
        with AnnualLeaveServiceManager(
            project_root=project_root,
            log_dir=service_log_dir,
            s_abac_enabled=args.scenario.startswith("dynamic-five"),
        ) as services:
            for index in range(args.runs):
                run_dir = batch_root / uuid.uuid4().hex
                try:
                    result = _run_scenario(
                        services,
                        run_dir=run_dir,
                        scenario=args.scenario,
                        query=args.query,
                    )
                    results.append(result)
                    passed += 1
                    print(
                        f"[annual-leave:{args.scenario}] "
                        f"run {index + 1}/{args.runs} passed: {run_dir.name}"
                    )
                except Exception as exc:  # noqa: BLE001 - retain evidence and fail at the end
                    run_dir.mkdir(parents=True, exist_ok=True)
                    (run_dir / "run-error.json").write_text(
                        json.dumps(
                            redact_evidence(
                                {
                                    "error_type": type(exc).__name__,
                                    "error": str(exc),
                                }
                            ),
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    services.log_snapshot(run_dir / "service-logs")
                    print(
                        f"[annual-leave:{args.scenario}] "
                        f"run {index + 1}/{args.runs} failed: {run_dir.name}",
                        file=sys.stderr,
                    )
                    results.append({"run_id": run_dir.name, "status": "FAILED", "error": str(exc)})
                    # The service lifetime remains active so the operator can
                    # inspect the next run only when explicitly requested; a
                    # failed acceptance never turns into a false 5/5 result.
                    break
    except (AnnualLeaveE2EError, OSError) as exc:
        print(f"Annual leave demo acceptance blocked: {exc}", file=sys.stderr)
        return 1

    summary = {
        "batch_id": batch_id,
        "scenario": args.scenario,
        "requested": args.runs,
        "passed": passed,
        "results": results,
    }
    (batch_root / "acceptance-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"Annual leave demo acceptance ({args.scenario}): "
        f"{passed}/{args.runs} passed"
    )
    print(f"Evidence batch: {batch_root}")
    return 0 if passed == args.runs else 1


if __name__ == "__main__":
    raise SystemExit(main())
