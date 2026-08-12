# SuperAgent 实习课题答辩重点稿

> 适用于 2026 年 8 月 13 日答辩。本稿可直接拆成 3～4 页 PPT，也可作为技术报告的主体章节。

## 一、PPT 直接可用版

### 第 1 页：意图识别——从“分类一句话”升级为“生成可执行任务画像”

**核心结论**

SuperAgent 不把意图识别只做成一个标签分类器，而是将用户语言转换为可校验、可路由、可审计的 `TaskProfile`，为后续 Agent 选择、权限判定和任务图执行提供统一语义入口。

**怎么做**

```text
用户请求
  → 规则识别与语义模型并行分析
  → 意图融合与冲突检测
  → Schema 校验与意图白名单约束
  → 显式/隐式/否定/条件意图建模
  → 实体、风险、缺失字段和依赖抽取
  → TaskProfile + 可执行子任务 DAG
```

**实测指标（33 条结构化任务画像用例）**

| 指标 | 规则模式 | 混合模式 |
|---|---:|---:|
| 主意图准确率 | **93.94%** | **93.94%** |
| 子意图召回率 | **98.48%** | **98.48%** |
| 实体字段准确率 | **96.46%** | 93.23% |
| 否定动作误执行率 | **0%** | **0%** |
| 严格整例通过率 | 60.61% | **63.64%** |

**建议放大展示的一句话**

> 相对课题设定的 90% 关键字段目标线，当前主意图准确率达 93.94%，规则模式实体字段准确率达 96.46%，同时将否定动作误执行率控制为 0%。

**我们的优势**

- 比纯关键词方案更懂同义表达、隐含前置动作、条件和指代。
- 比纯 LLM 方案更稳定：输出受限于标准意图目录和 Pydantic Schema，语义服务异常时可显式降级到规则链路。
- 比“Planner 一次性猜意图+选 Agent”更可评测：任务理解与计划生成解耦，每个意图都保留来源、证据、文本片段和置信度。
- 不只“识别对”，还要“执行对”：能将复合需求转成有顺序、依赖和条件的子任务。

---

### 第 2 页：为什么采用“主 Agent + 多 Agent”编排

**核心结论**

> 我们选择的不是 Agent 之间自由聊天，而是“主 Agent 统一决策、专业 Agent 受控执行”。这个架构同时解决能力扩展、路由准确、权限边界、执行恢复和结果一致性问题。

**架构**

```text
用户
  → 主 Agent（任务画像、澄清、权限候选、路由、DAG、结果汇总）
      → HR Agent
      → 日程/会议 Agent
      → 知识/研究 Agent
      → 文档/邮件 Agent
      → 风险合规 Agent
```

**与其他多 Agent 形态的对比**

| 方案 | 优点 | 局限 | SuperAgent 的选择 |
|---|---|---|---|
| 自由 Group Chat / 轮询 | 原型快、适合头脑风暴 | 消息容易膨胀，停止条件、调用责任和权限链不易统一 | 不用作生产主链路 |
| 去中心化 Swarm / Handoff | 局部决策灵活 | Agent 可能逐步放大上下文和授权，全链路审计和数据最小化更难 | 只允许主 Agent 跨部门调度 |
| 单一通用 Agent + 全部工具 | 结构简单 | 工具集大、Prompt 复杂、最小权限和专业上下文难保证 | 主 Agent 不直接获得所有业务工具权限 |
| **主 Agent + 受控专业 Agent** | 一致出口、专业分工、可观测、可恢复 | 需要更强的 Schema、路由和调度基础设施 | **本项目方案** |

**主 Agent 带来的六个具体优势**

1. **唯一责任主体**：主 Agent 持有用户目标和最终答复权，避免多个 Agent 互相覆盖结论。
2. **权限不扩散**：部门 Agent 只能接收主 Agent 派发的受限子任务，无权自由跨部门互调。
3. **工具选择可治理**：先按 AgentCard 的 intent、capability、action、data scope 和 risk ceiling 过滤，再对合法候选打分。
4. **复合任务可编排**：主 Agent 将请求拆成 TaskGraph，支持串行、并行、条件依赖和结果聚合。
5. **中断可恢复**：执行状态、Artifact 引用和回执可持久化，已成功步骤在恢复时不重复执行。
6. **失败可解释**：路由决策输出候选分数、排除原因、决策码和 trace_id，不再是“模型选了它”。

