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

测试验证：

- MCP Server 恰好登记 30 个工具；
- 六个历史 Agent 工具能够映射到对应 MCP 工具；
- 30 个工具逐项可调用；
- `BaseRemoteAgent.call_tool` 在既有授权校验之后进入 MCP。

## 当前限制

- 数据均为模拟数据，不连接真实银行系统。
- 课程场景复用 `RemoteKnowledgeAgent`，未新增独立 Course Agent。
- 本阶段未新增权限拒绝、审批中断恢复或故障治理演示。
- 六场景脚本验证 Agent 到 MCP 的稳定路径；完整 Web 自然语言入口仍依赖
  项目的 LLM 配置和现有 Planner/Scheduler 运行条件。
