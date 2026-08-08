"""Convert planner output (``planning_steps``) into a :class:`TaskGraph` (Plan §8, R4).

The existing planner already emits an implicit DAG: each step may declare
``inputs: [{parameter_name, source_step, source_output}]`` where ``source_step``
is the ``agent_name`` of an upstream step (see
``coor_task._validate_plan_data_flow``). This module makes that graph explicit so
the scheduler can execute it.

``depends_on`` is derived from ``inputs[].source_step`` and every
``inputs[].source_artifacts[].source_step``; the raw symbolic input mappings are
preserved on each step as an extra ``input_bindings`` field for the scheduler to
resolve to concrete :class:`ArtifactRef` at runtime.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Optional

from src.contracts.agent_contract import AgentContract
from src.contracts.scenario_contract import ANNUAL_LEAVE_REPORT_V1
from src.interface.task_graph import (
    CompletionCondition,
    TaskGraph,
    TaskGraphValidationError,
    TaskSpec,
    TaskStep,
)
from src.orchestration.output_contracts import (
    get_agent_output_logical_names,
    get_agent_output_schema_ref,
)

# Effective operation modes (ignoring the ubiquitous "delegate") that imply a
# side effect. An agent whose config declares any of these is NOT read-only.
_SEND_MODES = {"send"}
_WRITE_MODES = {
    "write",
    "generate",
    "execute",
    "export",
    "create",
    "update",
    "delete",
    "submit",
    "approve",
}
_READ_MODES = {"read", "query", "lookup", "search"}

# The Planner may label values already present in the original request with
# this sentinel. It is context metadata, not an Artifact-producing TaskGraph
# node. Runtime authorization resolves such entities from the server-generated
# TaskProfile, so Planner-authored literal values must never become inputs.
_USER_INSTRUCTION_SOURCE = "user_instruction"

# Risk ranking of the four classified modes. Higher = more dangerous. Used to
# enforce "Planner output is untrusted": an explicit step declaration may only
# RAISE the risk level, never lower a send/write down to read. ``unknown`` is
# the most dangerous (the runtime fails closed on it).
_MODE_RANK = {"read": 0, "write": 1, "send": 2, "unknown": 3}


def _classify_modes(modes: Optional[List[str]]) -> str:
    """Classify a set of declared operation modes into read/send/write/unknown.

    ``None`` (unregistered agent) or an empty effective set -> ``unknown`` so a
    potential side effect is never silently treated as read-only.
    """
    if modes is None:
        return "unknown"
    effective = {str(m).lower() for m in modes} - {"delegate"}
    if not effective:
        return "unknown"
    if effective & _SEND_MODES:
        return "send"
    if effective & _WRITE_MODES:
        return "write"
    if effective <= _READ_MODES:
        return "read"
    return "unknown"


def _classify_single(mode: str) -> str:
    """Classify a single explicit mode string into read/send/write/unknown."""
    low = str(mode).lower()
    if low in _SEND_MODES:
        return "send"
    if low in _WRITE_MODES:
        return "write"
    if low in _READ_MODES:
        return "read"
    return "unknown"


def _config_security_attributes(agent_name: str) -> Optional[dict[str, Any]]:
    """Return one Agent's trusted S-ABAC resource attributes."""

    if not agent_name:
        return None
    try:
        from config.s_abac_config import RESOURCE_SECURITY_ATTRIBUTES
    except Exception:  # pragma: no cover - config always present in-repo
        return None
    attrs = RESOURCE_SECURITY_ATTRIBUTES.get(agent_name)
    if not isinstance(attrs, dict):
        return None
    return dict(attrs)


def _config_operation_modes(agent_name: str) -> Optional[List[str]]:
    """Return an agent's declared ``allowed_operation_modes`` from S-ABAC config.

    Lazy import keeps this module importable without the security/config stack.
    Returns ``None`` when the agent is not registered.
    """

    attrs = _config_security_attributes(agent_name)
    if attrs is None:
        return None
    return list(attrs.get("allowed_operation_modes", []) or [])


def _constraint_tokens(value: Any) -> set[str]:
    if value is None:
        return set()
    values = value if isinstance(value, (list, tuple, set)) else [value]
    return {
        str(item).strip().lower()
        for item in values
        if str(item).strip()
    }


