from pathlib import Path
import re


APP_JS = Path(__file__).parents[1] / "web" / "app.js"


def test_every_workflow_run_request_sends_the_authenticated_user_header():
    source = APP_JS.read_text(encoding="utf-8")

    assert '"X-Authenticated-User": String(userId || "").trim()' in source

    workflow_run_requests = re.findall(
        r'fetch\("/api/workflows/run",\s*\{(.*?)\n\s*\}\)',
        source,
        flags=re.DOTALL,
    )
    assert len(workflow_run_requests) == 3
    assert all(
        "headers: getWorkflowRequestHeaders(userId)" in request
        for request in workflow_run_requests
    )


def test_execution_authorization_request_declares_json_content_type():
    source = APP_JS.read_text(encoding="utf-8")
    helper = re.search(
        r"const getExecutionAuthorizationHeaders = .*?\n\};",
        source,
        flags=re.DOTALL,
    )

    assert helper is not None
    assert "...getWorkflowRequestHeaders(userId)" in helper.group(0)
    assert '"Authorization"' not in helper.group(0)
    assert "window.prompt" not in helper.group(0)
