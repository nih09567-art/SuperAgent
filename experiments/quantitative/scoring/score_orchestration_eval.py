from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATASET = ROOT / "tests" / "evaluations" / "multi_agent_orchestration_eval.json"
DEFAULT_EVIDENCE = (
    ROOT
    / ".artifacts"
    / "quantitative"
    / "20260811-2225-realistic-2c9c684"
    / "realistic"
    / "orchestration"
)
DEFAULT_FALLBACK_JUNIT = (
    ROOT / ".artifacts" / "orchestration-eval" / "orch-013-020-junit.xml"
)
DEFAULT_OUTPUT = (
    ROOT
    / ".artifacts"
    / "quantitative"
    / "20260811-2225-realistic-2c9c684"
    / "realistic"
    / "scoring"
)
EXPECTED_DATASET_SHA256 = "19b59db4734ef206ef84ef49dcb325d0d39edae280a267145504cf82fe5b3ba3"

CASE_ID_RE = re.compile(r"ORCH[-_](\d{3})", re.IGNORECASE)
SCHEMA_REF_RE = re.compile(r"^[A-Za-z0-9_.:/-]+@v[0-9]+$")

CONFORMANT = "CONFORMANT"
NONCONFORMANT = "NONCONFORMANT"
NOT_RUN = "NOT_RUN"
NO_EVIDENCE = "NO_EVIDENCE"
VALID_RESULT_STATUSES = {CONFORMANT, NONCONFORMANT, NOT_RUN, NO_EVIDENCE}


@dataclass(frozen=True)
class Observation:
    case_id: str
    status: str
    source: str
    detail: str


def _canonical_case_id(value: str) -> str | None:
    # Parameterized pytest names can contain a range-like function name
    # (for example orch_018_019) followed by the concrete case in brackets.
    # The final occurrence is the actual parameterized case ID.
    matches = CASE_ID_RE.findall(value)
    return f"ORCH-{matches[-1]}" if matches else None


def _sha256(path: Path) -> str:
    # Git may materialize text datasets with CRLF on Windows. Freeze the
    # repository's canonical LF bytes so validation is stable across platforms.
    canonical = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(canonical).hexdigest()


def _normalise_status(value: Any) -> str | None:
    if value is None:
        return None
    token = str(value).strip().upper().replace("-", "_").replace(" ", "_")
    if token in {"PASS", "PASSED", "SUCCESS", "SUCCEEDED", "COMPLIANT", CONFORMANT}:
        return CONFORMANT
    if token in {
        "FAIL",
        "FAILED",
        "FAILURE",
        "ERROR",
        "XFAIL",
        "XPASS",
        "NON_COMPLIANT",
        NONCONFORMANT,
    }:
        return NONCONFORMANT
    if token in {"SKIP", "SKIPPED", "NOT_EXECUTED", NOT_RUN}:
        return NOT_RUN
    if token in {"UNKNOWN", "MISSING", NO_EVIDENCE}:
        return NO_EVIDENCE
    return None


