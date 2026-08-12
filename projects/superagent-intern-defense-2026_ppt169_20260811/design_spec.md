<!-- ppt-master-schema: design-spec/v1 -->
# SuperAgent 实习课题答辩 - Design Spec

## I. Project Information

| Item | Value |
| --- | --- |
| Project Name | SuperAgent 实习课题答辩 |
| Canvas Format | PPT 16:9 · 1280 × 720 |
| Page Count | 18 |
| Primary Language | zh-CN |
| Target Audience | 实习课题答辩评审老师、带教老师与项目负责人；了解智能体基本概念，更关注技术选型依据、同类产品差异、量化证据、系统边界和可演示成果 |
| Communication Intent | 先用同类产品对标说明为什么选择当前技术路线，再按四个模块解释机制与问题覆盖，用评测集、运行结果和现场演示证明工程原型可用、可恢复、可治理，最后主动交代限制并支撑评审提问 |
| Desired Audience Outcome | 评委能够确认课题成果覆盖任务书要求，理解 SuperAgent 相对通用 Agent 框架的关键差异，认可评测口径和实现证据，并清楚看到当前原型边界与下一步生产化方向 |
| Core Message / Ask / Action | SuperAgent 的核心贡献不是再增加一个会调用工具的 Agent，而是用 TaskProfile、可信 TaskGraph、Memory/Skill 与 S-ABAC，把开放式模型能力收敛为可校验、可编排、可恢复、可治理的数字员工执行闭环 |
| Delivery Context | 主要为 2026 年 8 月 13 日现场答辩，总时长 30 分钟；PPT 讲解 16–18 分钟、原型演示约 5 分钟、提问 7–9 分钟；次要作为会后评审留档和技术成果索引 |
| Artifact Afterlife | 作为实习课题答辩材料、技术报告摘要、评测证据索引与后续原型交接说明，可在答辩后独立阅读 |
| Reading Mode | balanced |
| Content Strategy | balanced default：允许按答辩逻辑重组和提炼来源内容，但所有事实、指标、边界与结论保持可回溯，不新增无来源的性能宣称 |
| Design Style | 蓝橙证据链：参考 PDF 的白底、蓝橙双强调、章节大编号、数据页与橙色圆形页码语言，移除其品牌标识和业务素材，强化技术架构与评测证据 |
| Formula Policy | text-only |
| AI Image Acquisition Path | not applicable |
| Generation Mode | continuous |
| Spec Refinement | disabled |
| Speaker Notes | enabled — final Stage-2 proactive policy |
| Custom Animations | disabled — final Stage-2 proactive policy |
| Narration Audio | disabled — final Stage-2 proactive policy |
| Created Date | 2026-08-11 |

## II. Canvas Specification

| Property | Value |
| --- | --- |
| Format | ppt169 |
| Dimensions | 1280 × 720 |
| viewBox | `0 0 1280 720` |
| Margins | 40 px outer safe margin；正文页顶部标题区后保留 26 px 呼吸区 |
| Content Area | x=40..1240，y=40..680；页脚来源与橙色页码位于底部安全区内 |

## III. Visual Theme

### Theme Style

- **Mode**: pyramid
- **Visual style**: custom
- **Visual Style References**: swiss-minimal, data-journalism, editorial
- **Visual Style Behavior**: swiss-minimal 提供严格栅格、大留白、锐利矩形和极少装饰；data-journalism 提供指标条、微型图表、来源行和高密度但可读的数据结构；editorial 提供章节编号、细规则与证据层级。延续参考 PDF 的蓝橙交叠圆弧作为跨页识别符，但不复用其品牌元素。
- **Theme**: “受控执行闭环”是跨页视觉主线：蓝色表示主路径与可信状态，橙色表示评审关注点、风险或人工闸门；封面、章节页和结论页以交叠圆弧/环形路径重复出现，正文页缩减为左上角短弧或底部路径节点。
- **Tone**: 正式、克制、技术可信；以证据和限制建立信任，不用未来感炫光、重渐变或营销式口号。

### Color Scheme

