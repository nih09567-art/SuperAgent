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

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Iterable, List, Mapping, Optional, Protocol, runtime_checkable

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
    ) -> RoutingResult:
        return RoutingResult(
            selected_agent=step.preferred_resource_id,
            decision="DISPATCH",
            confidence=1.0,
            reason_codes=["stub:preferred_resource_id"],
        )


class MainAgentRoutingProvider:
    """Production routing: delegate to ``src.orchestrator.make_routing_decision``.

    ``make_routing_decision`` returns ``(TaskProfile, list[AgentCard],
    RoutingDecision)``; we surface the third element's ``selected_agent``.
    """

    @staticmethod
    def _confirmed_step_profile(
        step: TaskStep,
        approved_profile: Any,
    ) -> Optional[dict[str, Any]]:
        """Project the confirmed global profile onto one approved graph step.

        Production pre-flight routing must still enforce registration,
        authorization, capability, and policy gates, but it must not re-run
        intent recognition against the global request for every individual
        step.  Doing so can add intents or propagate an unrelated global
        clarification (for example an email recipient) to a report step.
        """

        if not isinstance(approved_profile, Mapping):
            return None
        approved_subtasks = approved_profile.get("subtasks")
        step_subtask_ids = {
            str(item) for item in (getattr(step, "subtask_ids", None) or [])
        }
        if not isinstance(approved_subtasks, list) or not step_subtask_ids:
            return None
        selected_subtasks = [
            deepcopy(item)
            for item in approved_subtasks
            if isinstance(item, Mapping)
            and str(item.get("id") or "") in step_subtask_ids
        ]
        if not selected_subtasks:
            return None

        profile = deepcopy(dict(approved_profile))
        intents = list(getattr(step, "intents", None) or [])
        if not intents:
            intents = [
                str(item.get("intent") or "")
                for item in selected_subtasks
                if str(item.get("intent") or "")
            ]
        primary_intent = intents[0] if intents else str(profile.get("intent") or "")
        first_subtask = selected_subtasks[0]
        operation_mode = str(
            getattr(step, "operation_mode", None)
            or first_subtask.get("action")
            or profile.get("action")
            or "read"
        )
        capabilities = list(getattr(step, "required_capabilities", None) or [])
        if not capabilities:
            capabilities = list(first_subtask.get("expected_capabilities") or [])
        scenario_tags: list[str] = []
        data_scope: list[str] = []
        required_data: list[str] = []
        for subtask in selected_subtasks:
            for target, values in (
                (scenario_tags, subtask.get("scenario_tags") or []),
                (data_scope, subtask.get("data_scope") or []),
                (required_data, subtask.get("required_business_data") or []),
            ):
                for value in values:
                    value = str(value)
                    if value and value not in target:
                        target.append(value)

        profile.update(
            {
                "intent": primary_intent,
                "intents": intents,
                "primary_goal_intent": primary_intent,
                "sub_intents": intents,
                "task_type": str(
                    getattr(step, "task_type", None)
                    or first_subtask.get("task_type")
                    or profile.get("task_type")
                    or "GENERAL"
                ),
                "business_goal": str(
                    getattr(step, "description", None)
                    or getattr(step, "title", None)
                    or profile.get("business_goal")
                    or ""
                ),
                "action": operation_mode,
                "operation_mode": operation_mode,
                "required_business_data": required_data,
                "expected_deliverables": list(
                    getattr(step, "expected_outputs", None) or []
                ),
                "side_effects": (
                    list(profile.get("side_effects") or [])
                    if bool(getattr(step, "external_side_effect", False))
                    else []
                ),
                "data_scope": data_scope or ["general"],
                "scenario_tags": scenario_tags or ["general"],
                "expected_capabilities": capabilities or ["General"],
                "risk_level": str(
                    getattr(step, "risk_level", None)
                    or profile.get("risk_level")
                    or "LOW"
                ),
                "irreversible": bool(getattr(step, "external_side_effect", False)),
                "missing_fields": [],
                "subtasks": selected_subtasks,
                "is_composite": False,
                "needs_clarification": False,
                "clarification_questions": [],
                "clarification_reasons": [],
                "ambiguities": [],
            }
        )
        recognition = profile.get("recognition")
        if isinstance(recognition, Mapping):
            recognition = deepcopy(dict(recognition))
            recognition.update(
                {
                    "primary_intent": primary_intent,
                    "needs_clarification": False,
                    "clarification_questions": [],
                    "ambiguities": [],
                }
            )
            recognition.pop("clarification_analysis", None)
            profile["recognition"] = recognition
        return profile

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
    ) -> RoutingResult:
        # Lazy import keeps this module importable without the orchestrator stack.
        from src.orchestrator import make_routing_decision

        meta = dict(metadata or {})
        if step.required_capabilities:
            meta.setdefault("required_capabilities", list(step.required_capabilities))
        if step.preferred_resource_id:
            meta.setdefault("preferred_resource_id", step.preferred_resource_id)

        task_profile_override = self._confirmed_step_profile(
            step,
            meta.pop("approved_task_profile", None),
        )

        profile, cards, decision = await make_routing_decision(
            user_query=user_query,
            task_id=task_id,
            workflow_id=workflow_id,
            agents=agents,
            authorized_agent_ids=authorized_agent_ids,
            metadata=meta,
            task_profile_override=task_profile_override,
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