def _validate_planner_security_constraints(
    agent_name: str,
    raw_step: dict[str, Any],
) -> None:
    """Reject Planner claims that contradict trusted Agent classification.

    Planner security fields remain useful as audit/deny-only constraints, but
    they must never widen the target's trusted capability or scenario domain.
    Runtime authorization is built from the global task profile plus these
    trusted resource attributes, not from the raw fields validated here.
    """

    attrs = _config_security_attributes(agent_name)
    if attrs is None:
        return

    for planner_field, trusted_field in (
        ("required_capabilities", "expected_capabilities"),
        ("scenario_tags", "scenario_tags"),
    ):
        claimed = _constraint_tokens(raw_step.get(planner_field))
        trusted = _constraint_tokens(attrs.get(trusted_field))
        if claimed and not claimed.issubset(trusted):
            raise TaskGraphValidationError(
                f"step for {agent_name!r} has Planner {planner_field} "
                "outside trusted Agent security attributes"
            )

    claimed_task_type = str(raw_step.get("task_type") or "").strip().lower()
    trusted_task_type = str(attrs.get("capability_domain") or "").strip().lower()
    if (
        claimed_task_type
        and trusted_task_type
        and claimed_task_type != trusted_task_type
    ):
        raise TaskGraphValidationError(
            f"step for {agent_name!r} has Planner task_type outside trusted "
            "Agent security attributes"
        )


def _derive_operation_mode(
    agent_name: str,
    explicit_mode: Optional[str],
    write_agents: set[str],
    trusted_task_mode: Optional[str] = None,
) -> tuple[str, str, str]:
    """Derive a step's ``(operation_mode, source, reason)`` (read/write/send/unknown).

    Planner output is treated as UNTRUSTED input: the trusted baseline is the
    agent's declared ``allowed_operation_modes`` (S-ABAC config), optionally
    raised by the caller-supplied ``write_agents`` hint. An explicit per-step
    ``operation_mode`` from the plan can only RAISE the risk level (e.g.
    read -> write/send) -- it can NEVER lower a declared ``send``/``write`` down
    to ``read``. When the mode cannot be established it is ``"unknown"`` (never
    silently ``read``) so the runtime refuses to schedule the side effect.

    The returned ``source``/``reason`` provide a trusted audit trail of where
    the classification came from.
    """
    config_modes = _config_operation_modes(agent_name)
    registered = config_modes is not None

    if registered:
        base_mode = _classify_modes(config_modes)
        effective_config_modes = {
            str(mode).lower()
            for mode in config_modes
            if str(mode).lower() != "delegate"
        }
        # Preserve a single concrete mutation verb (generate/export/create/...)
        # end-to-end.  Collapsing it to generic ``write`` makes the scheduler's
        # trusted action disagree with the resource policy, e.g. a document
        # generator that allows ``generate`` is incorrectly denied as
        # ``write``.  Mixed-mode agents still use the conservative class below
        # until a trusted task-profile action selects the invocation mode.
        if len(effective_config_modes) == 1:
            concrete_mode = next(iter(effective_config_modes))
            if concrete_mode not in _READ_MODES:
                base_mode = concrete_mode
        base_source = "agent_config"
        base_reason = f"agent_config modes={sorted({str(m).lower() for m in config_modes})}"
        # Some Agents deliberately expose both query and mutation tools.  The
        # server-derived TaskProfile subtask action selects the concrete mode
        # for this invocation; unlike Planner text, it cannot invent a mode the
        # Agent config does not allow.  This keeps calendar/leave queries read-
        # only while create/update operations remain writes.
        configured_mode_classes = {
            _classify_single(mode)
            for mode in config_modes
            if str(mode).lower() != "delegate"
        }
        trusted_task_class = (
            _classify_single(trusted_task_mode)
            if trusted_task_mode
            else None
        )
        if trusted_task_mode in effective_config_modes:
            # The server-owned task profile chose an exact verb supported by
            # the resource.  Preserve it (notably ``generate``) for policy
            # enforcement and audit output.
            base_mode = str(trusted_task_mode)
            base_source = "task_profile_action"
            base_reason = (
                f"server task-profile action selected exact mode "
                f"{trusted_task_mode} from configured modes="
                f"{sorted(effective_config_modes)}"
            )
        elif trusted_task_class in configured_mode_classes:
            base_mode = str(trusted_task_class)
            base_source = "task_profile_action"
            base_reason = (
                f"server task-profile action selected {trusted_task_class} "
                f"from configured modes={sorted(configured_mode_classes)}"
            )
        # A caller "write" hint may raise a read baseline to write.
        base_rank = _MODE_RANK[_classify_single(base_mode)]
        if agent_name in write_agents and _MODE_RANK["write"] > base_rank:
            base_mode = "write"
            base_source = "caller_write_agents"
            base_reason = "caller-declared write raised read baseline"
    elif agent_name in write_agents:
        # Unregistered but the caller explicitly asserts a write side effect.
        base_mode = "write"
        base_source = "caller_write_agents"
        base_reason = "caller-declared write for unregistered agent"
    else:
        base_mode = "unknown"
        base_source = "unregistered"
        base_reason = "agent not in S-ABAC config; cannot classify side effect"

    if explicit_mode:
        exp_mode = _classify_single(explicit_mode)
        base_rank = _MODE_RANK[_classify_single(base_mode)]
        # Planner may only escalate risk, never de-escalate a side effect.
        if _MODE_RANK[exp_mode] > base_rank:
            return (
                exp_mode,
                "planner_upgrade",
                f"planner raised {base_mode}->{exp_mode} (declared={str(explicit_mode).lower()})",
            )
        return (
            base_mode,
            base_source,
            f"{base_reason}; planner declared={str(explicit_mode).lower()} (not lowered)",
        )

    return base_mode, base_source, base_reason