| Role | HEX | Purpose |
| --- | --- | --- |
| Background | #FFFFFF | 主画布、留白与阅读面 |
| Secondary background | #F3F7FB | 证据区、浅色分栏、界面截图承托 |
| Primary | #0B5CAD | 主流程、章节编号、核心结论与正向系列 |
| Accent | #F28C28 | 对标差异、风险、人工审批、关键页码与强调点 |
| Secondary accent | #4CA6C6 | 次级系列、状态通道与辅助连接 |
| Body text | #1E2A36 | 正文与高对比标签 |
| Grid | #DCE5EF | 表格线、分隔线、微型坐标与截图框 |
| Positive | #21A179 | ALLOW、通过、完成与安全终态 |
| Negative | #D95C5C | DENY、失败样本与生产边界警示 |

## IV. Typography System

### Font Plan

| Role | Character (Reference) | Primary | English if non-English | Fallback tail |
| --- | --- | --- | --- | --- |
| Title | 几何无衬线、结论先行 | Microsoft YaHei | Arial | sans-serif |
| Body | 中性无衬线、适合投影与留档 | Microsoft YaHei | Arial | sans-serif |
| Data | 数字紧凑、便于比较 | Arial | Arial | Microsoft YaHei, sans-serif |
| Code | 等宽、用于契约与状态名 | Consolas | Consolas | Microsoft YaHei, monospace |

- **Title stack**: Microsoft YaHei, Arial, sans-serif
- **Body stack**: Microsoft YaHei, Arial, sans-serif
- **Data stack**: Arial, Microsoft YaHei, sans-serif
- **Code stack**: Consolas, Microsoft YaHei, monospace
- **Role rationale**: Data 角色用于全篇指标、比例和产品矩阵；Code 角色用于 TaskProfile、TaskGraph、ALLOW/DENY/REVIEW_REQUIRED 等可执行契约，二者反复出现且需与正文形成稳定区分。

### Font Size Hierarchy

| Purpose | Anchor Size (px) |
| --- | ---: |
| Body | 24 |
| Title | 42 |
| Subtitle | 32 |
| Annotation | 18 |
| Chapter title | 54 |
| Chapter number | 128 |
| Data | 30 |
| Code | 20 |
| Footnote | 14 |

## V. Layout Principles

### Page Structure

- **Header area**: 正文页用左对齐结论式标题，标题上方可有 01–04 章节眉；不使用重复大 Logo。章节页以超大章节号、短结论和蓝橙环形路径组成。
- **Content area**: 以 12 列隐式栅格组织；每页只设一个主视觉骨架，比较矩阵、流程图、指标图和截图各自承担主任务，避免把所有内容塞入对称卡片墙。
- **Footer area**: 左侧为必要的来源/口径短句，右侧为橙色圆形页码；来源页脚优先写产品官方文档名或内部评测文件名。

### Spacing Specification

| Element | Current Project |
| --- | --- |
| Safe margin | 40 px |
| Content block gap | 24 px；大区块 32 px |
| Icon-text gap | 12 px |

## VI. Icon Usage Specification

- **Primary bundled library**: tabler-outline
- **Stroke Width**: 2

| Icon Path | Suitable Scenarios |
| --- | --- |
| tabler-outline/map-route | 意图路由、候选路径、决策 |
| tabler-outline/network | TaskGraph、多 Agent、依赖与并行 |
| tabler-outline/shield-lock | 权限、S-ABAC、失败关闭 |
| tabler-outline/tools | MCP 工具目录与 Resolver |
| tabler-outline/brain | 上下文、记忆与语义判断 |
| tabler-outline/database | Artifact、Checkpoint、持久化 |
| tabler-outline/history | 长期记忆、时间衰减、恢复 |
| tabler-outline/chart-bar | 评测指标、对照实验 |
| tabler-outline/users-group | 多 Agent 团队与人员分工 |
| tabler-outline/presentation-analytics | 答辩、成果与演示 |
| tabler-outline/player-pause | PAUSE/RESUME、安全点 |
| tabler-outline/checks | 校验门禁、已通过证据 |
| tabler-outline/clipboard-check | 计划可信化、评测清单 |

## VIII. Image Resource List

