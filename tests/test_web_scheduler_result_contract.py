from pathlib import Path


APP_JS = Path(__file__).resolve().parents[1] / "web" / "app.js"
INDEX_HTML = Path(__file__).resolve().parents[1] / "web" / "index.html"


def _source() -> str:
    return APP_JS.read_text(encoding="utf-8")


def test_web_recognizes_scheduler_agents_and_result_events():
    source = _source()

    assert 'normalized.startsWith("scheduler")' in source
    assert 'eventName === "step_result"' in source
    assert 'eventName === "final_result"' in source
    assert "renderFinalResult(payload.data || {})" in source


def test_web_displays_context_compaction_event_details():
    source = _source()

    assert source.count('eventName === "memory_compacted"') == 2
    assert "const handleResumeEvent" in source
    assert "token_count_before" in source
    assert "token_count_after" in source
    assert "covered_message_count" in source
    assert "retained_turn_count" in source
    assert "summary_mode" in source


def test_web_assigns_and_preserves_stable_message_ids():
    source = _source()
    load_conversation = source[
        source.index("const loadConversation") : source.index(
            "const clearChatHistory = async"
        )
    ]

    assert "const createConversationMessageId" in source
    assert "message_id: metadata.message_id || createConversationMessageId(role)" in source
    assert "message_id: message.message_id" in source
    assert "message_id: message.message_id" in load_conversation
    assert "execute-confirmed-plan" in source


def test_web_memory_control_message_breaks_stale_clarification_state():
    source = _source()

    assert "const isStandaloneMemoryMessage" in source
    assert "&& !isStandaloneMemoryMessage(message)" in source


def test_web_handles_agent_skill_lifecycle_events():
    source = _source()

    assert 'eventName.startsWith("agent_skill_")' in source
    for event_name in (
        "agent_skill_matched",
        "agent_skill_promoted",
        "agent_skill_candidate",
        "agent_skill_disabled",
        "agent_skill_rejected",
    ):
        assert event_name in source


def test_web_keys_parallel_step_cards_by_scheduler_event_identity():
    source = _source()

    assert "executionStepCardsByKey = new Map()" in source
    assert "const findStepCard = (data = {})" in source
    assert "data.agent_id, data.step_id" in source
    assert 'if (existing.status === "running")' in source
    assert "executionStepCardsByKey.delete(normalizedKey)" in source
    assert "finalizeStepCard(findStepCard(data) || currentStepCard)" in source


def test_legacy_workflow_emits_unique_identity_for_each_node_execution():
    process_source = (
        Path(__file__).resolve().parents[1] / "src" / "workflow" / "process.py"
    ).read_text(encoding="utf-8")

    assert 'node_event_id = f"{workflow_id}_{original_node_name}_{step_count}"' in process_source
    assert process_source.count('"agent_id": node_event_id') == 2


def test_web_handles_all_scheduler_terminal_statuses():
    source = _source()

    for status in (
        "SUCCEEDED",
        "FAILED",
        "PARTIAL_FAILED",
        "CLARIFY_REQUIRED",
        "APPROVAL_REQUIRED",
        "REJECTED",
        "NEEDS_RECONCILIATION",
    ):
        assert f'case "{status}"' in source


def test_web_treats_approval_as_paused_instead_of_retry_blocked():
    source = _source()

    assert 'terminalStatus === "APPROVAL_REQUIRED"' in source
    assert 'status: "approval_pending"' in source
    assert 'approval_pending: "等待人工审批"' in source
    assert 'approval_approved: "审批已通过"' in source
    resume_source = source[source.index("const resumeTask = async"):]
    assert "activePendingPlan = null;" in resume_source


