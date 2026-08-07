/**
 * S-ABAC Security Module for CoorAgent Web Studio
 * Handles security dashboard, user switching, and permission visualization.
 */

// Security State
let secUsers = [];
let secCurrentUser = null;
let secPolicies = [];
let secSystemStatus = null;
let secToolAccess = {};
let secAgentAttributes = {};
let secLastDeniedEvents = [];
let secApprovals = [];
let secReconciliations = [];
const SEC_MAX_DENIED_HISTORY = 5;

const SECURITY_ROLE_LABELS_ZH = {
    UniversalAssistant: "通用助理",
    HRAgent: "人力资源智能体",
    CodeAgent: "编程智能体",
    ResearchAgent: "研究智能体",
    CommunicationAgent: "通讯智能体",
};

const SECURITY_DEPARTMENT_LABELS_ZH = {
    System: "系统",
    HR: "人力资源",
    Engineering: "工程研发",
    Research: "研究",
    General: "通用",
    Office: "办公室",
};

const SECURITY_TRUST_LABELS_ZH = {
    HIGH: "高",
    MEDIUM: "中",
    LOW: "低",
};

const SECURITY_USER_DESCRIPTIONS_ZH = {
    admin: "拥有完整系统访问权限，可以调度任意智能体并使用任意工具。",
    hr_manager: "人力资源经理，可访问人员信息、薪资和文档工作流。",
    engineer: "软件工程师，可使用代码执行、搜索和浏览器工具。",
    researcher_user: "研究分析员，可使用搜索和网页抓取工具。",
    guest: "访客用户，仅拥有最低访问权限，只能使用基础搜索。",
    communication_officer: "通讯专员，可在匹配的场景中发送邮件和生成文档。",
};

const SECURITY_AGENT_LABELS_ZH = {
    RemoteHRAssistantAgent: "员工与薪资查询",
    RemoteDocumentGeneratorAgent: "文档生成",
    RemoteEmailDispatchAgent: "邮件发送",
    RemoteCommunicationAgent: "消息通知",
    RemoteMeetingManagerAgent: "会议安排",
    RemoteOfficeAssistantAgent: "办公与差旅行程",
    RemoteHRCalendarAgent: "日程查询",
    RemoteBusinessRiskAgent: "授信风险查询",
    RemoteReportAgent: "报告生成",
};

const SECURITY_TOOL_LABELS_ZH = {
    remote_docx_generator_tool: "文档生成工具",
    remote_email_tool: "邮件发送工具",
    remote_meeting_scheduling_tool: "会议安排工具",
    remote_notification_tool: "消息通知工具",
};

function formatSecurityTime(value) {
    if (!value) return "未知时间";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString("zh-CN", { hour12: false });
}

function reconciliationReasonZh(item) {
    const raw = String(item.error || "");
    const timeout = raw.match(/Tool\s+([^\s]+)\s+timed out after\s+(\d+)s/i);
    if (timeout) {
        const toolLabel = SECURITY_TOOL_LABELS_ZH[timeout[1]] || `远程工具 ${timeout[1]}`;
        return `${toolLabel}在 ${timeout[2]} 秒内未返回，系统无法确认外部操作是否已经完成。`;
    }
    if (/remote request timeout|timeout/i.test(raw)) {
        return "远程请求超时，系统无法确认外部操作是否已经完成。";
    }
    if (/side effect succeeded but no durable external operation id was returned/i.test(raw)) {
        return "外部操作可能已成功，但执行器没有返回可核验的外部操作编号，系统无法自动确认结果。";
    }
    if (/side effect succeeded but receipt persistence failed/i.test(raw)) {
        return "外部操作可能已成功，但执行回执保存失败，系统无法自动确认结果。";
    }
    if (/side effect returned but completion condition failed/i.test(raw)) {
        return "外部操作已经发起，但返回结果未满足完成条件，系统无法确认最终状态。";
    }
    if (/succeeded receipt.*output|receipt output/i.test(raw)) {
        return "外部操作已有成功回执，但回执中的结果数据不完整或无法校验，需要人工核对。";
    }
    return raw || "外部操作结果不确定，需要人工核对。";
}

