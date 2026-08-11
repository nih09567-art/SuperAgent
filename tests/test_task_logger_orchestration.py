import src.robust.task_logger as task_logger_module


def test_task_logger_persists_orchestration_projection(tmp_path, monkeypatch):
    monkeypatch.setattr(task_logger_module, "_get_task_logs_dir", lambda: tmp_path)
    logger = task_logger_module.TaskLogger("task-orch", "alice:wf", "demo")
    logger.set_task_graph_snapshot(
        {"spec": {"task_id": "task-orch"}, "steps": [{"step_id": "hr"}]}
    )
    logger.record_orchestration_batch(step_ids=["hr"], monotonic_ns=90)
    logger.record_orchestration_attempt(
        step_id="hr",
        attempt=1,
        phase="primary",
        planned_agent="HR",
        executed_agent="HR",
        event="start",
        monotonic_ns=100,
    )
    logger.record_tool_selection_decision(
        "hr",
        {
            "mode": "audit",
            "recommended_mcp_tool": "search_employees",
            "arguments": {"secret": "must-not-persist"},
        },
    )
    logger.record_artifact_lineage(
        "hr",
        [{"artifact_id": "artifact-1", "logical_name": "employee.info"}],
    )
    logger.record_orchestration_step_result("hr", {"status": "SUCCEEDED"})

    loaded = task_logger_module.TaskLogger.load("task-orch")

    assert loaded is not None
    assert loaded.task_graph["steps"][0]["step_id"] == "hr"
    assert loaded.orchestration_batches[0]["step_ids"] == ["hr"]
    assert loaded.orchestration_batches[0]["scheduled_monotonic_ns"] == 90
    assert loaded.orchestration_attempts[0]["event"] == "start"
    assert loaded.tool_selection_decisions["hr"]["mode"] == "audit"
    assert "arguments" not in loaded.tool_selection_decisions["hr"]
    assert loaded.artifact_lineage[0]["producer_step_id"] == "hr"
    assert loaded.orchestration_step_results["hr"]["status"] == "SUCCEEDED"
