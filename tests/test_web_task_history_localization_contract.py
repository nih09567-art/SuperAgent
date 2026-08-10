from pathlib import Path


ROOT = Path(__file__).parents[1]
APP_JS = ROOT / "web" / "app.js"
INDEX_HTML = ROOT / "web" / "index.html"
SECURITY_JS = ROOT / "web" / "security.js"
STYLES = ROOT / "web" / "styles.css"


def test_task_history_has_a_guided_empty_state_and_chinese_interface():
    index = INDEX_HTML.read_text(encoding="utf-8")
    task_panel = index[index.index('id="panel-tasks"') : index.index('id="panel-security"')]

    assert 'id="taskDetailEmpty"' in task_panel
    assert "选择一条任务记录" in task_panel
    for label in ("任务历史", "刷新", "检查点", "治理时间线", "任务日志", "恢复执行"):
        assert label in task_panel
    for old_label in ("Task History", "Refresh", "Checkpoints", "Governance Timeline", "Task Log"):
        assert old_label not in task_panel


def test_task_history_dynamic_labels_are_localized_without_renaming_agents():
    source = APP_JS.read_text(encoding="utf-8")

    for label in ("初始规划", "重新规划", "部分失败", "检查点已保存", "工作流结束"):
        assert label in source
    assert 'event.agent ? `Agent=${event.agent}`' in source
    assert 'roleSpan.textContent = entry.role || entry.node_name || "未知"' in source
    assert 'eventType.textContent = taskEventLabel(type)' in source
    assert 'statusBadge.textContent = taskStatusLabel(task.status)' in source


def test_available_agents_section_can_be_collapsed_and_starts_expanded():
    index = INDEX_HTML.read_text(encoding="utf-8")
    security_source = SECURITY_JS.read_text(encoding="utf-8")
    styles = STYLES.read_text(encoding="utf-8")

    assert 'id="toggleUserAgentsBtn"' in index
    assert 'aria-expanded="true" aria-controls="userAgentsList"' in index
    assert 'id="userAgentsList" class="sec-agents"' in index
    assert 'bindSecurityCollapseButton("toggleUserAgentsBtn", "userAgentsList", false)' in security_source
    assert "#userAgentsList[hidden]" in styles
