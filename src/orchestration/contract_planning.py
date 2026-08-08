"""Deterministic Registry-Contract selection around the existing LLM Planner.

The LLM still writes the human-readable execution plan. This module decides
which trusted Agent contracts can satisfy the TaskProfile goals and validates
that the proposed plan stays inside that candidate closure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from src.contracts.agent_contract import AgentContract


SYNTHETIC_PLANNING_INPUTS = frozenset(
    {
        "report.sources",
        "email.dispatch.request",
    }
)


@dataclass(frozen=True)
class ContractClosure:
    target_outputs: tuple[str, ...]
    selected_agent_ids: tuple[str, ...]
    missing_outputs: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return bool(self.target_outputs) and not self.missing_outputs


def _card_value(card: Any, name: str, default: Any = None) -> Any:
    if isinstance(card, dict):
        return card.get(name, default)
    return getattr(card, name, default)


def _planning_contract(card: Any) -> AgentContract | None:
    raw = _card_value(card, "planning_agent_contract") or _card_value(
        card, "agent_contract"
    )
    if isinstance(raw, AgentContract):
        return raw
    if isinstance(raw, dict):
        return AgentContract.model_validate(raw)
    return None


def contract_closure(
    task_profile: dict[str, Any],
    agent_cards: Iterable[Any],
    *,
    authorized_agent_ids: set[str] | None = None,
) -> ContractClosure:
    """Resolve goal outputs to Agent producers and recursively close requires."""

    targets = list(
        dict.fromkeys(
            str(value)
            for field in ("required_business_data", "expected_deliverables")
            for value in (task_profile.get(field) or [])
            if str(value)
        )
    )
    providers: dict[str, list[str]] = {}
    contracts: dict[str, AgentContract] = {}
    for card in agent_cards:
        agent_id = str(
            _card_value(card, "agent_id") or _card_value(card, "name") or ""
        ).strip()
        if not agent_id:
            continue
        if authorized_agent_ids is not None and agent_id not in authorized_agent_ids:
            continue
        if not bool(_card_value(card, "planning_eligible", False)):
            continue
        contract = _planning_contract(card)
        if contract is None:
            continue
        contracts[agent_id] = contract
        for output in contract.produces:
            providers.setdefault(output.name, []).append(agent_id)

    for candidates in providers.values():
        candidates.sort()

    selected: list[str] = []
    missing: list[str] = []
    visiting_outputs: set[str] = set()

    def require_output(logical_name: str) -> None:
        if logical_name in SYNTHETIC_PLANNING_INPUTS:
            return
        if logical_name in visiting_outputs:
            if logical_name not in missing:
                missing.append(logical_name)
            return
        candidates = providers.get(logical_name) or []
        if not candidates:
            if logical_name not in missing:
                missing.append(logical_name)
            return
        agent_id = candidates[0]
        if agent_id in selected:
            return
        visiting_outputs.add(logical_name)
        for requirement in contracts[agent_id].requires:
            if requirement.required:
                require_output(requirement.name)
        visiting_outputs.discard(logical_name)
        if agent_id not in selected:
            selected.append(agent_id)

    for target in targets:
        require_output(target)

    return ContractClosure(
        target_outputs=tuple(targets),
        selected_agent_ids=tuple(selected),
        missing_outputs=tuple(missing),
    )


def trusted_planning_catalog(
    agent_cards: Iterable[Any],
    selected_agent_ids: Iterable[str],
) -> list[dict[str, Any]]:
    """Serialize only Planner-relevant, Registry-owned Contract fields."""

    selected = set(selected_agent_ids)
    catalog: list[dict[str, Any]] = []
    for card in agent_cards:
        agent_id = str(
            _card_value(card, "agent_id") or _card_value(card, "name") or ""
        ).strip()
        if agent_id not in selected:
            continue
        contract = _planning_contract(card)
        if contract is None:
            continue
        actions = [str(value) for value in _card_value(card, "supported_actions", [])]
        planning_tools = list(_card_value(card, "planning_tool_scopes", []))
        query_only = bool(planning_tools) and all(
            str(tool).startswith(("query_", "get_", "search_"))
            for tool in planning_tools
        )
        operation_modes = ["read"] if query_only else actions
        catalog.append(
            {
                "agent_name": agent_id,
                "capabilities": list(_card_value(card, "capabilities", [])),
                "requires": [ref.model_dump(mode="json") for ref in contract.requires],
                "produces": [ref.model_dump(mode="json") for ref in contract.produces],
                "input_schema_refs": dict(contract.input_schema_refs),
                "output_schema_refs": dict(contract.output_schema_refs),
                "operation_modes": operation_modes,
                "external_side_effect": any(
                    action in {"send", "delete", "write"}
                    for action in operation_modes
                ),
                "planning_tool_scopes": planning_tools,
            }
        )
    return catalog


def validate_plan_candidate_closure(
    steps: list[Any],
    closure: ContractClosure,
) -> list[str]:
    """Reject missing or invented Agent choices relative to the trusted closure."""

    if not closure.complete:
        return []
    expected = set(closure.selected_agent_ids)
    actual = {
        str(step.get("agent_name") or "").strip()
        for step in steps
        if isinstance(step, dict) and str(step.get("agent_name") or "").strip()
    }
    errors: list[str] = []
    for agent_id in sorted(expected - actual):
        errors.append(f"Contract closure requires missing Agent {agent_id}")
    for agent_id in sorted(actual - expected):
        errors.append(f"Agent {agent_id} is outside the trusted Contract closure")
    return errors
