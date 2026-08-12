# 01_cover

各位老师好，我们的课题不是再做一个会调用工具的 Agent，而是把开放式模型能力收敛成一套可治理的数字员工执行闭环。整套成果围绕四个环节展开：主 Agent 决策与路由、多 Agent 协同编排与工具选择、记忆与 Skill，以及权限治理。接下来我会用产品对标、机制设计、评测数据和真实原型，说明这四层怎样共同保证决策、执行、状态和治理可信。

---

# 02_four_module_map

这一页先给出全局答案。主 Agent 负责把自然语言变成 TaskProfile，并在授权候选中做可解释路由，我们用三十三条意图用例验证这一层。多 Agent 层把计划变成 TaskGraph，负责调度、工具选择和恢复，四十八个工具平均收敛到二点一七个候选，B2 策略下任务闭环率达到百分之四十。Memory 和 Skill 分别治理上下文、长期事实和程序经验，Memory 的六十四条确定性用例全部通过。S-ABAC 则在模型之外执行四道 PEP 和三态授权，权限核心测试一百零五条全部通过。时间安排是 PPT 十六到十八分钟、演示五分钟，其余用于提问。

---

# 03_chapter_main_agent

第一章回答“做什么、谁来做”。我们的判断是，模型理解一句话并不等于系统可以执行；只有把理解结果固化为 TaskProfile，经过权限过滤并生成可审计的 RoutingDecision，后续计划和执行才有可靠入口。

---

# 04_main_agent_comparison

这一页重点回答为什么没有直接复刻通用框架的路由方式。OpenAI Agents SDK 提供 manager、agent-as-tool 和 handoff 等多 Agent 原语；AutoGen 的 SelectorGroupChat 可以根据对话选择下一位参与者；LangGraph 用 Router 和 StateGraph 明确状态与执行关系。这些能力都很强，但企业数字员工还需要业务意图、数据范围、动作风险和授权候选四类约束。SuperAgent 的差异不是新增一种 handoff，而是在模型打分以前，先用 TaskProfile 和 AgentCard 构造合法候选集合，再输出 Top-K、排除原因、reason codes 和 trace id。这里不做跨评测集准确率排名，只比较官方能力边界与我们补上的业务契约。

[Sources]
https://openai.github.io/openai-agents-python/multi_agent/
https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/selector-group-chat.html
https://docs.langchain.com/oss/python/langgraph/overview

---

# 05_permission_aware_routing

这一页解释路由链路。用户请求首先被解析成 TaskProfile，除了主意图、子意图和实体，还保留动作、数据范围、风险、否定与依赖。随后 AgentCard 按授权、在线状态、动作、范围和风险上限做硬过滤，被排除的候选留下 reason codes，但绝不进入评分。剩余候选才按意图、能力、场景、范围、历史和可用性计算 RouteScore。系统最终只有三种终态：合法候选明确时 ROUTE，关键信息缺失时 CLARIFY，没有合法候选时 REJECT 或 NO_CAPABLE_AGENT。核心原则是权限不能被更高的语义分抵消。

---

# 06_intent_eval_console

这一页同时展示评测和真实决策台。三十三条固定用例中，主意图百分之九十三点九四、实体字段百分之九十六点四六、依赖完整率百分之九十二点三一、子意图召回率百分之九十八点四八，严格整例达到百分之九十四点三，澄清触发达到百分之九十五点六，六项指标均已越过百分之九十目标线。右侧完整截图证明这些字段、候选 Agent 和推荐结果已经进入可操作原型。严格整例仍采用多字段同时正确的口径，否定动作误执行保持为零；下一步继续扩大测试语料，并补充规则、语义和混合模式的同配置对照。

---

# 07_chapter_orchestration

第二章回答任务怎样被多人、多工具可靠完成。计划不能只是一段模型生成的文本，它必须经过校验，转成可调度、可暂停、可恢复的执行契约，才能交给 Scheduler。

---

# 08_orchestration_comparison

这一页先对标编排路线。ReAct 擅长边思考边行动，但完整路径难以前置校验；AutoGen 的对话协作灵活，消息、状态和业务结果容易混在同一对话流里；LangGraph 提供显式图、Checkpoint 和 Interrupt，业务合同仍由应用补充；A2A 统一 Agent Card、Task、Message 和 Artifact 的互操作对象，但不负责组织内部权限和工具选择。SuperAgent 采用分层方案：LLM 生成候选计划，系统依次完成归一化、需求覆盖、Agent Contract、输入输出 Schema、数据依赖和 DAG 校验，最后才生成 TaskGraph 并交给 Scheduler。任何校验失败都回到 Planner 或进入澄清，不直接放行。

