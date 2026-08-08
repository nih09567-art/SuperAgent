"""Opt-in real Web/API E2E for the annual-leave three-Agent workflow.

These tests are intentionally separate from unit tests and from the older
hand-built TaskGraph integration test.  They require a configured LLM and are
skipped unless explicitly enabled.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from tests.integration.annual_leave_e2e_harness import (
    AnnualLeaveServiceManager,
    integration_prerequisite_reason,
    run_annual_leave_workflow,
    run_dynamic_five_agent_workflow,
)

pytestmark = pytest.mark.integration


def _require_real_http_e2e() -> None:
    reason = integration_prerequisite_reason()
    if reason:
        pytest.skip(reason)


def _run_dir() -> Path:
    root = (
        Path(__file__).resolve().parents[2] / "artifacts" / "demo-runs" / "annual-leave"
    )
    return root / uuid.uuid4().hex


def test_annual_leave_real_web_api_success():
    _require_real_http_e2e()
    run_dir = _run_dir()
    service_log_dir = run_dir.parent / f".service-logs-{run_dir.name}"
    with AnnualLeaveServiceManager(log_dir=service_log_dir) as services:
        result = run_annual_leave_workflow(
            services, run_dir=run_dir, scenario="success"
        )
    assert result["status"] == "SUCCEEDED"
    assert (run_dir / "final-report.md").exists()
    assert (run_dir / "export-manifest.json").exists()


def test_dynamic_five_agent_approval_resume_success():
    _require_real_http_e2e()
    run_dir = _run_dir()
    service_log_dir = run_dir.parent / f".service-logs-{run_dir.name}"
    with AnnualLeaveServiceManager(
        log_dir=service_log_dir,
        s_abac_enabled=True,
    ) as services:
        result = run_dynamic_five_agent_workflow(services, run_dir=run_dir)
    assert result["status"] == "SUCCEEDED"
    assert result["approval_id"]
    assert (run_dir / "approval.json").exists()
    assert (run_dir / "governance-events.json").exists()


def test_dynamic_five_agent_rejected_approval_does_not_send():
    _require_real_http_e2e()
    run_dir = _run_dir()
    service_log_dir = run_dir.parent / f".service-logs-{run_dir.name}"
    with AnnualLeaveServiceManager(
        log_dir=service_log_dir,
        s_abac_enabled=True,
    ) as services:
        result = run_dynamic_five_agent_workflow(
            services,
            run_dir=run_dir,
            approval_decision="reject",
        )
    assert result["status"] == "PARTIAL_FAILED"
    assert result["approval_id"]
    assert "email.dispatch.receipt" not in result["artifact_ids"]
    assert (run_dir / "approval.json").exists()
    assert (run_dir / "governance-events.json").exists()


@pytest.mark.parametrize(
    ("scenario", "fault_mode"),
    [
        ("policy_not_found", "knowledge_not_found"),
        ("knowledge_http_error", "knowledge_http_error"),
        ("knowledge_invalid_date", "knowledge_invalid_date"),
    ],
)
def test_annual_leave_real_web_api_failure_scenarios(scenario, fault_mode):
    _require_real_http_e2e()
    run_dir = _run_dir()
    service_log_dir = run_dir.parent / f".service-logs-{run_dir.name}"
    with AnnualLeaveServiceManager(
        log_dir=service_log_dir,
        fault_mode=fault_mode,
    ) as services:
        result = run_annual_leave_workflow(services, run_dir=run_dir, scenario=scenario)
    assert result["scenario"] == scenario
