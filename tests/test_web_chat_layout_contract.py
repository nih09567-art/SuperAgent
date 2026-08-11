from pathlib import Path


ROOT = Path(__file__).parents[1]
APP_JS = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
STYLES = (ROOT / "web" / "styles.css").read_text(encoding="utf-8")


def test_chat_empty_state_offers_prompts_and_hides_after_content_arrives():
    assert 'emptyState.className = "chat-empty-state"' in APP_JS
    assert '<div class="chat-empty-mark" aria-hidden="true">SA</div>' in APP_JS
    assert 'avatar.textContent = "SA";' in APP_JS
    assert "今天想完成什么？" in APP_JS
    assert APP_JS.count("data-chat-prompt=") == 4
    assert "emptyState.hidden = conversationClones.length > 0;" in APP_JS
    assert 'suggestion.addEventListener("click"' in APP_JS


def test_chat_history_has_a_new_conversation_action():
    assert 'historyNew.textContent = "+ 新对话";' in APP_JS
    assert 'historyNew.addEventListener("click", () => newButton.click());' in APP_JS


def test_chat_turns_are_edge_aligned_at_every_viewport_width():
    assert ".chat-conversation-view > .chat-turn" in STYLES
    assert ".chat-conversation-view > .chat-turn-assistant" in STYLES
    assert ".chat-conversation-view .chat-assistant-content" in STYLES
    assert ".chat-mirror-composer" in STYLES
    assert "width: min(calc(100% - 40px), 960px);" in STYLES
    assert "max-width: 960px;" in STYLES
    assert "margin-right: 0;" in STYLES
    assert "margin-left: 0;" in STYLES
    assert "width: 100%;" in STYLES
    assert "padding: 20px 0 26px;" in STYLES
    assert ".chat-suggestion-grid" in STYLES


def test_chat_visual_refinement_is_scoped_and_lightweight():
    assert "/* Chat visual refinement: calm, focused, conversation-first. */" in STYLES
    assert "#panel-chat .chat-lifecycle-section" in STYLES
    assert "#panel-chat .conversation-history-item.active" in STYLES
    assert "#panel-chat .chat-execution-steps-content .step-card" in STYLES
    assert "#panel-chat .chat-mirror-composer" in STYLES
    assert "animation: none;" in STYLES
    assert "background: var(--chat-green);" in STYLES


def test_search_before_planning_toggle_controls_every_workflow_request():
    index_html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert 'id="searchBeforePlanning"' in index_html
    assert 'const searchBeforePlanningInput = document.getElementById("searchBeforePlanning");' in APP_JS
    assert APP_JS.count("search_before_planning: searchBeforePlanningInput?.checked ?? false,") == 4