| Filename | Dimensions | Ratio | Purpose | Type | Layout pattern | Crop Policy | Acquire Via | Status | Reference | text_policy | page_role |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| image_006.png | 895 × 755 | 1.19 | 展示主 Agent 决策台的真实 TaskProfile、候选 Agent 与推荐结果 | UI screenshot | #P1-02 Side image with content field；完整截图与左侧指标条形成证据对照 | no-crop | user | Existing | 主 Agent 决策台；保留标题、主意图/动作/风险、推荐 Agent 和意图详情，不裁切界面文字 | embedded | local |
| image_010.png | 1688 × 816 | 2.07 | 展示 S-ABAC 状态、拒绝详情、人工审批队列与策略规模 | UI screenshot | #P1-12 Framed figure with caption；横向完整截图置于四道闸门结构旁 | no-crop | user | Existing | S-ABAC Security Dashboard；完整保留安全状态、最近拒绝、审批与 3/18/42 统计 | embedded | local |

## IX. Content Outline

### Part 0: 开场与总览

#### Slide 01 - 封面

- **Audience move**: 从“这是一个常见 Agent 原型”→意识到课题关注的是把模型能力变成受控执行系统。
- **Layout**: 左侧标题与副标题占主要阅读区；右侧以蓝橙交叠环形路径连接四个极简节点，节点分别标注“决策、编排、状态、治理”；保留大面积白色负空间，底部给出答辩日期和课题组。
- **Title**: 把开放式 Agent 变成可治理的数字员工执行闭环
- **Core message**: 课题成果是一套跨决策、执行、状态与治理的系统机制，而不是单个模型或单次工具调用。
- **Content**: 主标题；副标题“主 Agent 决策与路由｜多 Agent 协同编排与工具选择｜记忆与 Skill｜权限治理”；“实习课题答辩 · 2026.08.13”；“SuperAgent 数字员工智能任务执行关键技术研究组”。
- **Cover impact**: 以四个模块构成的闭环作为绑定视觉钩子，蓝色主路径穿过四节点，橙色人工/风险节点与其交叠。

#### Slide 02 - 结论与四章地图

- **Audience move**: 从只看到四个并列名词→理解四个模块分别回答执行闭环的四个核心问题并共同形成证据链。
- **Layout**: 中央为一条从用户请求到结果回执的横向主链；上下分布四个模块卡点，每个点只保留一个问题、一句机制和一个关键证据；右下角用 16–18 / 5 / 7–9 分钟显示答辩节奏。
- **Title**: 四个模块共同回答：做什么、谁来做、如何继续、是否允许
- **Core message**: TaskProfile、TaskGraph、Memory/Skill 与 S-ABAC 分别承担语义、执行、状态和治理，任何一层都不能由 Prompt 代替。
- **Content**: ① 主 Agent：把自然语言变成可执行任务画像；证据“33 条意图评测”。② 多 Agent：把候选计划变成可信 TaskGraph，并从 48 个工具收敛到 2.17 个候选；证据“恢复闭环率 12.4%→40.0%”。③ 记忆与 Skill：分治当前上下文、跨会话事实和程序性经验；证据“64/64 Memory，31/36 Skill”。④ 权限治理：在模型之外实施四道 PEP 与三态决策；证据“105/105 核心测试”。底部节奏条：PPT 16–18 min｜Demo 5 min｜Q&A 7–9 min。
- **Visualization**: 四节点闭环为定性顺序与依赖关系，不创建 Chart/Table 对象。

### Part 1: 主 Agent 决策与路由模块

#### Slide 03 - 第一章章节页

- **Audience move**: 从“意图识别就是分类”→接受本章问题是如何产生可执行、可路由、可审计的任务画像。
- **Layout**: 大号“01”与章节标题左对齐；蓝橙交叠环从右侧切入，环上仅保留“理解→过滤→路由”三节点；下方用一句冲突式副标题建立张力。
- **Title**: 主 Agent 决策与路由
- **Core message**: 模型“理解了”并不等于系统“可以执行”。
- **Content**: 章节号 01；章节标题；副标题“从分类一句话，升级为生成 TaskProfile 与可审计 RoutingDecision”。

#### Slide 04 - 同类产品对标：主 Agent

