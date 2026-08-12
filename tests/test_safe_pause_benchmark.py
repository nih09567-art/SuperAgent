import asyncio
import json

from experiments.safe_pause.benchmark import run


def test_safe_pause_benchmark_six_trials_conform(tmp_path):
    summary = asyncio.run(run(tmp_path / "results"))

    assert summary["trial_count"] == 6
    assert summary["scenario_count"] == 3
    assert summary["conformant"] == 6
    assert summary["safe_point_pause"] == 6
    assert summary["checkpoint_loaded"] == 6
    assert summary["resume_completed"] == 6
    assert summary["next_batch_blocked"] == 6
    assert summary["completed_step_duplicate_invocations"] == 0
    assert summary["parallel_trials_with_concurrency"] == 2
    assert summary["side_effect_invocations"] == 2
    assert summary["duplicate_side_effects"] == 0
    assert summary["automatic_resend_blocked_on_same_checkpoint_replay"] == 2

    persisted = json.loads((tmp_path / "results" / "summary.json").read_text("utf-8"))
    assert persisted == summary
