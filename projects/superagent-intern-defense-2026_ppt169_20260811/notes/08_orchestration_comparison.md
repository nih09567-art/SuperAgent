这一页先对标编排路线。ReAct 擅长边思考边行动，但完整路径难以前置校验；AutoGen 的对话协作灵活，消息、状态和业务结果容易混在同一对话流里；LangGraph 提供显式图、Checkpoint 和 Interrupt，业务合同仍由应用补充；A2A 统一 Agent Card、Task、Message 和 Artifact 的互操作对象，但不负责组织内部权限和工具选择。SuperAgent 采用分层方案：LLM 生成候选计划，系统依次完成归一化、需求覆盖、Agent Contract、输入输出 Schema、数据依赖和 DAG 校验，最后才生成 TaskGraph 并交给 Scheduler。任何校验失败都回到 Planner 或进入澄清，不直接放行。

[Sources]
https://arxiv.org/abs/2210.03629
https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/selector-group-chat.html
https://docs.langchain.com/oss/python/langgraph/overview
https://a2a-protocol.org/latest/specification/
https://docs.temporal.io/temporal