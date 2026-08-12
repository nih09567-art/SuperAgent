# MCP 独立盲测与只读 enforce 边界实验

本实验使用在读取 Selector 实现及既有评测金标准前冻结的 36 条新案例：24 条自然语言正例、8 条易混淆用例和 4 条应弃选用例。另从已冻结正例派生 8 个缺参 Schema 探针，并选取 8 条只读用例检查 enforce 边界。

运行：

```powershell
.venv\Scripts\python.exe -m experiments.mcp_realistic.benchmark `
  --output-dir .artifacts/quantitative/20260811-2225-realistic-2c9c684/realistic/mcp
```

输出包括 `cases.csv`、`schema_probes.csv`、`enforce.jsonl`、`summary.csv`、`summary.json`、`summary.md` 和 `manifest.json`。

生产 `ToolSelectionService` 当前会将请求的 `enforce` 模式降级为 `audit`，不会改变 Executor 实际工具集合。本实验保留该失败结果；隔离只读 enforce 使用 Recording Executor 验证 Top-K 工具集合约束和调用成员关系，不调用 MCP Server、LLM、网络或真实副作用工具，不能表述为生产 enforce 已接入。