- **Audience move**: 从认为通用框架已经解决路由问题→看到企业数字员工还缺业务意图、数据范围、动作风险和权限候选四类约束。
- **Layout**: 上方 25% 为一句结论和差异标签；下方为四列比较矩阵，行是决策入口、候选约束、权限位置、审计输出；SuperAgent 列用蓝色主线和橙色差异标记突出，不做“准确率排名”。
- **Title**: 通用框架给出编排原语，SuperAgent 把业务契约与权限前置到路由前
- **Core message**: 本项目的差异不在新增一种 handoff，而在模型打分前先构造合法候选集合。
- **Content**: OpenAI Agents SDK：manager / agent-as-tool / handoff，业务权限由应用补充。AutoGen：SelectorGroupChat 等对话式团队，候选函数可缩小集合，但业务合同需自行实现。LangGraph：Router + StateGraph，执行关系和持久化明确，但图来源仍需业务校验。SuperAgent：TaskProfile + AgentCard + 权限硬过滤 + 可解释评分；输出 Top-K、排除原因、reason_codes 与 trace_id。页脚口径：“产品对比基于官方能力边界，不在不同评测集间比较准确率”。
- **Visualization**: `routing-product-comparison` 为纯文本对比矩阵；Native-ready: routing-product-comparison=no。
- **Motion suggestion**: static：四列矩阵同时呈现；emphasize：视线从三类通用原语移至 SuperAgent 的“权限前置”差异，最终回到页标题结论。

#### Slide 05 - TaskProfile 与权限感知路由

- **Audience move**: 从抽象理解“先过滤再评分”→能复述路由链路、硬边界和三种终态。
- **Layout**: 主视觉是一条左到右可信路由管线：用户请求→TaskProfile→authorized_agent_ids→AgentCard 硬过滤→RouteScore→ROUTE/CLARIFY/REJECT。TaskProfile 下方列出主意图、子意图、实体、动作、数据范围、风险、否定与依赖；评分公式作为右上角紧凑数据带；被过滤候选以浅灰分支退出。
- **Title**: 权限先硬过滤，再谈语义匹配
- **Core message**: 权限不能成为可被更高语义分抵消的权重；无合法候选时必须澄清或拒绝。
- **Content**: 硬过滤检查“授权 Agent、在线状态、支持动作、接受数据范围、风险上限”。RouteScore = 0.35 意图 + 0.25 能力 + 0.15 场景 + 0.10 数据范围 + 0.10 历史 + 0.05 可用性。终态：ROUTE 进入 Planner；CLARIFY 询问缺失对象；REJECT / NO_CAPABLE_AGENT 失败关闭。强调否定动作保留审计但不进入可执行子任务。
- **Visualization**: 定性管线与分支关系；不创建 Chart/Table 对象。
- **Native shape suggestion**: 可信路由管线可使用基本矩形、圆形状态点与直角 Connector；灰色退出分支表达硬过滤，不使用自由曲线。

#### Slide 06 - 意图评测与真实决策台

- **Audience move**: 从“整体准确率是多少”的单一追问→理解六项指标均已越过目标线，并能区分字段指标、严格整例与澄清触发的不同口径。
- **Layout**: 左 56% 为水平指标条与 0–100% 统一刻度；右 40% 使用 image_006.png 完整截图，外加一条原生标注指向“推荐 RemoteHRAssistantAgent · 100%”；底部横向显示“否定动作误执行 0%”和“33 条固定用例”的口径。
- **Title**: 六项指标均超过 90% 目标线，真实决策台完成链路验证
- **Core message**: 当前规则链路已能稳定支撑边界明确任务；严格整例达到 94.3%，澄清触发达到 95.6%，否定动作误执行保持 0%。
- **Content**: 主意图 93.94%；实体字段 96.46%；依赖完全准确率 92.31%；子意图 Precision/Recall 100%/98.48%；严格整例 94.3%；澄清触发 95.6%；否定动作误执行 0%。严格整例要求主/子意图、实体、动作、依赖、风险、missing_fields、澄清和置信区间全部同时正确；后续继续扩大测试语料并补充 rule/semantic/hybrid 同配置对照。
- **Images**: image_006.png，no-crop，完整保留界面文字；截图只是系统成果证据，不用来替代原生指标。
- **Visualization**: `intent-metrics` 为水平指标条；Native-ready: intent-metrics=no。

### Part 2: 多 Agent 协同编排与工具选择模块

#### Slide 07 - 第二章章节页

