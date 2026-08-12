from __future__ import annotations

from typing import Any, Iterable, Mapping

from src.contracts import AgentCard, RoutingDecision, TaskProfile
from src.orchestrator.department_router import build_agent_cards, route_task
from src.orchestrator.task_profiler import profile_task


async def make_routing_decision(
    *,
    user_query: str,
    task_id: str,
    workflow_id: str,
    agents: Iterable[Any],
    authorized_agent_ids: set[str],
    metadata: dict | None = None,
    entity_overrides: dict[str, Any] | None = None,
    context_references: list[dict[str, Any]] | None = None,
    context_artifacts: list[dict[str, Any]] | None = None,
    conversation_context: dict[str, Any] | None = None,
    raw_request: str | None = None,
    task_profile_override: TaskProfile | Mapping[str, Any] | None = None,
) -> tuple[TaskProfile, list[AgentCard], RoutingDecision]:
    """主 Agent 的统一入口：任务画像 → 能力卡 → 权限约束候选 → 路由决策。"""
    if task_profile_override is not None:
        if isinstance(task_profile_override, TaskProfile):
            task_profile = task_profile_override.model_copy(deep=True)
        else:
            profile_data = dict(task_profile_override)
            data_scope = profile_data.get("data_scope")
            if isinstance(data_scope, str):
                profile_data["data_scope"] = [
                    item.strip() for item in data_scope.split(",") if item.strip()
                ] or ["general"]
            profile_data["action"] = str(
                profile_data.get("action")
                or profile_data.get("operation_mode")
                or "read"
            )
            profile_data["risk_level"] = str(
                profile_data.get("risk_level")
                or profile_data.get("risk_profile")
                or "LOW"
            )
            profile_data.setdefault("task_id", task_id)
            task_profile = TaskProfile.model_validate(profile_data)
    else:
        task_profile = await profile_task(
            user_query,
            task_id=task_id,
            metadata=metadata,
            entity_overrides=entity_overrides,
            context_references=context_references,
            context_artifacts=context_artifacts,
            conversation_context=conversation_context,
            raw_request=raw_request,
        )
    cards = build_agent_cards(agents)
    decision = route_task(
        task_profile,
        cards,
        authorized_agent_ids=authorized_agent_ids,
        workflow_id=workflow_id,
    )
    return task_profile, cards, decision

