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
        "orchestrationTimeline",
        "orchestrationToolEvidence",
        "orchestrationArtifactEvidence",
        "orchestrationGovernance",
        "orchestrationCheckpoints",
        "orchestrationPause",
        "orchestrationResume",
    ):
        assert f'id="{element_id}"' in html
    assert "审计推荐不等于授权工具" in script
    assert "不能仅根据 DAG 宣称实际并行" in script
    assert ".orchestration-empty[hidden]" in styles
    assert "计划编译与治理" not in html
    assert "orchestrationPlanningChain" not in script
