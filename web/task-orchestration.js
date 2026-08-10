(() => {
  "use strict";

  const panelTab = document.querySelector('[data-tab="orchestration"]');
  const list = document.getElementById("orchestrationRunsList");
  if (!panelTab || !list) return;

  const search = document.getElementById("orchestrationSearch");
  const statusFilter = document.getElementById("orchestrationStatusFilter");
  const refreshButton = document.getElementById("refreshOrchestrationRuns");
  const empty = document.getElementById("orchestrationEmpty");
  const selectedRoot = document.getElementById("orchestrationSelected");
  const summary = document.getElementById("orchestrationSummary");
  const graph = document.getElementById("orchestrationGraph");
  const inspector = document.getElementById("orchestrationNodeInspector");
  const timeline = document.getElementById("orchestrationTimeline");
  const toolEvidence = document.getElementById("orchestrationToolEvidence");
  const artifactEvidence = document.getElementById("orchestrationArtifactEvidence");
  const governance = document.getElementById("orchestrationGovernance");
  const checkpoints = document.getElementById("orchestrationCheckpoints");
  const openHistory = document.getElementById("orchestrationOpenHistory");
  const pauseButton = document.getElementById("orchestrationPause");
  const resumeButton = document.getElementById("orchestrationResume");
  const openFromHistory = document.getElementById("openTaskOrchestration");

  let runs = [];
  let selectedTaskId = "";
  let currentView = null;
  let activeLayer = "both";
  let pollTimer = null;
  let refreshDebounce = null;

  const text = (value) => String(value ?? "");
  const upper = (value) => text(value).toUpperCase();
  const statusClass = (value) => `orchestration-status-${upper(value).toLowerCase()}`;
  const statusLabel = (value) => ({
    RUNNING: "运行中",
    RESERVED: "等待执行",
    SUCCEEDED: "成功",
    COMPLETED: "成功",
    FAILED: "失败",
    PARTIAL_FAILED: "部分失败",
    PAUSED: "已暂停",
    APPROVAL_REQUIRED: "等待审批",
    NEEDS_RECONCILIATION: "待人工核对",
    SKIPPED: "已跳过",
    PENDING: "等待中",
  }[upper(value)] || text(value || "未知"));

  const formatTime = (value) => {
    if (!value) return "-";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? text(value) : date.toLocaleString("zh-CN", { hour12: false });
  };

  const setEmptyState = (root, message) => {
    root.replaceChildren();
    const item = document.createElement("p");
    item.className = "orchestration-empty-state";
    item.textContent = message;
    root.appendChild(item);
  };

  const badge = (value, className = "") => {
    const item = document.createElement("span");
    item.className = `orchestration-badge ${className}`.trim();
    item.textContent = value;
    return item;
  };

  const filteredRuns = () => {
    const needle = text(search?.value).trim().toLowerCase();
    const filter = upper(statusFilter?.value || "all");
    return runs.filter((run) => {
      const matchesStatus = filter === "ALL" || upper(run.status) === filter;
      const haystack = `${run.task_id} ${run.workflow_id} ${run.user_query}`.toLowerCase();
      return matchesStatus && (!needle || haystack.includes(needle));
    });
  };

  const renderRuns = () => {
    const items = filteredRuns();
    list.replaceChildren();
    if (!items.length) {
      setEmptyState(list, runs.length ? "没有符合筛选条件的运行。" : "暂无编排运行记录。");
      return;
    }
    items.forEach((run) => {
      const card = document.createElement("article");
      card.className = `orchestration-run-item${run.task_id === selectedTaskId ? " active" : ""}`;
      card.dataset.taskId = run.task_id;
      card.tabIndex = 0;
      card.setAttribute("role", "button");

      const header = document.createElement("div");
      header.className = "orchestration-run-item-header";
      const name = document.createElement("strong");
      name.textContent = run.user_query || run.task_id;
      name.title = run.user_query || run.task_id;
      header.append(name, badge(statusLabel(run.status), statusClass(run.status)));

      const runMeta = document.createElement("div");
      runMeta.className = "orchestration-run-item-meta";
      runMeta.textContent = formatTime(run.created_at);
      card.append(header, runMeta);
      card.addEventListener("click", () => selectRun(run.task_id));
      card.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          selectRun(run.task_id);
        }
      });
      list.appendChild(card);
    });
  };

  const loadRuns = async ({ preserveSelection = true } = {}) => {
    setEmptyState(list, "正在加载编排运行...");
    try {
      const response = await fetch("/api/tasks");
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      runs = await response.json();
      if (!Array.isArray(runs)) runs = [];
      runs = runs.slice(0, 100);
      renderRuns();
      const requested = new URLSearchParams(window.location.search).get("task_id");
      const target = preserveSelection ? (selectedTaskId || requested) : requested;
      if (target && runs.some((run) => run.task_id === target)) await selectRun(target, { updateUrl: false });
    } catch (error) {
      setEmptyState(list, `编排运行加载失败：${error.message || error}`);
    }
  };

  const parallelWidth = (attempts) => {
    const events = [];
    attempts.forEach((item) => {
      const start = Number(item.started_monotonic_ns || 0);
      const end = Number(item.finished_monotonic_ns || 0);
      if (start && end >= start) {
        events.push([start, 1], [end, -1]);
      }
    });
    events.sort((left, right) => left[0] - right[0] || left[1] - right[1]);
    let active = 0;
    let maximum = 0;
    events.forEach(([, delta]) => {
      active += delta;
      maximum = Math.max(maximum, active);
    });
    return maximum;
  };

  const renderHeader = (view) => {
    const task = view.task || {};
    pauseButton.disabled = !["RUNNING", "RESERVED"].includes(upper(task.status));
    resumeButton.disabled = upper(task.status) !== "PAUSED";
  };

  const renderSummary = (view) => {
    const attempts = view.runtime?.attempts || [];
    const values = [
      [view.graph?.steps?.length || 0, "TaskGraph 步骤"],
      [new Set((view.graph?.steps || []).map((step) => step.agent_name).filter(Boolean)).size, "执行 Agent"],
      [parallelWidth(attempts) || "未记录", "实际最大并行"],
      [view.artifacts?.nodes?.length || 0, "Artifact"],
      [Object.keys(view.tool_decisions || {}).length, "工具审计步骤"],
    ];
    summary.replaceChildren();
    values.forEach(([value, label]) => {
      const item = document.createElement("div");
      item.className = "orchestration-summary-item";
      const strong = document.createElement("strong");
      const caption = document.createElement("span");
      strong.textContent = value;
      caption.textContent = label;
      item.append(strong, caption);
      summary.appendChild(item);
    });
  };

  const safeMermaidId = (value, index) => `orch_${index}_${text(value).replace(/[^a-zA-Z0-9_]/g, "_")}`;
  const safeMermaidLabel = (value) => text(value).replace(/["\n\r]/g, " ").replace(/[<>]/g, "").slice(0, 70);

  const renderInspector = (step) => {
    if (!step || !currentView) {
      inspector.innerHTML = "<p>点击 Agent 节点查看详情。</p>";
      return;
    }
    const state = currentView.runtime?.step_states?.[step.step_id] || {};
    const tool = currentView.tool_decisions?.[step.step_id] || {};
    const artifacts = (currentView.artifacts?.nodes || []).filter((item) => item.producer_step_id === step.step_id);
    inspector.replaceChildren();
    const heading = document.createElement("h4");
    heading.textContent = step.title || step.step_id;
    const status = badge(statusLabel(state.status || "PENDING"), statusClass(state.status || "PENDING"));
    const dl = document.createElement("dl");
    [
      ["Step ID", step.step_id],
      ["Agent", state.executed_agent || step.agent_name || "-"],
      ["依赖", step.depends_on?.join(", ") || "无"],
      ["操作模式", step.operation_mode || "-"],
      ["风险", step.risk_level || "-"],
      ["推荐工具", tool.recommended_mcp_tool || tool.selected_tool || "未记录"],
      ["实际工具", tool.actual_legacy_tool || tool.actual_tool_names?.join(", ") || "未记录"],
      ["Artifact", artifacts.map((item) => item.logical_name || item.output_name).join(", ") || "无"],
    ].forEach(([key, value]) => {
      const dt = document.createElement("dt");
      const dd = document.createElement("dd");
      dt.textContent = key;
      dd.textContent = value;
      dl.append(dt, dd);
    });
    inspector.append(heading, status, dl);
  };

  const renderGraph = async () => {
    const steps = currentView?.graph?.steps || [];
    graph.replaceChildren();
    if (!steps.length) {
      setEmptyState(graph, "该任务没有记录可信 TaskGraph；不会用串行 Workflow 图代替。");
      renderInspector(null);
      return;
    }
    const ids = new Map(steps.map((step, index) => [step.step_id, safeMermaidId(step.step_id, index)]));
    const lines = ["flowchart LR"];
    steps.forEach((step) => {
      const state = currentView.runtime?.step_states?.[step.step_id] || {};
      const label = `${safeMermaidLabel(step.title || step.step_id)}<br/>${safeMermaidLabel(state.executed_agent || step.agent_name || "Agent")}<br/>${statusLabel(state.status || "PENDING")}`;
      lines.push(`  ${ids.get(step.step_id)}["${label}"]`);
    });
    if (activeLayer !== "artifact") {
      (currentView.graph.dependency_edges || []).forEach((edge) => {
        if (ids.has(edge.source) && ids.has(edge.target)) lines.push(`  ${ids.get(edge.source)} --> ${ids.get(edge.target)}`);
      });
    }
    if (activeLayer !== "dependency") {
      (currentView.graph.artifact_edges || []).forEach((edge) => {
        if (!ids.has(edge.source) || !ids.has(edge.target)) return;
        const label = safeMermaidLabel(edge.output || edge.schema_ref || "Artifact");
        lines.push(`  ${ids.get(edge.source)} -. "${label}" .-> ${ids.get(edge.target)}`);
      });
    }
    lines.push(
      "  classDef pending fill:#eef1f5,stroke:#8793a4,color:#364152",
      "  classDef running fill:#e5f0ff,stroke:#2872cc,stroke-width:3px,color:#154d8f",
      "  classDef succeeded fill:#e5f7ed,stroke:#238452,stroke-width:2px,color:#155b39",
      "  classDef failed fill:#ffe8e8,stroke:#c83e3e,stroke-width:2px,color:#8d2020",
      "  classDef paused fill:#fff1cf,stroke:#b57a09,stroke-width:2px,color:#754c00",
      "  classDef skipped fill:#eceff3,stroke:#8a94a3,color:#657184",
    );
    steps.forEach((step) => {
      const state = upper(currentView.runtime?.step_states?.[step.step_id]?.status || "PENDING");
      const className = state === "SUCCEEDED" || state === "COMPLETED" ? "succeeded"
        : state === "RUNNING" ? "running"
          : state === "FAILED" || state === "PARTIAL_FAILED" ? "failed"
            : state === "PAUSED" || state === "APPROVAL_REQUIRED" ? "paused"
              : state === "SKIPPED" ? "skipped" : "pending";
      lines.push(`  class ${ids.get(step.step_id)} ${className}`);
    });
    const pre = document.createElement("pre");
    pre.className = "mermaid";
    pre.textContent = lines.join("\n");
    graph.appendChild(pre);
    try {
      await mermaid.run({ nodes: [pre] });
      const svg = graph.querySelector("svg");
      if (svg) {
        svg.removeAttribute("height");
        steps.forEach((step) => {
          const safeId = ids.get(step.step_id);
          const node = Array.from(svg.querySelectorAll(".node")).find((item) => item.id.includes(safeId));
          if (node) {
            node.setAttribute("tabindex", "0");
            node.setAttribute("role", "button");
            node.addEventListener("click", () => renderInspector(step));
            node.addEventListener("keydown", (event) => {
              if (event.key === "Enter" || event.key === " ") renderInspector(step);
            });
          }
        });
      }
    } catch (error) {
      setEmptyState(graph, `TaskGraph 渲染失败：${error.message || error}`);
    }
  };

  const renderTimeline = (view) => {
    const attempts = view.runtime?.attempts || [];
    timeline.replaceChildren();
    const completed = attempts.filter((item) => Number(item.started_monotonic_ns) && Number(item.finished_monotonic_ns));
    if (!attempts.length) {
      setEmptyState(timeline, "该次运行未记录 attempt 时间证据，不能仅根据 DAG 宣称实际并行。");
      return;
    }
    const starts = completed.map((item) => Number(item.started_monotonic_ns));
    const finishes = completed.map((item) => Number(item.finished_monotonic_ns));
    const minimum = starts.length ? Math.min(...starts) : 0;
    const maximum = finishes.length ? Math.max(...finishes) : minimum + 1;
    const span = Math.max(1, maximum - minimum);
    attempts.forEach((item) => {
      const row = document.createElement("div");
      row.className = "orchestration-timeline-row";
      const label = document.createElement("strong");
      label.textContent = item.executed_agent || item.step_id;
      const track = document.createElement("div");
      track.className = "orchestration-timeline-track";
      const bar = document.createElement("div");
      bar.className = "orchestration-timeline-bar";
      const start = Number(item.started_monotonic_ns || minimum);
      const finish = Number(item.finished_monotonic_ns || start);
      bar.style.left = `${Math.max(0, ((start - minimum) / span) * 100)}%`;
      bar.style.width = `${Math.max(1, ((finish - start) / span) * 100)}%`;
      bar.title = `${formatTime(item.started_at)} → ${formatTime(item.finished_at)}`;
      track.appendChild(bar);
      const duration = document.createElement("span");
      duration.textContent = item.duration_ms == null ? statusLabel(item.status) : `${item.duration_ms} ms`;
      row.append(label, track, duration);
      timeline.appendChild(row);
    });
  };

  const evidenceItem = (headingText, statusValue, rows) => {
    const item = document.createElement("article");
    item.className = "orchestration-evidence-item";
    const header = document.createElement("header");
    const heading = document.createElement("strong");
    heading.textContent = headingText;
    header.appendChild(heading);
    if (statusValue) header.appendChild(badge(statusValue));
    item.appendChild(header);
    rows.forEach(([label, value]) => {
      const row = document.createElement("p");
      row.textContent = `${label}：${value || "-"}`;
      item.appendChild(row);
    });
    return item;
  };

  const renderTools = (view) => {
    const decisions = Object.entries(view.tool_decisions || {});
    toolEvidence.replaceChildren();
    if (!decisions.length) {
      setEmptyState(toolEvidence, "该次运行没有记录工具选择审计。");
      return;
    }
    const notice = document.createElement("p");
    notice.className = "orchestration-empty-state";
    notice.textContent = "审计推荐不等于授权工具，也不代表选择器替换了本次生产执行工具。";
    const items = document.createElement("div");
    items.className = "orchestration-evidence-list";
    decisions.forEach(([stepId, decision]) => {
      const top = (decision.candidates || []).slice(0, 3).map((candidate) => `${candidate.tool_key || candidate.name} (${candidate.score ?? "-"})`).join("；");
      items.appendChild(evidenceItem(stepId, decision.mode || "audit", [
        ["候选过滤", `${decision.candidate_count_before_filter ?? "-"} → ${decision.candidate_count_after_filter ?? "-"}`],
        ["推荐 MCP", decision.recommended_mcp_tool || decision.selected_tool],
        ["实际工具", decision.actual_legacy_tool || (decision.actual_tool_names || []).join(", ")],
        ["Top-K", top || "未记录"],
        ["推荐与实际一致", decision.recommendation_matches_actual === true ? "是" : decision.recommendation_matches_actual === false ? "否" : "未记录"],
      ]));
    });
    toolEvidence.append(notice, items);
  };

  const renderArtifacts = (view) => {
    const artifacts = view.artifacts?.nodes || [];
    artifactEvidence.replaceChildren();
    if (!artifacts.length) {
      setEmptyState(artifactEvidence, "该次运行没有记录 Artifact 元数据；payload 不会进入此页面。");
      return;
    }
    const items = document.createElement("div");
    items.className = "orchestration-evidence-list";
    artifacts.forEach((artifact) => {
      const derived = (artifact.derived_from || []).map((item) => `${text(item.artifact_id).slice(0, 12)}…`).join(", ");
      items.appendChild(evidenceItem(artifact.logical_name || artifact.output_name, artifact.sensitivity, [
        ["生产步骤", artifact.producer_step_id],
        ["Schema", artifact.schema_ref],
        ["Artifact ID", artifact.artifact_id ? `${text(artifact.artifact_id).slice(0, 16)}…` : "-"],
        ["版本", artifact.version],
        ["派生自", derived || "源 Artifact"],
        ["Schema 校验", artifact.schema_valid === true ? "通过" : artifact.schema_valid === false ? "失败" : "未记录"],
      ]));
    });
    artifactEvidence.appendChild(items);
  };

  const renderGovernance = (view) => {
    const events = view.governance?.events || [];
    governance.replaceChildren();
    if (!events.length) {
      setEmptyState(governance, "暂无治理事件。");
      return;
    }
    const items = document.createElement("div");
    items.className = "orchestration-evidence-list";
    events.forEach((event) => items.appendChild(evidenceItem(event.event_type || "治理事件", event.decision, [
      ["时间", formatTime(event.timestamp)],
      ["步骤", event.step_id],
      ["Agent", event.agent],
      ["原因", event.reason_code],
    ])));
    governance.appendChild(items);
  };

  const renderCheckpoints = (view) => {
    const values = view.checkpoints || [];
    checkpoints.replaceChildren();
    if (!values.length) {
      setEmptyState(checkpoints, "暂无 Checkpoint。暂停后只有完成当前安全批次并持久化，才会显示恢复位置。");
      return;
    }
    const control = view.control || {};
    const info = document.createElement("p");
    info.className = "orchestration-empty-state";
    info.textContent = `控制状态：${control.state || view.task?.status || "未知"}；恢复步骤：${control.resume_step ?? "未记录"}`;
    const items = document.createElement("div");
    items.className = "orchestration-evidence-list";
    values.forEach((item) => items.appendChild(evidenceItem(`步骤 ${item.step ?? "-"}`, item.node_name, [
      ["时间", formatTime(item.timestamp)],
      ["下一节点", item.next_node],
    ])));
    checkpoints.append(info, items);
  };

  const renderView = async (view) => {
    currentView = view;
    empty.hidden = true;
    selectedRoot.hidden = false;
    renderHeader(view);
    renderSummary(view);
    renderTimeline(view);
    renderTools(view);
    renderArtifacts(view);
    renderGovernance(view);
    renderCheckpoints(view);
    renderInspector(null);
    await renderGraph();
    schedulePolling(view.task?.status);
  };

  const loadView = async (taskId) => {
    try {
      const response = await fetch(`/api/tasks/${encodeURIComponent(taskId)}/orchestration-view`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const view = await response.json();
      if (taskId !== selectedTaskId) return;
      await renderView(view);
    } catch (error) {
      selectedRoot.hidden = false;
      empty.hidden = true;
      setEmptyState(graph, `编排详情加载失败：${error.message || error}`);
    }
  };

  async function selectRun(taskId, { updateUrl = true } = {}) {
    selectedTaskId = taskId;
    renderRuns();
    if (updateUrl) {
      const url = new URL(window.location.href);
      url.searchParams.set("task_id", taskId);
      history.replaceState({}, "", url);
    }
    await loadView(taskId);
  }

  const schedulePolling = (status) => {
    if (pollTimer) clearTimeout(pollTimer);
    if (!["RUNNING", "RESERVED"].includes(upper(status)) || !selectedTaskId) return;
    pollTimer = setTimeout(async () => {
      await loadView(selectedTaskId);
      await loadRuns();
    }, 2000);
  };

  const openHistoryForTask = (taskId) => {
    if (!taskId) return;
    const url = new URL(window.location.href);
    url.searchParams.set("task_id", taskId);
    history.replaceState({}, "", url);
    document.querySelector('[data-tab="tasks"]')?.click();
    setTimeout(() => {
      const card = Array.from(document.querySelectorAll(".task-item")).find((item) => item.dataset.taskId === taskId);
      card?.click();
      card?.scrollIntoView({ block: "nearest" });
    }, 100);
  };

  const openOrchestrationForTask = (taskId) => {
    if (!taskId) return;
    panelTab.click();
    selectRun(taskId);
  };

  document.querySelectorAll(".orchestration-tab").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll(".orchestration-tab").forEach((item) => item.classList.toggle("active", item === button));
      document.querySelectorAll("[data-orchestration-pane]").forEach((pane) => {
        pane.hidden = pane.dataset.orchestrationPane !== button.dataset.orchestrationTab;
      });
    });
  });

  document.querySelectorAll("[data-graph-layer]").forEach((button) => {
    button.addEventListener("click", async () => {
      activeLayer = button.dataset.graphLayer;
      document.querySelectorAll("[data-graph-layer]").forEach((item) => item.classList.toggle("active", item === button));
      await renderGraph();
    });
  });

  panelTab.addEventListener("click", () => loadRuns());
  refreshButton?.addEventListener("click", () => loadRuns());
  search?.addEventListener("input", renderRuns);
  statusFilter?.addEventListener("change", renderRuns);
  openHistory?.addEventListener("click", () => openHistoryForTask(selectedTaskId));
  resumeButton?.addEventListener("click", () => {
    openHistoryForTask(selectedTaskId);
    setTimeout(() => document.getElementById("resumePanel")?.scrollIntoView({ behavior: "smooth" }), 160);
  });
  pauseButton?.addEventListener("click", async () => {
    if (!selectedTaskId || !currentView) return;
    pauseButton.disabled = true;
    try {
      const response = await fetch(`/api/tasks/${encodeURIComponent(selectedTaskId)}/pause`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: currentView.control?.user_id || "test", reason: "orchestration_ui" }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || `HTTP ${response.status}`);
      }
      await loadView(selectedTaskId);
    } catch (error) {
      window.alert(`暂停请求失败：${error.message || error}`);
      pauseButton.disabled = false;
    }
  });

  document.getElementById("tasksList")?.addEventListener("click", (event) => {
    const card = event.target.closest(".task-item");
    if (card && openFromHistory) openFromHistory.dataset.taskId = card.dataset.taskId || "";
  });
  openFromHistory?.addEventListener("click", () => openOrchestrationForTask(openFromHistory.dataset.taskId));

  window.addEventListener("cooragent:sse", () => {
    if (!selectedTaskId) return;
    if (refreshDebounce) clearTimeout(refreshDebounce);
    refreshDebounce = setTimeout(() => loadView(selectedTaskId), 180);
  });

  const requested = new URLSearchParams(window.location.search).get("task_id");
  if (requested) {
    panelTab.click();
  }
})();
