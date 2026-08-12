import asyncio
import hashlib
import json
from argparse import Namespace
from pathlib import Path

from experiments.mcp_realistic.benchmark import run


ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "experiments" / "mcp_realistic" / "blind_cases.json"
REGISTRY = ROOT / "docs" / "mcp_tool_registry_baseline.json"
FROZEN_SHA256 = "7e091fab7b860288489257ef5e9c4bb54910c1c4acd36d8e6bc6016eab8bcb19"


def test_blind_dataset_is_frozen_with_required_shape():
    raw = CASES.read_bytes()
    dataset = json.loads(raw.decode("utf-8"))

    assert hashlib.sha256(raw).hexdigest() == FROZEN_SHA256
    assert len(dataset["cases"]) == 36
    assert sum(case["category"] == "natural_positive" for case in dataset["cases"]) == 24
    assert sum(case["category"] == "confusable" for case in dataset["cases"]) == 8
    assert sum(case["category"] == "abstain" for case in dataset["cases"]) == 4
    assert len(dataset["schema_probes"]) == 8
    assert len(dataset["readonly_enforce_cases"]) == 8


def test_benchmark_preserves_production_enforce_boundary(tmp_path):
    report = asyncio.run(
        run(Namespace(cases=CASES, registry=REGISTRY, output_dir=tmp_path))
    )

    assert report["metrics"]["case_count"] == 36
    assert report["metrics"]["registry_tool_count"] == 48
    assert report["metrics"]["schema_probe_count"] == 8
    assert report["metrics"]["readonly_enforce_case_count"] == 8
    assert report["metrics"]["top1_accuracy_pct"] >= 93.75
    assert report["metrics"]["top3_accuracy_pct"] == 100.0
    assert report["metrics"]["abstain_precision_pct"] == 100.0
    assert report["metrics"]["abstain_recall_pct"] == 100.0
    assert all(
        not row["predicted_abstain"]
        for row in report["cases"]
        if not row["expected_abstain"]
    )
    assert report["metrics"]["production_enforce_effective_rate_pct"] == 0.0
    assert report["metrics"]["production_enforce_blocked_rate_pct"] == 100.0
    assert all(row["production_enforce_blocked"] for row in report["readonly_enforce"])
    assert all(not row["production_tool_set_constrained"] for row in report["readonly_enforce"])