def validate_dataset(dataset: dict[str, Any], dataset_path: Path) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    cases = dataset.get("cases")
    templates = dataset.get("workflow_templates")
    declared_modes = dataset.get("evaluation_modes")
    if not isinstance(cases, list):
        raise ValueError("dataset.cases must be a list")
    if not isinstance(templates, dict) or not templates:
        raise ValueError("dataset.workflow_templates must be a non-empty object")
    if not isinstance(declared_modes, dict) or not declared_modes:
        raise ValueError("dataset.evaluation_modes must be a non-empty object")

    ids = [case.get("id") for case in cases]
    duplicates = sorted(case_id for case_id, count in Counter(ids).items() if count > 1)
    if len(cases) != 20:
        errors.append(f"expected 20 cases, found {len(cases)}")
    if duplicates:
        errors.append(f"duplicate case IDs: {', '.join(duplicates)}")
    expected_ids = {f"ORCH-{index:03d}" for index in range(1, 21)}
    actual_ids = {case_id for case_id in ids if isinstance(case_id, str)}
    if actual_ids != expected_ids:
        errors.append(
            "case ID set mismatch: missing="
            f"{sorted(expected_ids - actual_ids)}, extra={sorted(actual_ids - expected_ids)}"
        )

    produced_by_template: dict[str, dict[str, str]] = {}
    for template_name, template in templates.items():
        steps = template.get("logical_steps")
        if not isinstance(steps, list) or not steps:
            errors.append(f"workflow template {template_name!r} has no logical_steps")
            continue
        step_ids = [step.get("logical_step_id") for step in steps]
        if len(step_ids) != len(set(step_ids)):
            errors.append(f"workflow template {template_name!r} has duplicate logical_step_id")
        known_steps = set(step_ids)
        produced: dict[str, str] = {}
        required_by_step: dict[str, dict[str, str]] = {}
        for step in steps:
            step_id = step.get("logical_step_id")
            if not step_id or not step.get("accepted_agents"):
                errors.append(f"workflow template {template_name!r} has incomplete step {step_id!r}")
            required_by_step[step_id] = {}
            for field in ("requires", "produces"):
                for contract in step.get(field, []):
                    logical_name = contract.get("logical_name")
                    schema_ref = contract.get("schema_ref")
                    if not logical_name or not schema_ref:
                        errors.append(
                            f"workflow template {template_name!r} step {step_id!r} has incomplete {field} contract"
                        )
                    elif not SCHEMA_REF_RE.match(schema_ref):
                        errors.append(
                            f"workflow template {template_name!r} has malformed schema_ref {schema_ref!r}"
                        )
                    if field == "produces" and logical_name:
                        previous = produced.get(logical_name)
                        if previous and previous != schema_ref:
                            errors.append(
                                f"workflow template {template_name!r} produces {logical_name!r} with conflicting schemas"
                            )
                        produced[logical_name] = schema_ref
                    elif field == "requires" and logical_name:
                        required_by_step[step_id][logical_name] = schema_ref

        for edge in template.get("expected_execution_edges", []):
            if not isinstance(edge, list) or len(edge) != 2 or any(node not in known_steps for node in edge):
                errors.append(f"workflow template {template_name!r} has invalid execution edge {edge!r}")
        for group in template.get("expected_parallel_groups", []):
            if not isinstance(group, list) or len(group) < 2 or any(node not in known_steps for node in group):
                errors.append(f"workflow template {template_name!r} has invalid parallel group {group!r}")
        for binding in template.get("expected_data_bindings", []):
            consumer = binding.get("consumer_step")
            parameter = binding.get("parameter_name")
            if consumer not in known_steps:
                errors.append(f"workflow template {template_name!r} binding has unknown consumer {consumer!r}")
            if consumer in required_by_step and parameter not in required_by_step[consumer]:
                errors.append(
                    f"workflow template {template_name!r} binding parameter {parameter!r} is not required by {consumer!r}"
                )
            assembly_schema = binding.get("assembly_schema_ref")
            if assembly_schema and not SCHEMA_REF_RE.match(assembly_schema):
                errors.append(
                    f"workflow template {template_name!r} has malformed assembly schema {assembly_schema!r}"
                )
            for source in binding.get("source_artifacts", []):
                source_step = source.get("source_step")
                source_output = source.get("source_output")
                if source_step not in known_steps or source_output not in produced:
                    errors.append(
                        f"workflow template {template_name!r} binding has unknown source {source_step!r}/{source_output!r}"
                    )
        produced_by_template[template_name] = produced

    mode_counts: Counter[str] = Counter()
    for case in cases:
        case_id = case.get("id", "<missing>")
        mode = case.get("evaluation_mode")
        mode_counts[mode] += 1
        template_name = case.get("workflow_template")
        expected = case.get("expected")
        if mode not in declared_modes:
            errors.append(f"{case_id}: unknown evaluation_mode {mode!r}")
        if template_name not in templates:
            errors.append(f"{case_id}: unknown workflow_template {template_name!r}")
            continue
        if not isinstance(case.get("query"), str) or not case["query"].strip():
            errors.append(f"{case_id}: query must be non-empty")
        if not isinstance(expected, dict) or "plan_accepted" not in expected:
            errors.append(f"{case_id}: expected.plan_accepted is required")
            continue
        if mode == "candidate_plan_validation":
            for key in ("rejected_before_execution", "started_step_count", "produced_artifact_count"):
                if key not in expected:
                    errors.append(f"{case_id}: expected.{key} is required for candidate plan validation")
        elif "workflow_status" not in expected:
            errors.append(f"{case_id}: expected.workflow_status is required")
        known_steps = {
            step["logical_step_id"] for step in templates[template_name].get("logical_steps", [])
        }
        unknown_status_steps = set(expected.get("step_statuses", {})) - known_steps
        if unknown_status_steps:
            errors.append(f"{case_id}: expected.step_statuses has unknown steps {sorted(unknown_status_steps)}")
        unknown_parallel_steps = {
            step
            for group in expected.get("observed_parallel_groups", [])
            for step in group
            if step not in known_steps
        }
        if unknown_parallel_steps:
            errors.append(
                f"{case_id}: expected.observed_parallel_groups has unknown steps {sorted(unknown_parallel_steps)}"
            )
        produced = produced_by_template.get(template_name, {})
        for artifact, ancestors in expected.get("lineage", {}).items():
            if artifact not in produced:
                errors.append(f"{case_id}: lineage target {artifact!r} is not produced by its template")
            for ancestor in ancestors:
                if ancestor not in produced:
                    errors.append(f"{case_id}: lineage source {ancestor!r} is not produced by its template")
        for artifact in expected.get("required_artifacts", []):
            if artifact not in produced:
                errors.append(f"{case_id}: required artifact {artifact!r} is not produced by its template")

    expected_counts = {
        "natural_language_e2e": 7,
        "deterministic_composite_harness": 3,
        "candidate_plan_validation": 2,
        "controlled_fault_e2e": 8,
    }
    if dict(mode_counts) != expected_counts:
        errors.append(f"evaluation_mode counts changed: expected {expected_counts}, found {dict(mode_counts)}")

    return {
        "dataset_path": str(dataset_path.resolve()),
        "dataset_sha256": _sha256(dataset_path),
        "case_count": len(cases),
        "unique_id_count": len(set(ids)),
        "evaluation_mode_counts": dict(sorted(mode_counts.items())),
        "workflow_template_count": len(templates),
        "schema_reference_validation": "VALID" if not any("schema" in error for error in errors) else "INVALID",
        "status": "VALID" if not errors else "INVALID",
        "errors": errors,
        "warnings": warnings,
    }