**失败恢复离线对照实验（每种策略 500 次）**

| 策略 | 任务闭环率 | 首次失败后恢复成功率 | 重复副作用 | 治理违规 |
|---|---:|---:|---:|---:|
| B0：无自动恢复 | 12.4% | 0% | 0 | 0 |
| B1：同 Agent 有界重试 | 27.2% | 16.9% | 0 | 0 |
| B2：重试后等价 Agent 改派 | **40.0%** | **31.5%** | **0** | **0** |

> 口径：该实验使用固定随机种子、Stub Router/Executor 和人工故障注入，用于比较机制，不代表线上生产成功率。

---

### 第 3 页：权限治理——从“调用前检查”升级为“全链路、任务级、失败关闭”

**核心结论**

> 权限不是写在 Prompt 里请求模型遵守，而是写在模型之外的确定性策略引擎和执行闸门中。即使模型选错 Agent 或工具，越权调用也无法真正执行。

**参考与落地映射**

| 参考 | 业界原则 | SuperAgent 落地 |
|---|---|---|
| NIST SP 800-162 ABAC | 综合主体、对象、操作和环境属性判定权限 | `Subject + Object + Scenario + Action` 的 S-ABAC |
| NIST SP 800-207 零信任 | 不因网络位置或资源所有关系隐式信任，按请求实施最小权限 | 每个 Agent、每次工具、每次 Artifact 读取重新鉴权 |
| OPA 的 PDP/PEP 分离 | 策略决策与业务执行解耦 | PolicyEngine 集中决策，路由、AgentProxy、工具和 Artifact 读取处强制执行 |
| OAuth 2.0 Token Exchange / RFC 8693 | 区分委托与冒充，下游凭证应缩小 scope 和时效 | `DelegationGrant` 同时绑定原用户、执行 Agent、task/trace、动作、工具、数据范围、过期时间和调用次数 |
| Cedar / Verified Permissions | 细粒度授权与 default-deny | 未知身份、未知工具、敏感 Artifact 保护链异常时失败关闭 |
| OWASP Agentic AI Security | 工具白名单、最小代理能力和最小权限 | AgentCard + 受信工具清单 + 远程 Tool Gate，防止部门 Agent 绕过主服务调用额外工具 |

**四道权限闸门**

```text
路由前：用户能否看到/使用该 Agent
  → 派发前：本任务的动作、数据、用途和风险是否合法
  → 工具前：工具归属、grant、参数、场景与审批状态是否满足
  → 返回前：Artifact 所有权、敏感度、完整性和输出范围是否合法
```

**与通用多 Agent 框架的差异**

- 通用框架通常提供 guardrail、handoff 或人工审批扩展点；SuperAgent 在此基础上实现了组织角色、岗位、数据敏感度、业务场景、Agent 边界、工具归属和 Artifact 所有权的组合判定。
- 权限是候选召回的硬约束，不是路由评分中可被其他高分抵消的软因素。
- 人工审批只能放行 `REVIEW_REQUIRED`，不能把 `DENY` 提升成 `ALLOW`；审批与用户、资源、参数和任务指纹绑定。
- 敏感结果通过强类型 ArtifactRef 传递，checkpoint 只存脱敏索引和校验和，审计日志不记录 payload 正文。

**当前验证结果**

| 项目 | 结果 | 口径 |
|---|---:|---|
| 权限核心链路专项回归 | **102/102 通过** | 远程工具闸门、Artifact 守卫、调度和输出合约 |
| 机器可读权限治理评测矩阵 | **21 条** | 12 条 API、4 条工作流、5 条数据/审批/清理/远程绕过用例 |
| 上述离线恢复实验的重复副作用 | **0** | 固定种子故障注入实验 |
| 上述离线恢复实验的治理违规 | **0** | 未执行缺少受信合约的备用 Agent |