function governanceStepLabel(value) {
    const raw = String(value || "").trim();
    const numbered = raw.match(/^step[_-]?(\d+)$/i);
    return numbered ? `第 ${numbered[1]} 步` : (raw || "未知步骤");
}

function governanceConversationContext(item) {
    const query = String(item.user_query || "").trim();
    const round = Number(item.execution_round || 0);
    const total = Number(item.execution_round_total || 0);
    const roundText = round > 0
        ? `第 ${round} 次执行${total >= round ? `（当前共 ${total} 次）` : ""}`
        : "执行轮次未知";
    const stepTitle = String(item.step_title || "").trim();
    return `
      <div class="sec-governance-context">
        <div><strong>触发时间：</strong>${escapeHtml(formatSecurityTime(item.created_at || item.task_created_at))}</div>
        <div><strong>所属对话/工作流执行轮次：</strong>${escapeHtml(roundText)}</div>
        <div class="sec-governance-query" title="${escapeHtml(query)}"><strong>用户问题：</strong>${escapeHtml(query || "历史记录中未保存原始问题")}</div>
        ${stepTitle ? `<div><strong>触发步骤：</strong>${escapeHtml(stepTitle)}</div>` : ""}
      </div>`;
}

function formatScenarioFitSummary(fitResult) {
    if (!fitResult || typeof fitResult !== "object") return "";

    const fit = fitResult.fit || "uncertain";
    const confidence = typeof fitResult.confidence === "number"
        ? ` (confidence ${Math.round(fitResult.confidence * 100)}%)`
        : "";
    const reason = fitResult.reason
        ? escapeHtml(fitResult.reason)
        : "No scenario-fit explanation provided.";
    const labelMap = {
        match: "Scenario match",
        mismatch: "Scenario mismatch",
        uncertain: "Scenario uncertain",
    };
    const label = labelMap[fit] || "Scenario uncertain";

    return `<div style="margin-top:8px"><strong>${label}</strong>${confidence}<br><span style="color:var(--muted)">${reason}</span></div>`;
}

// Security API Helpers

async function secFetch(url, options = {}) {
    const resp = await fetch(url, options);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
}

async function loadSecurityStatus() {
    try {
        secSystemStatus = await secFetch("/api/security/status");
        renderSecurityStatus();
    } catch (e) {
        document.getElementById("securityStatus").innerHTML =
            `<div class="sec-error">S-ABAC: ${escapeHtml(e.message)}</div>`;
    }
}

async function loadSecurityUsers() {
    try {
        secUsers = await secFetch("/api/security/users");
        const selectedUserId = populateUserSelects();
        if (selectedUserId) {
            await loadUserSecurityProfile(selectedUserId);
        }
    } catch (e) {
        console.warn("Failed to load security users:", e);
    }
}

async function loadSecurityPolicies() {
    try {
        secPolicies = await secFetch("/api/security/policies");
        renderPolicies();
    } catch (e) {
        console.warn("Failed to load policies:", e);
    }
}

async function loadSecurityApprovals() {
    const el = document.getElementById("securityApprovals");
    try {
        secApprovals = await secFetch("/api/security/approvals");
        renderSecurityApprovals();
    } catch (e) {
        if (el) {
            el.innerHTML = `<div class="sec-error">审批队列加载失败：${escapeHtml(e.message)}</div>`;
        }
    }
}

async function loadSecurityReconciliations() {
    const el = document.getElementById("securityReconciliations");
    try {
        secReconciliations = await secFetch("/api/security/reconciliations");
        renderSecurityReconciliations();
    } catch (e) {
        if (el) {
            el.innerHTML = `<div class="sec-error">人工核对队列加载失败：${escapeHtml(e.message)}</div>`;
        }
    }
}

