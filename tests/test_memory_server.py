from fastapi.testclient import TestClient

from src.memory import CurrentRequestOverflowError
from src.memory.models import (
    CompactionBoundary,
    CompactionRecord,
    MemoryContextMetadata,
    PreparedMemoryContext,
)
from src.service.server import (
    Server,
    _assistant_memory_outputs,
    _is_visible_remote_agent,
)
from src.service.web_app import app


def test_web_aligned_launch_request_uses_memory_and_captures_stream(monkeypatch):
    captured = {}

    class FakeMemoryManager:
        async def prepare_context(self, **kwargs):
            captured["prepare"] = kwargs
            return PreparedMemoryContext(
                messages=(
                    {
                        "role": "assistant",
                        "content": "prior memory",
                        "metadata": {"memory_type": "long_term_reference"},
                    },
                    {"role": "user", "content": "current request"},
                ),
                metadata=MemoryContextMetadata(
                    session_id="thread",
                    token_estimate=10,
                    retrieved_memories=(
                        {
                            "memory_id": "memory-language",
                            "key": "preference.language",
                            "value": "zh",
                            "label": "Default response language: Chinese.",
                            "kind": "preference",
                            "scope": "user",
                            "confidence": 1.0,
                            "score": 1.0,
                        },
                    ),
                ),
            )

        async def record_assistant_outputs(self, **kwargs):
            captured["outputs"] = kwargs
            captured.setdefault("order", []).append("persist")
            return []

        async def compact_if_needed(self, **kwargs):
            captured["compact"] = kwargs
            captured.setdefault("order", []).append("compact")
            return CompactionRecord(
                compaction_id="compact-1",
                user_id="alice",
                session_id="thread",
                boundary=CompactionBoundary(
                    kind="automatic",
                    trigger="auto",
                    token_count_before=420,
                    token_count_after=180,
                    last_message_id="a-final",
                    last_sequence=3,
                    retained_message_ids=("u-current", "a-final"),
                    retained_turn_count=1,
                ),
                summary="summary",
                metadata={
                    "compaction_generation": 2,
                    "covered_user_message_ids": ["u-old"],
                    "markdown_projection_path": "memory_views/alice/compactions/thread/LATEST.md",
                },
            )

    async def fake_initialize():
        return None

    async def fake_reload(force=False):
        return None

    async def fake_workflow(**kwargs):
        captured["workflow"] = kwargs
        yield {
            "event": "start_of_workflow",
            "data": {"workflow_id": "alice:wf", "task_id": "task"},
        }
        yield {
            "event": "messages",
            "agent_name": "planner",
            "data": {"delta": {"content": "plan "}},
        }
        yield {
            "event": "messages",
            "agent_name": "planner",
            "data": {"delta": {"content": "done"}},
        }
        yield {
            "event": "messages",
            "agent_name": "agent_proxy",
            "data": {"delta": {"content": "internal tool stream"}},
        }
        yield {
            "event": "final_result",
            "data": {
                "workflow_id": "alice:wf",
                "status": "SUCCEEDED",
                "available": True,
                "result": "final report",
            },
        }
        yield {"event": "end_of_workflow", "data": {"workflow_id": "alice:wf"}}

    monkeypatch.setattr("src.service.server.get_memory_manager", lambda: FakeMemoryManager())
    monkeypatch.setattr("src.service.server.agent_manager.ensure_initialized", fake_initialize)
    monkeypatch.setattr(Server, "_trigger_mcp_reload", staticmethod(fake_reload))
    monkeypatch.setattr("src.service.server.run_agent_workflow", fake_workflow)

    instruction = "Analyze quarterly sales data and create an execution plan"
    payload = {
        "user_id": "alice",
        "lang": "zh",
        "workmode": "launch",
        "stop_after_planner": True,
        "instruction": instruction,
        "instruction_history": [instruction],
        "original_user_query": instruction,
        "messages": [{"role": "user", "content": instruction}],
        "debug": False,
        "deep_thinking_mode": False,
        "search_before_planning": False,
        "coor_agents": None,
        "workflow_id": None,
        "memory_enabled": True,
        "memory_session_id": "thread",
    }

    with TestClient(app) as client:
        response = client.post("/api/workflows/run", json=payload)

    assert response.status_code == 200
    assert "event: start_of_workflow" in response.text
    assert "event: end_of_workflow" in response.text
    assert "event: memory_compacted" in response.text
    assert '"generation": 2' in response.text
    assert '"token_count_before": 420' in response.text
    assert '"token_count_after": 180' in response.text
    assert captured["prepare"]["incoming_messages"] == [
        {
            "role": "user",
            "content": instruction,
            "message_id": None,
            "metadata": {},
        }
    ]
    assert captured["prepare"]["session_id"] == "thread"
    assert captured["prepare"]["request_enabled"] is True
    assert captured["prepare"]["retrieval_query"] == instruction
    assert captured["prepare"]["memory_keys"] == ()
    assert captured["prepare"]["attachments"]["current_plan"] == [instruction]
    assert captured["workflow"]["user_input_messages"][0]["content"] == "prior memory"
    assert captured["workflow"]["request_input_messages"] == [
        {"role": "user", "content": instruction}
    ]
    assert captured["workflow"]["workmode"].value == "launch"
    assert captured["workflow"]["stop_after_planner"] is True
    assert captured["workflow"]["instruction"] == instruction
    assert captured["workflow"]["instruction_history"] == [instruction]
    assert captured["workflow"]["original_user_query"] == instruction
    assert captured["workflow"]["memory_session_id"] == "thread"
    assert captured["workflow"]["memory_context"]["long_term_reference"] == "prior memory"
    assert captured["workflow"]["memory_context"]["retrieved_memories"][0]["key"] == (
        "preference.language"
    )
    assert captured["outputs"]["workflow_id"] == "alice:wf"
    assert captured["outputs"]["outputs"] == [
        {"agent_name": "planner", "content": "plan done", "user_visible": True},
        {
            "agent_name": "execution_result",
            "content": "final report",
            "user_visible": True,
        },
    ]
    assert captured["order"] == ["persist", "compact"]
    assert captured["compact"]["current_step_id"] == "assistant_persisted"


