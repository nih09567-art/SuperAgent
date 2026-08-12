# 银行数字员工 Office MCP 原型

## 原型边界

本原型复用现有 Agent 选择、TaskGraph 和 Scheduler，不重写编排主链。
新增一个 `office-mcp` Server，将人员查询、日程管理、课程检索、员工差旅、
会议助手和消息发送六个场景统一暴露为 30 个 MCP 工具。

```text
Remote Agent (8010)
  -> BaseRemoteAgent.call_tool 授权与参数校验
  -> 进程级共享 MCP Client
  -> office-mcp (8013, SSE)
  -> 现有 Mock 数据与少量新增课程数据
```

`REMOTE_TOOL_TRANSPORT=hybrid` 时，能够映射到六个办公场景的工具通过 MCP
执行；其他历史工具继续使用 8011 HTTP Mock 服务。

本轮在该原型上新增的是“统一发现与选择控制面”，没有统一或替换底层执行：

```text
已配置 MCP Server 的真实 tools/list
  -> ToolLoader + ToolRegistry（以 server_name/tool_name 为唯一键）
  -> 场景 / 操作 / Agent / Schema 分层选择
  -> off 或 audit Top-K
  -> 原有 Agent.selected_tools / 参数提取器
  -> 原有 Local/Remote Executor 与 MCP Client
```

实时基线确认 Excel MCP 有 18 个工具、Office MCP 有 30 个工具，因此统一控制面
覆盖的实际数量是 48，而不是预设的 38。机器可读基线及摘要分别见
`docs/mcp_tool_registry_baseline.json` 和 `docs/mcp_tool_registry_baseline.md`。

## 30 个工具

### 人员查询

1. `search_employees`
2. `get_employee_profile`
3. `get_org_unit`
4. `get_manager_chain`
5. `get_employee_contact`

### 日程管理

1. `list_calendar_events`
2. `find_free_slots`
3. `create_calendar_event`
4. `update_calendar_event`
5. `cancel_calendar_event`

### 课程检索

1. `search_courses`
2. `get_course_detail`
3. `list_course_sessions`
4. `enroll_course`
5. `get_learning_record`

### 员工差旅

1. `search_travel_policy`
2. `estimate_trip_cost`
3. `create_travel_request`
4. `get_travel_request`
5. `cancel_travel_request`

### 会议助手

1. `find_meeting_slots`
2. `create_meeting`
3. `update_meeting`
4. `cancel_meeting`
5. `get_meeting_minutes`

### 消息发送

1. `resolve_contacts`
2. `list_message_templates`
3. `send_email`
4. `send_message`
5. `get_delivery_status`

## 统一 ToolRegistry 元数据视图

动态字段与语义字段分开维护：

- 工具名、描述和 JSON Schema 由每个 MCP Server 的真实 `tools/list` 提供；
- `config/office_mcp_tools.json` 补充 Office 30 个工具的场景、Agent、意图、
  操作、必需输入、产物及 legacy 映射；
- `config/mcp_tool_semantics.json` 为 Excel MCP 的 18 个实际工具补充同类语义；
- `ToolMetadata` 使用 `(server_name, runtime_tool_name)` 区分跨 Server 同名工具；
- 加载时校验 Server、注册表、语义目录以及 Schema 必需参数恰好一致，拒绝缺失、
  重复或孤儿语义。

基线结果为 `server_tools = registry_tools = semantic_tools = 48`，四个双向差集
均为空。`agent_selected_minus_registry_or_legacy` 中仍有 9 个名称，它们是现有
HTTP/legacy Remote Tool，不属于这两个 MCP Server，因此没有伪装成 MCP 工具。

## 分层工具选择器

`src/tools/office_mcp/selector.py` 的主入口接收 ToolRegistry 候选，不再固定读取
Office JSON，并保留旧 Office API 的兼容封装。算法是确定性的，不调用 LLM：

```text
自然语言任务
  -> 识别 MCP Server/领域、scenario 与 operation_mode
  -> 按 scenario / operation_mode / owner_agents 缩小集合
  -> 按 known_inputs、required_inputs 和 input_schema 过滤
  -> 按工具名、意图、别名、描述、操作类型、已知输入解释性评分
  -> 返回 server_name、selected_tool、Top-K 分数、excluded 及逐项原因
```

未知任务或语义最佳候选缺少必需参数时返回 `selected_tool=null`，不会退化为选择
无关工具，也不会生成 ToolRegistry 外的名称。

`ToolSelectionService` 在 Agent 已确定、执行器构建调用前运行。选择模式由
`MCP_TOOL_SELECTION_MODE` 控制：

- `off`：默认值，完全不加载注册表或语义选择逻辑；
- `audit`：把 Top-K、分数、排除原因及 legacy 名称映射写入
  `ExecutionContext.metadata["tool_selection_audit"]`；
- `enforce`：真实执行服务会明确降级为 `audit` 并记录 `enforce_blocked=true`，
  本轮不允许它替换实际工具。

LocalExecutor 与 RemoteExecutor 都只观察已选工具：不修改 `agent.selected_tools`、
参数、Remote request 的 `tools` 或实际 MCP Client。Remote audit 同时记录推荐
具体 MCP 工具、兼容 legacy 工具和实际 legacy 工具，避免跨越现有名称契约。