async function decideSecurityReconciliation(reconciliationId, decision) {
    let externalOperationId = "";
    let outputs = {};
    if (decision === "succeeded") {
        externalOperationId = window.prompt(
            "请输入成功凭证编号（邮件 ID、文档 ID、会议 ID 或业务流水号）：",
            ""
        );
        if (externalOperationId === null) return null;
        if (!externalOperationId.trim()) {
            throw new Error("确认成功必须填写凭证编号");
        }
    }
    if (decision === "succeeded") {
        const reconciliation = secReconciliations.find(
            (item) => item.reconciliation_id === reconciliationId
        );
        const expectedOutputs = Array.isArray(reconciliation?.expected_outputs)
            ? reconciliation.expected_outputs.filter(Boolean)
            : [];
        if (expectedOutputs.length) {
            const suggested = Object.fromEntries(
                expectedOutputs.map((name) => [name, externalOperationId.trim()])
            );
            const rawOutputs = window.prompt(
                `请确认输出 Contract（必填：${expectedOutputs.join(", ")}），格式为 JSON：`,
                JSON.stringify(suggested)
            );
            if (rawOutputs === null) return null;
            try {
                outputs = JSON.parse(rawOutputs);
            } catch (_error) {
                throw new Error("输出 Contract 必须是有效的 JSON 对象");
            }
            if (!outputs || Array.isArray(outputs) || typeof outputs !== "object") {
                throw new Error("输出 Contract 必须是 JSON 对象");
            }
            const missing = expectedOutputs.filter(
                (name) => !(name in outputs) || outputs[name] === null
            );
            if (missing.length) {
                throw new Error(`输出 Contract 缺少必填项：${missing.join(", ")}`);
            }
        }
    }
    const prompts = {
        retry: "请确认已在外部系统核对：操作没有发生。可填写核对依据：",
        succeeded: "可填写确认成功的核对依据：",
        freeze: "可填写继续冻结的原因：",
        terminate: "可填写终止原因：",
    };
    const comment = window.prompt(prompts[decision] || "处置说明：", "");
    if (comment === null) return null;
    const response = await fetch(
        `/api/security/reconciliations/${encodeURIComponent(reconciliationId)}/${decision}`,
        {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                comment,
                external_operation_id: externalOperationId.trim(),
                outputs,
            }),
        }
    );
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
        throw new Error(data.detail || `HTTP ${response.status}`);
    }
    await loadSecurityReconciliations();
    return data;
}

