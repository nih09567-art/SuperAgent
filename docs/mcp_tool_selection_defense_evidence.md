# MCP 工具选择答辩证据

生成时间：`2026-08-10T10:41:04.7805404+08:00`

## 结论

当前原型已验证统一 ToolRegistry 中 48 个 MCP 工具（Excel 18、Office 30）的发现、语义覆盖、分层选择和 audit 接入。audit 只记录推荐结果，不改变 Agent 原有工具列表或实际调用。

## 聚焦测试

```powershell
.\.venv\Scripts\python.exe -B -m pytest tests\test_mcp_tool_selection_control_plane.py tests\test_office_mcp_tool_selector.py tests\test_office_mcp_prototype.py -q -o addopts= -p no:cacheprovider --basetemp .pytest-defense-mcp-selection
```

结果：`21 passed in 4.51s`。

## 真实 tools/list 与 ToolRegistry

数据采集时间：`2026-08-10T10:38:26.735948+08:00`。

| 项目 | 结果 |
| --- | ---: |
| Excel MCP tools/list | 18 |
| Office MCP tools/list | 30 |
| Server 工具合计 | 48 |
| ToolRegistry 工具 | 48 |
| 语义目录工具 | 48 |

以下集合差异均为 `[]`：`server_minus_registry`、`registry_minus_server`、`server_minus_semantics`、`semantics_minus_server`。

完整工具清单和动态 Schema 见 `docs/mcp_tool_registry_baseline.json`。

## 固定评估集实际指标

数据采集时间：`2026-08-10T10:38:45.820544+08:00`；共 53 条，其中 48 条正常任务覆盖全部 48 个 MCP 工具，另含模糊、参数不足和不存在工具案例。

| 指标 | 实际结果 |
| --- | ---: |
| 工具覆盖率 | 100.0% |
| Server 识别准确率 | 100.0% |
| Tool Top-1 | 100.0% |
| Tool Top-3 | 100.0% |
| 参数/Schema 有效率 | 100.0% |
| 不存在工具幻觉率 | 0.0% |
| 过滤前平均候选数 | 48.0 |
| 过滤后平均候选数 | 2.17 |
| 平均选择耗时 | 1.513715 ms |
| audit 推荐与原调用匹配率 | 100.0% |

逐案例结果见 `docs/office_mcp_tool_selection_evaluation.json`，以上数字是本次运行现场计算结果。

## 真实 audit 接入证据

以脱敏任务“搜索员工 `<REDACTED>`”和 `RemoteHRAssistantAgent` 为输入，通过 live `tools/list` 填充独立 ToolRegistry 后调用真实 `ToolSelectionService.audit`：

- 过滤前 48 个候选，过滤后 4 个候选。
- Top-K：`office-mcp:search_employees`、`office-mcp:get_employee_contact`、`office-mcp:get_employee_profile`。
- Top-1 为 `office-mcp:search_employees`。
- 它对应现有 Agent 逻辑工具 `remote_person_info_tool`，`recommendation_matches_actual=true`。
- 模式为 `audit`，没有替换 Agent 原有工具列表。

## 真实 MCP 协议调用证据

使用只读“人员查询”场景，以确定性参数提取器执行：

`AgentFactory → BaseRemoteAgent → MCP Client → Office MCP Server`

捕获到逻辑工具 `remote_person_info_tool` 映射并调用 MCP 工具 `search_employees`，参数键为 `department`、`job_keyword`、`keyword`、`limit`，返回状态为 `success`。证据没有保存员工资料或完整返回值。

## 工具执行可观测性

新增结构化日志事件 `remote_tool_execution` 和 Agent 返回字段 `metadata.tool_executions`。记录逻辑工具、运行时工具、MCP/HTTP 通道、Server、状态和调用前的参数键，不记录参数值。使用请求级 `ContextVar` 隔离并发记录，离线六场景演示也逐场景重置。

相关 MCP、HTTP、并发隔离、远端授权、Agent 结果契约和 Office MCP 测试结果：`65 passed in 4.13s`。

真实只读 Office MCP 调用返回：

```json
{
  "logical_tool": "remote_person_info_tool",
  "runtime_tool": "search_employees",
  "execution_transport": "mcp",
  "server_name": "office-mcp",
  "status": "success",
  "argument_keys": ["department", "job_keyword", "keyword", "limit"]
}
```

当前记录已经到达 Remote Agent metadata；Scheduler SSE 白名单和前端展示尚未实现。

## 答辩口径边界

可以陈述：已完成并验证“任务与 MCP 工具能力匹配”的 audit 原型，统一覆盖当前两个 MCP Server 的 48 个工具；真实 Office MCP 只读调用成功。

不能陈述：Planner/Web 全链路已经依据选择器自动改选工具；LLM 参数抽取已完成真实端到端验证；enforce 已控制生产调用；本轮完成了权限、审批或写操作验证。
