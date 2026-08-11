from pathlib import Path


ROOT = Path(__file__).parents[1]
APP_JS = ROOT / "web" / "app.js"
INDEX_HTML = ROOT / "web" / "index.html"
DIAGRAM_HTML = ROOT / "web" / "workflow-diagram.html"


def test_workflow_diagram_uses_planning_steps_for_execution_stage():
    source = APP_JS.read_text(encoding="utf-8")

    assert "const planningSteps = getWorkflowPlanningSteps(detail);" in source
    assert "? planningSteps.map((step) =>" in source
    assert "workflowStep: step" in source
    assert "&& isWorkflowPlanningLinear(planningSteps)" in source


def test_workflow_diagram_assets_share_the_latest_cache_version():
    index = INDEX_HTML.read_text(encoding="utf-8")
    standalone = DIAGRAM_HTML.read_text(encoding="utf-8")
    version = "20260810-task-history-scroll-1"

    assert f"/static/styles.css?v={version}" in index
    assert f"/static/app.js?v={version}" in index
    assert f"/static/styles.css?v={version}" in standalone
