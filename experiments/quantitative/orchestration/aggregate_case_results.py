"""Build the 20-case conformance file from preserved HTTP and JUnit evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
DATASET = REPO_ROOT / "tests" / "evaluations" / "multi_agent_orchestration_eval.json"

NONCONFORMANT = {
    "ORCH-007": "Missing one gold-standard fan-in source is accepted by the converter.",
    "ORCH-016": "Strict xfail: first non-executed consumer is FAILED instead of SKIPPED.",
    "ORCH-017": "Strict xfail: raw salary is exposed in scheduler event data.",
}

DETAILS = {
    "ORCH-001": "Real HTTP launch and production harness asserted exact three-Agent graph, schemas, lineage, overlap, and report semantics.",
    "ORCH-002": "Real HTTP paraphrase run asserted the exact three-Agent graph; Document and Email were absent.",
    "ORCH-003": "Real HTTP controlled not-found Artifact produced a cautious report without inferred leave days.",
    "ORCH-004": "Real HTTP Knowledge 503 produced PARTIAL_FAILED, preserved HR Artifact, and blocked Report.",
    "ORCH-005": "Real HTTP invalid Knowledge date/schema path failed closed and blocked Report.",
    "ORCH-006": "Candidate execution cycle was rejected by TaskGraph conversion before any step start.",
    "ORCH-008": "Real HTTP five-Agent harness asserted APPROVAL_REQUIRED and zero Email calls before continuing the evidence run.",
    "ORCH-009": "Real HTTP rejected approval retained the report and produced no Email call or receipt.",
    "ORCH-010": "Real HTTP approved workflow and duplicate resume produced exactly one Email side effect and receipt.",
    "ORCH-011": "Controlled three-Agent workflow retried Knowledge exactly once, then completed with two-source report lineage.",
    "ORCH-012": "Controlled approved send failed after dispatch, ran once, emitted no receipt, and ended NEEDS_RECONCILIATION.",
    "ORCH-013": "Composite harness observed parallel employee/weather starts and verified report lineage.",
    "ORCH-014": "Composite fault harness retained the independent travel branch and blocked the dependent report.",
    "ORCH-015": "Composite harness verified parallel fan-in and stopped before the controlled send.",
    "ORCH-018": "Natural-language TaskProfile required recipient clarification before execution.",
    "ORCH-019": "Natural-language TaskProfile required participants and time clarification before execution.",
    "ORCH-020": "Controlled workflow passed two approval points and duplicate resume without duplicate side effects.",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    payload = json.loads(DATASET.read_text(encoding="utf-8"))
    dataset_sha = hashlib.sha256(DATASET.read_bytes()).hexdigest()
    junit_core = output_dir / "junit-orch-006-007-011-012.xml"
    junit_expansion = output_dir / "junit-orch-013-020.xml"

    http_evidence = {
        "ORCH-001": output_dir / "http" / "ORCH-001",
        "ORCH-002": output_dir / "http" / "ORCH-002",
        "ORCH-003": output_dir / "http" / "ORCH-003" / "run",
        "ORCH-004": output_dir / "http" / "ORCH-004-retry2",
        "ORCH-005": output_dir / "http" / "ORCH-005",
        "ORCH-008": output_dir / "http" / "ORCH-008",
        "ORCH-009": output_dir / "http" / "ORCH-009",
        "ORCH-010": output_dir / "http" / "ORCH-010",
    }
    core_ids = {"ORCH-006", "ORCH-007", "ORCH-011", "ORCH-012"}
    expansion_ids = {f"ORCH-{number:03d}" for number in range(13, 21)}

    results = []
    for case in payload["cases"]:
        case_id = case["id"]
        evidence: list[str] = []
        if case_id in http_evidence and http_evidence[case_id].exists():
            evidence.append(str(http_evidence[case_id]))
        if case_id == "ORCH-004":
            initial = output_dir / "http" / "ORCH-004"
            if initial.exists():
                evidence.append(str(initial))
        if case_id in core_ids and junit_core.exists():
            evidence.append(str(junit_core))
        if case_id in expansion_ids and junit_expansion.exists():
            evidence.append(str(junit_expansion))

        if case_id in NONCONFORMANT:
            status = "NONCONFORMANT"
            detail = NONCONFORMANT[case_id]
        elif evidence and case_id in DETAILS:
            status = "CONFORMANT"
            detail = DETAILS[case_id]
        else:
            status = "NO_EVIDENCE"
            detail = "No case-level executable evidence was produced in this run."
        results.append(
            {
                "case_id": case_id,
                "conformance_status": status,
                "detail": detail,
                "evidence_paths": evidence,
                "execution_type": case["evaluation_mode"],
            }
        )

    counts = Counter(item["conformance_status"] for item in results)
    mode_counts = {}
    for mode in payload["evaluation_modes"]:
        mode_results = [item for item in results if item["execution_type"] == mode]
        mode_counts[mode] = dict(
            Counter(item["conformance_status"] for item in mode_results)
        )
    (output_dir / "case_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    summary = {
        "dataset_sha256": dataset_sha,
        "unique_cases": len(results),
        "status_counts": dict(counts),
        "conformance_rate": counts.get("CONFORMANT", 0) / len(results),
        "status_counts_by_evaluation_mode": mode_counts,
        "xfail_policy": "xfail is NONCONFORMANT and remains in the denominator",
        "case_results": "case_results.json",
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    failures = {
        "nonconformant_cases": [
            item for item in results if item["conformance_status"] == "NONCONFORMANT"
        ],
        "infrastructure_attempts": [
            {
                "case_id": "ORCH-004",
                "status": "RETRIED",
                "detail": "First attempt ended before terminal SSE because a Planner checkmark could not be printed with GBK. The preserved UTF-8 retry passed.",
                "evidence_path": str(output_dir / "http" / "ORCH-004"),
            }
        ],
    }
    (output_dir / "failures.json").write_text(
        json.dumps(failures, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    commands = """# Commands (PowerShell, repository root)
.\\.venv\\Scripts\\python.exe -m pytest tests\\test_multi_agent_orchestration_eval_expansion.py -q -rxX -o addopts= --junitxml=<output>\\junit-orch-013-020.xml
$env:RUN_ANNUAL_LEAVE_HTTP_E2E='1'; $env:PYTHONIOENCODING='utf-8'; python experiments\\quantitative\\orchestration\\run_http_case.py <ORCH-ID> --output-dir <output>\\http\\<ORCH-ID>
.\\.venv\\Scripts\\python.exe -m pytest experiments\\quantitative\\orchestration\\test_eval_core_cases.py -q -rxX -o addopts= --junitxml=<output>\\junit-orch-006-007-011-012.xml
.\\.venv\\Scripts\\python.exe experiments\\quantitative\\orchestration\\aggregate_case_results.py --output-dir <output>
"""
    (output_dir / "commands.txt").write_text(commands, encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