function renderSecurityReconciliations() {
    const el = document.getElementById("securityReconciliations");
    if (!el) return;
    if (!secReconciliations.length) {
        el.innerHTML = '<div class="sec-empty">暂无人工核对记录</div>';
        return;
    }

    const labels = {
        pending: "待核对",
        frozen: "已冻结",
        retry_ready: "已确认未执行，可安全重试",
        confirmed_succeeded: "已确认外部执行成功",
        resuming: "正在恢复原任务",
        consumed: "已恢复完成",
        terminated: "已人工终止",
    };
    el.innerHTML = secReconciliations.slice(0, 20).map((item) => {
        const status = String(item.status || "pending").toLowerCase();
        const isActionable = status === "pending" || status === "frozen";
        const canResume = status === "retry_ready" || status === "confirmed_succeeded";
        const externalId = item.external_operation_id
            ? `<div><strong>外部操作编号：</strong>${escapeHtml(item.external_operation_id)}</div>`
            : "";
        const agentLabel = SECURITY_AGENT_LABELS_ZH[item.agent_name] || item.agent_name || "未知执行器";
        const handledAt = item.updated_at && item.updated_at !== item.created_at
            ? `<div><strong>最近处理时间：</strong>${escapeHtml(formatSecurityTime(item.updated_at))}</div>`
            : "";
        const actions = isActionable
            ? `<div class="sec-approval-actions sec-reconciliation-actions">
                 <button class="primary sec-reconciliation-decide" data-id="${escapeHtml(item.reconciliation_id)}" data-decision="retry" type="button">确认未执行，安全重试</button>
                 <button class="ghost sec-reconciliation-decide" data-id="${escapeHtml(item.reconciliation_id)}" data-decision="succeeded" type="button">确认已成功</button>
                 <button class="ghost sec-reconciliation-decide" data-id="${escapeHtml(item.reconciliation_id)}" data-decision="freeze" type="button">保持冻结</button>
                 <button class="ghost danger sec-reconciliation-decide" data-id="${escapeHtml(item.reconciliation_id)}" data-decision="terminate" type="button">人工终止</button>
               </div>`
            : "";
        const resume = canResume
            ? `<div class="sec-approval-actions">
                 <button class="primary sec-reconciliation-resume" data-task-id="${escapeHtml(item.task_id)}" data-workflow-id="${escapeHtml(item.workflow_id)}" data-resume-step="${Number(item.resume_step) || 1}" data-user-id="${escapeHtml(item.user_id)}" type="button">继续原任务</button>
               </div>`
            : "";
        return `
          <div class="sec-approval-item status-${escapeHtml(status)}">
            <div class="sec-approval-header">
              <strong>${escapeHtml(labels[status] || status.toUpperCase())}</strong>
              <span class="tag warn">${escapeHtml(governanceStepLabel(item.step_id))}</span>
            </div>
            ${governanceConversationContext(item)}
            <div class="sec-approval-body">
              <div><strong>执行器：</strong>${escapeHtml(agentLabel)}</div>
              <div title="${escapeHtml(item.error || "")}"><strong>触发原因：</strong>${escapeHtml(reconciliationReasonZh(item))}</div>
              ${handledAt}
              ${externalId}
            </div>
            <details class="sec-governance-technical">
              <summary>查看内部编号</summary>
              <div>核对编号：${escapeHtml(item.reconciliation_id)}</div>
              <div>任务编号：${escapeHtml(item.task_id)}</div>
              <div>工作流编号：${escapeHtml(item.workflow_id || "-")}</div>
            </details>
            ${actions}${resume}
          </div>`;
    }).join("");

    el.querySelectorAll(".sec-reconciliation-decide").forEach((button) => {
        button.addEventListener("click", async () => {
            if (button.dataset.decision === "terminate") {
                const confirmed = window.confirm(
                    "终止后任务不会继续执行，未确认回执仍会保留。确定终止吗？"
                );
                if (!confirmed) return;
            }
            button.disabled = true;
            try {
                await decideSecurityReconciliation(
                    button.dataset.id,
                    button.dataset.decision
                );
            } catch (e) {
                window.alert(`人工核对操作失败：${e.message}`);
                button.disabled = false;
            }
        });
    });
    el.querySelectorAll(".sec-reconciliation-resume").forEach((button) => {
        button.addEventListener("click", async () => {
            if (typeof window.resumeApprovedTask === "function") {
                button.disabled = true;
                try {
                    await window.resumeApprovedTask({
                        task_id: button.dataset.taskId,
                        workflow_id: button.dataset.workflowId,
                        resume_step: Number(button.dataset.resumeStep) || 1,
                        user_id: button.dataset.userId || "test",
                    });
                    await loadSecurityReconciliations();
                } catch (e) {
                    window.alert(`恢复原任务失败：${e.message || e}`);
                    button.disabled = false;
                }
            }
        });
    });
}

async function decideSecurityApproval(approvalId, decision) {
    const comment = window.prompt(
        decision === "approve" ? "审批意见（可选）" : "拒绝原因（可选）",
        ""
    );
    if (comment === null) return;
    const response = await fetch(
        `/api/security/approvals/${encodeURIComponent(approvalId)}/${decision}`,
        {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ comment }),
        }
    );
    if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.detail || `HTTP ${response.status}`);
    }
    await loadSecurityApprovals();
}

