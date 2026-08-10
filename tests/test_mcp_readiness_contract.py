import asyncio
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from src.manager.hot_reload.mcp_reload import MCPHotReloadManager
from src.manager.mcp import CONFIG_FILE_PATH, mcp_client_config
from src.service.web_app import _count_loaded_mcp_tools, create_app


APP_JS = Path(__file__).resolve().parents[1] / "web" / "app.js"


def test_default_mcp_config_includes_office_and_excel_servers():
    config = mcp_client_config()

    assert set(config) == {"office-mcp", "excel-mcp-remote"}
    assert config["office-mcp"]["url"] == "http://127.0.0.1:8013/sse"


def test_hot_reload_merges_tracked_defaults_with_local_overrides():
    manager = MCPHotReloadManager(
        registry=SimpleNamespace(),
        config_path=CONFIG_FILE_PATH,
    )

    config = asyncio.run(manager._load_config())

    assert set(config) == {"office-mcp", "excel-mcp-remote"}


def test_mcp_server_endpoint_is_not_shadowed_by_tool_detail_route():
    response = TestClient(create_app()).get("/api/mcp/servers")

    assert response.status_code == 200
    names = {server["name"] for server in response.json()["servers"]}
    assert names == {"office-mcp", "excel-mcp-remote"}


def test_readiness_counts_only_loaded_mcp_tools():
    tools = [
        SimpleNamespace(identifier=SimpleNamespace(is_mcp=True)),
        SimpleNamespace(identifier=SimpleNamespace(is_mcp=True)),
        SimpleNamespace(identifier=SimpleNamespace(is_mcp=False)),
        SimpleNamespace(),
    ]

    assert _count_loaded_mcp_tools(tools) == 2


def test_frontend_distinguishes_mcp_servers_from_loaded_tools():
    source = APP_JS.read_text(encoding="utf-8")

    assert "const mcpServerCount" in source
    assert "const mcpToolCount" in source
    assert 'fetch("/api/mcp/servers")' in source
    assert "MCP 服务：${servers.length}" in source
    assert "已加载工具：${mcpCount}" in source
    assert (
        "MCP 服务 ${mcpServerCount} 个 · 已加载工具 ${mcpToolCount} 个"
        in source
    )
