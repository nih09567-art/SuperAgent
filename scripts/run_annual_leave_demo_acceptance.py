"""Run the annual-leave defense success scenario five times over real HTTP."""

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
)


def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("artifacts/demo-runs/annual-leave"),
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
    service_log_dir = output_root / f".service-logs-{uuid.uuid4().hex}"
    results: list[dict] = []
    passed = 0

    try:
        # One service lifetime covers every independent workflow run.
        with AnnualLeaveServiceManager(
            project_root=project_root,
            log_dir=service_log_dir,
        ) as services:
            for index in range(args.runs):
                run_dir = output_root / uuid.uuid4().hex
                try:
                    result = run_annual_leave_workflow(
                        services,
                        run_dir=run_dir,
                        scenario="success",
                    )
                    results.append(result)
                    passed += 1
                    print(f"[annual-leave] run {index + 1}/{args.runs} passed: {run_dir.name}")
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
                    print(f"[annual-leave] run {index + 1}/{args.runs} failed: {run_dir.name}", file=sys.stderr)
                    results.append({"run_id": run_dir.name, "status": "FAILED", "error": str(exc)})
                    # The service lifetime remains active so the operator can
                    # inspect the next run only when explicitly requested; a
                    # failed acceptance never turns into a false 5/5 result.
                    break
    except (AnnualLeaveE2EError, OSError) as exc:
        print(f"Annual leave demo acceptance blocked: {exc}", file=sys.stderr)
        return 1

    summary = {
        "requested": args.runs,
        "passed": passed,
        "results": results,
    }
    (output_root / "acceptance-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Annual leave demo acceptance: {passed}/{args.runs} passed")
    return 0 if passed == args.runs else 1


if __name__ == "__main__":
    raise SystemExit(main())