def read_junit(path: Path) -> list[Observation]:
    observations: list[Observation] = []
    root = ET.parse(path).getroot()
    for testcase in root.iter("testcase"):
        name = " ".join((testcase.get("classname", ""), testcase.get("name", "")))
        case_id = _canonical_case_id(name)
        if not case_id:
            continue
        failure = testcase.find("failure")
        error = testcase.find("error")
        skipped = testcase.find("skipped")
        if failure is not None or error is not None:
            node = failure if failure is not None else error
            status = NONCONFORMANT
            detail = (node.get("message") or node.text or "pytest failure/error").strip()
        elif skipped is not None:
            skip_type = (skipped.get("type") or "").lower()
            message = (skipped.get("message") or skipped.text or "pytest skipped").strip()
            if "xfail" in skip_type or "xfail" in message.lower():
                status = NONCONFORMANT
                detail = f"xfail counts as nonconformant: {message}"
            else:
                status = NOT_RUN
                detail = f"skipped without conformance evidence: {message}"
        else:
            status = CONFORMANT
            detail = "pytest assertion set passed"
        observations.append(Observation(case_id, status, str(path.resolve()), detail))
    return observations


def _iter_result_records(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, list):
        yield from (item for item in value if isinstance(item, dict))
    elif isinstance(value, dict):
        for key in ("cases", "case_results", "results"):
            records = value.get(key)
            if isinstance(records, list):
                yield from (item for item in records if isinstance(item, dict))
                return
        if any(key in value for key in ("id", "case_id")):
            yield value


