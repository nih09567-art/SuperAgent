# 主动安全暂停与恢复实验

该实验验证运行中的 Scheduler 收到暂停请求后，是否在当前批次完成持久化后进入
`PAUSED`，以及从持久化检查点恢复时是否跳过已完成步骤。

固定数据集包含 3 类场景，每类运行 2 轮：

1. 串行链路：首步执行期间请求暂停，后续步骤不得提前启动。
2. 并行批次：两个无依赖只读步骤并行完成后暂停，下游汇总步骤不得启动。
3. 副作用前暂停：准备步骤后暂停，恢复时发送一次；再次重放同一检查点时不得
   再次调用副作用执行器。系统可以复用可信 Receipt，或在证据不完整时进入人工对账。

运行方式：

```powershell
.venv\Scripts\python.exe experiments\safe_pause\benchmark.py `
  --output-dir .artifacts\quantitative\<run-id>\safe-pause
```

结果包括 `trials.csv`、`summary.json`、`summary.csv`、`summary.md` 和
`manifest.json`。

证据边界：实验使用确定性进程内执行器，但调用生产 Scheduler、持久化
Checkpoint、TaskControlStore 和 ReceiptStore。它验证的是主动安全暂停协议，不能替代
真实 HTTP 请求或跨进程恢复实验，也不与跨进程样本合并计算成功率。