function renderSecurityApprovals() {
    const el = document.getElementById("securityApprovals");
    if (!el) return;
    if (!secApprovals.length) {
        el.innerHTML = '<div class="sec-empty">暂无审批记录</div>';
        return;
    }

    const labels = {
        pending: "待审批",
        approved: "已批准，待恢复",
        consumed: "已恢复完成",
        rejected: "已拒绝",
        expired: "已过期",
    };
    el.innerHTML = secApprovals.slice(0, 20).map((item) => {
        const status = String(item.status || "pending").toLowerCase();
        const reason = item.policy_result?.reason || "策略要求人工审批";
        const target = item.object?.id || item.node_name || "unknown";
        const action = item.action?.attributes?.action_type || item.action?.verb || "execute";
        const pendingActions = status === "pending"
            ? `<div class="sec-approval-actions">
                 <button class="primary sec-approval-decide" data-id="${escapeHtml(item.approval_id)}" data-decision="approve" type="button">批准</button>
                 <button class="ghost sec-approval-decide" data-id="${escapeHtml(item.approval_id)}" data-decision="reject" type="button">拒绝</button>
               </div>`
            : "";
        const resumeAction = status === "approved"
            ? `<div class="sec-approval-actions">
                 <button class="primary sec-approval-resume" data-task-id="${escapeHtml(item.task_id)}" data-workflow-id="${escapeHtml(item.workflow_id)}" data-resume-step="${Number(item.resume_step) || 1}" data-user-id="${escapeHtml(item.user_id)}" type="button">恢复任务</button>
               </div>`
            : "";
        return `
          <div class="sec-approval-item status-${escapeHtml(status)}">
            <div class="sec-approval-header">
              <strong>${escapeHtml(labels[status] || status)}</strong>
              <span class="tag accent">${escapeHtml(governanceStepLabel(item.step_id || item.node_name))}</span>
            </div>
            ${governanceConversationContext(item)}
            <div class="sec-approval-body">
              <div><strong>操作：</strong>${escapeHtml(action)} → ${escapeHtml(target)}</div>
              <div><strong>审批原因：</strong>${escapeHtml(reason)}</div>
              ${item.updated_at && item.updated_at !== item.created_at
                ? `<div><strong>最近处理时间：</strong>${escapeHtml(formatSecurityTime(item.updated_at))}</div>`
                : ""}
            </div>
            <details class="sec-governance-technical">
              <summary>查看内部编号</summary>
              <div>审批编号：${escapeHtml(item.approval_id)}</div>
              <div>任务编号：${escapeHtml(item.task_id)}</div>
              <div>工作流编号：${escapeHtml(item.workflow_id || "-")}</div>
            </details>
            ${pendingActions}${resumeAction}
          </div>`;
    }).join("");

    el.querySelectorAll(".sec-approval-decide").forEach((button) => {
        button.addEventListener("click", async () => {
            button.disabled = true;
            try {
                await decideSecurityApproval(button.dataset.id, button.dataset.decision);
            } catch (e) {
                window.alert(`审批操作失败：${e.message}`);
                button.disabled = false;
            }
        });
    });
    el.querySelectorAll(".sec-approval-resume").forEach((button) => {
        button.addEventListener("click", () => {
            if (typeof window.resumeApprovedTask === "function") {
                window.resumeApprovedTask({
                    task_id: button.dataset.taskId,
                    workflow_id: button.dataset.workflowId,
                    resume_step: Number(button.dataset.resumeStep) || 1,
                    user_id: button.dataset.userId || "test",
                });
            }
        });
    });
}

async function loadUserSecurityProfile(userId) {
    try {
        const data = await secFetch(`/api/security/users/${userId}`);
        secCurrentUser = data;
        secToolAccess = data.tool_access || {};
        renderUserProfile();
        renderAgentsList();
        renderToolAccessGrid();

        const userIdInput = document.getElementById("userId");
        if (userIdInput && data.user_id) {
            userIdInput.value = data.user_id;
        }

        const demoRole = document.getElementById("demoUserRole");
        if (demoRole && data.user_id && demoRole.value !== data.user_id) {
            demoRole.value = data.user_id;
        }
    } catch (e) {
        console.warn("Failed to load user profile:", e);
    }
}

// Render Functions

function renderSecurityStatus() {
    const el = document.getElementById("securityStatus");
    if (!el || !secSystemStatus) return;

    const enabled = secSystemStatus.s_abac_enabled;
    const schedulerEnabled = secSystemStatus.orchestration_scheduler_enabled;
    const recoveryEnabled = secSystemStatus.auto_recovery_enabled;
    const recoveryAttempts = Number(
        secSystemStatus.auto_recovery_max_attempts || 0
    );
    el.innerHTML = `
        <div class="sec-status-row">
            <span class="sec-status-dot ${enabled ? "on" : "off"}"></span>
            <span class="sec-status-label">S-ABAC：<strong>${enabled ? "已启用" : "已停用"}</strong></span>
        </div>
        <div class="sec-status-row">
            <span class="sec-status-dot ${schedulerEnabled ? "on" : "off"}"></span>
            <span class="sec-status-label">TaskGraph 调度器：<strong>${schedulerEnabled ? "已启用" : "已停用"}</strong></span>
        </div>
        <div class="sec-status-row">
            <span class="sec-status-dot ${recoveryEnabled ? "on" : "off"}"></span>
            <span class="sec-status-label">DAG 自动恢复：<strong>${recoveryEnabled ? "已启用" : "已停用"}</strong>${recoveryEnabled ? ` · 最多尝试 ${recoveryAttempts} 次` : ""}</span>
        </div>
        <div class="sec-stats">
            <div class="sec-stat"><span>${secSystemStatus.policies_count}</span><small>策略</small></div>
            <div class="sec-stat"><span>${secSystemStatus.agent_attributes_count}</span><small>智能体属性</small></div>
            <div class="sec-stat"><span>${secSystemStatus.resource_attributes_count}</span><small>资源属性</small></div>
        </div>
    `;
}