def read_json_results(path: Path) -> list[Observation]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []
    observations: list[Observation] = []
    for record in _iter_result_records(payload):
        case_id = _canonical_case_id(str(record.get("case_id") or record.get("id") or ""))
        raw_status = record.get("conformance_status")
        if raw_status is None:
            raw_status = record.get("status", record.get("outcome"))
        status = _normalise_status(raw_status)
        if not case_id or not status:
            continue
        detail = str(record.get("detail") or record.get("reason") or f"JSON status={raw_status}")
        observations.append(Observation(case_id, status, str(path.resolve()), detail))
    return observations


def collect_evidence(directory: Path) -> list[Observation]:
    if not directory.exists():
        return []
    # A run-level case_results file is the track owner's final case-level
    # adjudication. Prefer it over intermediate retries and raw Harness files;
    # those may intentionally contain earlier failed attempts for auditability.
    authoritative: list[Observation] = []
    for name in ("case_results.json", "case-results.json"):
        candidate = directory / name
        if candidate.is_file():
            authoritative.extend(read_json_results(candidate))
    authoritative_ids = {item.case_id for item in authoritative}
    observations: list[Observation] = list(authoritative)
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        if path.parent == directory and path.name in {"case_results.json", "case-results.json"}:
            continue
        discovered: list[Observation] = []
        if path.suffix.lower() == ".xml":
            try:
                discovered = read_junit(path)
            except ET.ParseError:
                continue
        elif path.suffix.lower() == ".json":
            discovered = read_json_results(path)
        observations.extend(item for item in discovered if item.case_id not in authoritative_ids)
    return observations


def _merge_case_observations(observations: list[Observation]) -> tuple[str, str]:
    if not observations:
        return NO_EVIDENCE, "no case-level result was found"
    statuses = {item.status for item in observations}
    if NONCONFORMANT in statuses:
        return NONCONFORMANT, "at least one assertion set was nonconformant"
    if NO_EVIDENCE in statuses:
        return NO_EVIDENCE, "evidence explicitly reports no determination"
    if NOT_RUN in statuses:
        return NOT_RUN, "at least one required assertion set was not run"
    if statuses == {CONFORMANT}:
        return CONFORMANT, "all discovered assertion sets passed"
    return NO_EVIDENCE, f"unhandled evidence statuses: {sorted(statuses)}"


def _dimension_members(cases: list[dict[str, Any]]) -> dict[str, list[str]]:
    def ids(predicate):
        return [case["id"] for case in cases if predicate(case)]

    nl_profile_modes = {"natural_language_e2e", "deterministic_composite_harness"}
    clarification = {
        case["id"]
        for case in cases
        if case.get("expected", {}).get("workflow_status") == "CLARIFICATION_REQUIRED"
    }
    return {
        "task_profile_and_clarification": ids(lambda case: case["evaluation_mode"] in nl_profile_modes),
        "planner_generated_taskgraph": ids(
            lambda case: case["evaluation_mode"] == "natural_language_e2e" and case["id"] not in clarification
        ),
        "deterministic_composite": ids(lambda case: case["evaluation_mode"] == "deterministic_composite_harness"),
        "invalid_candidate_plan": ids(lambda case: case["evaluation_mode"] == "candidate_plan_validation"),
        "actual_multi_agent_execution": ids(
            lambda case: case["evaluation_mode"] in nl_profile_modes and case["id"] not in clarification
        ),
        "clarification_block": sorted(clarification),
        "controlled_fault": ids(lambda case: case["evaluation_mode"] == "controlled_fault_e2e"),
        "overall_gold_behavior": [case["id"] for case in cases],
    }


