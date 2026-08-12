"""Run one evaluation case through the existing real HTTP acceptance harness."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.integration.annual_leave_e2e_harness import (  # noqa: E402
    AnnualLeaveServiceManager,
    run_annual_leave_workflow,
    run_dynamic_five_agent_workflow,
)


CASE_CONFIG = {
    "ORCH-001": {"kind": "three", "scenario": "success", "fault": ""},
    "ORCH-002": {"kind": "three", "scenario": "success", "fault": ""},
    "ORCH-003": {
        "kind": "three",
        "scenario": "policy_not_found",
        "fault": "knowledge_not_found",
    },
    "ORCH-004": {
        "kind": "three",
        "scenario": "knowledge_http_error",
        "fault": "knowledge_http_error",
    },
    "ORCH-005": {
        "kind": "three",
        "scenario": "knowledge_invalid_date",
        "fault": "knowledge_invalid_date",
    },
    "ORCH-008": {"kind": "five", "approval": "approve"},
    "ORCH-009": {"kind": "five", "approval": "reject"},
    "ORCH-010": {"kind": "five", "approval": "approve"},
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("case_id", choices=tuple(CASE_CONFIG))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--query")
    args = parser.parse_args()

    if not os.getenv("RUN_ANNUAL_LEAVE_HTTP_E2E"):
        parser.error("RUN_ANNUAL_LEAVE_HTTP_E2E=1 is required")
    config = CASE_CONFIG[args.case_id]
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    run_dir = output_dir / "run"
    service_log_dir = output_dir / ".service-logs"

    try:
        with AnnualLeaveServiceManager(
            log_dir=service_log_dir,
            fault_mode=str(config.get("fault") or ""),
            s_abac_enabled=config["kind"] == "five",
        ) as services:
            if config["kind"] == "three":
                kwargs = {
                    "run_dir": run_dir,
                    "scenario": config["scenario"],
                }
                if args.query:
                    kwargs["query"] = args.query
                result = run_annual_leave_workflow(services, **kwargs)
            else:
                result = run_dynamic_five_agent_workflow(
                    services,
                    run_dir=run_dir,
                    approval_decision=config["approval"],
                )
        payload = {
            "case_id": args.case_id,
            "execution_status": "PASS",
            "harness_result": result,
        }
        exit_code = 0
    except Exception as exc:  # noqa: BLE001 - preserve a case-level failure
        payload = {
            "case_id": args.case_id,
            "execution_status": "INFRASTRUCTURE_ERROR",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        exit_code = 1

    (output_dir / "case-result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