### 固定评估与实际结果

评估集位于 `experiments/office_mcp_tool_selection_cases.json`，包含 48 条逐工具
正常任务，以及模糊、缺参和不存在工具任务，共 53 条。先在两个本地 MCP
Server 可用时生成真实基线，再运行离线可复现评估：

```powershell
uv --cache-dir .uv run --extra test --frozen python -B scripts/snapshot_mcp_tool_registry.py
uv --cache-dir .uv run --extra test --frozen python -B scripts/evaluate_office_mcp_tool_selector.py
```

本次实际运行结果保存在：

- `docs/office_mcp_tool_selection_evaluation.json`：机器可读逐用例证据；
- `docs/office_mcp_tool_selection_evaluation.md`：指标表和逐用例 Top-K。

本次实际结果：48 工具覆盖率 100%，Server 识别准确率 100%，Tool Top-1 100%，
Tool Top-3 100%，参数/Schema 有效率 100%，不存在工具幻觉率 0%，平均过滤前
候选数 48.0、过滤后 2.17，平均选择耗时 1.436074 ms，audit 建议与当前具体/
legacy 契约一致率 100%。耗时是本机单次进程内实测值，重新运行会波动。

### Top-K 交给 Agent 的最小证明

兼容辅助函数 `finalize_top_k_with_agent` 把选择器 Top-K 转换为现有
`parameter_extractor.select_tool_and_extract` 兼容的工具定义。聚焦测试使用轻量
测试双完成最终工具选择和参数生成，并验证最终工具必须属于 Top-K；该证明不
执行 MCP 工具。

```powershell
uv --cache-dir .uv run --extra test --frozen python -B -m pytest tests/test_mcp_tool_selection_control_plane.py tests/test_office_mcp_tool_selector.py -q -o addopts= -p no:cacheprovider --basetemp .pytest-mcp-selection
```

## 启动方式

完整系统可以使用：

```powershell
.\start-superagent.ps1
```

脚本会启动 Excel MCP、Office MCP、Remote Agent、Remote Tool、Remote Registry
和 Web 服务，并为子进程设置 `REMOTE_TOOL_TRANSPORT=hybrid`。

只验证 Office MCP 时：

```powershell
python mock_office_mcp_server.py
python scripts/check_office_mcp.py
```

预期输出包括：

```text
tools/list=30
tools/call={"status": "success", ...}
```

## 六场景稳定演示

先启动 `mock_office_mcp_server.py`，再运行：

```powershell
$env:REMOTE_TOOL_TRANSPORT = "hybrid"
python scripts/demo_office_mcp_scenarios.py
```

该脚本使用固定参数提取器替代在线 LLM，但保留真实 Remote Agent、公共
`call_tool`、授权清单和 MCP SSE 调用链，适合无模型凭据的现场演示。

预期六项均为 `success=True`：

- 人员查询：`RemoteHRAssistantAgent`
- 日程管理：`RemoteHRCalendarAgent`
- 课程检索：`RemoteKnowledgeAgent`
- 员工差旅：`RemoteOfficeAssistantAgent`
- 会议助手：`RemoteMeetingManagerAgent`
- 消息发送：`RemoteEmailDispatchAgent`

## 验证命令

```powershell
python -m pytest tests/test_office_mcp_prototype.py -q -o addopts=
```

聚焦回归验证：

- 两个实际 MCP Server 合计登记 48 个工具，Office Server 恰好 30 个；
- 动态 Schema、ToolRegistry 和语义目录恰好一致，无重复、缺失或孤儿；
- 分层选择器覆盖全部 48 工具、缺参解释、未知任务弃选和 Top-K Agent 测试双；
- `off` 不触发选择，`audit` 不改变 Local/Remote 实际工具，生产服务拒绝 enforce；
- 跨 Server 同名工具的注册键不冲突；
- 六个历史 Agent 工具能够映射到对应 MCP 工具；
- 30 个工具逐项可调用；
- `BaseRemoteAgent.call_tool` 在既有授权校验之后进入 MCP。

## 当前限制

- 数据均为模拟数据，不连接真实银行系统。
- 课程场景复用 `RemoteKnowledgeAgent`，未新增独立 Course Agent。
- 本阶段未新增权限拒绝、审批中断恢复或故障治理演示。
- 现有系统的授权清单和 S-ABAC 基础保持原样；30 个 MCP 工具的细粒度权限、
  角色、授权项和审批接入不属于本轮选择器原型。
- `risk_level` 与 `side_effect` 只是报告标签，不参与过滤或执行决策。
- 本轮不实现多步骤组合任务，不替换现有 MCP Client，也不统一 HTTP/legacy 与
  MCP 的底层执行。
- 生产 `enforce` 未启用；未来启用前仍需解决具体 MCP 名称与 Agent legacy
  契约迁移、Top-K 内最终选择校验及更长时间的 audit 数据验证。
- Planner、Scheduler、Artifact Schema 和现有 Agent 主链未改写。
- 六场景脚本验证 Agent 到 MCP 的稳定路径；完整 Web 自然语言入口仍依赖
  项目的 LLM 配置和现有 Planner/Scheduler 运行条件。