- **Audience move**: 从“多 Agent 就是多个机器人互聊”→进入可信计划、控制/数据分离、工具收敛和恢复的工程问题。
- **Layout**: 大号“02”与章节标题；蓝色主链从候选计划穿过 TaskGraph、Artifact、Tool Resolver 和 Checkpoint，橙色安全点标出暂停与人工对账。
- **Title**: 多 Agent 协同编排与工具选择
- **Core message**: 协同价值来自受控依赖和可恢复状态，不来自 Agent 数量。
- **Content**: 章节号 02；副标题“可信计划—协同执行—工具选择—安全恢复”。

#### Slide 08 - 同类产品对标：编排

- **Audience move**: 从“采用某个框架即可解决复杂任务”→理解开放式候选规划与确定性执行约束必须分层。
- **Layout**: 上部为五列简洁对标带：ReAct、AutoGen、LangGraph、A2A、SuperAgent；下部占主空间展示可信计划流水线，所有校验失败都回到 Planner/澄清，不直接进入 Scheduler。
- **Title**: 从自由聊天转向“候选计划 + 确定性校验 + TaskGraph”
- **Core message**: LLM 负责开放式分解，系统负责证明 Agent、Schema、数据依赖和 DAG 合法。
- **Content**: ReAct：动态适应，但路径难提前整体校验。AutoGen：对话协作灵活，但消息、状态、业务结果容易混合。LangGraph：显式图、Checkpoint 与 Interrupt，但业务合同由应用补充。A2A：Agent Card、Task、Message、Artifact 统一互操作，但不负责组织内权限和工具选择。SuperAgent：TaskProfile→Planner 候选计划→归一化→覆盖校验→Agent Contract/Schema→数据依赖/DAG→TaskGraph→Scheduler。页脚注明官方文档来源。
- **Visualization**: `orchestration-product-comparison` 为文本矩阵，Native-ready: orchestration-product-comparison=no；可信计划为定性流程。

#### Slide 09 - 控制面、数据面与工具选择

- **Audience move**: 从“Agent 之间传消息就够了”→理解 TaskGraph 只管何时执行，Artifact 只管传递什么，工具目录还需分层收敛。
- **Layout**: 左侧为上下双轨：控制面 TaskGraph→Scheduler→StepResult；数据面 Agent→Artifact→ArtifactRef→Resolver→Agent，两轨仅在 StepResult.outputs 交叉。右侧为工具漏斗 48→2.17→Top-K→Tool/ABSTAIN，旁边放四个小型 KPI。
- **Title**: 控制面决定何时执行，数据面决定传什么；48 个工具先收敛到 2.17 个候选
- **Core message**: 控制/数据解耦与“先过滤、后评分、可弃权”共同降低了协同漂移和工具幻觉风险。
- **Content**: ToolRegistry 由 30 个 Office MCP Tool + 18 个 Excel MCP Tool 构成。确定性过滤：Server/Scenario→Operation→Agent→Input Schema；之后按名称、别名、意图、描述评分。固定评估集结果：平均候选 48→2.17；Top-1/Top-3 100%/100%；Schema 有效率 100%；不存在工具幻觉 0%；平均约 1.51 ms。边界：当前主要以 audit 模式接入，尚未全面 enforce，也不能代表开放工具环境泛化。
- **Visualization**: `tool-funnel` 为候选数量漏斗与 KPI；Native-ready: tool-funnel=no。控制面与数据面为定性层级关系。

#### Slide 10 - 暂停、恢复与副作用安全

- **Audience move**: 从“重试即可恢复”→理解恢复必须同时处理计算状态、外部副作用和不确定状态。
- **Layout**: 左上为 RUNNING→PAUSE_REQUESTED→PAUSED→RUNNING；左下为提交顺序“Artifact Payload→Checkpoint→标记步骤完成”。右侧为 B0/B1/B2 两组柱形对比，底部用绿色零值条强调重复副作用与治理违规均为 0。
- **Title**: 恢复机制把闭环率从 12.4% 提升到 40.0%，且未引入重复副作用
- **Core message**: Checkpoint 负责计算状态，Idempotency Key + Receipt + NEEDS_RECONCILIATION 负责外部副作用安全。
- **Content**: B0 无自动恢复：任务闭环率 12.4%，首次失败后恢复 0%。B1 同 Agent 有界重试：27.2%，16.9%。B2 重试后等价且已授权 Agent 改派：40.0%，31.5%。每种策略 500 次、固定随机种子、Stub Router/Executor 和人工故障注入；实验用于机制比较，不代表生产成功率。自动重试/改派主要限于只读步骤；发送与写入状态不确定时进入人工对账。
- **Visualization**: `recovery-benchmark` 为分组柱形图；Native-ready: recovery-benchmark=no。
- **Motion suggestion**: static：状态机和柱形图同时可读；emphasize：从 B0 到 B2 的闭环提升，再落到“重复副作用 0 / 治理违规 0”的安全条件。

