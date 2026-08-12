"""Routing & reliability providers (Plan §8, Phase 3).

Thin seams the scheduler depends on. Two implementations each:

- **Real** (production default): binds to the teammate code already on ``main``
  (``src.orchestrator.make_routing_decision``). Imported lazily so this module
  stays unit-testable in isolation.
- **Stub** (unit tests): deterministic, dependency-free.

The scheduler only reads ``RoutingResult.selected_agent``; both the stub result
and the real ``RoutingDecision`` expose that attribute (duck-typed).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, List, Mapping, Optional, Protocol, runtime_checkable

from src.contracts import TaskProfile
from src.interface.task_graph import TaskStep


@dataclass
class RoutingResult:
    """Minimal routing outcome consumed by the scheduler.

    ``decision`` mirrors the main agent's verdict (``DISPATCH`` / ``REJECT`` /
    ``CLARIFY`` / ``NO_CAPABLE_AGENT``). The scheduler MUST honor it: only
    ``DISPATCH`` with a concrete ``selected_agent`` may execute; a rejection or
    clarification must never silently fall back to ``preferred_resource_id``.
    """

    selected_agent: Optional[str]
    decision: str = "DISPATCH"
    clarification: Optional[str] = None
    confidence: float = 1.0
    reason_codes: List[str] = field(default_factory=list)
    raw: Any = None  # the underlying RoutingDecision when produced by the real provider


@runtime_checkable
class RoutingProvider(Protocol):
    """Selects the agent that should execute a step."""

    async def decide(
        self,
        step: TaskStep,
        *,
        user_query: str,
        task_id: str,
        workflow_id: str,
        agents: Iterable[Any],
        authorized_agent_ids: set[str],
        metadata: Optional[dict] = None,
        task_profile: Optional[dict] = None,
    ) -> RoutingResult: ...


@runtime_checkable
class ReliabilityProvider(Protocol):
    """Supplies a historical-reliability prior for an agent in a scenario."""

    def prior(
        self,
        *,
        agent_id: str,
        step: Optional[TaskStep] = None,
        scenario_tags: Optional[Iterable[str]] = None,
    ) -> float: ...


class StubRoutingProvider:
    """Deterministic routing for unit tests: honor ``preferred_resource_id``."""

    async def decide(
        self,
        step: TaskStep,
        *,
        user_query: str = "",
        task_id: str = "",
        workflow_id: str = "",
        agents: Iterable[Any] = (),
        authorized_agent_ids: Optional[set[str]] = None,
        metadata: Optional[dict] = None,
        task_profile: Optional[dict] = None,
    ) -> RoutingResult:
        return RoutingResult(
            selected_agent=step.preferred_resource_id,
            decision="DISPATCH",
            confidence=1.0,
            reason_codes=["stub:preferred_resource_id"],
        )


def _unique_strings(values: Iterable[Any]) -> list[str]:
    return list(
        dict.fromkeys(str(value).strip() for value in values if str(value).strip())
    )


def _step_task_profile(
    trusted_profile: Mapping[str, Any],
    step: TaskStep,
) -> TaskProfile:
    """Narrow the validated TaskProfile to this scheduler step.

    Natural-language recognition is deliberately not repeated here. The graph
    has already been validated against ``subtask_ids`` before execution, and
    this function retains confirmed entities such as the email recipient.
    """

    raw_subtasks = trusted_profile.get("subtasks") or []
    if not isinstance(raw_subtasks, list) or not raw_subtasks:
        raise ValueError("trusted TaskProfile has no subtasks")

    indexed = {
        str(item.get("id") or "").strip(): item
        for item in raw_subtasks
        if isinstance(item, Mapping) and str(item.get("id") or "").strip()
    }
    raw_ids = getattr(step, "subtask_ids", None) or []
    subtask_ids = _unique_strings(
        raw_ids if isinstance(raw_ids, (list, tuple, set)) else [raw_ids]
    )
    if not subtask_ids:
        raise ValueError(f"step {step.step_id!r} has no trusted subtask binding")
    unknown = [subtask_id for subtask_id in subtask_ids if subtask_id not in indexed]
    if unknown:
        raise ValueError(
            f"step {step.step_id!r} references unknown trusted subtasks {unknown}"
        )
    subtasks = [dict(indexed[subtask_id]) for subtask_id in subtask_ids]

    intents = _unique_strings(item.get("intent") for item in subtasks)
    capabilities = _unique_strings(
        [
            capability
            for item in subtasks
            for capability in (item.get("expected_capabilities") or [])
        ]
        + list(step.required_capabilities or [])
    )
    scenario_tags = _unique_strings(
        tag for item in subtasks for tag in (item.get("scenario_tags") or [])
    )
    data_scope = _unique_strings(
        scope for item in subtasks for scope in (item.get("data_scope") or [])
    )
    required_business_data = _unique_strings(
        value
        for item in subtasks
        for value in (item.get("required_business_data") or [])
    )
    expected_deliverables = _unique_strings(
        value
        for item in subtasks
        for value in (item.get("expected_deliverables") or [])
    )
    actions = _unique_strings(item.get("action") for item in subtasks)
    action = str(getattr(step, "operation_mode", "") or "").strip()
    if not action or action == "unknown":
        action = actions[0] if len(actions) == 1 else "read"
    goals = _unique_strings(item.get("goal") for item in subtasks)
    task_types = _unique_strings(item.get("task_type") for item in subtasks)

    return TaskProfile(
        task_id=str(trusted_profile.get("task_id") or step.step_id),
        intent=intents[0] if intents else "general_assistance",
        intents=intents,
        task_type=(
            task_types[0]
            if len(task_types) == 1
            else "COMPOSITE" if task_types else "GENERAL"
        ),
        business_goal="；".join(goals)
        or str(getattr(step, "description", "") or getattr(step, "title", "")),
        action=action,
        operation_mode=action,
        entities=dict(trusted_profile.get("entities") or {}),
        required_business_data=required_business_data,
        expected_deliverables=expected_deliverables,
        side_effects=(
            list(trusted_profile.get("side_effects") or [])
            if action in {"send", "write", "delete", "execute"}
            else []
        ),
        data_scope=data_scope or ["general"],
        scenario_tags=scenario_tags or ["general"],
        expected_capabilities=capabilities or ["General"],
        risk_level=str(getattr(step, "risk_level", "") or "LOW"),
        irreversible=bool(
            getattr(step, "external_side_effect", False)
            or trusted_profile.get("irreversible") and action != "read"
        ),
        constraints=list(trusted_profile.get("constraints") or []),
        missing_fields=list(trusted_profile.get("missing_fields") or []),
        confidence=float(trusted_profile.get("confidence") or 0.5),
        reason="trusted_step_profile",
        sub_intents=intents,
        subtasks=subtasks,
        is_composite=len(subtasks) > 1,
        primary_goal_intent=intents[0] if intents else "general_assistance",
        ambiguities=list(trusted_profile.get("ambiguities") or []),
        needs_clarification=bool(trusted_profile.get("needs_clarification")),
        clarification_questions=list(
            trusted_profile.get("clarification_questions") or []
        ),
        clarification_reasons=list(
            trusted_profile.get("clarification_reasons") or []
        ),
        recognition_mode="trusted_profile",
        raw_request=str(trusted_profile.get("raw_request") or ""),
        resolved_request=str(trusted_profile.get("resolved_request") or ""),
        context_references=list(trusted_profile.get("context_references") or []),
        context_artifacts=list(trusted_profile.get("context_artifacts") or []),
    )


class MainAgentRoutingProvider:
    """Production routing: delegate to ``src.orchestrator.make_routing_decision``.

    ``make_routing_decision`` returns ``(TaskProfile, list[AgentCard],
    RoutingDecision)``; we surface the third element's ``selected_agent``.
    """

    async def decide(
        self,
        step: TaskStep,
        *,
        user_query: str,
        task_id: str,
        workflow_id: str,
        agents: Iterable[Any],
        authorized_agent_ids: set[str],
        metadata: Optional[dict] = None,
        task_profile: Optional[dict] = None,
    ) -> RoutingResult:
        # Lazy import keeps this module importable without the orchestrator stack.
        from src.orchestrator import make_routing_decision

        meta = dict(metadata or {})
        if step.required_capabilities:
            meta.setdefault("required_capabilities", list(step.required_capabilities))
        if step.preferred_resource_id:
            meta.setdefault("preferred_resource_id", step.preferred_resource_id)

        # Profiles created before structured subtasks existed retain the
        # legacy recognition path. Current validated graphs always carry
        # subtasks and therefore use deterministic step-level routing.
        if task_profile and task_profile.get("subtasks"):
            from src.orchestrator.department_router import build_agent_cards, route_task

            try:
                profile = _step_task_profile(task_profile, step)
            except (TypeError, ValueError) as exc:
                return RoutingResult(
                    selected_agent=None,
                    decision="ROUTING_ERROR",
                    confidence=0.0,
                    reason_codes=[f"TRUSTED_STEP_PROFILE_INVALID: {exc}"],
                )
            cards = build_agent_cards(agents)
            decision = route_task(
                profile,
                cards,
                authorized_agent_ids=authorized_agent_ids,
                workflow_id=workflow_id,
            )
        else:
            profile, cards, decision = await make_routing_decision(
                user_query=user_query,
                task_id=task_id,
                workflow_id=workflow_id,
                agents=agents,
                authorized_agent_ids=authorized_agent_ids,
                metadata=meta,
            )
        # Honor the main agent's verdict. Only DISPATCH may execute; on
        # REJECT/CLARIFY/NO_CAPABLE_AGENT we return no agent so the scheduler
        # cannot fall back to ``preferred_resource_id`` and bypass the decision.
        decision_kind = str(getattr(decision, "decision", "DISPATCH") or "DISPATCH").upper()
        selected = getattr(decision, "selected_agent", None)
        clarification: Optional[str] = None
        reason_codes = list(getattr(decision, "reason_codes", []) or [])
        if decision_kind != "DISPATCH":
            selected = None
            questions = list(getattr(profile, "clarification_questions", []) or [])
            clarification = "; ".join(questions) if questions else None
        else:
            # Per-step assignment: the candidate scoring above runs against the
            # GLOBAL ``user_query``, which is identical for every step of a
            # multi-agent plan. Without this, each step would collapse onto the
            # same top-scoring agent (e.g. an HR-query step and a
            # knowledge-lookup step both routed to the knowledge agent). Honor
            # the plan's per-step ``preferred_resource_id`` when it is among the
            # candidate agents -- those already passed the main agent's
            # permission / online / capability gate, so this narrows within the
            # authorized set and never bypasses the decision.
            preferred = getattr(step, "preferred_resource_id", None)
            if preferred and preferred != selected:
                candidate_ids = {
                    getattr(c, "agent_id", None)
                    for c in getattr(decision, "candidate_agents", []) or []
                }
                registered_ids = {
                    getattr(card, "agent_id", None) for card in cards or []
                }
                excluded_ids = {
                    getattr(item, "agent_id", None)
                    for item in getattr(decision, "excluded_agents", []) or []
                }
                preferred_passed_gate = preferred in candidate_ids or (
                    preferred in registered_ids
                    and preferred in authorized_agent_ids
                    and preferred not in excluded_ids
                )
                if preferred_passed_gate:
                    selected = preferred
                    reason_codes = ["HONOR_PREFERRED_RESOURCE", *reason_codes]
        return RoutingResult(
            selected_agent=selected,
            decision=decision_kind,
            clarification=clarification,
            confidence=float(getattr(decision, "confidence", 0.0) or 0.0),
            reason_codes=reason_codes,
            raw=decision,
        )


class ScenarioPriorReliabilityProvider:
    """Heuristic reliability prior.

    NOTE: ``src/memory`` currently exposes session/long-term memory only, not an
    agent success-rate API, so this returns a scenario-informed prior rather than
    a learned score. FUTURE: aggregate historical success from
    ``store/task_logs`` / memory to replace the static prior.
    """

    def __init__(self, default_prior: float = 0.8, risk_penalty: float = 0.2) -> None:
        self._default = default_prior
        self._risk_penalty = risk_penalty

    def prior(
        self,
        *,
        agent_id: str,
        step: Optional[TaskStep] = None,
        scenario_tags: Optional[Iterable[str]] = None,
    ) -> float:
        score = self._default
        if step is not None and str(step.risk_level).upper() in {"HIGH", "CRITICAL"}:
            score -= self._risk_penalty
        return max(0.0, min(1.0, score))