[Sources]
https://arxiv.org/abs/2210.03629
https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/selector-group-chat.html
https://docs.langchain.com/oss/python/langgraph/overview
https://a2a-protocol.org/latest/specification/
https://docs.temporal.io/temporal

---

# 09_control_data_tool_resolver

这一页解释两个容易混淆的问题。第一，控制面和数据面必须解耦：TaskGraph、Scheduler 和 StepResult 决定状态、依赖与何时执行；Agent、Artifact 和 ArtifactRef 负责产物、引用与血缘，两条轨道只在 StepResult.outputs 交叉。第二，工具不能把整个目录直接交给模型。我们的 ToolRegistry 有三十个 Office MCP 工具和十八个 Excel MCP 工具，先按 Server、Scenario、Operation、Agent 和 Input Schema 做确定性过滤，再按名称、别名、意图和描述评分，并允许 ABSTAIN。固定评测集中，平均候选从四十八降到二点一七，Top-1、Top-3 和 Schema 有效率均为百分之百，工具幻觉为零，平均耗时一点五一毫秒。当前仍以 audit 模式为主，不能外推到开放工具环境。

---

# 10_pause_resume_recovery

这一页回答中断后如何安全继续。暂停不是停掉线程，而是 RUNNING、PAUSE_REQUESTED、PAUSED 再回到 RUNNING 的显式状态机，只在安全点落盘。提交顺序是先写 Artifact Payload，再写 Checkpoint，最后标记步骤完成；如果外部写入状态未知，就进入 NEEDS_RECONCILIATION，由人工对账。右侧对照实验每种策略运行五百次，使用固定随机种子、Stub Router 与 Executor 和人工故障注入。B0 没有自动恢复时，闭环率百分之十二点四，首次失败后恢复为零；B1 有界重试提升到百分之二十七点二和十六点九；B2 允许改派等价且已授权 Agent 后，达到百分之四十和三十一点五。重复副作用和治理违规都为零。实验用于比较机制，不代表生产成功率；自动重试和改派主要限于只读步骤。

---

# 11_chapter_memory_skill

第三章讨论系统怎样“记住”。会话上下文、跨会话长期事实和程序性经验的来源、生命周期与风险完全不同，必须分成三条治理通道，并且只由主 Agent 统一使用，避免直接污染远端 Agent。

---

# 12_memory_comparison_channels

这一页先看同类能力。OpenAI Sessions 重点是会话历史持久化；LangGraph 用 Checkpointer 与 Store 区分线程状态和跨线程存储；Mem0 强调动态抽取、整合与检索；Voyager 把成功经验沉淀成可执行 Skill Library。我们的先进性不在宣称某个单项记忆算法达到 SOTA，而在三通道组合与治理。上下文压缩在窗口超限前发生，保留最近两轮和未完成的 Plan、TaskGraph、Artifact；长期 Memory 用固定办公标签记录来源、用户或项目范围、冲突和衰减，只注入主 Agent；Step 或 Agent Skill 需要 LLM 反思、至少两次独立成功证据，并通过 Agent、Tool、Schema、范围和副作用门禁。命中 Skill 只增强对应 Plan 步骤，不能跳过 Planner。

[Sources]
https://openai.github.io/openai-agents-python/sessions/
https://docs.langchain.com/oss/python/langgraph/persistence
https://docs.langchain.com/oss/python/langgraph/add-memory
https://docs.mem0.ai/platform/overview
https://arxiv.org/abs/2305.16291

---

# 13_memory_skill_evaluation

这一页把三个通道分别评测。上下文压缩在八 K、十六 K、三十二 K 档位分别从四千四百二十六降到三千四百六十、六千九百三十九降到六千二百六十二、一万一千九百零五降到六千一百三十七；三档都保留最近两轮和四分之四事实，压缩后的三条工作流恢复九分之九步骤、Artifact 和事实，禁止副作用为零。Memory 的六十四条确定性状态机用例全部通过，TP 四十四、FP 和 FN 都为零，但这不等同于在线 LLM 抽取准确率。Skill 是三十一分之三十六，五个失败都定位到 data scope 规范化不一致：计划侧已经规范化，Skill 卡仍保留点号原格式。邮件高风险复用一分之一被正确阻断，其余门禁维度均为六分之六。另有十七条本地端到端链路验证模块连接，但不包装成线上成功率。

