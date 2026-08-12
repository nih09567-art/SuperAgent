这一页重点回答为什么没有直接复刻通用框架的路由方式。OpenAI Agents SDK 提供 manager、agent-as-tool 和 handoff 等多 Agent 原语；AutoGen 的 SelectorGroupChat 可以根据对话选择下一位参与者；LangGraph 用 Router 和 StateGraph 明确状态与执行关系。这些能力都很强，但企业数字员工还需要业务意图、数据范围、动作风险和授权候选四类约束。SuperAgent 的差异不是新增一种 handoff，而是在模型打分以前，先用 TaskProfile 和 AgentCard 构造合法候选集合，再输出 Top-K、排除原因、reason codes 和 trace id。这里不做跨评测集准确率排名，只比较官方能力边界与我们补上的业务契约。

[Sources]
https://openai.github.io/openai-agents-python/multi_agent/
https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/selector-group-chat.html
https://docs.langchain.com/oss/python/langgraph/overview