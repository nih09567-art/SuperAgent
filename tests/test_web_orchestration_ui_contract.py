from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_orchestration_is_independent_from_task_history_and_cross_links_by_task_id():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "task-orchestration.js").read_text(encoding="utf-8")

    assert 'data-tab="orchestration"' in html
    assert 'id="panel-orchestration"' in html
    assert 'data-tab="tasks"' in html
    assert 'id="panel-tasks"' in html
    assert 'id="openTaskOrchestration"' in html
    assert 'fetch("/api/tasks?execution_phase=execution")' in script
    assert 'fetch("/api/tasks");' not in script
    assert "/api/tasks/${encodeURIComponent(taskId)}/orchestration-view" in script
    assert 'url.searchParams.set("task_id", taskId)' in script
    assert "查看任务历史" in html
    assert "条事件" not in script
    assert 'workflow.textContent = run.workflow_id' not in script
    assert 'id="orchestrationTitle"' not in html
    assert 'id="orchestrationMeta"' not in html
    assert "TASK ORCHESTRATION" not in html


def test_orchestration_ui_has_required_p0_p1_p2_surfaces():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "task-orchestration.js").read_text(encoding="utf-8")
    styles = (ROOT / "web" / "task-orchestration.css").read_text(encoding="utf-8")

    for element_id in (
        "orchestrationGraph",
        "orchestrationGraphCard",
        "orchestrationGraphFullscreen",
        "orchestrationNodeInspector",
        "orchestrationTimeline",
        "orchestrationArtifactEvidence",
        "orchestrationGovernance",
        "orchestrationCheckpoints",
        "orchestrationPause",
        "orchestrationResume",
    ):
        assert f'id="{element_id}"' in html
    assert "该次运行未记录 Batch 调度或 attempt 时间证据" not in script
    assert "Agent 调用与调度时间轴" in html
    assert 'batchLabel.textContent = `B${batch.sequence}${(batch.step_ids || []).length > 1 ? " · 并行" : ""}`' in script
    assert "同一 Batch 表示并发调度" in script
    assert "不代表远程 Agent 同时开始" in script
    assert "orchestration-timeline-canvas" in styles
    assert "orchestration-timeline-identity" in styles
    assert "orchestration-timeline-dispatch-marker" in styles
    assert "orchestration-timeline-batch > header" not in styles
    assert "orchestration-graph-card-fullscreen" in script
    assert "orchestration-inspector-close" in script
    assert "min-height: 540px" in styles
    assert ".orchestration-empty[hidden]" in styles
    assert "计划编译与治理" not in html
    assert "orchestrationPlanningChain" not in script
    assert "Artifact 数据" in html
    assert "orchestrationToolEvidence" not in html
    assert "工具审计" not in html
    assert "renderTools" not in script
    assert "推荐工具" not in script
    assert "实际工具" not in script


def test_orchestration_polling_keeps_run_list_stable():
    script = (ROOT / "web" / "task-orchestration.js").read_text(encoding="utf-8")

    polling = script.split("const schedulePolling = (status) => {", 1)[1].split(
        "const openHistoryForTask", 1
    )[0]
    assert "await loadView(selectedTaskId);" in polling
    assert "loadRuns" not in polling
    assert "syncSelectedRunStatus(view);" in script
    assert "const previousScrollTop = list.scrollTop;" in script
    assert "list.scrollTop = previousScrollTop;" in script