def _step_id_for(index: int, raw: Dict[str, Any]) -> str:
    explicit = raw.get("step_id") or raw.get("subtask_id")
    return str(explicit) if explicit else f"step_{index + 1}"


_ANNUAL_LEAVE_AGENT_STEP_IDS = {
    "RemoteHRAssistantAgent": "hr_query",
    "RemoteKnowledgeAgent": "policy_query",
    "RemoteReportAgent": "generate_report",
}
_ANNUAL_LEAVE_REPORT_OUTPUTS = {"employee.info", "policy.info"}


def trusted_scenario_contract_for_plan(
    planning_steps: List[Dict[str, Any]] | None,
    *,
    user_query: str = "",
) -> str | None:
    """Return a platform-owned contract id for the fixed annual-leave demo."""

    if not isinstance(planning_steps, list):
        return None
    query = str(user_query or "").lower()
    if "王强" not in query or not any(
        marker in query for marker in ("年假", "年休假", "带薪休假")
    ):
        return None
    if len(planning_steps) != len(_ANNUAL_LEAVE_AGENT_STEP_IDS):
        return None
    agent_names = [
        str(step.get("agent_name") or "")
        for step in planning_steps
        if isinstance(step, dict)
    ]
    if (
        set(agent_names) != set(_ANNUAL_LEAVE_AGENT_STEP_IDS)
        or len(set(agent_names)) != 3
    ):
        return None
    return ANNUAL_LEAVE_REPORT_V1


def canonicalize_annual_leave_plan(
    planning_steps: List[Dict[str, Any]] | None,
    *,
    user_query: str = "",
) -> List[Dict[str, Any]] | None:
    """Give the fixed annual-leave demo stable step identities.

    The real Planner remains responsible for selecting the three Agents and
    declaring their data flow.  Some models emit positional IDs such as
    ``step_1`` and use Agent names in ``source_step``; that representation is
    semantically equivalent but makes the defense evidence and downstream
    contracts unstable.  For this explicitly scoped demo, rename only when
    the Planner already returned exactly one step for each of the three
    trusted Agents.  Dependencies and fan-in sources are remapped, never
    invented; malformed or incomplete plans remain unchanged and fail closed
    in normal validation.
    """

    if trusted_scenario_contract_for_plan(
        planning_steps,
        user_query=user_query,
    ) != ANNUAL_LEAVE_REPORT_V1:
        return planning_steps

    agent_names = [str(step.get("agent_name") or "") for step in planning_steps]

    aliases = dict(_ANNUAL_LEAVE_AGENT_STEP_IDS)
    for step, agent_name in zip(planning_steps, agent_names):
        old_step_id = str(step.get("step_id") or "").strip()
        if old_step_id:
            aliases[old_step_id] = _ANNUAL_LEAVE_AGENT_STEP_IDS[agent_name]

    normalized = deepcopy(planning_steps)
    for step in normalized:
        agent_name = str(step.get("agent_name") or "")
        step["step_id"] = _ANNUAL_LEAVE_AGENT_STEP_IDS[agent_name]
        if "depends_on" in step:
            dependencies = step.get("depends_on")
            if isinstance(dependencies, list):
                step["depends_on"] = [
                    aliases.get(str(item), item) for item in dependencies
                ]
        inputs = step.get("inputs")
        if not isinstance(inputs, list):
            continue
        for binding in inputs:
            if not isinstance(binding, dict):
                continue
            if "source_step" in binding:
                binding["source_step"] = aliases.get(
                    str(binding.get("source_step")), binding.get("source_step")
                )
            sources = binding.get("source_artifacts")
            if not isinstance(sources, list):
                continue
            for source in sources:
                if isinstance(source, dict) and "source_step" in source:
                    source["source_step"] = aliases.get(
                        str(source.get("source_step")), source.get("source_step")
                    )
    return normalized


