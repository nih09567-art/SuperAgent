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


def test_task_history_guidance_stays_in_the_first_view_and_only_shows_latest_twenty():
    source = APP_JS.read_text(encoding="utf-8")
    styles = STYLES.read_text(encoding="utf-8")

    assert "const TASK_HISTORY_DISPLAY_LIMIT = 20;" in source
    assert "(await res.json()).slice(0, TASK_HISTORY_DISPLAY_LIMIT)" in source
    assert 'tasksListCount.textContent = `最近 ${tasks.length} 条`' in source
    tasks_layout = styles[styles.index(".tasks-layout {") : styles.index(".tasks-left {")]
    assert "height: clamp(500px, calc(100vh - 250px), 720px);" in tasks_layout
    assert "align-items: stretch;" in tasks_layout
    tasks_list = styles[styles.index("#tasksList {") : styles.index("#tasksList::-webkit-scrollbar {")]
    assert "overflow-y: auto;" in tasks_list
    assert "overscroll-behavior: contain;" in tasks_list


def test_task_history_dynamic_labels_are_localized_without_renaming_agents():
    source = APP_JS.read_text(encoding="utf-8")

    for label in ("初始规划", "重新规划", "部分失败", "检查点已保存", "工作流结束"):
        assert label in source
    assert 'event.agent ? `Agent=${event.agent}`' in source
    assert 'roleSpan.textContent = entry.role || entry.node_name || "未知"' in source
    assert 'eventType.textContent = taskEventLabel(type)' in source
    assert 'statusBadge.textContent = taskStatusLabel(task.status)' in source


def test_available_agents_section_can_be_expanded_and_starts_collapsed():
    index = INDEX_HTML.read_text(encoding="utf-8")
    security_source = SECURITY_JS.read_text(encoding="utf-8")
    styles = STYLES.read_text(encoding="utf-8")

    assert 'id="toggleUserAgentsBtn"' in index
    assert 'aria-expanded="false" aria-controls="userAgentsList"' in index
    assert 'id="userAgentsList" class="sec-agents" hidden' in index
    assert 'bindSecurityCollapseButton("toggleUserAgentsBtn", "userAgentsList", true)' in security_source
    assert "#userAgentsList[hidden]" in styles


def test_search_before_planning_is_exposed_and_controls_workflow_requests():
    index = INDEX_HTML.read_text(encoding="utf-8")
    source = APP_JS.read_text(encoding="utf-8")

    assert 'id="searchBeforePlanning"' in index
    assert "Search Before Planning" in index
    assert "searchBeforePlanningInput" in source
    assert source.count("search_before_planning: searchBeforePlanningInput?.checked ?? false,") == 4


def test_workflow_execution_page_uses_a_responsive_primary_and_sidebar_layout():
    index = INDEX_HTML.read_text(encoding="utf-8")
    source = APP_JS.read_text(encoding="utf-8")
    styles = STYLES.read_text(encoding="utf-8")
    run_panel = index[index.index('id="panel-run"') : index.index('id="panel-chat"')]

    assert 'class="run-workspace-grid"' in run_panel
    assert 'class="run-primary-column"' in run_panel
    assert 'class="run-side-column"' in run_panel
    assert "工作流执行" in run_panel
    for label in ("自动滚动：开", "导出日志", "实时流程", "计划预览"):
        assert label in run_panel
    assert "grid-template-columns: minmax(0, 2fr) minmax(290px, 0.8fr);" in styles
    assert "max-width: 1280px;" in styles
    assert 'autoScrollEnabled ? "自动滚动：开" : "自动滚动：关"' in source


def test_main_agent_console_is_always_present_but_waits_for_conversation_selection():
    index = INDEX_HTML.read_text(encoding="utf-8")
    source = APP_JS.read_text(encoding="utf-8")
    run_panel = index[index.index('id="panel-run"') : index.index('id="panel-chat"')]

    primary_start = run_panel.index('class="run-primary-column"')
    decision_start = run_panel.index('id="mainAgentDecisionCard"')
    plan_start = run_panel.index('class="summary-card plan-editor-card"')
    assert primary_start < decision_start < plan_start
    assert 'id="mainAgentDecisionCard" style="display:none"' not in run_panel
    assert 'id="mainAgentDecisionContent" class="main-agent-decision-content" hidden' in run_panel
    assert 'id="routingDecisionBadge" class="tag" hidden' in run_panel
    assert "if (mainAgentDecisionContent) mainAgentDecisionContent.hidden = true;" in source
    assert "if (mainAgentDecisionContent) mainAgentDecisionContent.hidden = false;" in source
    assert "|| (activeConversationId" not in source[source.index("const renderDecisionHistoryControls") : source.index("const renderDecisionDetailControls")]