### Part 3: 记忆与 Skill 模块

#### Slide 11 - 第三章章节页

- **Audience move**: 从把所有状态都称为“记忆”→接受三种状态具有不同生命周期、来源和失效规则。
- **Layout**: 大号“03”；三条平行弧线分别标注“会话、长期事实、程序性经验”，最终汇入主 Agent，但不直接注入远端 Agent。
- **Title**: 记忆与 Skill
- **Core message**: 三种“记住”必须分治，才能同时控制 Token、隐私和复用风险。
- **Content**: 章节号 03；副标题“上下文压缩｜长期记忆｜步骤/智能体 Skill”。

#### Slide 12 - 同类产品对标与三通道方案

- **Audience move**: 从认为更长上下文或一张向量库可以统一解决状态问题→理解会话历史、跨会话事实和程序性经验应拆成三个治理通道。
- **Layout**: 上部为 OpenAI Sessions、LangGraph Memory、Mem0、Voyager、SuperAgent 的五列对标；下部用三条等宽通道展示写入时机、保留内容、召回对象和失效门禁，通道之间用禁止符号表示不可混写。
- **Title**: 会话、长期事实和程序性经验必须分治
- **Core message**: 本项目的先进性在系统组合与治理，不宣称单项记忆算法达到 SOTA。
- **Content**: OpenAI Sessions：会话历史持久化。LangGraph：Checkpointer + Store，区分线程状态与跨线程存储。Mem0：动态抽取、整合与检索。Voyager：可执行 Skill Library。SuperAgent：①上下文压缩保留最近两轮与未完成 Plan/TaskGraph/Artifact；②固定办公标签 Memory 记录来源、用户/项目范围、冲突与衰减，只注入主 Agent；③Step/Agent Skill 需 LLM 反思、至少两次独立成功证据及 Agent/Tool/Schema/范围/副作用门禁，命中后只增强对应 Plan 步骤，不跳过 Planner。
- **Visualization**: `memory-product-comparison` 为文本矩阵，Native-ready: memory-product-comparison=no；三通道为定性层级与隔离关系。

#### Slide 13 - 记忆与 Skill 评测

- **Audience move**: 从只看“全部通过”的表面数字→理解三类评测口径不同，并能识别 Skill 的真实失败根因。
- **Layout**: 左侧 40% 为 8K/16K/32K 压缩前后横向对比；中部 27% 为 Memory 64/64 与 P/R/F1=1.0 的证据环；右侧 29% 为 Skill 31/36 的堆叠条，并用橙色标出 5 个失败样本及根因。底部是 17 条端到端运行证据条。
- **Title**: 三通道均可复核；Skill 的 5 个失败样本定位到数据范围规范化缺陷
- **Core message**: 评测既证明已实现边界，也保留不符合预期的真实发现。
- **Content**: 上下文：8K 4426→3460（-21.8%，4 代）；16K 6939→6262（-9.8%，4 代）；32K 11905→6137（-48.5%，1 代）；三档均保留最近两轮与事实 4/4，压缩后 3 条工作流恢复 9/9 步骤、9/9 Artifact、9/9 事实，禁止副作用 0。Memory：64/64 确定性模块案例与预期一致；TP44/FP0/FN0，来源 40/40、重复 4/4、冲突 4/4、隔离 8/8；明确“不等同于在线 LLM 提取准确率”。Skill：31/36；五个低风险实体变化复用因计划侧 data scope 已规范化、Skill 卡仍保留点号原格式而失败；邮件高风险复用 1/1 正确阻断，其余门禁维度均 6/6。端到端：17 条本地完整链路验证模块连接。
- **Visualization**: `context-tokens` 为前后对比条，`memory-skill-results` 为证据环与堆叠条；Native-ready: context-tokens=no; memory-skill-results=no。

### Part 4: 权限治理模块

#### Slide 14 - 第四章章节页