**边界要主动说明**

当前是治理原型，已验证 S-ABAC、Artifact 所有权、审批、审计、校验和和恢复等机制，但不宣称已实现真实身份认证、生产级密钥管理、AES-GCM 静态加密或 Windows ACL。本次综合相关回归（意图、编排、权限和恢复）中仍有 2 个 Windows 并发审批场景和 1 个环境配置相关场景待处理，因此不应宣称“全量权限测试 100%”。

---

### 第 4 页：量化总结

| 能力 | 目标/对照 | SuperAgent 实测 | 结论 |
|---|---:|---:|---|
| TaskProfile 关键字段/意图 | 课题目标 ≥ 90% | 主意图 93.94%；规则实体字段 96.46% | 超过目标线 |
| 子意图覆盖 | 尽量减少漏调用 | 召回率 98.48% | 复合任务覆盖较高 |
| 否定动作安全 | 不执行“不要发送”类动作 | 误执行率 0% | 评测集内达标 |
| 权限核心链路 | 越权应确定性拦截 | 102/102 专项测试通过 | 核心链路达标 |
| 失败恢复 | B0 无恢复 | B2 闭环率 12.4% → 40.0% | 恢复机制有明显增益 |
| 副作用安全 | 不重复发送/写入 | 离线实验重复副作用 0 | 安全性未被恢复牺牲 |

## 二、技术报告正文版

### 1. 意图识别方案、参考思路与优势

在传统 Agent 系统中，用户需求往往直接进入 Planner，由同一次模型调用同时完成意图理解、任务拆解和 Agent 选择。该做法实现成本低，但是存在三个问题：第一，输出不稳定，难以对意图识别本身单独评测；第二，复合任务的显式动作、隐含前置动作和条件依赖容易混在一段自然语言中；第三，模型可能在不完整理解权限和数据范围的情况下直接选择 Agent。

本项目将任务理解从 Planner 中拆出，建立独立的 `TaskProfile` 契约。识别层同时运行规则识别器和语义识别器：规则侧负责高确定性的动作词、格式化实体、否定范围和业务边界；语义侧负责同义表达、隐含意图、指代消解和歧义发现。两路结果在融合前互不覆盖，融合时按一致性增加置信度，对冲突、高风险、未知意图和非法 Schema 执行程序化约束。

与只输出一个 intent label 的分类方式不同，系统记录每个意图的 `source`、`provenance`、`evidence`、`text_span`、`confidence`、`negated`、`condition` 和 `condition_on`。其中 `provenance` 区分用户明确提出的动作、完成目标所需的隐含前置动作以及由安全策略产生的动作。否定意图会被保留用于审计，但不进入可执行子任务；条件意图会被转成显式依赖边。这使意图识别从“文本分类”变成“可执行语义建模”。

该设计参考了高质量 Agent 项目中的结构化输出、专业 Agent、中心调度、代码编排和离线评测思路。OpenAI Agents SDK 将“Agents as tools”作为 manager-style orchestration 的核心模式，并建议用结构化输出与代码编排获得更可预测的速度、成本和性能；AutoGen 的 SelectorGroupChat 展示了基于模型的下一发言者选择，也允许用自定义 candidate function 缩小候选范围；LangGraph 提供了可持久执行、checkpoint 和人工中断恢复能力。SuperAgent 在这些通用模式之上，将业务意图、数据范围和权限属性纳入同一个可审计任务画像，是本项目相对通用框架的主要工程优势。

### 2. 主 Agent + 多 Agent 编排的选型依据和优势

本项目面向人力、日程、会议、知识、文档和通信等多类业务。如果由一个通用 Agent 持有所有 Prompt、工具和权限，上下文会随工具数量增长，权限半径也会过大；如果采用完全去中心化的 Agent 互调，又难以确保每一次跨部门数据传递都继承原始用户身份、任务目的和最小权限。

因此，本项目选择中心化 manager 模式。主 Agent 是唯一跨部门入口，负责形成 TaskProfile、确定是否澄清、召回已授权 Agent、生成 RoutingDecision、构建 TaskGraph 并汇总最终结果。部门 Agent 仅处理边界明确的子任务，只能使用自身 AgentCard 和 DelegationGrant 允许的工具与数据。

