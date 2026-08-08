from langchain_anthropic import ChatAnthropic

from src.llm import llm as llm_module


def test_opencode_go_qwen_uses_messages_api(monkeypatch):
    monkeypatch.setattr(llm_module, "REASONING_MODEL", "qwen3.8-max")
    monkeypatch.setattr(
        llm_module, "REASONING_BASE_URL", "https://opencode.ai/zen/go/v1"
    )
    monkeypatch.setattr(llm_module, "REASONING_API_KEY", "test-api-key")
    llm_module._llm_cache.clear()

    client = llm_module.get_llm_by_type("reasoning")

    assert isinstance(client, ChatAnthropic)
    assert client.model == "qwen3.8-max"
    assert client.thinking == {"type": "disabled"}
    assert str(client._client.base_url).rstrip("/") == "https://opencode.ai/zen/go"


def test_other_opencode_go_models_keep_chat_completions_api():
    assert not llm_module._uses_anthropic_messages_api(
        "glm-5.2", "https://opencode.ai/zen/go/v1"
    )