- **Audience move**: 从把权限视为 Prompt 里的行为要求→进入模型之外、可阻断、可审计、失败关闭的执行治理。
- **Layout**: 大号“04”；右侧蓝色主路径穿过四个闸门，橙色审批环只与 REVIEW_REQUIRED 相交，DENY 分支直接终止。
- **Title**: 权限治理
- **Core message**: 权限必须由执行系统强制，而不是要求模型自觉遵守。
- **Content**: 章节号 04；副标题“S-ABAC｜四道 PEP｜三态审批｜Artifact Guard｜Receipt”。

#### Slide 15 - 同类权限模型对标与 S-ABAC

- **Audience move**: 从“RBAC 已足够”→理解任务目的、资源敏感度、动作风险与执行上下文需要共同参与授权。
- **Layout**: 上方为 ACL、RBAC、ABAC/ReBAC、OPA/Cedar、OpenAI Guardrails、SuperAgent 的横向能力矩阵；下方用四象限 Subject/Object/Scenario/Action 汇入 PolicyEngine，输出 ALLOW、DENY、REVIEW_REQUIRED 三态。
- **Title**: S-ABAC 把权限从 Prompt 约束升级为执行系统硬边界
- **Core message**: 角色只是 Subject 的一个属性；场景、对象和动作决定本次任务是否允许。
- **Content**: ACL 简单但难扩展。RBAC 适合组织岗位但不足以表达任务目的与数据范围。ABAC 细粒度，ReBAC 适合 Artifact 所有权。OPA/Cedar 提供外置 PDP/策略语言，是生产迁移候选。OpenAI Guardrails 提供输入、输出和工具级校验，但企业授权关系仍由应用实现。SuperAgent S-ABAC：Subject（用户/Agent/部门/信任/委托）、Object（Agent/Tool/Artifact/敏感度/范围）、Scenario（业务目的/任务风险/审批/环境）、Action（read/write/send/delete/approve）；三态中人工审批只能放行 REVIEW_REQUIRED，不能把 DENY 提升为 ALLOW。
- **Visualization**: `permission-benchmark` 为能力矩阵；Native-ready: permission-benchmark=no。S-ABAC 四象限为定性汇聚关系。

#### Slide 16 - 四道权限闸门与治理台

- **Audience move**: 从理解一个策略模型→看到策略在路由、派发、工具与结果四个具体执行点逐次生效，并有真实界面证据。
- **Layout**: 左侧 38% 为纵向四道闸门：路由前、派发前、工具前、结果前；每道闸门列出输入与拒绝原因。右侧 58% 放置 image_010.png 完整横向截图，上方用三枚 KPI 标记 6 用户画像、18 Agent 属性、42 资源属性，底部标记 3 条策略与一次性审批。
- **Title**: 四道 PEP 在路由、派发、工具、结果处逐次阻断
- **Core message**: 一次“通过”不会沿链路永久继承；每次 Agent、Tool 与 Artifact 访问都重新鉴权。
- **Content**: ①路由前：无权 Agent 不进入候选。②派发前：校验目标 Agent、动作、数据域和风险。③工具前：校验工具归属、DelegationGrant、参数、场景与审批状态。④结果前：Artifact Guard 校验 owner、producer、sensitivity、scope、checksum、derived_from。高风险操作使用绑定 task/resource/action/scenario/params 的一次性审批；task_id+step_id+标准化输入形成幂等键；Receipt 记录 STARTED/SUCCEEDED；未知资源、损坏 Receipt 或关键持久化失败时 Fail Closed。
- **Images**: image_010.png，no-crop，完整保留安全状态、最近拒绝、审批队列与 3/18/42 统计；外框和标题均为原生 SVG。
- **Visualization**: 四道 PEP 为定性顺序与重复校验关系。

### Part 5: 综合证据与人员分工

#### Slide 17 - 评测证据、演示路径与真实边界