def _summarise_dimension(name: str, members: list[str], statuses: dict[str, str]) -> dict[str, Any]:
    counts = Counter(statuses[case_id] for case_id in members)
    assessed = counts[CONFORMANT] + counts[NONCONFORMANT]
    return {
        "name": name,
        "denominator_unique_cases": len(members),
        "case_ids": members,
        "status_counts": {status: counts[status] for status in sorted(VALID_RESULT_STATUSES)},
        "conformant_over_full_denominator": f"{counts[CONFORMANT]}/{len(members)}",
        "conformance_rate_full_denominator": counts[CONFORMANT] / len(members) if members else None,
        "assessed_cases": assessed,
        "coverage_rate": assessed / len(members) if members else None,
        "note": "NOT_RUN and NO_EVIDENCE remain in the denominator and are never inferred as passes.",
    }


def score(
    dataset_path: Path,
    evidence_dir: Path,
    fallback_junit: Path | None,
    expected_sha256: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    validation = validate_dataset(dataset, dataset_path)
    if validation["dataset_sha256"] != expected_sha256:
        validation["errors"].append(
            f"dataset SHA-256 mismatch: expected {expected_sha256}, found {validation['dataset_sha256']}"
        )
        validation["status"] = "INVALID"

    primary = collect_evidence(evidence_dir)
    primary_ids = {item.case_id for item in primary}
    fallback: list[Observation] = []
    if fallback_junit and fallback_junit.exists():
        fallback = [item for item in read_junit(fallback_junit) if item.case_id not in primary_ids]
    observations = primary + fallback
    grouped: dict[str, list[Observation]] = defaultdict(list)
    for item in observations:
        grouped[item.case_id].append(item)

    rows: list[dict[str, Any]] = []
    statuses: dict[str, str] = {}
    dimensions = _dimension_members(dataset["cases"])
    memberships = {
        case_id: [name for name, members in dimensions.items() if case_id in members]
        for case_id in (case["id"] for case in dataset["cases"])
    }
    for case in dataset["cases"]:
        case_id = case["id"]
        case_observations = grouped.get(case_id, [])
        case_status, reason = _merge_case_observations(case_observations)
        statuses[case_id] = case_status
        rows.append(
            {
                "case_id": case_id,
                "evaluation_mode": case["evaluation_mode"],
                "workflow_template": case["workflow_template"],
                "expected_workflow_status": case["expected"].get("workflow_status", "REJECTED_BEFORE_EXECUTION"),
                "conformance_status": case_status,
                "reason": reason,
                "dimensions": memberships[case_id],
                "evidence_sources": sorted({item.source for item in case_observations}),
                "evidence_details": [item.detail for item in case_observations],
            }
        )

    summary = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "method": {
            "unit": "unique evaluation case",
            "xfail_policy": "xfail is NONCONFORMANT",
            "missing_policy": "unrun cases are NOT_RUN; absent or indeterminate evidence is NO_EVIDENCE",
            "fallback_policy": "current-run evidence takes precedence by case ID; fallback JUnit fills only missing IDs",
            "assertion_policy": "a case is CONFORMANT only when every discovered assertion set for that case passes",
        },
        "dataset_validation": validation,
        "evidence": {
            "primary_directory": str(evidence_dir.resolve()),
            "primary_observation_count": len(primary),
            "fallback_junit": str(fallback_junit.resolve()) if fallback_junit and fallback_junit.exists() else None,
            "fallback_observation_count": len(fallback),
        },
        "dimensions": {
            name: _summarise_dimension(name, members, statuses)
            for name, members in dimensions.items()
        },
        "case_status_counts": dict(sorted(Counter(statuses.values()).items())),
    }
    return summary, rows