function populateUserSelects() {
    const sel = document.getElementById("securityUserSelect");
    if (!sel) return "";

    const requestedUserId = String(
        document.getElementById("userId")?.value
        || document.getElementById("demoUserRole")?.value
        || "admin"
    ).trim();
    sel.innerHTML = "";
    secUsers.forEach((u) => {
        sel.innerHTML += `<option value="${u.user_id}">${u.icon} ${u.display_name}</option>`;
    });
    const selectedUser = secUsers.find((user) => user.user_id === requestedUserId)
        || secUsers[0];
    if (!selectedUser) return "";
    sel.value = selectedUser.user_id;
    return selectedUser.user_id;
}

function renderUserProfile() {
    const el = document.getElementById("userProfileDetail");
    if (!el || !secCurrentUser) return;

    const p = secCurrentUser.profile;
    const levelBar = getClearanceBar(p.clearance_level);
    const roleLabel = SECURITY_ROLE_LABELS_ZH[p.role] || p.role;
    const departmentLabel = SECURITY_DEPARTMENT_LABELS_ZH[p.department] || p.department;
    const trustLabel = SECURITY_TRUST_LABELS_ZH[p.trust_level] || p.trust_level;
    const description = SECURITY_USER_DESCRIPTIONS_ZH[secCurrentUser.user_id] || p.description;
    el.innerHTML = `
        <div class="sec-profile-card">
            <div class="sec-profile-icon">${p.icon || "USER"}</div>
            <div class="sec-profile-info">
                <strong>${escapeHtml(p.display_name)}</strong>
                <div>角色：<span class="tag accent">${escapeHtml(roleLabel)}</span></div>
                <div>部门：${escapeHtml(departmentLabel)} | 信任等级：${escapeHtml(trustLabel)}</div>
                <div class="sec-clearance">权限等级：${levelBar}</div>
                <div class="sec-desc">${escapeHtml(description)}</div>
            </div>
        </div>
    `;
}

function getClearanceBar(level) {
    const max = 5;
    let bar = '<span class="clearance-bar">';
    for (let i = 1; i <= max; i++) {
        bar += `<span class="clearance-seg ${i <= level ? "filled" : ""}" style="--lvl:${i}"></span>`;
    }
    bar += `</span><span class="clearance-text">${level} 级</span>`;
    return bar;
}

function renderAgentsList() {
    const el = document.getElementById("userAgentsList");
    if (!el || !secCurrentUser) return;

    const agents = secCurrentUser.available_agents || [];
    if (!agents.length) {
        el.innerHTML = '<div class="sec-empty">No available agents for this user.</div>';
        return;
    }

    el.innerHTML = agents.map((a) => `
        <div class="sec-agent-item">
            <span class="sec-agent-name">AGENT ${escapeHtml(a.agent_name)}</span>
            <span class="tag">${escapeHtml(a.role)}</span>
            <span class="tag accent">CL${a.clearance_level}</span>
        </div>
    `).join("");
}

function renderToolAccessGrid() {
    const el = document.getElementById("toolAccessGrid");
    if (!el || !secToolAccess) return;

    const tools = Object.entries(secToolAccess);
    if (!tools.length) {
        el.innerHTML = '<div class="sec-empty">No tool-access data.</div>';
        return;
    }

    el.innerHTML = tools.map(([name, info]) => {
        const icon = info.can_access ? "ALLOW" : "DENY";
        const cls = info.can_access ? "allowed" : "denied";
        return `
            <div class="sec-tool-row ${cls}">
                <span class="sec-tool-icon">${icon}</span>
                <span class="sec-tool-name">${escapeHtml(name)}</span>
                <span class="tag ${info.sensitivity === "HIGH" || info.sensitivity === "CRITICAL" ? "warn" : ""}">${info.sensitivity}</span>
                <span class="sec-tool-roles">${(info.allowed_roles || []).join(", ") || "any"}</span>
            </div>
        `;
    }).join("");
}