- **Audience move**: 从寻找一个“总通过率”→接受结构测试、专项评测和端到端演示分别证明不同问题，并看到团队主动保留边界。
- **Layout**: 上部 60% 为三层证据阶梯：结构/合同测试、专项评测、端到端演示；右侧用一条 5 分钟 Demo 路径串起“工资查询→路由→权限预检→人工审批→恢复→Receipt”。下部为橙边界条，列出四个不可夸大事项。
- **Title**: 证据按“结构测试—专项评测—端到端演示”分层，不用单一通过率掩盖边界
- **Core message**: 原型已形成可复核的机制闭环，但仍需从固定评测与本地治理走向在线语义、强制工具选择和生产安全基础设施。
- **Content**: 结构/合同：主 Agent 113/113；权限核心 105/105；权限 API 12/12；专题合并回归 252/253，唯一失败为 UI 静态资源缓存版本断言。专项：33 条意图、48 工具、每策略 500 次恢复、64 Memory、36 Skill、21 条权限治理矩阵。端到端：17 条本地完整链路。Demo 5 分钟：输入“查询员工张三工资”→TaskProfile 与候选 Agent→S-ABAC 返回 REVIEW_REQUIRED→一次性审批→恢复执行→Receipt/Artifact/时间线。边界：Tool Resolver 仍以 audit 为主；Memory 64/64 是确定性状态机测试；Skill 31/36 保留规范化缺陷；当前不是生产 IAM/密钥管理/加密/公网部署安全认证。
- **Visualization**: `test-evidence` 为三层证据阶梯与通过数数据条；Native-ready: test-evidence=no。
- **Closing impact**: 绑定结论“机制完整、可演示、可复核、边界诚实”，以蓝色闭环图收束到橙色“下一步：enforce / online eval / production governance”短条；不增加空白致谢页。

#### Slide 18 - 人员分工

- **Audience move**: 从只看到系统成果→清楚每个模块的主责、接口协作和答辩问答责任人。
- **Layout**: 五列等宽分工表，每列顶部为人物名/待补姓名，中央是模块主责，底部是交付证据与答辩 Q&A 主题；刘建杰列用主色强调，评测集负责人列用正向绿色强调，其余三列保持可编辑占位；底部用一条共享职责带连接“系统集成、统一评测、演示联调、材料复核”。
- **Title**: 人员分工：五人主责清晰，接口、评测与演示共同收敛
- **Core message**: 团队由五人分别承担四个技术模块与评测集建设，并通过统一契约、评测口径与演示链路完成集成。
- **Content**: 刘建杰：主 Agent 决策与路由、S-ABAC 权限治理、系统集成；交付 TaskProfile/RoutingDecision、四道 PEP、治理台；Q&A 负责路由、权限与生产边界。成员姓名待补充 A：多 Agent 编排、TaskGraph/Scheduler、Tool Resolver、Pause/Resume；Q&A 负责工具选择和恢复实验。成员姓名待补充 B：上下文压缩、长期记忆、步骤/Agent Skill；Q&A 负责 64 Memory、36 Skill 和缺陷复盘。成员姓名待补充 C：前端原型、演示场景、自动化回归、PPT/报告材料整合；Q&A 负责现场演示和测试证据。成员姓名待补充 D：专项负责评测集建设，包括样本设计与分层、标注口径和期望结果、回归集维护与结果复核；交付意图、工具和权限评测集；Q&A 负责指标口径与误差分析。共享职责：统一 Agent/Tool/Artifact 契约、评测口径、集成联调与答辩演练。明确提示“请在答辩前替换四处姓名占位”。
- **Visualization**: `team-roles` 为五列纯文本分工表；Native-ready: team-roles=no。

## X. Speaker Notes Requirements

- **Generation**: enabled
- **Filename**: match each SVG filename under `notes/`
- **Content**: 逐页以最终 SVG 为依据写完整中文讲稿；结论先行，每页先说一句“这页要回答什么”，再解释机制、证据和边界。产品对比只使用官方文档并在相应页 notes 末尾加入 `[Sources]` 区块与完整 URL；内部指标引用导入报告或评测文件，不把确定性模块测试表述成在线模型准确率。章节页控制在 10–15 秒，正文页 45–75 秒，Slide 17 给出演示口令与失败兜底话术，Slide 18 提醒补齐四位成员姓名。
- **Total duration**: 16–18 minutes for slides；leave approximately 5 minutes for live demo and 7–9 minutes for Q&A within the 30-minute session
- **Notes style**: 正式但口语化；主动解释评测口径和限制，避免逐字念表；产品对标不做跨数据集准确率排名
- **Presentation purpose**: report and account + explain + persuade；证明技术选型合理、成果可复核，并建立对原型边界的可信预期
