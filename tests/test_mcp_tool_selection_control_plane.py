import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from src.manager.executor.base import ExecutionContext, ExecutionStatus
from src.manager.executor.local import LocalExecutor
from src.manager.executor.remote import RemoteExecutor
from src.manager.hot_reload.mcp_reload import MCPHotReloadManager
from src.manager.registry.tool_loader import ToolLoader
from src.manager.registry.tool_registry import ToolRegistry
from src.manager.registry.tool_selection_service import ToolSelectionService
from src.manager.registry.tool_semantics import load_mcp_tool_semantics
from src.manager.registry.tool_identifier import ToolIdentifier, ToolScope
from src.tools.office_mcp.selector import select_tools


ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads(
    (ROOT / "experiments" / "office_mcp_tool_selection_cases.json").read_text(
        encoding="utf-8-sig"
    )
)["cases"]


def _json_type(value):
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"


def _semantic_candidates():
    semantics = load_mcp_tool_semantics()
    examples = {
        (
            case.get("expected_server") or "office-mcp",
            case["expected_tool"],
        ): case["known_inputs"]
        for case in CASES
        if case.get("expected_tool") and case["kind"] == "normal"
    }
    candidates = []
    for (server, name), semantic in semantics.items():
        values = examples[(server, name)]
        properties = {
            key: {"type": _json_type(value)} for key, value in values.items()
        }
        for required in semantic["required_inputs"]:
            properties.setdefault(required, {"type": "string"})
        candidates.append(
            {
                "server_name": server,
                "name": name,
                "description": "",
                "input_schema": {
                    "type": "object",
                    "properties": properties,
                    "required": semantic["required_inputs"],
                },
                **semantic,
            }
        )
    return candidates


def test_semantic_overlays_cover_actual_18_plus_30_baseline_without_duplicates():
    semantics = load_mcp_tool_semantics()
    assert len(semantics) == 48
    assert sum(server == "excel-mcp-remote" for server, _ in semantics) == 18
    assert sum(server == "office-mcp" for server, _ in semantics) == 30
    assert len(semantics) == len(set(semantics))


def test_generic_selector_covers_every_discovered_tool_and_recognizes_server():
    candidates = _semantic_candidates()
    normal = [case for case in CASES if case["kind"] == "normal"]
    assert len(normal) == 48
    assert {
        (case.get("expected_server") or "office-mcp", case["expected_tool"])
        for case in normal
    } == {(item["server_name"], item["name"]) for item in candidates}

    for case in normal:
        expected_server = case.get("expected_server") or "office-mcp"
        decision = select_tools(
            task_text=case["query"],
            target_agent=case.get("agent_name"),
            known_inputs=case["known_inputs"],
            candidates=candidates,
            top_k=3,
            mode="audit",
        )
        assert decision["selected_tool"] == case["expected_tool"], case["id"]
        assert decision["server_name"] == expected_server, case["id"]
        assert decision["candidate_count_before_filter"] == 48
        assert decision["selected_tool_key"] == (
            f"{expected_server}:{case['expected_tool']}"
        )


def test_generic_selector_excludes_missing_inputs_and_abstains_unknown():
    candidates = _semantic_candidates()
    missing = select_tools(
        task_text="创建Excel图表",
        target_agent="coder",
        known_inputs={"filepath": "demo.xlsx", "sheet_name": "Sheet1"},
        candidates=candidates,
        mode="audit",
    )
    assert missing["selected_tool"] is None
    create_chart = next(
        item
        for item in missing["excluded"]
        if item["server_name"] == "excel-mcp-remote"
        and item["name"] == "create_chart"
    )
    assert any(
        reason["reason"] == "missing_required_inputs"
        for reason in create_chart["reasons"]
    )

    unknown = select_tools(
        task_text="生成增值税发票",
        target_agent=None,
        known_inputs={},
        candidates=candidates,
        mode="audit",
    )
    assert unknown["selected_tool"] is None


