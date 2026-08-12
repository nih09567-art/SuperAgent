# Unified orchestration evaluation scorer

This directory contains a read-only scorer for the frozen 20-case orchestration
evaluation. It never edits the evaluation dataset or execution Harness.

Run from the repository root:

```powershell
.\.venv\Scripts\python.exe experiments\quantitative\scoring\score_orchestration_eval.py
```

Default inputs and outputs are pinned to run
`20260811-2225-realistic-2c9c684`. Override them with `--dataset`,
`--evidence-dir`, `--fallback-junit`, and `--output-dir` when reproducing a
different run.

Accepted evidence formats:

- pytest JUnit XML. A passed testcase is conformant, an `xfail`/failure/error is
  nonconformant, and an ordinary skip is not run.
- JSON containing `cases`, `case_results`, or `results`, whose records include
  `case_id` (or `id`) and `conformance_status` (or `status`/`outcome`).

The run-level `case_results.json` (or `case-results.json`) is authoritative for
the case IDs it contains. This prevents retained failed retries from overriding
the track owner's final case adjudication. Other current-run files and then the
legacy ORCH-013--020 JUnit fill only missing IDs. Missing evidence is preserved
as `NO_EVIDENCE`; it is not inferred from a workflow terminal status.

Outputs:

- `summary.json`: dataset validation, evidence provenance, and dimension totals.
- `case-results.csv`: one row per frozen case.
- `summary.md`: human-readable totals with the full denominators.
- `failures.json`: nonconformant, not-run, and no-evidence cases.