def write_outputs(output_dir: Path, summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with (output_dir / "case-results.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "case_id",
                "evaluation_mode",
                "workflow_template",
                "expected_workflow_status",
                "conformance_status",
                "reason",
                "dimensions",
                "evidence_sources",
                "evidence_details",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    **row,
                    "dimensions": ";".join(row["dimensions"]),
                    "evidence_sources": ";".join(row["evidence_sources"]),
                    "evidence_details": " | ".join(row["evidence_details"]),
                }
            )

    failures = [row for row in rows if row["conformance_status"] != CONFORMANT]
    (output_dir / "failures.json").write_text(
        json.dumps(failures, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    dimension_labels = {
        "task_profile_and_clarification": "TaskProfile / clarification behavior",
        "planner_generated_taskgraph": "Planner-generated TaskGraph",
        "deterministic_composite": "Deterministic composite Harness",
        "invalid_candidate_plan": "Invalid candidate-plan rejection",
        "actual_multi_agent_execution": "Actual multi-Agent execution",
        "clarification_block": "Correct clarification block",
        "controlled_fault": "Controlled fault / governance",
        "overall_gold_behavior": "Overall gold-standard behavior",
    }
    lines = [
        "# Realistic orchestration scoring summary",
        "",
        f"Dataset SHA-256: `{summary['dataset_validation']['dataset_sha256']}`",
        "",
        f"Dataset validation: **{summary['dataset_validation']['status']}**",
        "",
        "`xfail` is counted as `NONCONFORMANT`. `NOT_RUN` and `NO_EVIDENCE` stay in each full denominator.",
        "",
        "| Dimension | Conformant / denominator | Assessed | Evidence coverage |",
        "|---|---:|---:|---:|",
    ]
    for name, result in summary["dimensions"].items():
        coverage = result["coverage_rate"]
        lines.append(
            f"| {dimension_labels[name]} | {result['conformant_over_full_denominator']} | "
            f"{result['assessed_cases']} | {coverage:.1%} |"
        )
    lines.extend(["", "## Case-level results", "", "| Case | Mode | Expected terminal behavior | Result |", "|---|---|---|---|"])
    for row in rows:
        lines.append(
            f"| {row['case_id']} | {row['evaluation_mode']} | {row['expected_workflow_status']} | {row['conformance_status']} |"
        )
    lines.extend(["", "## Nonconformant or missing evidence", ""])
    if failures:
        for row in failures:
            lines.append(f"- `{row['case_id']}` — **{row['conformance_status']}**: {row['reason']}")
    else:
        lines.append("None.")
    if summary["dataset_validation"]["errors"]:
        lines.extend(["", "## Dataset validation errors", ""])
        lines.extend(f"- {error}" for error in summary["dataset_validation"]["errors"])
    (output_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Score the frozen 20-case orchestration evaluation without changing it.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--evidence-dir", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--fallback-junit", type=Path, default=DEFAULT_FALLBACK_JUNIT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--expected-dataset-sha256", default=EXPECTED_DATASET_SHA256)
    parser.add_argument("--no-fallback", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    fallback = None if args.no_fallback else args.fallback_junit
    summary, rows = score(args.dataset, args.evidence_dir, fallback, args.expected_dataset_sha256)
    write_outputs(args.output_dir, summary, rows)
    print(json.dumps({
        "output_dir": str(args.output_dir.resolve()),
        "dataset_validation": summary["dataset_validation"]["status"],
        "case_status_counts": summary["case_status_counts"],
        "overall": summary["dimensions"]["overall_gold_behavior"]["conformant_over_full_denominator"],
    }, ensure_ascii=False, indent=2))
    return 0 if summary["dataset_validation"]["status"] == "VALID" else 2


if __name__ == "__main__":
    raise SystemExit(main())