def test_registry_view_keys_by_server_and_runtime_name_without_scope_duplicates():
    async def scenario():
        registry = ToolRegistry()
        for server in ("server-a", "server-b"):
            tool = SimpleNamespace(name="same_name")
            await registry.register_tool(
                ToolIdentifier(ToolScope.GLOBAL, server, "same_name"), tool
            )
        await registry.register_agent_tool(
            "coder",
            ToolIdentifier(ToolScope.AGENT, "server-a", "same_name"),
            SimpleNamespace(name="same_name"),
        )
        view = await registry.mcp_metadata_view()
        assert set(view) == {
            ("server-a", "same_name"),
            ("server-b", "same_name"),
        }
        assert view[("server-a", "same_name")].identifier.scope == ToolScope.GLOBAL

    asyncio.run(scenario())


def test_tool_loader_uses_real_server_name_and_dynamic_schema(monkeypatch):
    semantics = load_mcp_tool_semantics()

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def get_tools(self, server_name):
            self.calls.append(server_name)
            return [
                SimpleNamespace(
                    name=name,
                    description=f"live:{name}",
                    args_schema={
                        "type": "object",
                        "properties": {
                            key: {"type": "string"}
                            for key in semantic["required_inputs"]
                        },
                        "required": semantic["required_inputs"],
                    },
                )
                for (server, name), semantic in semantics.items()
                if server == server_name
            ]

    async def scenario():
        registry = ToolRegistry()
        loader = ToolLoader(registry=registry)
        client = FakeClient()

        async def fake_client(_config, _hash):
            return client

        monkeypatch.setattr(
            "src.manager.registry.tool_loader.mcp_client_config",
            lambda: {"office-mcp": {}, "excel-mcp-remote": {}},
        )
        monkeypatch.setattr(loader, "_get_or_create_client", fake_client)
        loaded = await loader.load_mcp_tools()
        metadata = await registry.list_all_tools()
        assert loaded == 48
        assert client.calls == ["excel-mcp-remote", "office-mcp"]
        assert {(item.identifier.server, item.identifier.name) for item in metadata} == set(
            semantics
        )
        assert all(item.description.startswith("live:") for item in metadata)
        assert all(item.input_schema.get("type") == "object" for item in metadata)

    asyncio.run(scenario())


def test_hot_reload_preserves_dynamic_schema_and_semantic_identity(monkeypatch, tmp_path):
    semantics = load_mcp_tool_semantics()

    class FakeClient:
        async def get_tools(self, server_name):
            return [
                SimpleNamespace(
                    name=name,
                    description=f"hot:{name}",
                    args_schema={
                        "type": "object",
                        "properties": {
                            key: {"type": "string"}
                            for key in semantic["required_inputs"]
                        },
                        "required": semantic["required_inputs"],
                    },
                )
                for (server, name), semantic in semantics.items()
                if server == server_name
            ]

    async def scenario():
        manager = MCPHotReloadManager(
            ToolRegistry(), str(tmp_path / "unused.json")
        )

        async def fake_client(_hash, _config):
            return FakeClient()

        monkeypatch.setattr(manager, "_get_or_create_client", fake_client)
        metadata = await manager._load_mcp_tools(
            {
                "excel-mcp-remote": {"url": "http://example.test/excel"},
                "office-mcp": {"url": "http://example.test/office"},
            },
            ToolScope.GLOBAL,
        )
        assert len(metadata) == 48
        assert {
            (item.server_name, item.runtime_tool_name) for item in metadata
        } == set(semantics)
        assert all(item.description.startswith("hot:") for item in metadata)
        assert all(item.input_schema.get("type") == "object" for item in metadata)

    asyncio.run(scenario())


def test_off_mode_does_not_touch_registry(monkeypatch):
    class ExplodingRegistry:
        async def list_all_tools(self):
            raise AssertionError("off mode must not load the registry")

    monkeypatch.setenv("MCP_TOOL_SELECTION_MODE", "off")
    context = ExecutionContext(user_id="u1", metadata={})
    result = asyncio.run(
        ToolSelectionService(ExplodingRegistry()).audit(
            agent_name="coder",
            messages=[{"role": "user", "content": "读取Excel数据"}],
            context=context,
            actual_tool_names=["read_data_from_excel"],
        )
    )
    assert result is None
    assert "tool_selection_audit" not in context.metadata


