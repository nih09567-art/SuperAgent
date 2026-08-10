from __future__ import annotations

import asyncio
import json
import logging

import httpx

from remote_agents.base_agent import (
    BaseRemoteAgent,
    bind_authorized_remote_tools,
    bind_tool_execution_trace,
    get_tool_execution_trace,
    reset_authorized_remote_tools,
    reset_tool_execution_trace,
)


class _DemoAgent(BaseRemoteAgent):
    async def execute(self, tools, messages, context, parameter_extractor):
        raise NotImplementedError


def test_mcp_execution_is_logged_and_added_to_agent_metadata(monkeypatch, caplog):
    async def scenario():
        import remote_agents.mcp_tool_client as mcp_client

        async def fake_call(tool_name, arguments, timeout=None):
            assert tool_name == "search_employees"
            arguments["run_manager"] = "framework-internal"
            return {"status": "success"}

        monkeypatch.setattr(
            mcp_client,
            "resolve_office_mcp_call",
            lambda _name, _arguments: (
                "search_employees",
                {"keyword": "sensitive-name", "limit": 3},
            ),
        )
        monkeypatch.setattr(mcp_client, "call_office_mcp_tool", fake_call)
        monkeypatch.setenv("REMOTE_TOOL_TRANSPORT", "mcp")

        authorization_token = bind_authorized_remote_tools(
            {
                "authorized_remote_tools": [
                    {
                        "tool_name": "remote_person_info_tool",
                        "arguments": {"employee_name": "sensitive-name"},
                    }
                ]
            }
        )
        trace_token = bind_tool_execution_trace()
        try:
            agent = _DemoAgent("demo", "demo")
            await agent.call_tool(
                "remote_person_info_tool",
                {"keyword": "sensitive-name"},
            )
            trace = get_tool_execution_trace()
            envelope = agent.result_envelope(outputs={"demo.output": {"ok": True}})
        finally:
            reset_tool_execution_trace(trace_token)
            reset_authorized_remote_tools(authorization_token)
        return trace, envelope

    with caplog.at_level(logging.INFO, logger="remote_agents.base_agent"):
        trace, envelope = asyncio.run(scenario())

    assert trace == [
        {
            "logical_tool": "remote_person_info_tool",
            "runtime_tool": "search_employees",
            "execution_transport": "mcp",
            "server_name": "office-mcp",
            "status": "success",
            "argument_keys": ["keyword", "limit"],
        }
    ]
    assert envelope["metadata"]["tool_executions"] == trace
    structured = next(
        record.message.split("=", 1)[1]
        for record in caplog.records
        if record.message.startswith("remote_tool_execution=")
    )
    assert json.loads(structured) == trace[0]
    assert "sensitive-name" not in structured


def test_request_scoped_traces_do_not_leak_between_async_tasks(monkeypatch):
    async def scenario():
        import remote_agents.mcp_tool_client as mcp_client

        async def fake_call(tool_name, arguments, timeout=None):
            await asyncio.sleep(0)
            return {"status": "success"}

        monkeypatch.setattr(
            mcp_client,
            "resolve_office_mcp_call",
            lambda name, arguments: (f"runtime_{arguments['keyword']}", arguments),
        )
        monkeypatch.setattr(mcp_client, "call_office_mcp_tool", fake_call)
        monkeypatch.setenv("REMOTE_TOOL_TRANSPORT", "mcp")

        async def one(label):
            authorization_token = bind_authorized_remote_tools(
                {
                    "authorized_remote_tools": [
                        {
                            "tool_name": "remote_person_info_tool",
                            "arguments": {"employee_name": label},
                        }
                    ]
                }
            )
            trace_token = bind_tool_execution_trace()
            try:
                await _DemoAgent("demo", "demo").call_tool(
                    "remote_person_info_tool", {"keyword": label}
                )
                return get_tool_execution_trace()
            finally:
                reset_tool_execution_trace(trace_token)
                reset_authorized_remote_tools(authorization_token)

        return await asyncio.gather(one("alpha"), one("beta"))

    first, second = asyncio.run(scenario())

    assert [item["runtime_tool"] for item in first] == ["runtime_alpha"]
    assert [item["runtime_tool"] for item in second] == ["runtime_beta"]
    assert first[0]["argument_keys"] == ["keyword"]
    assert second[0]["argument_keys"] == ["keyword"]


def test_http_execution_uses_the_same_redacted_trace_shape(monkeypatch):
    real_async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": {"status": "success"}})

    def client_factory(*_args, **kwargs):
        kwargs.pop("transport", None)
        return real_async_client(
            transport=httpx.MockTransport(handler),
            timeout=kwargs.get("timeout"),
        )

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    monkeypatch.setenv("REMOTE_TOOL_TRANSPORT", "http")

    async def scenario():
        authorization_token = bind_authorized_remote_tools(
            {
                "authorized_remote_tools": [
                    {"tool_name": "remote_weather_tool", "arguments": {}}
                ]
            }
        )
        trace_token = bind_tool_execution_trace()
        try:
            await _DemoAgent("demo", "demo").call_tool(
                "remote_weather_tool", {"location": "sensitive-location"}
            )
            return get_tool_execution_trace()
        finally:
            reset_tool_execution_trace(trace_token)
            reset_authorized_remote_tools(authorization_token)

    trace = asyncio.run(scenario())

    assert trace == [
        {
            "logical_tool": "remote_weather_tool",
            "runtime_tool": "remote_weather_tool",
            "execution_transport": "http",
            "server_name": "remote-tool-service",
            "status": "success",
            "argument_keys": ["location"],
        }
    ]
    assert "sensitive-location" not in json.dumps(trace)
