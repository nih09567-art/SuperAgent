# Contract 驱动的动态 Agent DAG 演示

本文档描述同一个自然语言入口如何根据 TaskProfile 和 Registry Agent
Contract 动态选择 Agent、校验 Artifact 数据依赖并生成 TaskGraph。

## 实现边界

- Planner 只使用 Registry 提供的可信 Agent Contract，不能发明 Agent、Schema
  或输出。
- `depends_on` 只表示执行顺序，实际数据通过
  `Artifact -> ArtifactRef -> source_artifacts -> ArtifactResolver` 传递。
- Scheduler、ArtifactResolver、ArtifactGuard、Checkpoint/Resume 仍是原有基础设施；
  本轮在其上接入 Contract 校验、审批门和副作用 Receipt。
- 所有邮件均为模拟发送，演示收件人固定使用不可路由的
  `hr@example.test`，不得替换为真实地址。
- 复杂演示是五个业务 Agent 加一个审批门，不包含 Document/Word Agent，
  不应称为“六 Agent”。

## 演示一：动态三 Agent

输入：

```text
查询王强的在职状态、岗位和累计工龄，并依据国务院关于职工带薪年休假的规定，判断其年假天数，生成一份 Markdown 汇总。
```

期望 DAG：

```text
HR + Knowledge（并行）
        -> Report
```

期望 Artifact：

```text
employee.info@v1
policy.info@v2
report.markdown@v1
```

## 演示二：动态五 Agent与审批门

输入：

```text
查询王强的工龄和年假政策，查询历史请假记录，生成报告，经确认后发送给 hr@example.test。
```

期望 DAG：

```text
HR + Knowledge（并行）
HR -> Office
HR + Knowledge + Office -> Report
Report -> Approval -> Email（模拟发送）
```

期望 Artifact：

```text
employee.info@v1
policy.info@v2
employee.leave_records@v1
report.markdown@v1
email.dispatch.receipt@v1（仅批准后）
```

审批拒绝时，HR、Knowledge、Office 和 Report 的成功结果保留，Email 不执行，
且不能生成 `email.dispatch.receipt`。重复批准或重复 Resume 不得产生第二次发送。

## 真实 HTTP 连续验收

在 PowerShell 中显式授权真实入口测试：

```powershell
$env:RUN_ANNUAL_LEAVE_HTTP_E2E = "1"
```

动态三 Agent连续五次：

```powershell
.\.venv\Scripts\python.exe scripts\run_annual_leave_demo_acceptance.py --scenario dynamic-three --runs 5
```

动态五 Agent批准发送连续五次：

```powershell
.\.venv\Scripts\python.exe scripts\run_annual_leave_demo_acceptance.py --scenario dynamic-five-approved --runs 5
```

审批拒绝路径：

```powershell
.\.venv\Scripts\python.exe scripts\run_annual_leave_demo_acceptance.py --scenario dynamic-five-rejected --runs 1
```

每次命令都会创建独立批次目录：

```text
artifacts/demo-runs/annual-leave/acceptance-<scenario>-<batch-id>/
```

批次目录包含 `acceptance-summary.json`，各次运行使用独立的 run ID、
workflow ID、task ID、Artifact ID 和邮件幂等键。原始证据目录保持 Git
忽略，不覆盖已有证据，也不提交未经脱敏的日志。

验收服务进程在显式设置 `RUN_ANNUAL_LEAVE_HTTP_E2E=1` 时，将 S-ABAC
环境时间固定为 `working_hours`，保证夜间运行仍能稳定到达审批门。该覆盖只在
真实 HTTP 验收进程内生效；普通服务继续使用主机实际时间，工作时间策略没有
被放宽。

## 完成判定

- TaskProfile 从问题中提取意图、业务数据、交付物、操作模式和副作用。
- Agent 由 Registry Contract 闭包动态选择。
- Planner 输出通过 Contract、Schema、数据依赖和 DAG 校验。
- 简单问题生成 HR、Knowledge、Report 三 Agent DAG。
- 复杂问题生成 HR、Knowledge、Office、Report、Email 五 Agent DAG及审批门。
- HR 与 Knowledge 实际并行，Office 等待 HR，Report 完成三路 fan-in。
- Email 在审批前不执行，拒绝审批不发送，重复 Resume 不重复发送。
- 两条成功路径均达到真实 HTTP `5/5`，并保留 Artifact、Schema、lineage、
  execution timeline 和 SSE 终态证据。
