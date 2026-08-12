这一页先看同类能力。OpenAI Sessions 重点是会话历史持久化；LangGraph 用 Checkpointer 与 Store 区分线程状态和跨线程存储；Mem0 强调动态抽取、整合与检索；Voyager 把成功经验沉淀成可执行 Skill Library。我们的先进性不在宣称某个单项记忆算法达到 SOTA，而在三通道组合与治理。上下文压缩在窗口超限前发生，保留最近两轮和未完成的 Plan、TaskGraph、Artifact；长期 Memory 用固定办公标签记录来源、用户或项目范围、冲突和衰减，只注入主 Agent；Step 或 Agent Skill 需要 LLM 反思、至少两次独立成功证据，并通过 Agent、Tool、Schema、范围和副作用门禁。命中 Skill 只增强对应 Plan 步骤，不能跳过 Planner。

[Sources]
https://openai.github.io/openai-agents-python/sessions/
https://docs.langchain.com/oss/python/langgraph/persistence
https://docs.langchain.com/oss/python/langgraph/add-memory
https://docs.mem0.ai/platform/overview
https://arxiv.org/abs/2305.16291