def test_web_treats_reconciliation_as_paused_and_refreshable():
    source = _source()

    assert 'terminalStatus === "NEEDS_RECONCILIATION"' in source
    assert 'serverStatus === "NEEDS_RECONCILIATION"' in source
    assert 'status: "reconciliation_pending"' in source
    assert 'normalized.startsWith("reconciliation_")' in source
    assert 'normalized.status.startsWith("reconciliation_")' in source
    assert 'reconciliation_pending: "等待人工核对"' in source
    assert 'reconciliation_retry_ready: "检查核对状态"' in source
    assert 'reconciliation_confirmed_succeeded: "检查核对状态"' in source
    assert 'reconciliation_terminated: "已人工终止"' in source
    assert '/api/security/reconciliations?task_id=${encodeURIComponent(normalized.taskId)}' in source
    resume_source = source[source.index("const resumeTask = async"):]
    assert 'resumeTerminalStatus === "NEEDS_RECONCILIATION"' in resume_source


def test_web_prefers_structured_failure_and_keeps_legacy_error_fallback():
    source = _source()

    assert "normalizeFailure = (failure, legacyError" in source
    assert "failure.message || legacyError" in source
    assert "data.failure || (status && status !== \"SUCCEEDED\")" in source
    assert "errorStepCard(content, card, data.failure)" in source
    assert 'data?.error || "该步骤未返回可展示的结果。"' in source


def test_web_failure_display_covers_actionable_categories_and_escapes_fields():
    source = _source()

    for value in (
        "UPSTREAM_STEP_FAILED",
        'category === "permission"',
        'category === "schema"',
        'category === "contract"',
        'category === "reconciliation"',
        "SIDE_EFFECT_UNCONFIRMED",
        "UNKNOWN_WORKFLOW_FAILURE",
    ):
        assert value in source

    for field in (
        "failure.message",
        "failure.code",
        "action",
        "retryText",
        'failure.blockedBy.join("、")',
    ):
        assert f"escapeHtml({field})" in source

    assert "parameterName: failure.parameter_name" in source
    assert "pre.textContent = JSON.stringify(data, null, 2)" in source
    assert "${escapeHtml(log.error)}" in source


def test_web_renders_terminal_failure_and_blocked_step_summary():
    source = _source()

    assert "workflowFailureSummary = {" in source
    assert "workflowData.failures" in source
    assert "workflowData.blocked_steps" in source
    assert "renderWorkflowFailureSummaryInto(workflowFailureSummary, frag)" in source
    assert 'section.setAttribute("aria-label", "工作流失败摘要")' in source
def test_web_routes_reconciliation_to_security_queue():
    source = _source()

    assert 'eventName === "reconciliation_required"' in source
    assert "Security → 人工核对队列" in source
    security_source = (
        Path(__file__).resolve().parents[1] / "web" / "security.js"
    ).read_text(encoding="utf-8")
    assert 'secFetch("/api/security/reconciliations"' in security_source
    assert "sessionStorage" not in security_source
    assert '"Authorization"' not in security_source
    assert "requester_id=" not in security_source
    for decision in ("retry", "succeeded", "freeze", "terminate"):
        assert f'data-decision="{decision}"' in security_source


def test_security_details_use_expected_collapsed_visibility():
    index = INDEX_HTML.read_text(encoding="utf-8")
    security_source = (
        Path(__file__).resolve().parents[1] / "web" / "security.js"
    ).read_text(encoding="utf-8")

    assert 'id="securityLastDeniedCard" hidden' in index
    assert 'id="toggleToolAccessBtn"' in index
    assert 'aria-controls="toolAccessGrid"' in index
    assert 'id="toolAccessGrid" class="sec-tool-grid" hidden' in index
    assert 'id="toggleAdvancedSecurityBtn"' in index
    assert 'id="advancedSecurityContent" class="sec-advanced-content" hidden' in index
    assert "高级/开发者信息" in index
    assert 'bindSecurityCollapseButton("toggleToolAccessBtn", "toolAccessGrid", true)' in security_source
    assert '"advancedSecurityContent",\n        true' in security_source
    assert 'setSecurityCollapse(\n        "toggleAdvancedSecurityBtn"' in security_source
    assert "if (card) card.hidden = true" in security_source
    assert "if (card) card.hidden = false" in security_source