def test_remote_executor_audit_preserves_request_tools_and_legacy_name(monkeypatch):
    async def scenario():
        semantics = load_mcp_tool_semantics()
        registry = ToolRegistry()
        semantic = semantics[("office-mcp", "search_employees")]
        await registry.register_tool(
            ToolIdentifier(ToolScope.GLOBAL, "office-mcp", "search_employees"),
            SimpleNamespace(name="search_employees"),
            description="Search employee profiles",
            input_schema={
                "type": "object",
                "properties": {"keyword": {"type": "string"}},
                "required": [],
            },
            semantics=semantic,
        )
        executor = RemoteExecutor(max_retries=1)
        executor._tool_selection_service = ToolSelectionService(registry)
        captured = {}

        async def fake_send(endpoint, data, headers, retries=None):
            captured["tools"] = [item["name"] for item in data.get("tools", [])]
            return {"status": "success", "result": {"status": "success"}}

        executor._send_request = fake_send
        agent = SimpleNamespace(
            source="remote",
            agent_name="RemoteHRAssistantAgent",
            endpoint="https://example.test/agent",
            prompt="query employee",
            selected_tools=[
                SimpleNamespace(
                    name="remote_person_info_tool",
                    description="legacy",
                    parameters={},
                )
            ],
        )
        context = ExecutionContext(
            user_id="u1",
            metadata={
                "known_inputs": {"keyword": "王强"},
                "authorized_remote_tools": [
                    {"tool_name": "remote_person_info_tool", "arguments": {}}
                ],
            },
        )
        result = await executor.execute(
            agent,
            [{"role": "user", "content": "搜索员工王强"}],
            context,
        )
        assert result.status == ExecutionStatus.SUCCESS
        assert captured["tools"] == ["remote_person_info_tool"]
        audit = result.metadata["tool_selection_audit"]
        assert audit["recommended_mcp_tool"] == "search_employees"
        assert audit["compatible_legacy_tool"] == "remote_person_info_tool"
        assert audit["actual_legacy_tool"] == "remote_person_info_tool"
        assert audit["recommendation_matches_actual"] is True

    monkeypatch.setenv("MCP_TOOL_SELECTION_MODE", "audit")
    asyncio.run(scenario())


def test_enforce_request_is_downgraded_to_audit_in_real_service(monkeypatch):
    monkeypatch.setenv("MCP_TOOL_SELECTION_MODE", "enforce")
    registry = ToolRegistry()
    context = ExecutionContext(user_id="u1", metadata={})
    report = asyncio.run(
        ToolSelectionService(registry).audit(
            agent_name="coder",
            messages=[{"role": "user", "content": "unknown"}],
            context=context,
            actual_tool_names=[],
        )
    )
    assert report["requested_mode"] == "enforce"
    assert report["mode"] == "audit"
    assert report["enforce_blocked"] is True


def test_local_executor_emits_audit_without_changing_supplied_tools(monkeypatch):
    class FakeAudit:
        async def audit(self, *, context, actual_tool_names, **_kwargs):
            context.metadata["tool_selection_audit"] = {
                "mode": "audit",
                "actual_tool_names": list(actual_tool_names),
            }

    class FakeReactAgent:
        async def ainvoke(self, state, config):
            return {"messages": [SimpleNamespace(content="done")]}

    tools = [SimpleNamespace(name="read_data_from_excel")]
    executor = LocalExecutor()
    executor._tool_selection_service = FakeAudit()
    monkeypatch.setattr("langgraph.prebuilt.create_react_agent", lambda *a, **k: FakeReactAgent())
    monkeypatch.setattr("src.llm.llm.get_llm_by_type", lambda *_a, **_k: object())
    monkeypatch.setattr("src.prompts.template.apply_prompt", lambda *_a, **_k: "prompt")
    monkeypatch.setattr("src.security.tool_wrapper.wrap_tools_for_agent", lambda value, *_a: value)
    agent = SimpleNamespace(agent_name="coder", llm_type="basic", prompt="work")
    context = ExecutionContext(user_id="u1", metadata={})
    result = asyncio.run(
        executor.execute_with_tools(
            agent,
            [{"role": "user", "content": "读取Excel数据"}],
            tools,
            context,
        )
    )
    assert result.status == ExecutionStatus.SUCCESS
    assert result.metadata["tool_count"] == 1
    assert result.metadata["tool_selection_audit"]["actual_tool_names"] == [
        "read_data_from_excel"
    ]
    assert tools[0].name == "read_data_from_excel"
