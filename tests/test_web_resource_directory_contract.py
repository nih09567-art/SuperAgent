from pathlib import Path


ROOT = Path(__file__).parents[1]
INDEX_HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
STYLES = (ROOT / "web" / "styles.css").read_text(encoding="utf-8")


def _panel(start_id: str, end_id: str) -> str:
    return INDEX_HTML[
        INDEX_HTML.index(f'id="{start_id}"') : INDEX_HTML.index(f'id="{end_id}"')
    ]


def test_agents_page_uses_localized_two_level_resource_toolbar():
    panel = _panel("panel-agents", "panel-tools")

    for label in (
        "Agent 中心",
        "搜索 Agent 名称、别名或描述",
        "全部 Agent",
        "按名称排序",
        "清除选择",
        "检查所选",
        "Agent 列表",
        "未选择 Agent",
    ):
        assert label in panel
    assert 'class="resource-toolbar-main"' in panel
    assert 'class="resource-toolbar-secondary"' in panel
    assert 'id="agentsVisibleCount"' in panel


def test_primary_navigation_keeps_the_requested_english_labels():
    for label in ("Workflow", "Agents", "Tools", "Workflow Library", "Task History"):
        assert f">{label}</button>" in INDEX_HTML


def test_tools_page_uses_localized_two_level_resource_toolbar_and_collapsed_mcp():
    panel = _panel("panel-tools", "panel-workflows")

    for label in (
        "工具目录",
        "搜索工具名称或描述",
        "全部来源",
        "按最近使用排序",
        "工具列表",
        "未选择工具",
        "MCP 服务",
    ):
        assert label in panel
    assert 'id="toolsVisibleCount"' in panel
    assert 'id="mcpToggle" class="ghost mcp-toggle collapsed"' in panel
    assert 'id="mcpContent" class="mcp-content collapsed"' in panel


def test_resource_directory_has_readable_cards_and_sticky_scrollable_details():
    assert "grid-template-columns: minmax(0, 1fr) 360px;" in STYLES
    assert "grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));" in STYLES
    assert "max-height: calc(100vh - 130px);" in STYLES
    assert ".resource-detail-empty" in STYLES
    assert 'card.setAttribute("role", "button")' in APP_JS
    assert 'toolsVisibleCount.textContent = `显示 ${filtered.length} 个`' in APP_JS
    assert 'agentsVisibleCount.textContent = `显示 ${filtered.length} 个`' in APP_JS


def test_agent_and_tool_detail_static_labels_are_localized():
    for label in (
        "功能说明",
        "可用工具",
        "健康状态",
        "使用情况",
        "服务端点",
        "参数结构",
        "复制参数结构",
        "正在加载工具详情",
    ):
        assert label in APP_JS