def test_web_reports_typed_error_for_oversized_current_request(monkeypatch):
    class FakeMemoryManager:
        def resolve_session_id(self, user_id, *, session_id=None):
            return session_id or user_id

        async def prepare_context(self, **_kwargs):
            raise CurrentRequestOverflowError(
                current_request_tokens=5000,
                input_budget=1000,
            )

    async def fake_initialize():
        return None

    async def fake_reload(force=False):
        return None

    monkeypatch.setattr("src.service.server.get_memory_manager", lambda: FakeMemoryManager())
    monkeypatch.setattr("src.service.server.agent_manager.ensure_initialized", fake_initialize)
    monkeypatch.setattr(Server, "_trigger_mcp_reload", staticmethod(fake_reload))
    payload = {
        "user_id": "alice",
        "lang": "zh",
        "workmode": "launch",
        "messages": [{"role": "user", "content": "large request"}],
        "debug": False,
        "deep_thinking_mode": False,
        "search_before_planning": False,
        "coor_agents": None,
        "memory_enabled": True,
        "memory_session_id": "thread",
    }

    with TestClient(app) as client:
        response = client.post("/api/workflows/run", json=payload)

    assert response.status_code == 200
    assert "CURRENT_REQUEST_CONTEXT_OVERFLOW" in response.text
    assert '"current_request_tokens": 5000' in response.text


def test_remote_result_fallback_keeps_visible_output_and_excludes_internal_agents():
    outputs = _assistant_memory_outputs({}, {"reporter": "visible final report"})

    assert outputs == [
        {
            "agent_name": "reporter",
            "content": "visible final report",
            "user_visible": True,
        }
    ]
    assert _is_visible_remote_agent("reporter") is True
    assert _is_visible_remote_agent("agent_proxy") is False
    assert _is_visible_remote_agent("scheduler:step-1") is False


def test_reporter_result_wrapped_by_agent_proxy_is_captured_as_user_visible():
    outputs = _assistant_memory_outputs(
        {}, {"agent_proxy【reporter】": "visible final report"}
    )

    assert outputs == [
        {
            "agent_name": "reporter",
            "content": "visible final report",
            "user_visible": True,
        }
    ]
    assert _is_visible_remote_agent("agent_proxy【reporter】") is True


def test_remote_result_fallback_redacts_and_marks_truncation():
    outputs = _assistant_memory_outputs(
        {},
        {
            "reporter": (
                "Bearer abcdefghijklmnop " + "visible report content " * 500
            )
        },
    )

    content = outputs[0]["content"]
    assert isinstance(content, str)
    assert len(content) == 8000
    assert content.endswith("...")
    assert "Bearer abcdefghijklmnop" not in content
    assert "[REDACTED]" in content