在路由层，系统先执行硬过滤：用户是否有权访问 Agent、Agent 是否在线、是否支持所需动作、数据范围和风险上限。然后只对合法候选按意图 35%、能力 25%、场景 15%、数据范围 10%、历史成功率 10% 和可用性/成本 5% 进行可解释打分。权限不加入加权总分，因为权限不能被“更高的语义匹配度”抵消。

该方案与 OpenAI Agents SDK 所述的 manager-style orchestration 原则一致：由一个 manager 保持对会话和最终答案的控制，将专业 Agent 视为边界明确的能力。它也与 LangGraph Supervisor 的中央调度方向相符。但 SuperAgent 不只做“下一个 Agent 是谁”的语义选择，还将权限候选、输入输出 Schema、Artifact 数据血缘、副作用幂等和 checkpoint 恢复统一纳入调度闭环。

故障注入对照实验进一步验证了编排价值。在 500 次固定种子试验中，无恢复策略 B0 的任务闭环率为 12.4%，同 Agent 有界重试 B1 为 27.2%，在 B1 之后允许等价且已授权 Agent 改派的 B2 为 40.0%。B2 对首次失败的恢复成功率为 31.5%，同时重复副作用和治理违规均为 0。这说明系统并非通过无限重试换取成功率，而是在安全预算内提升任务闭环能力。

### 3. 权限治理的参考体系、落地方案与对标优势

本项目的 S-ABAC 以 NIST SP 800-162 的 ABAC 定义为基础。NIST 将 ABAC 定义为根据主体、对象、请求操作以及环境条件等属性进行授权判定。SuperAgent 将其扩展为 `Subject + Object + Scenario + Action`：Subject 包括原始用户和执行 Agent 的身份、组织和 grant；Object 包括 Agent、工具和 Artifact 的归属、敏感度和数据范围；Scenario 表示本次任务的业务目的、风险、时间、网络区域和审批状态；Action 表示 read、write、send、delete 等具体操作。

NIST SP 800-207 的零信任原则强调不能因网络位置或所有关系赋予隐式信任，并应针对每次请求实施最小权限。因此，本项目没有采用“主 Agent 已授权，下游全部信任”的方式，而是在路由、派发、工具和结果读取四个阶段重新评估。即使 S-ABAC 开关未启用，`confidential` 和 `restricted` Artifact 的读取仍失败关闭，避免安全组件异常反而放大权限。

在架构分层上，本项目参考 OPA 对 PDP 与 PEP 的分工，使用 PolicyEngine 集中执行策略决策，并将执行点分布在主 Agent 路由、AgentProxy、本地/远程工具包装器和 ArtifactResolver 等位置。这种分层避免了每个部门 Agent 自行实现一套不一致的权限逻辑。

在跨 Agent 委托方面，本项目参考 RFC 8693 对 delegation 和 impersonation 的区分。主 Agent 不冒充原始用户，也不把自身的权限传递给部门 Agent，而是签发同时记录原始用户和当前执行者的任务级 `DelegationGrant`。该 grant 只对指定 task/trace、指定 Agent、指定动作、工具和数据范围生效，并受过期时间、最大调用次数和 approval_id 约束。

与通用 Agent 框架相比，SuperAgent 的差异不是“有没有 guardrail API”，而是已将组织、业务、任务、工具和数据五个层次的权限关系串成可执行闭环。通用框架通常提供安全扩展点，而本项目针对部门 Agent 场景实现了权限感知候选、任务级委托、远程工具闸门、可信 Artifact、一次性审批、幂等回执和脱敏审计。专项回归测试中，这些核心链路共 102 项全部通过。

## 三、指标口径与答辩风险提示

### 1. 不要直接说“竞品 90%，我们 96%”

除非竞品和本项目使用同一数据集、同一标签体系、同一评分脚本和同一模型配置，否则两个准确率不可直接比较。AutoGen、LangGraph、CrewAI 和 OpenAI Agents SDK 是编排框架，并不提供可直接对齐本项目 33 条业务意图集的“产品意图正确率”。

