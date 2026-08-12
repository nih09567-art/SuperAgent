from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "experiments" / "checkpoint_restart" / "cases.json"
WORKER = ROOT / "experiments" / "checkpoint_restart" / "worker.py"


def test_checkpoint_restart_dataset_is_frozen_and_complete():
    dataset = json.loads(CASES.read_text(encoding="utf-8"))
    cases = dataset["cases"]
    assert dataset["dataset_id"] == "checkpoint-process-restart-v2"
    assert [case["id"] for case in cases] == [f"CP-{index:02d}" for index in range(1, 7)]
    assert len({case["name"] for case in cases}) == 6
    assert cases[4]["steps"][1]["schema_ref"] == "email.dispatch.receipt@v1"
    assert cases[5]["expected_terminal"] == "NEEDS_RECONCILIATION"


def test_fresh_process_loads_checkpoint_and_skips_completed_step(tmp_path):
    run_dir = tmp_path / "cp01"
    task_id = "cp01-focused"

    def invoke(phase: str):
        return subprocess.run(
            [
                sys.executable,
                str(WORKER),
                "--cases", str(CASES),
                "--case-id", "CP-01",
                "--task-id", task_id,
                "--run-dir", str(run_dir),
                "--phase", phase,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    crashed = invoke("crash")
    assert crashed.returncode == 86
    marker = json.loads((run_dir / "crash-marker.json").read_text(encoding="utf-8"))
    assert marker["completed_steps"] == ["lookup"]

    resumed = invoke("resume")
    assert resumed.returncode == 0, resumed.stderr or resumed.stdout
    result = json.loads((run_dir / "resume-result.json").read_text(encoding="utf-8"))
    assert result["loaded_checkpoint"]
    assert result["pid"] != marker["pid"]
    assert result["terminal"]["status"] == "SUCCEEDED"
    calls = Counter(
        json.loads(line)["step_id"]
        for line in (run_dir / "step-calls.jsonl").read_text(encoding="utf-8").splitlines()
    )
    assert calls == {"lookup": 1, "analyze": 1, "finish": 1}