---

# 14_chapter_permission_governance

第四章进入权限治理。我们的原则是，权限必须由模型之外的执行系统强制；一次通过不会沿整条链路永久继承，路由、派发、工具和结果四个执行点都要重新鉴权。

---

# 15_permission_comparison_sabac

这一页回答为什么不只使用 RBAC。ACL 对单个对象直观，但规模扩大后难维护；RBAC 适合稳定岗位，却不足以表达任务目的和数据范围；ABAC 与 ReBAC 能提供属性和关系层面的细粒度控制；OPA 和 Cedar 提供外置 PDP 与策略语言，是后续生产迁移候选；OpenAI Guardrails 能做输入、输出和工具级校验，但企业授权关系仍由应用实现。SuperAgent 的 S-ABAC 同时考虑 Subject、Object、Scenario 和 Action：用户、Agent、部门、信任与委托是一组主体属性，Agent、Tool、Artifact、敏感度和范围是一组对象属性，再叠加业务目的、风险、审批、环境和具体动作。PolicyEngine 输出 ALLOW、DENY 或 REVIEW_REQUIRED；人工审批只能放行 REVIEW_REQUIRED，不能把 DENY 提升为允许。

[Sources]
https://csrc.nist.gov/pubs/sp/800/162/upd2/final
https://www.openpolicyagent.org/docs
https://docs.cedarpolicy.com/
https://openai.github.io/openai-agents-python/guardrails/

---

# 16_four_pep_security_dashboard

这一页把策略落到四个真实执行点。路由前，无权 Agent 不进入候选；派发前，重新核验目标 Agent、动作、数据域和风险；工具前，核验工具归属、DelegationGrant、参数、场景与审批状态；结果前，Artifact Guard 检查 owner、producer、sensitivity、scope、checksum 和 derived_from。右侧是完整 S-ABAC 管理台截图，当前原型包含六个用户画像、十八项 Agent 属性、四十二项资源属性和三条策略。高风险审批不是一张永久通行证，它一次性绑定 task、resource、action、scenario 和 params。未知资源、Receipt 损坏或关键持久化失败时一律 Fail Closed。

---

# 17_evidence_demo_boundaries

最后用三层证据收束。结构和合同测试验证接口是否按设计工作，包括主 Agent 一百一十三分之一百一十三、权限核心一百零五分之一百零五和 API 十二分之十二；专项评测分别回答意图、工具、恢复、Memory、Skill 与权限机制的效果；十七条本地端到端链路和现场演示验证模块能连起来。专题合并回归是二百五十二分之二百五十三，唯一失败是 UI 静态资源缓存版本断言。五分钟演示从“查询员工张三工资”开始，依次展示 TaskProfile、候选 Agent、S-ABAC 返回 REVIEW_REQUIRED、一次性审批、恢复执行，以及 Receipt、Artifact 和时间线。如果现场服务异常，就切换到截图与已有回归证据，仍按这条链路解释。边界也明确：Tool Resolver 仍以 audit 为主，Memory 六十四分之六十四是确定性状态机，Skill 保留五个缺陷；当前不等同于生产 IAM、密钥管理、加密或公网部署安全认证。

---

# 18_team_roles

最后是人员分工。刘建杰负责主 Agent 决策与路由、S-ABAC 权限治理和系统集成，对应 TaskProfile、RoutingDecision、四道 PEP、治理台及相关问答。成员 A 负责多 Agent 编排、TaskGraph、Scheduler、Tool Resolver 和暂停恢复；成员 B 负责上下文压缩、长期 Memory 和 Step 或 Agent Skill；成员 C 负责前端原型、演示场景、自动化回归及 PPT、报告材料整合；成员 D 专门负责评测集建设，包括样本设计与分层、标注口径和期望结果、回归集维护与结果复核，并负责解释指标口径和误差分析。五个人共同负责 Agent、Tool、Artifact 契约、统一评测口径、联调、演示演练和材料复核。答辩前需要把四处“成员姓名待补充”替换为真实姓名。
