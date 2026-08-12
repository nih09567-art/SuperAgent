# Trusted plan validation benchmark

This benchmark measures the deterministic validation and conversion boundary
between Planner output and an executable `TaskGraph`. It does **not** measure
LLM plan-generation accuracy.

The fixed corpus contains 60 candidate plans:

- 12 legal plans derived from the same three-step HR/knowledge/report workflow;
- 48 invalid plans: six examples for each of eight mutation categories.

The pipeline records three independent gates:

1. TaskProfile coverage, intent, and dependency validation;
2. `plan_to_task_graph()` structural and data-flow validation;
3. PlanSnapshot integrity plus rebuild-and-compare validation.

Run from the repository root:

```powershell
python experiments/quantitative/planning/benchmark.py `
  --output-dir .artifacts/quantitative/20260811-2c9c684/planning
```

Outputs are `case_catalog.jsonl`, `raw_results.jsonl`, `summary.json`,
`summary.csv`, and `category_summary.csv`. Timing repetitions do not increase
the reported corpus size: accuracy uses 60 unique candidates, while latency
uses `60 * timing_repeats` pipeline invocations.