推荐改成：

> “课题设定的 TaskProfile 关键字段目标是 90%。在自建 33 条固定评测集上，我们的主意图准确率为 93.94%，规则模式实体字段准确率为 96.46%，超过项目目标线。”

### 2. 96.46% 的正确含义

96.46% 是当前规则模式的**实体字段准确率**，不是整套意图识别的严格整例通过率。主意图准确率是 93.94%；要求主意图、子意图、实体、依赖、风险、缺失字段、澄清和置信度区间全部同时正确时，混合模式的严格整例通过率为 63.64%。答辩时主动说明该口径，反而能体现评测严谨性。

### 3. 权限“100%”的正确说法

可以说“权限核心链路专项回归 102/102 通过”，不要说“所有权限测试 100%”。本次综合相关回归（意图、编排、权限和恢复）为 172/175，仍有并发审批 ID 碰撞、Windows 文件原子替换和当前环境配置影响的场景待修复或隔离。

## 四、建议准备的评委问答

**Q1：为什么规则模式的实体准确率比混合模式高？**

A：当前评测集的人名、时间和文档类型格式较稳定，规则在这类字段上更确定。语义模型的价值主要体现在隐含依赖、邮件接收人和否定表达等场景，因此严格整例通过率从 60.61% 提升到 63.64%。后续需要通过融合权重和语义回归稳定性继续优化，而不是默认 LLM 一定比规则好。

**Q2：多 Agent 一定比单 Agent 好吗？**

A：不一定。简单任务优先由一个专业 Agent 闭环，只有跨能力、跨数据或存在显式依赖时才生成多 Agent TaskGraph。本项目追求的不是 Agent 数量，而是在必要时使用专业分工，并将无效多 Agent 调用率作为成本指标。

**Q3：主 Agent 不会成为单点吗？**

A：逻辑上它是唯一编排责任主体，但不代表部署上只有一个进程。主 Agent 的决策对象、PlanSnapshot、checkpoint、Artifact 和回执都可持久化，因此可以实现无状态水平扩展和中断恢复。原型已验证恢复机制，生产化仍需引入稳定的共享存储和身份系统。

**Q4：如果模型被 Prompt Injection 诱导调用越权工具怎么办？**

A：模型输出不是授权依据。工具调用必须经过模型之外的 PolicyEngine 和 Tool Gate，并同时满足原用户 grant、Agent 边界、任务数据范围、工具策略和当前场景。远程 Agent 选择了授权清单以外的工具时，主服务在真实调用发生前拒绝。

**Q5：为什么不只用 RBAC？**

A：RBAC 适合表达“谁是 HR 经理”，但不足以表达“HR 经理是否可以在这个任务中，为这个员工，以这个用途，在已审批状态下，调用这个工具读取这类字段”。S-ABAC 保留角色信息，同时加入对象、任务场景、动作和环境属性，才能做到任务级最小权限。

## 五、参考资料

- [NIST SP 800-162: Guide to Attribute Based Access Control](https://csrc.nist.gov/pubs/sp/800/162/upd2/final)
- [NIST SP 800-207: Zero Trust Architecture](https://csrc.nist.gov/pubs/sp/800/207/final)
- [RFC 8693: OAuth 2.0 Token Exchange](https://www.rfc-editor.org/info/rfc8693/)
- [Open Policy Agent: How to Deploy OPA](https://www.openpolicyagent.org/docs/deploy)
- [Amazon Verified Permissions / Cedar](https://docs.aws.amazon.com/verifiedpermissions/latest/userguide/what-is-avp.html)
- [OWASP Top 10 for Agentic Applications 2026](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/)
- [OpenAI Agents SDK: Agent orchestration](https://openai.github.io/openai-agents-python/multi_agent/)
- [AutoGen: Selector Group Chat](https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/selector-group-chat.html)
- [AutoGen: Swarm](https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/swarm.html)
- [LangGraph: Overview](https://docs.langchain.com/oss/python/langgraph/overview)
- [LangGraph: Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)0