def test_collection_views_auto_load_and_security_labels_are_localized():
    source = _source()
    security_source = (
        Path(__file__).resolve().parents[1] / "web" / "security.js"
    ).read_text(encoding="utf-8")
    styles = (
        Path(__file__).resolve().parents[1] / "web" / "styles.css"
    ).read_text(encoding="utf-8")

    auto_load = source[source.rindex("Promise.allSettled([") :]
    for call in ("fetchAgents()", "fetchTools()", "fetchWorkflows()", "fetchTasks()"):
        assert call in auto_load
    for label in ("策略", "Agent 属性", "资源属性", "角色：", "部门：", "信任等级：", "权限级别："):
        assert label in security_source
    for label in ("允许", "拒绝", "允许的 Agent：", "所有 Agent"):
        assert label in security_source
    assert "sec-agent-meta" in security_source
    assert ".sec-agent-meta" in styles
    assert "overflow-wrap: anywhere" in styles


def test_manual_queue_refresh_preserves_expanded_state():
    security_source = (
        Path(__file__).resolve().parents[1] / "web" / "security.js"
    ).read_text(encoding="utf-8")
    approvals_loader = security_source[
        security_source.index("async function loadSecurityApprovals"):
        security_source.index("async function loadSecurityReconciliations")
    ]
    reconciliations_loader = security_source[
        security_source.index("async function loadSecurityReconciliations"):
        security_source.index("async function decideSecurityReconciliation")
    ]

    assert "setSecurityCollapse" not in approvals_loader
    assert "setSecurityCollapse" not in reconciliations_loader


def test_clear_conversation_cascades_backend_history_before_local_storage():
    source = _source()

    assert 'fetch(`/api/tasks/${encodeURIComponent(taskId)}`' in source
    assert 'fetch(`/api/conversation-history?${query}`' in source
    assert "new URLSearchParams({ user_id: userId })" in source
    assert "X-Task-Owner-Token" not in source
    assert "getTaskCleanupHeaders" not in source
    assert '"Authorization"' not in source[source.index("const loadTaskGovernance"):]
    assert "conversation.decisions.flatMap((decision)" in source
    assert "Array.isArray(decision.taskIds) ? decision.taskIds : []" in source
    fetch_index = source.index('fetch(`/api/conversation-history?${query}`')
    local_delete_index = source.index(
        "localStorage.removeItem(getChatHistoryKey(userId))"
    )
    assert fetch_index < local_delete_index


def test_decision_console_selects_conversation_and_keeps_five_rounds():
    source = _source()
    index = INDEX_HTML.read_text(encoding="utf-8")

    assert 'id="decisionConversationSelect"' in index
    assert 'id="decisionRoundSelect"' in index
    assert "const DECISION_HISTORY_LIMIT = 5" in source
    assert "].slice(-DECISION_HISTORY_LIMIT)" in source
    assert "activeConversationDecisions.findIndex((item) => item.workflowId === workflowId)" in source
    assert "const rememberRoutingDecision = (eventData)" in source
    assert "const renderDecisionHistoryControls = ({" in source
    assert "decisions: activeConversationDecisions.map" in source
    decision_controls = source[
        source.index("const renderDecisionHistoryControls = ({"):
        source.index("const renderDecisionDetailControls")
    ]
    assert "|| conversations[0]" not in decision_controls
    assert "hideDecisionConsole();" in decision_controls
    assert "请选择对话" in decision_controls


def test_routing_decision_is_saved_before_history_console_renders():
    source = _source()
    event_branch = source[source.index('if (eventName === "routing_decision")'):]

    remember_index = event_branch.index(
        "const storedDecision = rememberRoutingDecision(latestRoutingDecision)"
    )
    save_index = event_branch.index("saveActiveConversation()")
    render_index = event_branch.index("renderDecisionHistoryControls({")

    assert remember_index < save_index < render_index

