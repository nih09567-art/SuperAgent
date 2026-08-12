这一页回答为什么不只使用 RBAC。ACL 对单个对象直观，但规模扩大后难维护；RBAC 适合稳定岗位，却不足以表达任务目的和数据范围；ABAC 与 ReBAC 能提供属性和关系层面的细粒度控制；OPA 和 Cedar 提供外置 PDP 与策略语言，是后续生产迁移候选；OpenAI Guardrails 能做输入、输出和工具级校验，但企业授权关系仍由应用实现。SuperAgent 的 S-ABAC 同时考虑 Subject、Object、Scenario 和 Action：用户、Agent、部门、信任与委托是一组主体属性，Agent、Tool、Artifact、敏感度和范围是一组对象属性，再叠加业务目的、风险、审批、环境和具体动作。PolicyEngine 输出 ALLOW、DENY 或 REVIEW_REQUIRED；人工审批只能放行 REVIEW_REQUIRED，不能把 DENY 提升为允许。

[Sources]
https://csrc.nist.gov/pubs/sp/800/162/upd2/final
https://www.openpolicyagent.org/docs
https://docs.cedarpolicy.com/
https://openai.github.io/openai-agents-python/guardrails/