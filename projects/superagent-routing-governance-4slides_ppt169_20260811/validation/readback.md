# SuperAgent_主Agent决策路由与权限治理_20260811_202425

- Source: `SuperAgent_主Agent决策路由与权限治理_20260811_202425.pptx`
- Total slides: 4

## Slide 1

框架对比

| 对比维度 | OpenAI Agents SDK | LangGraph | AutoGen | SuperAgent |
| --- | --- | --- | --- | --- |
| 编排定位 | Manager / Handoff | 图状态机 / Supervisor | Selector GroupChat | 主 Agent 受控调度 |
| 决策方式 | LLM + 代码编排 | 节点与边显式控制 | 模型选下一发言者 | 硬过滤 + 可解释评分 |
| 权限边界 | Guardrail / 审批扩展 | HITL / Guardrail 可扩展 | Candidate function 约束 | S-ABAC + 四道 PEP |
| 状态恢复 | Session / 耐久引擎集成 | Checkpoint / HITL | 消息状态 / 终止条件 | Checkpoint + 幂等回执 |
| 审计粒度 | Run / Trace | State / Graph | 消息 / 选人过程 | Task / Agent / Tool / Artifact |
| 适用结论 | 通用 Agent SDK | 耐久工作流 | 多 Agent 协作原型 | 数字员工治理闭环 |

结论：借鉴中央调度与持久化，但将权限作为候选硬约束，并把 Artifact、审批与副作用安全纳入同一 trace。

> [SmartArt scan unavailable: Missing required PPTX part: ppt/slides/slide1.xml]

### Speaker Notes

- 调研表明，OpenAI Agents SDK 和 LangGraph 都支持中央调度，AutoGen 更偏向会话式的下一 Agent 选择。SuperAgent 借鉴这些编排思路，但把权限候选、数据范围、Artifact 所有权和副作用安全一并收口到主 Agent。
- [Sources]
- • https://openai.github.io/openai-agents-python/multi_agent/
- • https://langchain-ai.github.io/langgraphjs/reference/modules/langgraph-supervisor.html
- • https://microsoft.github.io/autogen/dev/user-guide/agentchat-user-guide/selector-group-chat.html

## Slide 2

决策路由

01 任务画像

02 合法候选

03 路由评分

04 执行闭环

生成 TaskGraph，将依赖、重试、改派、Artifact 与 Receipt 统一追踪。

意图35%、能力25%、场景15%、数据10%、成功率10%、成本5%。

规则+语义生成 TaskProfile，保留显式、隐式、否定与条件意图。

AgentCard 按意图、动作、数据范围和风险上限硬过滤。

> [SmartArt scan unavailable: Missing required PPTX part: ppt/slides/slide2.xml]

### Speaker Notes

- 主 Agent 不直接凭一次模型输出选 Agent，而是先把请求变成可校验的 TaskProfile，再以权限与能力硬过滤收缩候选。权限不进入加权总分，因为高匹配度不能抵消越权；最终 RoutingDecision 同时输出分数、排除原因和 trace_id。
- [Sources]
- • E:/Program/SuperAgent/docs/答辩重点稿-意图识别-多Agent编排-权限治理.md
- • E:/Program/SuperAgent/src/orchestrator/task_profiler.py
- • E:/Program/SuperAgent/src/orchestrator/department_router.py

## Slide 3

权限治理

01

02

任务授权

结果守卫

校验动作、数据、用途与风险，签发绑定 task/trace 的 Grant。

检查 Artifact 所有权、敏感度、Schema 完整性与输出范围。

03

04

路由预检

工具闸门

用户是否可见、可用该 Agent；无合法候选则直接拒绝。

校验工具归属、Grant、参数指纹、场景与审批状态。

> [SmartArt scan unavailable: Missing required PPTX part: ppt/slides/slide3.xml]

### Speaker Notes

- 权限治理不是写在 Prompt 里要求模型自律，而是在路由、派发、工具和结果四个位置部署确定性执行闸门。这一设计对应 ABAC 的主体、对象、操作和环境属性，也对应零信任的逐请求最小权限、OPA 的 PDP/PEP 分离与 RFC 8693 的委托语义。
- [Sources]
- • https://csrc.nist.gov/pubs/sp/800/162/upd2/final
- • https://csrc.nist.gov/pubs/sp/800/207/final
- • https://www.openpolicyagent.org/docs/deploy
- • https://www.rfc-editor.org/info/rfc8693/

## Slide 4

评测量化

01 严格整例

02 主意图

03 主目标

04 子意图 P/R

07 澄清判断

08 安全治理

05 实体字段

06 依赖完全

02

60.61%（20/33）；任一标注字段错误即整例失败。

93.94%（31/33）；顶层业务域判断较稳定。

100%（9/9）；有主目标标注的样本全部正确。

100% / 98.48%；没有多执行，少量隐含意图漏召回。

96.46%；按样本逐字段得分后宏平均。

92.31%（24/26）；整套依赖边必须完全相等。

75%（6/8）；模糊与边界表达是当前主要短板。

否定动作误执行 0%（0/5）；权限核心回归 102/102，治理矩阵 21 条。

01

03

08

04

原型实测

07

05

06

> [SmartArt scan unavailable: Missing required PPTX part: ppt/slides/slide4.xml]

### Speaker Notes

- 这些数字来自三十三条固定标注样本，指标口径彼此独立：严格整例要求全部标注字段同时正确，因此明显低于单字段准确率。权限部分只表述为核心链路专项回归一百零二项全部通过，不延伸为生产级安全认证。
- [Sources]
- • E:/Program/SuperAgent/docs/答辩重点稿-意图识别-多Agent编排-权限治理.md
- • E:/Program/SuperAgent/tests/intent_eval_cases.json
- • E:/Program/SuperAgent/tests/evaluations/permission_governance_eval.json
