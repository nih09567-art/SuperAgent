from pathlib import Path

import pytest

from scripts import run_annual_leave_demo_acceptance as acceptance


@pytest.mark.parametrize(
    ("scenario", "expected_call", "expected_decision"),
    [
        ("dynamic-three", "three", None),
        ("dynamic-five-approved", "five", "approve"),
        ("dynamic-five-rejected", "five", "reject"),
    ],
)
def test_run_scenario_dispatches_to_expected_harness(
    monkeypatch,
    tmp_path: Path,
    scenario: str,
    expected_call: str,
    expected_decision: str | None,
) -> None:
    captured = {}

    def run_three(services, *, run_dir, scenario):
        captured.update(call="three", run_dir=run_dir, scenario=scenario)
        return {"status": "SUCCEEDED"}

    def run_five(services, *, run_dir, approval_decision):
        captured.update(
            call="five",
            run_dir=run_dir,
            approval_decision=approval_decision,
        )
        return {
            "status": (
                "SUCCEEDED" if approval_decision == "approve" else "PARTIAL_FAILED"
            )
        }

    monkeypatch.setattr(acceptance, "run_annual_leave_workflow", run_three)
    monkeypatch.setattr(acceptance, "run_dynamic_five_agent_workflow", run_five)

    result = acceptance._run_scenario(
        object(),
        run_dir=tmp_path,
        scenario=scenario,
    )

    assert captured["call"] == expected_call
    assert captured["run_dir"] == tmp_path
    if expected_decision is None:
        assert captured["scenario"] == "success"
        assert result["status"] == "SUCCEEDED"
    else:
        assert captured["approval_decision"] == expected_decision


def test_run_scenario_rejects_unknown_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported acceptance scenario"):
        acceptance._run_scenario(
            object(),
            run_dir=tmp_path,
            scenario="dynamic-six",
        )