function renderPolicies() {
    const el = document.getElementById("policyList");
    if (!el || !secPolicies.length) {
        if (el) el.innerHTML = '<div class="sec-empty">No policy data.</div>';
        return;
    }

    el.innerHTML = secPolicies.map((p) => `
        <div class="sec-policy-card">
            <div class="sec-policy-header">
                <strong>${escapeHtml(p.policy_id)}</strong>
                <span class="tag accent">${p.rules.length} rule(s)</span>
            </div>
            <div class="sec-policy-desc">${escapeHtml(p.description)}</div>
            ${p.rules.map((r) => `
                <div class="sec-policy-rule">
                    <span class="tag ${r.effect === "ALLOW" ? "accent" : "warn"}">${r.effect}</span>
                    ${renderRuleCondition(r.condition)}
                </div>
            `).join("")}
        </div>
    `).join("");
}

function renderLastDeniedEvent() {
    const el = document.getElementById("securityLastDenied");
    const card = document.getElementById("securityLastDeniedCard");
    if (!el) return;

    if (!secLastDeniedEvents.length) {
        if (card) card.hidden = true;
        el.replaceChildren();
        return;
    }

    if (card) card.hidden = false;

    el.innerHTML = secLastDeniedEvents.map((d, idx) => {
        const policy = d.policy_result || {};
        const subject = d.subject || {};
        const object = d.object || {};
        const action = d.action || {};
        const scenarioFit = d.scenario_fit_result || {};

        const subjectName = subject.subject_name || subject.attributes?.display_name || subject.id || "Unknown user";
        const subjectRole = subject.attributes?.job_role || subject.attributes?.role || "Unknown role";
        const objectName = object.object_name || object.id || "Unknown object";
        const objectSensitivity = object.attributes?.sensitivity || "UNKNOWN";
        const actionVerb = action.attributes?.action_type || action.verb || "unknown";
        const deniedReason = policy.reason || d.error || "Permission denied";

        return `
            <div class="sec-policy-card" style="border-left:3px solid var(--danger); margin-bottom:${idx === secLastDeniedEvents.length - 1 ? "0" : "10px"}">
                <div class="sec-policy-header">
                    <strong>${idx === 0 ? "Most recent denied event" : `Denied history #${idx + 1}`}</strong>
                    <span class="tag warn">${escapeHtml(objectSensitivity)}</span>
                </div>
                <div class="sec-policy-desc">
                    <div><strong>Subject:</strong> ${escapeHtml(subjectName)} <span class="tag accent">${escapeHtml(subjectRole)}</span></div>
                    <div style="margin-top:6px"><strong>Target:</strong> ${escapeHtml(actionVerb)} -> ${escapeHtml(objectName)}</div>
                    <div style="margin-top:6px;color:var(--danger)"><strong>Reason:</strong> ${escapeHtml(deniedReason)}</div>
                    ${formatScenarioFitSummary(scenarioFit)}
                </div>
            </div>
        `;
    }).join("");
}

function renderRuleCondition(cond) {
    if (!cond || !cond.all) return "";
    return cond.all.map((c) => {
        const entries = Object.entries(c);
        if (!entries.length) return "";
        const [key, val] = entries[0];
        const shortKey = key
            .replace("subject.attributes.", "subj.")
            .replace("object.attributes.", "obj.")
            .replace("action.", "act.");
        return `<span class="sec-cond">${shortKey}=${JSON.stringify(val)}</span>`;
    }).join(" ");
}

// Event Handlers

function bindSecurityCollapseButton(buttonId, contentId, defaultCollapsed) {
    const button = document.getElementById(buttonId);
    const content = document.getElementById(contentId);
    if (!button || !content) return;

    const update = (collapsed) => {
        content.hidden = collapsed;
        button.setAttribute("aria-expanded", String(!collapsed));
        const label = button.querySelector(".sec-collapse-label");
        const icon = button.querySelector(".sec-collapse-icon");
        if (label) label.textContent = collapsed ? "展开" : "收起";
        if (icon) icon.textContent = collapsed ? "⌄" : "⌃";
    };

    update(defaultCollapsed);
    button.addEventListener("click", () => update(!content.hidden));
}

function initSecurityTab() {
    bindSecurityCollapseButton("toggleToolAccessBtn", "toolAccessGrid", true);
    bindSecurityCollapseButton(
        "toggleAdvancedSecurityBtn",
        "advancedSecurityContent",
        true
    );

    const sel = document.getElementById("securityUserSelect");
    if (sel) {
        sel.addEventListener("change", () => {
            if (sel.value) {
                loadUserSecurityProfile(sel.value);
            }
        });
    }

    const refreshApprovalsBtn = document.getElementById("refreshApprovalsBtn");
    if (refreshApprovalsBtn) {
        refreshApprovalsBtn.addEventListener("click", loadSecurityApprovals);
    }

    const refreshReconciliationsBtn = document.getElementById("refreshReconciliationsBtn");
    if (refreshReconciliationsBtn) {
        refreshReconciliationsBtn.addEventListener(
            "click",
            loadSecurityReconciliations
        );
    }

    const demoRole = document.getElementById("demoUserRole");
    if (demoRole) {
        demoRole.addEventListener("change", () => {
            const userIdInput = document.getElementById("userId");
            if (userIdInput) {
                userIdInput.value = demoRole.value;
            }
            if (demoRole.value) {
                loadUserSecurityProfile(demoRole.value);
                if (typeof loadPermissionSummary === "function") {
                    loadPermissionSummary(demoRole.value);
                }
            }
        });
    }
}

// Security Event Display (for Run output)

function displaySecurityEvent(eventName, data) {
    const container = document.getElementById("executionOutput");
    if (!container) return;

    let html = "";
    if (eventName === "permission_denied") {
        secLastDeniedEvents = [data, ...secLastDeniedEvents].slice(0, SEC_MAX_DENIED_HISTORY);
        renderLastDeniedEvent();

        const policy = data.policy_result || {};
        html = `
            <div class="step-card error" style="margin-top:8px">
                <div class="step-card-header" style="background:rgba(217,83,79,0.1)">
                    <span class="step-status-icon">DENY</span>
                    <span class="step-agent-name">S-ABAC Permission Denied</span>
                    <span class="step-summary-text">${escapeHtml(data.error || policy.reason || "Permission denied")}</span>
                </div>
                <div class="step-card-body">
                    <div class="sec-event-detail">
                        <div style="color:var(--danger)"><strong>Reason:</strong> ${escapeHtml(policy.reason || data.error || "Permission denied")}</div>
                        ${formatScenarioFitSummary(data.scenario_fit_result)}
                        <div style="margin-top:8px;color:var(--muted);font-size:12px">
                            Retry with a user that has higher privileges, or contact an administrator.
                        </div>
                    </div>
                </div>
            </div>`;
    }

    if (html) {
        const wrapper = document.createElement("div");
        wrapper.innerHTML = html;
        container.appendChild(wrapper.firstElementChild);
        if (typeof autoScrollEnabled !== "undefined" && autoScrollEnabled) {
            container.scrollTop = container.scrollHeight;
        }
    }
}

// Initialize

function initSecurityModule() {
    initSecurityTab();
    loadSecurityStatus();
    loadSecurityUsers();
    loadSecurityPolicies();
    loadSecurityApprovals();
    loadSecurityReconciliations();
    renderLastDeniedEvent();

    const demoRole = document.getElementById("demoUserRole");
    if (demoRole && demoRole.value) {
        if (typeof loadPermissionSummary === "function") {
            loadPermissionSummary(demoRole.value);
        }
    }
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => {
        setTimeout(initSecurityModule, 500);
    });
} else {
    setTimeout(initSecurityModule, 500);
}

// Expose for integration with app.js

window.SecurityModule = {
    loadUserSecurityProfile,
    displaySecurityEvent,
    loadSecurityStatus,
    loadSecurityApprovals,
    loadSecurityReconciliations,
    formatScenarioFitSummary,
};