def _reference_list(value: Any) -> List[str]:
    """Normalize a single-value/array reference field to a deduplicated list.

    Mirrors ``coor_task._string_list``: upstream validation accepts
    ``"depends_on": "subtask_1"`` as a legal single-value form, so iterating
    the raw field here would silently split the string into characters and
    drop every dependency edge.
    """
    if value is None:
        return []
    raw_items = value if isinstance(value, (list, tuple, set)) else [value]
    return list(dict.fromkeys(
        str(item).strip() for item in raw_items if str(item).strip()
    ))


def _subtask_ids_for(raw: Dict[str, Any]) -> List[str]:
    values = raw.get("subtask_ids")
    if not values:
        values = [raw.get("subtask_id")] if raw.get("subtask_id") else []
    elif not isinstance(values, list):
        values = [values]
    return [str(value) for value in values if value]


def _list_field(raw: Dict[str, Any], plural: str, singular: str) -> List[Any]:
    values = raw.get(plural)
    if not values:
        value = raw.get(singular)
        return [value] if value else []
    return values if isinstance(values, list) else [values]


def _as_list(value: Any) -> List[Any]:
    if not value:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, (tuple, set)):
        return list(value)
    return [value]


def derive_step_dependencies(
    planning_steps: List[Dict[str, Any]],
    subtasks: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Recover dropped data-flow edges from the task profile's ``subtasks``.

    The Planner expresses ordering through ``inputs[].source_step``. But the
    planner prompt tells it to leave ``inputs`` EMPTY for *autonomous* remote
    agents (those without a ``Requires`` field). A report/summary step that
    consumes upstream query outputs is exactly such an autonomous agent, so its
    dependency is lost and the scheduler runs it in parallel with -- instead of
    after -- the steps it depends on.

    The task profile's ``subtasks`` already carry the correct ``depends_on`` DAG
    (computed by the intent profiler). When the Planner declared NO explicit
    dependency on ANY step, and the plan aligns 1:1 with the subtasks (same
    count, emitted in the same topological order), copy each subtask's backward
    ``depends_on`` edges onto the matching plan step as ``depends_on`` expressed
    as upstream ``agent_name`` values -- which :func:`plan_to_task_graph`
    already resolves to concrete ``step_id`` references.

    This is a pure, best-effort *correction*: it never overrides an explicit
    Planner edge and returns ``planning_steps`` unchanged whenever it cannot map
    confidently, so conversion is byte-identical for every plan that already
    carries its own dependencies.
    """
    if not planning_steps or not subtasks:
        return planning_steps
    steps = [s for s in planning_steps if isinstance(s, dict)]
    if len(steps) != len(planning_steps):
        return planning_steps  # non-dict entries -> refuse to guess
    # Trust the Planner: if it declared ANY dependency edge, keep its DAG as-is.
    for step in steps:
        if step.get("depends_on") or step.get("inputs"):
            return planning_steps
    subs = [t for t in subtasks if isinstance(t, dict)]
    if len(subs) != len(steps):
        return planning_steps  # cannot align plan steps to subtasks confidently

    index_by_subtask_id: Dict[str, int] = {}
    for i, task in enumerate(subs):
        sid = task.get("id")
        if sid:
            index_by_subtask_id[str(sid)] = i

    augmented: List[Dict[str, Any]] = [dict(s) for s in steps]
    changed = False
    for i, task in enumerate(subs):
        dep_agents: List[str] = []
        for dep_id in _reference_list(task.get("depends_on")):
            j = index_by_subtask_id.get(str(dep_id))
            # Only backward edges (upstream step precedes this one). Skipping
            # forward/unknown references keeps the derived graph a valid DAG.
            if j is None or j >= i:
                continue
            dep_agent = augmented[j].get("agent_name")
            if dep_agent and dep_agent not in dep_agents:
                dep_agents.append(dep_agent)
        if dep_agents:
            augmented[i]["depends_on"] = dep_agents
            changed = True
    return augmented if changed else planning_steps


def plan_to_task_graph(
    planning_steps: List[Dict[str, Any]],
    *,
    task_id: str,
    subject: Optional[str] = None,
    goal: str = "",
    agent_produces: Optional[Dict[str, List[str]]] = None,
    agent_contracts: Optional[Dict[str, AgentContract | Dict[str, Any]]] = None,
    write_agents: Optional[set[str]] = None,
    subtasks: Optional[List[Dict[str, Any]]] = None,
    trusted_scenario_contract_id: Optional[str] = None,
) -> TaskGraph:
    """Build (and validate) a :class:`TaskGraph` from ``planning_steps``.

    Args:
        planning_steps: planner output (list of step dicts).
        task_id: id for the resulting :class:`TaskSpec`.
        subject: acting user id (for downstream authorization).
        goal: optional human-readable task goal.
        agent_produces: ``{agent_name: [logical_output, ...]}`` used to fill
            ``expected_outputs`` when a step does not declare it.
        agent_contracts: versioned Agent contracts copied onto TaskSteps so the
            Scheduler can normalize and validate actual results.
        write_agents: agent names whose steps should default to
            ``operation_mode="write"`` when the step does not declare a mode.
        subtasks: optional task-profile ``subtasks`` used as a fail-safe to
            recover dependency edges the Planner drops for autonomous agents
            (see :func:`derive_step_dependencies`). Ignored when the plan
            already declares its own edges.
        trusted_scenario_contract_id: platform-derived scenario contract. Raw
            Planner fields with the same name are intentionally ignored.

    Returns:
        A validated :class:`TaskGraph` (raises ``TaskGraphValidationError`` if the
        derived graph is structurally invalid).
    """
    agent_produces = agent_produces or {}
    agent_contracts = agent_contracts or {}
    write_agents = write_agents or set()
    if subtasks:
        planning_steps = derive_step_dependencies(planning_steps, subtasks)

    subtask_by_id = {
        str(item.get("id")): item
        for item in (subtasks or [])
        if isinstance(item, dict) and item.get("id")
    }

    raw_steps = [
        (idx, raw)
        for idx, raw in enumerate(planning_steps or [])
        if isinstance(raw, dict)
    ]

    # Build structural aliases before resolving edges so a valid TaskGraph can
    # reference any step_id/subtask_id, including a step that appears later in
    # the Planner's list. Agent-name aliases remain a legacy, backward-only
    # fallback because the same agent may legitimately execute multiple steps.
    reference_to_step: Dict[str, str] = {}
    step_position: Dict[str, int] = {}
    raw_step_by_id: Dict[str, Dict[str, Any]] = {}
    for idx, raw in raw_steps:
        step_id = _step_id_for(idx, raw)
        step_position[step_id] = idx
        raw_step_by_id[step_id] = raw
        aliases = [step_id, *_subtask_ids_for(raw)]
        for alias in aliases:
            existing = reference_to_step.get(alias)
            if existing and existing != step_id:
                raise TaskGraphValidationError(
                    f"reference '{alias}' maps to multiple steps: "
                    f"'{existing}' and '{step_id}'"
                )
            reference_to_step[alias] = step_id

    steps: List[TaskStep] = []
    prior_agent_to_step: Dict[str, str] = {}

    def resolve_reference(reference: Any) -> str:
        key = str(reference)
        return reference_to_step.get(key) or prior_agent_to_step.get(key) or key

    def trusted_contract(agent_name: str) -> AgentContract | None:
        raw_contract = agent_contracts.get(agent_name)
        if isinstance(raw_contract, AgentContract):
            return raw_contract
        if raw_contract:
            return AgentContract.model_validate(raw_contract)
        return None

    def producer_outputs(step_id: str) -> tuple[set[str], AgentContract | None]:
        """Return the trusted output vocabulary for one upstream step.

        A Planner may name an output, but it cannot invent one when the
        registry supplied a Contract.  Legacy, uncontracted graphs retain the
        historical single-output resolution path for compatibility.
        """

        producer = raw_step_by_id.get(step_id)
        if not producer:
            return set(), None
        producer_agent = str(
            producer.get("agent_name") or producer.get("agent") or ""
        )
        contract = trusted_contract(producer_agent)
        if contract is not None:
            return {ref.name for ref in contract.produces}, contract
        declared = producer.get("expected_outputs") or producer.get("produces")
        if isinstance(declared, str):
            declared = [declared]
        if declared:
            return set(declared), None
        if producer_agent in agent_produces:
            return set(agent_produces.get(producer_agent, []) or []), None
        # Built-in logical-name defaults are useful for filling a TaskStep's
        # expected_outputs, but are not strong enough to reject a legacy
        # binding that uses an older alias (for example ``person_info``).
        return set(), None

    for idx, raw in raw_steps:
        agent_name = raw.get("agent_name") or raw.get("agent") or ""
        step_id = _step_id_for(idx, raw)

        raw_contract = trusted_contract(str(agent_name))
        raw_inputs = raw.get("inputs")
        if raw_inputs is not None and not isinstance(raw_inputs, list):
            raise TaskGraphValidationError(
                f"step {step_id!r} inputs must be a list"
            )
        inputs = list(raw_inputs or [])
        depends_on: List[str] = []
        for dependency_ref in _reference_list(raw.get("depends_on")):
            resolved = resolve_reference(dependency_ref)
            if resolved not in depends_on:
                depends_on.append(resolved)
        seen_input_parameters: set[str] = set()
        for binding in inputs:
            if not isinstance(binding, dict):
                raise TaskGraphValidationError(
                    f"step {step_id!r} input bindings must be objects"
                )
            parameter_name = str(binding.get("parameter_name") or "").strip()
            if not parameter_name:
                raise TaskGraphValidationError(
                    f"step {step_id!r} input binding is missing parameter_name"
                )
            if parameter_name in seen_input_parameters:
                raise TaskGraphValidationError(
                    f"step {step_id!r} has duplicate input binding for "
                    f"{parameter_name!r}"
                )
            seen_input_parameters.add(parameter_name)
            sources = binding.get("source_artifacts")
            if isinstance(sources, list):
                if binding.get("source_step") or binding.get("source_output"):
                    raise TaskGraphValidationError(
                        "input binding cannot mix source_artifacts with "
                        "source_step/source_output"
                    )
                if not sources:
                    raise TaskGraphValidationError(
                        f"step {step_id!r} fan-in binding for "
                        f"{parameter_name!r} must not be empty"
                    )
                source_bindings = sources
            elif sources is not None:
                raise TaskGraphValidationError(
                    "source_artifacts must be a list"
                )
            else:
                source_bindings = [binding]
            for source_binding in source_bindings:
                if not isinstance(source_binding, dict):
                    raise TaskGraphValidationError(
                        "source_artifacts entries must be objects"
                    )
                source_step = source_binding.get("source_step")
                source_output = source_binding.get("source_output")
                if not source_step:
                    raise TaskGraphValidationError(
                        f"step {step_id!r} input binding for "
                        f"{parameter_name!r} must declare source_step and "
                        "source_output"
                    )
                if str(source_step).strip() == _USER_INSTRUCTION_SOURCE:
                    if isinstance(sources, list):
                        raise TaskGraphValidationError(
                            f"step {step_id!r} input binding for "
                            f"{parameter_name!r} cannot use user_instruction "
                            "as an Artifact source"
                        )
                    if not source_output:
                        raise TaskGraphValidationError(
                            f"step {step_id!r} context binding for "
                            f"{parameter_name!r} must declare source_output"
                        )
                    # This source contributes no DAG edge. The binding itself
                    # is removed below so the Scheduler cannot trust a literal
                    # value or description emitted by the Planner.
                    continue
                # Legacy uncontracted bindings may omit source_output when the
                # producer has exactly one output; the Scheduler can resolve
                # that unambiguously. Contracted/fan-in bindings must always
                # name the business output so a Planner cannot rely on a
                # positional or first-output guess.
                if not source_output and (
                    isinstance(sources, list) or raw_contract is not None
                ):
                    raise TaskGraphValidationError(
                        f"step {step_id!r} input binding for "
                        f"{parameter_name!r} must declare source_output"
                    )
                resolved = resolve_reference(source_step)
                if resolved not in step_position:
                    raise TaskGraphValidationError(
                        f"step {step_id!r} depends on unknown step "
                        f"{source_step!r}"
                    )
                available_outputs, producer_contract = producer_outputs(resolved)
                if (
                    source_output
                    and available_outputs
                    and str(source_output) not in available_outputs
                ):
                    required_outputs = (
                        [ref.name for ref in producer_contract.produces if ref.required]
                        if producer_contract is not None
                        else []
                    )
                    if len(required_outputs) == 1:
                        # Planner labels are descriptive text. When a trusted
                        # producer has exactly one mandatory business output,
                        # that Contract makes the intended binding unambiguous
                        # even if optional outputs are also available.
                        source_output = required_outputs[0]
                        source_binding["source_output"] = source_output
                    else:
                        raise TaskGraphValidationError(
                            f"step {step_id!r} input binding references output "
                            f"{source_output!r}, but source step {resolved!r} "
                            f"produces {sorted(available_outputs)!r}"
                        )
                declared_source_schema = source_binding.get("schema_ref")
                if declared_source_schema and producer_contract is not None:
                    produced_ref = next(
                        (
                            ref
                            for ref in producer_contract.produces
                            if ref.name == source_output
                        ),
                        None,
                    )
                    if produced_ref and declared_source_schema != produced_ref.schema_ref:
                        raise TaskGraphValidationError(
                            f"step {step_id!r} input binding schema for "
                            f"{source_output!r} does not match trusted source "
                            f"schema {produced_ref.schema_ref!r}"
                        )
                if resolved not in depends_on:
                    depends_on.append(resolved)

        # The Planner may use a prior Agent name as a legacy source alias.
        # Execution and Artifact storage are keyed by TaskGraph step_id, so
        # normalize both single-source and fan-in bindings at this trusted
        # conversion boundary. Leaving the raw Agent alias here makes a valid
        # upstream Artifact look missing at runtime.
        normalized_inputs: List[Dict[str, Any]] = []
        for binding in inputs:
            if not isinstance(binding, dict):
                continue
            if (
                str(binding.get("source_step") or "").strip()
                == _USER_INSTRUCTION_SOURCE
            ):
                continue
            normalized = dict(binding)
            source_artifacts = normalized.get("source_artifacts")
            if isinstance(source_artifacts, list):
                normalized["source_artifacts"] = [
                    {
                        **source,
                        "source_step": resolve_reference(source.get("source_step")),
                    }
                    if isinstance(source, dict) and source.get("source_step")
                    else source
                    for source in source_artifacts
                ]
            elif normalized.get("source_step"):
                normalized["source_step"] = resolve_reference(
                    normalized.get("source_step")
                )
            normalized_inputs.append(normalized)
        inputs = normalized_inputs

        # The registry-provided contract is trusted platform metadata. Planner
        # output is untrusted and must never inject a contract: a step-level
        # ``agent_contract`` in the plan is ignored outright, so a fabricated
        # or weakened contract can never reach the Scheduler.
        contract = raw_contract
        declared_outputs = raw.get("expected_outputs") or raw.get("produces")
        if isinstance(declared_outputs, str):
            declared_outputs = [declared_outputs]
        if contract:
            contract_outputs = [ref.name for ref in contract.produces]
            undeclared = set(declared_outputs or []) - set(contract_outputs)
            if undeclared:
                raise TaskGraphValidationError(
                    f"step {step_id!r} declares outputs not present in trusted "
                    f"Agent contract: {sorted(undeclared)}"
                )
            expected_outputs = contract_outputs
        else:
            expected_outputs = (
                declared_outputs
                or agent_produces.get(agent_name, [])
                or get_agent_output_logical_names(agent_name)
            )
        if isinstance(expected_outputs, str):
            expected_outputs = [expected_outputs]

        if contract is not None:
            required_inputs = {
                ref.name for ref in contract.requires if ref.required
            }
            bound_inputs = {
                str(binding.get("parameter_name"))
                for binding in inputs
                if isinstance(binding, dict)
            }
            missing_inputs = sorted(required_inputs - bound_inputs)
            if missing_inputs:
                raise TaskGraphValidationError(
                    f"step {step_id!r} is missing trusted input bindings: "
                    f"{missing_inputs!r}"
                )
            for binding in inputs:
                if not isinstance(binding, dict):
                    continue
                parameter_name = str(binding.get("parameter_name") or "")
                expected_schema = contract.input_schema_refs.get(parameter_name)
                assembly = binding.get("assembly")
                if expected_schema and isinstance(assembly, dict):
                    assembly_schema = assembly.get("schema_ref")
                    if not assembly_schema:
                        raise TaskGraphValidationError(
                            f"step {step_id!r} input {parameter_name!r} is "
                            f"missing trusted assembly schema {expected_schema!r}"
                        )
                    if assembly_schema != expected_schema:
                        raise TaskGraphValidationError(
                            f"step {step_id!r} input {parameter_name!r} assembly "
                            f"schema {assembly_schema!r} does not match "
                            f"trusted schema {expected_schema!r}"
                        )
                if parameter_name == "report.sources" and isinstance(
                    binding.get("source_artifacts"), list
                ):
                    source_outputs = [
                        str(source.get("source_output") or "")
                        for source in binding["source_artifacts"]
                        if isinstance(source, dict)
                    ]
                    if trusted_scenario_contract_id == ANNUAL_LEAVE_REPORT_V1:
                        missing = sorted(
                            output
                            for output in _ANNUAL_LEAVE_REPORT_OUTPUTS
                            if source_outputs.count(output) == 0
                        )
                        duplicates = sorted(
                            output
                            for output in _ANNUAL_LEAVE_REPORT_OUTPUTS
                            if source_outputs.count(output) > 1
                        )
                        if missing or duplicates:
                            raise TaskGraphValidationError(
                                "annual-leave report.sources must contain exactly "
                                "one employee.info and one policy.info Artifact; "
                                f"missing={missing!r}, duplicates={duplicates!r}"
                            )

        # ``depends_on`` is an execution-order edge.  For a governed Agent
        # chain it must also carry the producer Artifact into the consumer.
        # Planner-generated autonomous steps commonly omit ``inputs``; when a
        # dependency has a trusted primary output, materialize one unambiguous
        # binding.  Explicit Planner bindings are always preserved unchanged.
        if not inputs and depends_on:
            prior_steps = {item.step_id: item for item in steps}
            for dependency_id in depends_on:
                producer = prior_steps.get(dependency_id)
                dependency_outputs = list(
                    getattr(producer, "expected_outputs", []) or []
                )
                if not dependency_outputs:
                    continue
                inputs.append(
                    {
                        "parameter_name": f"upstream_{dependency_id}",
                        "source_step": dependency_id,
                        "source_output": dependency_outputs[0],
                    }
                )

        # Planner-authored output labels are descriptive and frequently differ
        # from the producer's platform-owned logical output name
        # (``research_results`` vs ``research.markdown``). If the trusted
        # producer contract has exactly one output, bind that canonical output
        # instead of failing a valid chain at execution time.
        prior_steps = {item.step_id: item for item in steps}

        def canonical_source(source: Dict[str, Any]) -> Dict[str, Any]:
            normalized_source = dict(source)
            source_step = str(normalized_source.get("source_step") or "")
            producer = prior_steps.get(source_step)
            canonical_producer_outputs = list(
                getattr(producer, "expected_outputs", []) or []
            )
            requested_output = str(
                normalized_source.get("source_output") or ""
            )
            if (
                producer is not None
                and len(canonical_producer_outputs) == 1
                and requested_output not in canonical_producer_outputs
            ):
                normalized_source["source_output"] = canonical_producer_outputs[0]
            return normalized_source

        canonical_inputs: List[Dict[str, Any]] = []
        for binding in inputs:
            canonical_binding = dict(binding)
            sources = canonical_binding.get("source_artifacts")
            if isinstance(sources, list):
                canonical_binding["source_artifacts"] = [
                    canonical_source(source)
                    if isinstance(source, dict)
                    else source
                    for source in sources
                ]
            elif canonical_binding.get("source_step"):
                canonical_binding = canonical_source(canonical_binding)
            canonical_inputs.append(canonical_binding)
        inputs = canonical_inputs

        completion_conditions = []
        for condition in raw.get("completion_conditions") or []:
            if isinstance(condition, str) and condition.strip():
                completion_conditions.append(
                    CompletionCondition(expression=condition.strip())
                )
            elif isinstance(condition, dict) and condition.get("expression"):
                completion_conditions.append(CompletionCondition(**condition))

        trusted_modes = [
            str(subtask_by_id[subtask_id].get("action") or "").lower()
            for subtask_id in _subtask_ids_for(raw)
            if subtask_id in subtask_by_id
        ]
        trusted_task_mode = (
            max(
                trusted_modes,
                key=lambda value: _MODE_RANK[_classify_single(value)],
            )
            if trusted_modes
            else None
        )
        operation_mode, operation_mode_source, operation_mode_reason = _derive_operation_mode(
            agent_name,
            raw.get("operation_mode"),
            write_agents,
            trusted_task_mode,
        )
        _validate_planner_security_constraints(agent_name, raw)
        trusted_resource_attrs = _config_security_attributes(agent_name) or {}

        step = TaskStep(
            step_id=step_id,
            required_capabilities=raw.get("required_capabilities", []) or [],
            expected_outputs=list(expected_outputs),
            depends_on=depends_on,
            completion_conditions=completion_conditions,
            operation_mode=operation_mode,
            risk_level=raw.get("risk_level", "LOW"),
            timeout=raw.get("timeout"),
            retry=max(0, int(raw.get("retry") or 0)),
            resource_locks=raw.get("resource_locks", []) or [],
            preferred_resource_id=agent_name or None,
            external_side_effect=bool(
                trusted_resource_attrs.get("external_side_effect", False)
            ),
            # extras (TaskStep has extra="allow"):
            agent_name=agent_name,
            input_bindings=inputs,
            title=raw.get("title", ""),
            description=raw.get("description", ""),
            task_type=raw.get("task_type", ""),
            scenario_tags=raw.get("scenario_tags", []) or [],
            scenario_contract_id=(
                trusted_scenario_contract_id
                if agent_name == "RemoteReportAgent"
                else None
            ),
            data_scope=raw.get("data_scope", ""),
            expected_schema_ref=(
                raw.get("expected_schema_ref")
                or raw.get("output_schema_ref")
                # A caller-supplied output vocabulary may describe a legacy or
                # custom contract.  Do not attach the built-in schema to that
                # payload; the trusted schema is selected only together with
                # the server-owned logical-output contract.
                or (
                    get_agent_output_schema_ref(agent_name)
                    if not declared_outputs
                    else None
                )
            ),
            expected_schema_refs=(
                dict(contract.output_schema_refs) if contract else {}
            ),
            agent_contract=(
                contract.model_dump(mode="json") if contract else None
            ),
            verification_contract=dict(raw.get("verification_contract") or {}),
            subtask_ids=_subtask_ids_for(raw),
            intents=_list_field(raw, "intents", "intent"),
            # trusted classification audit trail
            operation_mode_source=operation_mode_source,
            operation_mode_reason=operation_mode_reason,
        )
        steps.append(step)
        if agent_name:
            prior_agent_to_step[agent_name] = step_id

    graph = TaskGraph(spec=TaskSpec(
        task_id=task_id, goal=goal, subject=subject), steps=steps)
    graph.validate_dag()
    return graph
