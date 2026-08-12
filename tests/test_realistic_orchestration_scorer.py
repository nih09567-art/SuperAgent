from __future__ import annotations

import json
from pathlib import Path

from experiments.quantitative.scoring.score_orchestration_eval import (
    CONFORMANT,
    NO_EVIDENCE,
    NONCONFORMANT,
    EXPECTED_DATASET_SHA256,
    read_junit,
    score,
)


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "tests" / "evaluations" / "multi_agent_orchestration_eval.json"
FALLBACK = ROOT / ".artifacts" / "orchestration-eval" / "orch-013-020-junit.xml"


def test_current_dataset_is_valid_and_has_frozen_dimensions(tmp_path: Path) -> None:
    summary, rows = score(DATASET, tmp_path / "missing", None, EXPECTED_DATASET_SHA256)

    assert summary["dataset_validation"]["status"] == "VALID"
    assert summary["dataset_validation"]["evaluation_mode_counts"] == {
        "candidate_plan_validation": 2,
        "controlled_fault_e2e": 8,
        "deterministic_composite_harness": 3,
        "natural_language_e2e": 7,
    }
    assert {name: value["denominator_unique_cases"] for name, value in summary["dimensions"].items()} == {
        "task_profile_and_clarification": 10,
        "planner_generated_taskgraph": 5,
        "deterministic_composite": 3,
        "invalid_candidate_plan": 2,
        "actual_multi_agent_execution": 8,
        "clarification_block": 2,
        "controlled_fault": 8,
        "overall_gold_behavior": 20,
    }
    assert len(rows) == 20
    assert {row["conformance_status"] for row in rows} == {NO_EVIDENCE}


def test_existing_junit_counts_xfail_as_nonconformant() -> None:
    observations = read_junit(FALLBACK)
    by_id = {observation.case_id: observation for observation in observations}

    assert set(by_id) == {f"ORCH-{index:03d}" for index in range(13, 21)}
    assert sum(item.status == CONFORMANT for item in observations) == 6
    assert by_id["ORCH-016"].status == NONCONFORMANT
    assert by_id["ORCH-017"].status == NONCONFORMANT


def test_primary_json_evidence_takes_precedence_over_fallback(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "case-results.json").write_text(
        json.dumps(
            {
                "case_results": [
                    {"case_id": "ORCH-016", "conformance_status": "CONFORMANT"},
                    {"case_id": "ORCH-001", "conformance_status": "NOT_RUN"},
                ]
            }
        ),
        encoding="utf-8",
    )

    _summary, rows = score(DATASET, evidence_dir, FALLBACK, EXPECTED_DATASET_SHA256)
    by_id = {row["case_id"]: row for row in rows}

    assert by_id["ORCH-016"]["conformance_status"] == CONFORMANT
    assert by_id["ORCH-001"]["conformance_status"] == "NOT_RUN"
    assert by_id["ORCH-017"]["conformance_status"] == NONCONFORMANT


def test_run_level_case_results_override_retained_failed_retry(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence"
    retry_dir = evidence_dir / "http" / "ORCH-004-retry"
    retry_dir.mkdir(parents=True)
    (evidence_dir / "case_results.json").write_text(
        json.dumps([{"case_id": "ORCH-004", "conformance_status": "CONFORMANT"}]),
        encoding="utf-8",
    )
    (retry_dir / "case-result.json").write_text(
        json.dumps({"case_id": "ORCH-004", "conformance_status": "NONCONFORMANT"}),
        encoding="utf-8",
    )

    _summary, rows = score(DATASET, evidence_dir, None, EXPECTED_DATASET_SHA256)
    by_id = {row["case_id"]: row for row in rows}

    assert by_id["ORCH-004"]["conformance_status"] == CONFORMANT
    assert by_id["ORCH-004"]["evidence_sources"] == [
        str((evidence_dir / "case_results.json").resolve())
    ]
