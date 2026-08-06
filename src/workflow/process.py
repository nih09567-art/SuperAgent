import logging
import hashlib
import asyncio
import json
from typing import Any
from collections import deque
from collections.abc import AsyncGenerator, Mapping
from src.workflow import build_graph
from src.manager import agent_manager
from rich.console import Console
from src.interface.agent import State
from src.service.env import (
    AUTO_RECOVERY_ENABLED,
    DISABLE_DEFAULT_AGENTS,
    MEMORY_ENABLED,
    S_ABAC_ENABLED,
    USE_BROWSER,
)
from src.memory import get_memory_manager
from src.memory.provenance import build_conversation_provenance
from src.workflow.cache import workflow_cache as cache
from src.workflow.graph import CompiledWorkflow
from src.interface.agent import WorkMode
from src.manager.registry import ToolRegistry
from src.robust.checkpoint import CheckpointManager
from src.robust.task_logger import TaskLogger
from config.s_abac_demo_users import get_user_available_agents
from config.global_variables import orchestration_scheduler_enabled
from src.llm.llm import get_llm_by_type
from src.manager.resource import get_resource_registry

# Hook system imports
from src.robust.hooks import (
    HookEngine,
    HookContext,
    HookPoint,
    initialize_hook_system,
)
from src.security.enforcement import PermissionDeniedError
from src.security.scenario_analyzer import analyze_task_context
from src.orchestrator import make_routing_decision
from src.orchestrator.intent_recognition import memory_lookup_keys
# Compatibility import for callers/tests that inspect the retired manager; the
# production workflow no longer invokes it.
from src.skills.workflow_skill import get_workflow_skill_manager
from src.skills.agent_skill import (
    get_agent_skill_manager,
    slice_agent_skill_evidence,
)
from src.skills.execution_evidence import (
    SIDE_EFFECT_MODES,
    SkillExecutionEvidence,
    VerificationStatus,
    build_legacy_evidence,
    load_execution_evidence,
)

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

console = Console()


DEFAULT_PLANNER_AGENTS = ["researcher", "coder", "reporter", "browser"]


def _checkpoint_user_message(
    routing_query: str,
    identity_messages: list,
    memory_context: dict[str, Any] | None,
) -> dict[str, Any]:
    checkpoint_message: dict[str, Any] = {
        "role": "user",
        "content": routing_query,
    }
    for message in reversed(identity_messages):
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "").casefold() != "user":
            continue
        if message.get("message_id"):
            checkpoint_message["message_id"] = str(message["message_id"])
        if isinstance(message.get("metadata"), dict):
            checkpoint_message["metadata"] = dict(message["metadata"])
        break
    memory_turn_id = str((memory_context or {}).get("current_turn_id") or "").strip()
    if memory_turn_id:
        checkpoint_message.setdefault("message_id", memory_turn_id)
        checkpoint_message.setdefault("metadata", {})["turn_id"] = memory_turn_id
    return checkpoint_message


def enable_debug_logging():
    """Enable debug level logging for more detailed execution information."""
    logging.getLogger("src").setLevel(logging.DEBUG)


logger = logging.getLogger(__name__)


def _normalize_planning_steps(raw: Any) -> list:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        if "steps" in raw and isinstance(raw.get("steps"), list):
            return raw.get("steps")
        if "planning_steps" in raw and isinstance(raw.get("planning_steps"), list):
            return raw.get("planning_steps")
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except Exception:
            return []
        return _normalize_planning_steps(parsed)
    return []


def _skill_source_conversation(
    source_state: Mapping[str, Any], task_logger: TaskLogger
) -> dict[str, Any]:
    """Build a bounded audit snapshot from the current user turn and visible outputs."""

    messages = source_state.get("messages") or []
    source_user_message = next(
        (
            message
            for message in reversed(messages)
            if (
                isinstance(message, Mapping)
                and str(message.get("role") or "").casefold() == "user"
            )
            or (
                not isinstance(message, Mapping)
                and str(getattr(message, "role", "") or "").casefold() == "user"
            )
        ),
        None,
    )
    source_user_id = (
        source_user_message.get("message_id")
        if isinstance(source_user_message, Mapping)
        else getattr(source_user_message, "message_id", None)
    )
    source_user_metadata = (
        source_user_message.get("metadata", {})
        if isinstance(source_user_message, Mapping)
        else getattr(source_user_message, "metadata", {})
    )
    source_user_metadata = (
        source_user_metadata if isinstance(source_user_metadata, Mapping) else {}
    )
    current_user = {
        "role": "user",
        "content": source_state.get("original_user_query")
        or source_state.get("USER_QUERY")
        or getattr(task_logger, "user_query", ""),
        "message_id": source_state.get("current_turn_id")
        or source_state.get("memory_turn_id")
        or source_state.get("message_id")
        or source_user_metadata.get("turn_id")
        or source_user_id
        or "",
    }
    visible_messages: list[Any] = [current_user]
    for message in messages:
        if isinstance(message, Mapping):
            role = str(message.get("role") or "").casefold()
            tool = str(message.get("tool") or "").casefold()
        else:
            role = str(getattr(message, "role", "") or "").casefold()
            tool = str(getattr(message, "tool", "") or "").casefold()
        if role == "assistant" and tool in {"agent_proxy", "publisher", "assistant"}:
            visible_messages.append(message)
    if len(visible_messages) == 1:
        for entry in getattr(task_logger, "history", []) or []:
            if not isinstance(entry, Mapping) or entry.get("event") != "message":
                continue
            node_name = str(entry.get("node_name") or "").casefold()
            if node_name in {"agent_proxy", "publisher", "assistant"}:
                visible_messages.append(
                    {
                        "role": "assistant",
                        "tool": node_name,
                        "content": entry.get("content", ""),
                        "message_id": entry.get("message_id", ""),
                        "created_at": entry.get("timestamp", ""),
                    }
                )
    turn_id = str(
        current_user.get("message_id")
        or f"task:{getattr(task_logger, 'task_id', '')}"
    )
    snapshot = build_conversation_provenance(visible_messages, turn_id=turn_id)
    snapshot["task_id"] = str(getattr(task_logger, "task_id", ""))
    snapshot["workflow_id"] = str(getattr(task_logger, "workflow_id", ""))
    snapshot["created_at"] = str(getattr(task_logger, "created_at", ""))
    snapshot["finished_at"] = str(getattr(task_logger, "finished_at", ""))
    return snapshot


def _agent_skill_bindings_from_steps(steps: Any) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for index, step in enumerate(_normalize_planning_steps(steps)):
        if not isinstance(step, dict):
            continue
        binding = step.get("agent_skill_binding")
        if not isinstance(binding, dict) or not binding.get("skill_id"):
            continue
        step_id = str(
            step.get("step_id") or step.get("subtask_id") or f"step_{index + 1}"
        )
        bindings[step_id] = str(binding["skill_id"])
    return bindings


def _strip_agent_skill_runtime_state(state: dict[str, Any]) -> None:
    """Remove attempt-local bindings/evidence before a resumed execution."""

    stripped_steps: list[dict[str, Any]] = []
    for step in _normalize_planning_steps(state.get("planning_steps")):
        if not isinstance(step, dict):
            continue
        clean = dict(step)
        clean.pop("agent_skill_binding", None)
        clean.pop("agent_skill_guidance", None)
        stripped_steps.append(clean)
    if stripped_steps:
        state["planning_steps"] = stripped_steps
        try:
            cache.restore_planning_steps(
                str(state.get("workflow_id") or ""),
                stripped_steps,
                str(state.get("user_id") or ""),
            )
        except Exception:  # pragma: no cover - cache may be absent in unit tests
            pass
    task_graph = state.get("task_graph")
    if isinstance(task_graph, dict) and isinstance(task_graph.get("steps"), list):
        for raw_step in task_graph["steps"]:
            if isinstance(raw_step, dict):
                raw_step.pop("agent_skill_binding", None)
                raw_step.pop("agent_skill_guidance", None)
    state["agent_skill_bindings"] = {}
    state["agent_skill_applied_steps"] = {}


def _resume_step_evidence(raw: Any, resume_step: int | None) -> dict[str, Any]:
    """Keep only durable legacy evidence produced before the resume frontier."""

    if not isinstance(raw, dict) or resume_step is None or resume_step < 1:
        return {}
    preserved: dict[str, Any] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        step_id = str(value.get("step_id") or key)
        raw_step_number = step_id.split(":", 1)[0]
        try:
            step_number = int(raw_step_number)
        except ValueError:
            continue
        if step_number < resume_step:
            preserved[str(key)] = value
    return preserved


def _agent_contract_fingerprints(agent_cards: Any) -> dict[str, str]:
    """Hash the current dispatch contract without persisting runtime state."""

    fingerprints: dict[str, str] = {}
    for card in agent_cards if isinstance(agent_cards, list) else []:
        if not isinstance(card, dict):
            continue
        agent_id = str(card.get("agent_id") or card.get("name") or "").strip()
        if not agent_id:
            continue
        contract = {
            "capabilities": card.get("capabilities") or [],
            "intents": card.get("intents") or [],
            "supported_actions": card.get("supported_actions") or [],
            "accepted_data_scopes": card.get("accepted_data_scopes") or [],
            "risk_ceiling": card.get("risk_ceiling") or "LOW",
            "input_schema": card.get("input_schema") or {},
            "output_schema": card.get("output_schema") or {},
            "contract_version": card.get("contract_version"),
            "requires": card.get("requires") or [],
            "produces": card.get("produces") or [],
            "input_schema_refs": card.get("input_schema_refs") or {},
            "output_schema_refs": card.get("output_schema_refs") or {},
            "version": card.get("version") or "1.0.0",
        }
        fingerprints[agent_id] = hashlib.sha256(
            json.dumps(
                contract,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    return fingerprints


def _agent_capability_bindings(agent_cards: Any) -> dict[str, list[str]]:
    bindings: dict[str, list[str]] = {}
    for card in agent_cards if isinstance(agent_cards, list) else []:
        if not isinstance(card, dict):
            continue
        agent_id = str(card.get("agent_id") or card.get("name") or "").strip()
        if agent_id:
            bindings[agent_id] = [
                str(item) for item in card.get("capabilities") or [] if str(item)
            ]
    return bindings


def _current_agent_contracts(agent_cards: Any) -> dict[str, dict[str, Any]]:
    """Extract only current trusted registry Contracts from serialized cards."""

    contracts: dict[str, dict[str, Any]] = {}
    for card in agent_cards if isinstance(agent_cards, list) else []:
        if not isinstance(card, dict):
            continue
        agent_id = str(card.get("agent_id") or card.get("name") or "").strip()
        contract = card.get("agent_contract")
        if agent_id and isinstance(contract, dict):
            contracts[agent_id] = contract
    return contracts


async def _trusted_registry_contract_data(
    user_id: Any,
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """Contract/produces mappings from the SAME source as the planner save path
    (the current trusted Agent registry), so the snapshot rebuild gate compares
    against live registry state instead of data echoed from checkpoints."""

    await agent_manager.ensure_initialized()
    registered_agents = await agent_manager.agent_registry.list()
    contracts = {
        agent.agent_name: agent.agent_contract
        for agent in registered_agents
        if getattr(agent, "agent_contract", None) is not None
        and (agent.user_id == "share" or agent.user_id == user_id)
    }
    produces = {
        agent.agent_name: list(getattr(agent, "produces", []) or [])
        for agent in registered_agents
        if agent.user_id == "share" or agent.user_id == user_id
    }
    # Built-in contracts and live Agent Cards may enumerate the same logical
    # outputs in different orders. Preserve the server-owned catalog order when
    # the sets are identical so snapshot re-derivation is deterministic and the
    # primary output used for implicit bindings does not drift across restarts.
    from src.orchestration.output_contracts import get_agent_output_logical_names

    for agent_name, live_outputs in list(produces.items()):
        catalog_outputs = get_agent_output_logical_names(agent_name)
        if catalog_outputs and set(catalog_outputs) == set(live_outputs):
            produces[agent_name] = catalog_outputs
    return contracts, produces


async def _execute_node_with_runtime_events(
    state: State, node_func, enable_runtime_events: bool
):
    if not enable_runtime_events:
        yield await node_func(state)
        return

    runtime_event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def _emit_runtime_event(event: dict[str, Any]) -> None:
        await runtime_event_queue.put(event)

    state["runtime_event_handler"] = _emit_runtime_event
    command_task = asyncio.create_task(node_func(state))

    try:
        while True:
            if command_task.done():
                while not runtime_event_queue.empty():
                    yield await runtime_event_queue.get()
                break
            try:
                event = await asyncio.wait_for(runtime_event_queue.get(), timeout=0.05)
                yield event
            except asyncio.TimeoutError:
                continue

        command = await command_task
        yield command
    finally:
        state.pop("runtime_event_handler", None)
        if not command_task.done():
            command_task.cancel()


def has_task_graph_state(state: dict) -> bool:
    return bool(state.get("task_graph"))


def load_production_task_graph(
    state: dict,
    execution_phase: str,
    *,
    current_agent_contracts: dict[str, Any] | None = None,
    current_agent_produces: dict[str, list[str]] | None = None,
) -> tuple[bool, str]:
    """For a production execution request, load + verify the PlanSnapshot and
    inject its TaskGraph into ``state``.

    Returns ``(injected, reason)``. Fails closed: on a missing or inconsistent
    snapshot (workflow/user/plan_hash/version mismatch) nothing is injected, so
    the downstream scheduler gate refuses execution and requires a re-plan.
    """
    if state.get("workflow_mode") != "production" or execution_phase != "execution":
        return False, "not_production_exec"

    # A graph restored from a checkpoint or supplied by a caller is not an
    # approval artifact. Production execution must always replace it with the
    # graph loaded from, and re-derived against, the persisted PlanSnapshot.
    # Remove it before every fail-closed branch so the scheduler cannot consume
    # an unverified graph when the snapshot is absent or invalid.
    state.pop("task_graph", None)
    from src.orchestration.plan_snapshot import (
        load_plan_snapshot,
        verify_snapshot_for_execution,
    )

    workflow_id = state.get("workflow_id")
    snapshot = load_plan_snapshot(workflow_id)
    if not snapshot:
        return False, "no_snapshot"
    current_steps = _normalize_planning_steps(
        cache.get_planning_steps(workflow_id))
    if not current_steps:
        reason = "current planning steps unavailable (replan required)"
        state["task_graph_rejection_reason"] = reason
        return False, reason
    # Authoritative gate: re-derive the TaskGraph from the CURRENT planning
    # steps and deep-compare against the approved snapshot (plus version +
    # content-hash integrity). Only inject the approved graph on an exact match;
    # any drift fails closed so the scheduler gate requires a re-plan.
    task_graph, reason = verify_snapshot_for_execution(
        snapshot,
        workflow_id=workflow_id,
        user_id=state.get("user_id"),
        planning_steps=current_steps,
        goal=state.get("original_user_query", "")
        or state.get("USER_QUERY", ""),
        current_agent_contracts=(
            current_agent_contracts
            if current_agent_contracts is not None
            else _current_agent_contracts(state.get("agent_cards"))
        ),
        current_agent_produces=current_agent_produces,
        subtasks=(state.get("task_profile") or {}).get("subtasks"),
    )
    if task_graph is None:
        logger.warning("plan snapshot rejected for %s: %s",
                       workflow_id, reason)
        state["task_graph_rejection_reason"] = reason
        return False, reason
    state.pop("task_graph_rejection_reason", None)
    state["task_graph"] = task_graph
    return True, "loaded"


async def _prepare_execution_graph(workflow_id: str, user_id: str, resume_step: int = None) -> None:
    """Prepare execution graph and queue for production mode.

    Args:
        workflow_id: Workflow ID
        user_id: User ID
        resume_step: If provided, fast-forward the queue to start from this step.
                     resume_step=5 means the first 4 steps are done, start from step 5.
    """
    workflow = cache.cache.get(workflow_id)
    if not workflow:
        cache._load_workflow(user_id)
        workflow = cache.cache.get(workflow_id)
    if not workflow:
        raise ValueError("workflow not found for execution")

    steps = _normalize_planning_steps(cache.get_planning_steps(workflow_id))
    if not steps:
        raise RuntimeError("no planning steps found for execution")

    await agent_manager.ensure_initialized()
    nodes = workflow.get("nodes") if isinstance(
        workflow.get("nodes"), dict) else {}
    graph = workflow.get("graph") if isinstance(
        workflow.get("graph"), list) else []
    system_graph = [
        node
        for node in graph
        if isinstance(node, dict) and (node.get("config") or {}).get("node_type") == "system_agent"
    ]

    exec_graph = []
    missing = []
    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            missing.append(f"step_{idx + 1}")
            continue
        agent_name = step.get("agent_name")
        if not agent_name:
            missing.append(f"step_{idx + 1}")
            continue
        agent = await agent_manager.agent_registry.get(agent_name)
        if agent is None:
            missing.append(agent_name)
            continue

        tools = []
        for tool in agent.selected_tools:
            tools.append(
                {
                    "component_type": "function",
                    "label": tool.name,
                    "name": tool.name,
                    "config": {
                        "name": tool.name,
                        "description": tool.description,
                    },
                }
            )

        nodes[agent_name] = {
            "component_type": "agent",
            "label": agent.agent_name,
            "name": agent.agent_name,
            "config": {
                "type": "execution_agent",
                "name": agent.agent_name,
                "description": agent.description,
                "tools": tools,
                "prompt": agent.prompt,
                "llm_type": agent.llm_type,
            },
        }

        exec_graph.append(
            {
                "component_type": "agent",
                "label": agent.agent_name,
                "name": agent.agent_name,
                "config": {
                    "node_name": agent.agent_name,
                    "node_type": "execution_agent",
                    "next_to": [],
                    "condition": "supervised",
                },
            }
        )

    if missing:
        raise RuntimeError(
            f"missing agents for execution: {', '.join(missing)}")

    for i, node in enumerate(exec_graph):
        if i + 1 < len(exec_graph):
            node["config"]["next_to"] = [
                exec_graph[i + 1]["config"]["node_name"]]
        else:
            node["config"]["next_to"] = []

    workflow["planning_steps"] = steps
    workflow["nodes"] = nodes
    workflow["graph"] = system_graph + exec_graph
    cache.cache[workflow_id] = workflow
    cache.save_workflow(workflow)

    cache.queue[workflow_id] = deque(exec_graph)
    if exec_graph:
        begin_node = {
            "component_type": "agent",
            "label": "begin_node",
            "name": "begin_node",
            "config": {
                "node_name": "begin_node",
                "node_type": "execution_agent",
                "next_to": [exec_graph[0]["config"]["node_name"]],
                "condition": "supervised",
            },
        }
        cache.queue[workflow_id].appendleft(begin_node)

    # Fast-forward queue for resume
    if resume_step is not None and resume_step >= 1:
        # Execution sequence analysis:
        # - Steps 0,1: coordinator, planner (system nodes, no queue update)
        # - Step 2: publisher (system node, no queue update)
        # - Step 3: agent_proxy->agent1 (update_stack pops begin_node)
        # - Step 4: publisher (no update)
        # - Step 5: agent_proxy->agent2 (update_stack pops agent1)
        # - Step 6: publisher (no update)
        # - ...
        #
        # Pattern: agent_proxy runs at odd steps (3, 5, 7, ...), each pops queue[0]
        #          publisher runs at even steps (2, 4, 6, ...), no pop
        #
        # Checkpoint is saved AFTER node execution and update_stack
        # So for resume_step=M (checkpoint step=M-1):
        # - Queue state reflects all update_stack calls from steps < M
        # - We need to replay those pops
        #
        # Count agent_proxy steps that completed before resume_step:
        # - Agent_proxy steps are: 3, 5, 7, ... (odd steps >= 3)
        # - Count odd numbers in range [3, resume_step)
        agent_proxy_steps_completed = sum(
            1 for s in range(3, resume_step) if s % 2 == 1)

        # Log initial queue state
        initial_queue = list(cache.queue[workflow_id])
        logger.info(
            f"Queue BEFORE fast-forward (resume_step={resume_step}): {[n['name'] for n in initial_queue]}")
        logger.info(
            f"Agent_proxy steps completed: {agent_proxy_steps_completed}")

        # Pop that many elements from queue
        for i in range(agent_proxy_steps_completed):
            if cache.queue[workflow_id]:
                popped = cache.queue[workflow_id].popleft()
                logger.info(
                    f"Fast-forward pop {i+1}: removed '{popped.get('name')}' from queue")
            else:
                logger.warning(
                    f"Queue empty at iteration {i+1}, stopping fast-forward")
                break

        final_queue = list(cache.queue[workflow_id])
        logger.info(
            f"Queue AFTER fast-forward: {[n['name'] for n in final_queue]}")

if USE_BROWSER and not DISABLE_DEFAULT_AGENTS:
    DEFAULT_TEAM_MEMBERS_DESCRIPTION = """
        - **`coder`**: Executes Python or Bash commands, performs mathematical calculations, and outputs a Markdown report. Must be used for all mathematical computations.
        - **`browser`**: Directly interacts with web pages, performing complex operations and interactions. You can also leverage `browser` to perform in-domain search, like Facebook, Instagram, Github, etc.
        - **`reporter`**: Write a professional report based on the result of each step.
        
        """
elif not DISABLE_DEFAULT_AGENTS:
    DEFAULT_TEAM_MEMBERS_DESCRIPTION = """
        - **`researcher`**: Uses search engines and web crawlers to gather information from the internet. Outputs a Markdown report summarizing findings. Researcher can not do math or programming.
        - **`coder`**: Executes Python or Bash commands, performs mathematical calculations, and outputs a Markdown report. Must be used for all mathematical computations.
        - **`reporter`**: Write a professional report based on the result of each step.
        
        """
else:
    DEFAULT_TEAM_MEMBERS_DESCRIPTION = ""

TEAM_MEMBERS_DESCRIPTION_TEMPLATE = """
- **`{agent_name}`**: {agent_description}
  - Requires: {requires}
  - Produces: {produces}
"""
TOOLS_DESCRIPTION_TEMPLATE = """
- **`{tool_name}`**: {tool_description}
"""
# Cache for coordinator messages
coordinator_cache = []
MAX_CACHE_SIZE = 2


async def _build_team_members(
    user_id: str,
    coor_agents: list[str] | None,
) -> tuple[list[str], str]:
    coor_agents = coor_agents or []
    member_desc = ""
    members = []

    available = get_user_available_agents(user_id)
    has_user_profile = bool(available)

    agents = await agent_manager.agent_registry.list()
    for agent in agents:
        should_include = (
            agent.user_id == "share"
            or agent.user_id == user_id
            or agent.agent_name in coor_agents
        )
        if has_user_profile and available != ["*"]:
            if agent.agent_name not in available:
                should_include = False
                if agent.agent_name in coor_agents:
                    logger.warning(
                        "S-ABAC: ignored explicitly selected unauthorized agent '%s' for user '%s'",
                        agent.agent_name,
                        user_id,
                    )

        if should_include and agent.agent_name not in members:
            members.append(agent.agent_name)
            requires = getattr(agent, "requires", [])
            produces = getattr(agent, "produces", [])
            requires_str = ", ".join(requires) if requires else "None"
            produces_str = ", ".join(produces) if produces else "None"

            member_desc += "\n" + TEAM_MEMBERS_DESCRIPTION_TEMPLATE.format(
                agent_name=agent.agent_name,
                agent_description=agent.description,
                requires=requires_str,
                produces=produces_str,
            )

    if has_user_profile and available != ["*"]:
        for agent_name in available:
            if agent_name in DEFAULT_PLANNER_AGENTS and agent_name not in members:
                members.append(agent_name)

    if not members and has_user_profile:
        logger.warning(
            "S-ABAC: No agents available for user '%s' (available=%s, DISABLE_DEFAULT_AGENTS=%s). "
            "Planner will have an empty team.",
            user_id, available, DISABLE_DEFAULT_AGENTS,
        )

    return members, member_desc


async def _build_tools_description() -> str:
    registry = await ToolRegistry.get_instance()
    tools = await registry.list_global_tools()
    resource_registry = await get_resource_registry()
    resource_tools = await resource_registry.list(type="tool")
    descriptions = []

    for meta in tools:
        tool_name = getattr(meta.tool, "name", "")
        if not tool_name:
            continue
        tool_desc = meta.description or getattr(meta.tool, "description", "")
        descriptions.append(
            TOOLS_DESCRIPTION_TEMPLATE.format(
                tool_name=tool_name,
                tool_description=tool_desc,
            )
        )

    for spec in resource_tools:
        if spec.server_id == "local":
            continue
        tool_desc = (spec.metadata or {}).get("description", "")
        suffix = f"(remote/{spec.protocol or 'http'} from {spec.server_id})"
        descriptions.append(
            TOOLS_DESCRIPTION_TEMPLATE.format(
                tool_name=spec.name,
                tool_description=f"{tool_desc} {suffix}".strip(),
            )
        )
    return "".join(descriptions)


async def _build_resource_catalog() -> str:
    registry = await get_resource_registry()
    specs = await registry.list()
    if not specs:
        return ""

    lines = []
    for spec in sorted(specs, key=lambda s: (s.type, s.server_id, s.name)):
        desc = (spec.metadata or {}).get("description", "")
        proto = spec.protocol or "local"
        location = "local" if spec.server_id == "local" else f"remote/{spec.server_id}"
        lines.append(
            f"- [{spec.type}] {spec.name} ({location}, protocol={proto}) {desc}".strip()
        )
    return "\n".join(lines)


async def run_agent_workflow(
    user_id: str,
    user_input_messages: list,
    debug: bool = False,
    deep_thinking_mode: bool = False,
    search_before_planning: bool = False,
    coor_agents: list[str] | None = None,
    polish_id: str = None,
    lap: int = 0,
    workmode: WorkMode = "launch",
    workflow_id: str = None,
    polish_instruction: str = None,
    resume_step: int = None,
    task_id: str = None,
    stop_after_planner: bool = False,
    instruction: str | None = None,
    instruction_history: list[str] | None = None,
    original_user_query: str | None = None,
    memory_session_id: str | None = None,
    memory_enabled: bool | None = None,
    memory_context: dict[str, Any] | None = None,
    project_id: str | None = None,
    compaction_model_type: str | None = None,
    skill_reuse_enabled: bool | None = None,
    request_input_messages: list | None = None,
    current_request: str | None = None,
    raw_request: str | None = None,
    entity_overrides: dict[str, Any] | None = None,
    context_references: list[dict[str, Any]] | None = None,
    context_artifacts: list[dict[str, Any]] | None = None,
    conversation_context: dict[str, Any] | None = None,
):
    """Run the agent workflow with the given user input.

    Args:
        user_input_messages: The user request messages
        debug: If True, enables debug level logging

    Returns:
        The final state after the workflow completes
    """
    identity_messages = request_input_messages or user_input_messages
    effective_memory_enabled = bool(
        MEMORY_ENABLED
        and (
            memory_enabled
            if memory_enabled is not None
            else bool(memory_session_id)
        )
    )
    if not workflow_id:
        if not polish_id:
            if workmode == "launch":
                msg = f"{user_id}_{identity_messages}_{deep_thinking_mode}_{search_before_planning}_{coor_agents}"
                polish_id = hashlib.md5(msg.encode("utf-8")).hexdigest()
            else:
                polish_id = cache.get_latest_polish_id(user_id)

        workflow_id = f"{user_id}:{polish_id}"

    await agent_manager.ensure_initialized()
    lap = cache.get_lap(workflow_id) if workmode != "launch" else 0

    if workmode != "production":
        lap = lap + 1

    cache.init_cache(
        user_id=user_id,
        mode=workmode,
        workflow_id=workflow_id,
        lap=lap,
        version=1,
        user_input_messages=identity_messages.copy(),
        deep_thinking_mode=deep_thinking_mode,
        search_before_planning=search_before_planning,
        coor_agents=coor_agents,
    )

    if instruction_history is not None:
        cache.set_instruction_history(
            workflow_id, instruction_history, user_id=user_id)
    elif instruction:
        cache.append_instruction(workflow_id, instruction, user_id=user_id)

    if workmode == "production":
        try:
            await _prepare_execution_graph(workflow_id, user_id, resume_step=resume_step)
        except RuntimeError as exc:
            error_text = str(exc)
            if error_text == "no planning steps found for execution":
                reason_code = "PLAN_STEPS_UNAVAILABLE"
                reason = "Confirmed planning steps could not be loaded for execution"
            elif error_text.startswith("missing agents for execution:"):
                reason_code = "EXECUTION_AGENTS_UNAVAILABLE"
                reason = "One or more planned agents are unavailable"
            else:
                reason_code = "WORKFLOW_PREPARATION_FAILED"
                reason = "Workflow preparation failed"
            logger.warning(
                "S-ABAC workflow preparation blocked execution: %s", exc)
            if task_id:
                task_logger_loader = getattr(TaskLogger, "load", None)
                reserved_task = (
                    task_logger_loader(task_id)
                    if callable(task_logger_loader)
                    else None
                )
                if reserved_task is not None:
                    reserved_task.log_workflow_terminal("FAILED", error=error_text)
            yield {
                "event": "workflow_error",
                "data": {
                    "workflow_id": workflow_id,
                    "task_id": task_id or CheckpointManager.generate_task_id(workflow_id),
                    "error": error_text,
                    "reason": reason,
                    "reason_code": reason_code,
                },
            }
            return

    # Generate a unique task_id for this execution instance if not provided
    if not task_id:
        task_id = CheckpointManager.generate_task_id(workflow_id)

    graph = build_graph()
    if not user_input_messages:
        raise ValueError("Input could not be empty")

    if debug:
        enable_debug_logging()

    logger.info(f"Starting workflow with user input: {user_input_messages}")

    team_members, team_members_description = await _build_team_members(
        user_id=user_id,
        coor_agents=coor_agents,
    )
    registered_agents = await agent_manager.agent_registry.list()
    # 对话历史只用于显示、审计和记忆，不得再拼接后交给任务边界识别。
    # 当前请求已经由 ConversationContextResolver 完成澄清回填和指代消解。
    routing_query = (
        str(current_request or "").strip()
        or str(instruction or "").strip()
        or str(original_user_query or "").strip()
        or str(user_input_messages[-1]["content"])
    )
    checkpoint_message = _checkpoint_user_message(
        routing_query,
        identity_messages,
        memory_context,
    )
    task_profile_model, agent_cards, routing_decision_model = await make_routing_decision(
        user_query=routing_query,
        task_id=task_id,
        workflow_id=workflow_id,
        agents=registered_agents,
        authorized_agent_ids=set(team_members),
        metadata={
            "workflow_mode": str(workmode),
            "s_abac_enabled": S_ABAC_ENABLED,
        },
        entity_overrides=dict(entity_overrides or {}),
        context_references=list(context_references or []),
        context_artifacts=list(context_artifacts or []),
        conversation_context=dict(conversation_context or {}),
        raw_request=raw_request or routing_query,
    )
    routing_decision = routing_decision_model.model_dump()
    routing_decision_for_prompt = dict(routing_decision)
    routing_decision_for_prompt.pop("excluded_agents", None)
    task_profile = task_profile_model.to_legacy_scenario()
    resolved_memory_context = dict(memory_context or {})
    if effective_memory_enabled and memory_session_id:
        try:
            scenario_memory_tags = tuple(
                dict.fromkeys(
                    [
                        str(tag)
                        for tag in task_profile.get("scenario_tags", [])
                        if str(tag).strip()
                    ]
                    + [
                        f"task.{tag}"
                        for tag in task_profile.get("scenario_tags", [])
                        if str(tag).strip()
                    ]
                    + [
                        f"task.{item.get('intent')}"
                        for item in task_profile.get("subtasks", [])
                        if isinstance(item, dict) and item.get("intent")
                    ]
                )
            )
            memory_manager = get_memory_manager()
            recall_context = getattr(memory_manager, "recall_context", None)
            requested_memory_keys = memory_lookup_keys(routing_query)
            if callable(recall_context):
                reference, memory_ids, memory_entries = await recall_context(
                    user_id=user_id,
                    query=routing_query,
                    intent_tags=scenario_memory_tags,
                    scopes=("user", "task", "project") if project_id else ("user", "task"),
                    project_id=project_id or str(resolved_memory_context.get("project_id") or "") or None,
                    memory_keys=requested_memory_keys,
                )
            else:
                reference, memory_ids = await memory_manager.recall_labels(
                    user_id=user_id,
                    query=routing_query,
                    intent_tags=scenario_memory_tags,
                    scopes=("user", "task", "project") if project_id else ("user", "task"),
                    project_id=project_id or str(resolved_memory_context.get("project_id") or "") or None,
                )
                memory_entries = ()
            if reference:
                resolved_memory_context["long_term_reference"] = reference
            if memory_ids:
                resolved_memory_context["retrieved_memory_ids"] = list(memory_ids)
            if memory_entries:
                resolved_memory_context["retrieved_memories"] = [
                    dict(item) for item in memory_entries
                ]
        except Exception as exc:
            logger.warning(
                "Scenario-tag memory recall skipped: %s", type(exc).__name__
            )
    routed_member_ids = [
        item.agent_id for item in routing_decision_model.candidate_agents
    ]
    if routing_decision_model.decision == "DISPATCH":
        team_members = routed_member_ids
        routed_cards = {
            card.agent_id: card for card in agent_cards if card.agent_id in set(routed_member_ids)
        }
        team_members_description = "\n".join(
            (
                f"- **`{agent_id}`**: {routed_cards[agent_id].description}\n"
                f"  - Department: {routed_cards[agent_id].department}\n"
                f"  - Capabilities: {', '.join(routed_cards[agent_id].capabilities)}\n"
                f"  - Intents: {', '.join(routed_cards[agent_id].intents)}\n"
                f"  - Actions: {', '.join(routed_cards[agent_id].supported_actions)}"
            )
            for agent_id in team_members
            if agent_id in routed_cards
        )
    elif workmode != "production":
        team_members = []
        team_members_description = ""
    tools_description = await _build_tools_description()
    resource_catalog = await _build_resource_catalog()

    global coordinator_cache
    coordinator_cache = []
    global is_handoff_case
    is_handoff_case = False

    # 判断执行阶段（调用TaskLogger的静态方法）
    instruction_history_list = cache.get_instruction_history(workflow_id) or []
    execution_phase = TaskLogger.determine_execution_phase(
        workmode, instruction_history_list)
    logger.info(f"Execution phase determined: {execution_phase}")
    serialized_agent_cards = [card.model_dump() for card in agent_cards]
    contract_fingerprints = _agent_contract_fingerprints(serialized_agent_cards)
    capability_bindings = _agent_capability_bindings(serialized_agent_cards)

    async for event_data in _process_workflow(
        graph,
        {
            "user_id": user_id,
            "TEAM_MEMBERS": team_members,
            "TEAM_MEMBERS_DESCRIPTION": team_members_description,
            "TOOLS": tools_description,
            "RESOURCE_CATALOG": resource_catalog,
            "USER_QUERY": routing_query,
            "execution_user_query": routing_query,
            "original_user_query": routing_query,
            # LLM 工作节点只处理本轮已解析请求。完整聊天记录已经由记忆系统
            # 独立保存，不能作为多条 user message 再次进入 Planner。
            "messages": [checkpoint_message],
            "deep_thinking_mode": deep_thinking_mode,
            "search_before_planning": search_before_planning,
            "workflow_id": workflow_id,
            "workflow_mode": workmode,
            "polish_instruction": polish_instruction,
            "initialized": False,
            "stop_after_planner": stop_after_planner,
            # Planner 只接收当前已解析请求；完整会话历史保留在 cache/memory。
            "instruction_history": [routing_query],
            "task_profile": task_profile,
            "task_profile_reason": task_profile.get("reason", ""),
            "scenario_tags": task_profile.get("scenario_tags", []),
            "expected_capabilities": task_profile.get("expected_capabilities", []),
            "task_type": task_profile.get("task_type", "GENERAL"),
            "business_goal": task_profile.get("business_goal", routing_query),
            "data_scope": task_profile.get("data_scope", "general"),
            "operation_mode": task_profile.get("operation_mode", "read"),
            "risk_profile": task_profile.get("risk_profile", "LOW"),
            "scenario_fit_cache": {},
            "TASK_PROFILE_TEXT": json.dumps(task_profile, ensure_ascii=False, indent=2),
            "SCENARIO_TAGS_TEXT": ", ".join(task_profile.get("scenario_tags", [])),
            "EXPECTED_CAPABILITIES_TEXT": ", ".join(task_profile.get("expected_capabilities", [])),
            "routing_decision": routing_decision,
            "ROUTING_DECISION_TEXT": json.dumps(
                routing_decision_for_prompt,
                ensure_ascii=False,
                indent=2,
            ),
            "agent_cards": serialized_agent_cards,
            "agent_contract_fingerprints": contract_fingerprints,
            "agent_capability_bindings": capability_bindings,
            "memory_session_id": memory_session_id or "",
            "memory_enabled": effective_memory_enabled,
            "memory_context": resolved_memory_context,
            "compaction_model_type": compaction_model_type or "basic",
            "skill_reuse_enabled": skill_reuse_enabled is not False,
            "reused_skill_id": "",
            "reused_skill_owner_id": "",
            "workflow_skill_match": {},
            "agent_skill_bindings": {},
            "agent_skill_applied_steps": {},
            "workflow_execution_failed": False,
            "skill_step_evidence": {},
            "skill_execution_evidence": {},
            "business_success": None,
        },
        resume_step=resume_step,
        task_id=task_id,
        execution_phase=execution_phase,  # 新增：传递执行阶段
    ):
        yield event_data


async def _process_workflow(
    workflow: CompiledWorkflow,
    initial_state: dict[str, Any],
    resume_step: int = None,
    task_id: str = None,
    execution_phase: str = "initial_planning"  # 新增：执行阶段参数
) -> AsyncGenerator[dict[str, Any], None]:
    """处理自定义工作流的事件流

    Args:
        resume_step: The step to START executing (not the checkpoint step).
                     So resume_step=5 means: load checkpoint from step 4, then execute step 5.
                     Must be >= 1.
        execution_phase: 执行阶段 ("initial_planning" | "re_planning" | "execution")
    """
    current_node = None

    runtime_context = {
        key: initial_state.get(key)
        for key in (
            "TEAM_MEMBERS",
            "TEAM_MEMBERS_DESCRIPTION",
            "TOOLS",
            "RESOURCE_CATALOG",
            "agent_cards",
            "agent_contract_fingerprints",
            "agent_capability_bindings",
        )
        if initial_state.get(key) is not None
    }

    workflow_id = initial_state["workflow_id"]
    checkpoint_manager = CheckpointManager()
    step_count = 0

    def _restore_scenario_state_from_source(target_state: dict[str, Any], source_state: dict[str, Any] | None) -> None:
        if not isinstance(source_state, dict):
            return
        for key in (
            "original_user_query",
            "execution_user_query",
            "task_profile",
            "task_profile_reason",
            "scenario_tags",
            "expected_capabilities",
            "task_type",
            "business_goal",
            "data_scope",
            "operation_mode",
            "risk_profile",
            "scenario_fit_cache",
            "TASK_PROFILE_TEXT",
            "SCENARIO_TAGS_TEXT",
            "EXPECTED_CAPABILITIES_TEXT",
            "routing_decision",
            "ROUTING_DECISION_TEXT",
            "agent_cards",
            "agent_contract_fingerprints",
            "agent_capability_bindings",
            "skill_reuse_enabled",
            "reused_skill_id",
            "reused_skill_owner_id",
            "workflow_skill_match",
            "agent_skill_bindings",
            "agent_skill_applied_steps",
            "workflow_execution_failed",
            "skill_step_evidence",
            "skill_execution_evidence",
            "business_success",
        ):
            if not target_state.get(key) and source_state.get(key) is not None:
                target_state[key] = source_state.get(key)

    # Initialize TaskLogger for this execution
    user_query = initial_state.get(
        "original_user_query") or initial_state.get("USER_QUERY", "")
    if not task_id:
        task_id = CheckpointManager.generate_task_id(workflow_id)

    # Resume logic: Check if we are in a mode that supports resuming or resume_step is specified
    # resume_step indicates the step to START executing, so we need checkpoint from (resume_step - 1)
    should_resume = resume_step is not None and resume_step >= 1

    if should_resume:
        # Load existing TaskLogger and truncate history
        from src.robust.task_logger import TaskLogger as TL
        existing_logger = TL.load(task_id)
        if existing_logger:
            # Truncate history/failures and reset terminal fields so the re-run
            # starts from a consistent pre-resume log state.
            existing_logger.truncate_for_resume(resume_step)
            task_logger = existing_logger
            user_query = existing_logger.user_query
            logger.info(
                f"Resumed TaskLogger for task {task_id}, truncated to step {resume_step - 1}")
        else:
            task_logger = TaskLogger(
                task_id=task_id, workflow_id=workflow_id, user_query=user_query)
            task_logger.set_execution_phase(execution_phase)  # 设置执行阶段
    else:
        task_logger_loader = getattr(TaskLogger, "load", None)
        existing_logger = (
            task_logger_loader(task_id) if callable(task_logger_loader) else None
        )
        if existing_logger and existing_logger.status == "reserved":
            task_logger = existing_logger
            task_logger.activate_reserved_execution()
            task_logger.set_execution_phase(execution_phase)
        elif existing_logger:
            raise RuntimeError(f"task id already exists: {task_id}")
        else:
            task_logger = TaskLogger(
                task_id=task_id, workflow_id=workflow_id, user_query=user_query)
            task_logger.set_execution_phase(execution_phase)  # 设置执行阶段

    def _reset_resume_evidence(target_state: dict[str, Any]) -> None:
        """Drop stale terminal evidence and keep only pre-resume step evidence."""

        target_state["skill_execution_evidence"] = {}
        target_state["skill_step_evidence"] = _resume_step_evidence(
            target_state.get("skill_step_evidence"), resume_step
        )
        target_state["business_success"] = None
        target_state["workflow_execution_failed"] = False
        _strip_agent_skill_runtime_state(target_state)
        if hasattr(task_logger, "set_skill_execution_evidence"):
            task_logger.set_skill_execution_evidence({})
        else:
            task_logger.skill_execution_evidence = {}

    def _resolve_skill_execution_evidence(
        source_state: dict[str, Any],
        *,
        execution_failed: bool,
    ) -> SkillExecutionEvidence:
        raw_evidence = source_state.get("skill_execution_evidence") or getattr(
            task_logger, "skill_execution_evidence", {}
        )
        planning_steps = (
            _normalize_planning_steps(getattr(task_logger, "planning_steps", []))
            or _normalize_planning_steps(source_state.get("planning_steps"))
            or _normalize_planning_steps(cache.get_planning_steps(workflow_id))
        )
        if isinstance(raw_evidence, dict) and raw_evidence:
            evidence = load_execution_evidence(
                raw_evidence,
                planning_steps=planning_steps,
                task_graph=source_state.get("task_graph"),
            )
        else:
            evidence = build_legacy_evidence(
                task_id=task_id,
                workflow_id=workflow_id,
                execution_failed=execution_failed,
                step_evidence=(source_state.get("skill_step_evidence") or {}).values(),
                planning_steps=planning_steps,
            )
        payload = evidence.model_dump(mode="json")
        source_state["skill_execution_evidence"] = payload
        source_state["business_success"] = evidence.business_success
        if hasattr(task_logger, "set_skill_execution_evidence"):
            task_logger.set_skill_execution_evidence(payload)
        return evidence

    def _complete_workflow_skill(
        source_state: dict[str, Any],
        *,
        execution_failed: bool,
    ) -> tuple[SkillExecutionEvidence, list[dict[str, Any]]]:
        evidence = _resolve_skill_execution_evidence(
            source_state,
            execution_failed=execution_failed,
        )
        events: list[dict[str, Any]] = []
        planning_steps = (
            _normalize_planning_steps(getattr(task_logger, "planning_steps", []))
            or evidence.planning_steps
            or _normalize_planning_steps(source_state.get("planning_steps"))
            or _normalize_planning_steps(cache.get_planning_steps(workflow_id))
        )

        # Agent Skills learn and account independently for each completed step.
        try:
            agent_skill_manager = get_agent_skill_manager()
            if (
                planning_steps
                and agent_skill_manager.settings.enabled
                and agent_skill_manager.settings.auto_distill_enabled
            ):
                user_id = str(source_state.get("user_id") or "")
                contract_fingerprints = (
                    getattr(task_logger, "agent_contract_fingerprints", {})
                    or source_state.get("agent_contract_fingerprints")
                    or _agent_contract_fingerprints(source_state.get("agent_cards"))
                )
                capability_bindings = (
                    getattr(task_logger, "agent_capability_bindings", {})
                    or source_state.get("agent_capability_bindings")
                    or _agent_capability_bindings(source_state.get("agent_cards"))
                )
                source_conversation = _skill_source_conversation(
                    source_state, task_logger
                )
                sliced_evidence = slice_agent_skill_evidence(
                    user_id=user_id,
                    evidence=evidence,
                    planning_steps=planning_steps,
                    task_profile=(
                        getattr(task_logger, "task_profile", {})
                        or source_state.get("task_profile")
                        or {}
                    ),
                    agent_contracts=contract_fingerprints,
                    agent_capabilities=capability_bindings,
                    source_conversations=(source_conversation,),
                )
                for step_evidence in sliced_evidence:
                    reflected = agent_skill_manager.reflect(
                        step_evidence,
                        source_conversations=(source_conversation,),
                    )
                    if not reflected.reflection_accepted:
                        events.append(
                            {
                                "event": "agent_skill_reflection_rejected",
                                "data": {
                                    "step_id": reflected.step_id,
                                    "agent_name": reflected.agent_name,
                                    "reasons": list(reflected.reflection_reasons),
                                    "confidence": reflected.reflection_confidence,
                                    "source_turn_ids": [
                                        str(item.get("turn_id") or "")
                                        for item in reflected.source_conversations
                                        if isinstance(item, Mapping)
                                    ],
                                },
                            }
                        )
                        continue
                    result = agent_skill_manager.distill(reflected)
                    events.append(
                        {
                            "event": (
                                "agent_skill_promoted"
                                if result.promoted
                                else "agent_skill_candidate"
                            ),
                            "data": {
                                "skill_id": result.card.skill_id,
                                "step_id": step_evidence.step_id,
                                "agent_name": step_evidence.agent_name,
                                "status": result.card.status.value,
                                "version": result.card.version,
                                "evidence_count": result.card.evidence_count,
                                "promotion_ready": result.decision.promotion_ready,
                                "reasons": list(result.decision.reasons),
                                "reflection": {
                                    "family": reflected.reflection_family,
                                    "confidence": reflected.reflection_confidence,
                                    "reasons": list(reflected.reflection_reasons),
                                    "model_version": reflected.reflection_model_version,
                                },
                                "source_turn_ids": [
                                    str(item.get("turn_id") or "")
                                    for item in reflected.source_conversations
                                    if isinstance(item, Mapping)
                                ],
                            },
                        }
                    )

                plan_by_id: dict[str, dict[str, Any]] = {}
                aliases: dict[str, str] = {}
                agent_to_steps: dict[str, list[str]] = {}
                for index, raw_step in enumerate(planning_steps):
                    if not isinstance(raw_step, dict):
                        continue
                    step_id = str(
                        raw_step.get("step_id")
                        or raw_step.get("subtask_id")
                        or f"step_{index + 1}"
                    )
                    plan_by_id[step_id] = raw_step
                    aliases[step_id] = step_id
                    for alias in raw_step.get("subtask_ids") or ():
                        aliases[str(alias)] = step_id
                    if raw_step.get("subtask_id"):
                        aliases[str(raw_step["subtask_id"])] = step_id
                    agent_name = str(raw_step.get("agent_name") or "")
                    if agent_name:
                        agent_to_steps.setdefault(agent_name, []).append(step_id)

                observed_by_step: dict[str, Any] = {}
                for observed in evidence.steps:
                    step_id = aliases.get(observed.step_id)
                    if step_id is None:
                        candidates = agent_to_steps.get(observed.agent_name, [])
                        if len(candidates) == 1:
                            step_id = candidates[0]
                    if step_id:
                        observed_by_step[step_id] = observed

                for step_id, skill_id in dict(
                    source_state.get("agent_skill_applied_steps") or {}
                ).items():
                    observed = observed_by_step.get(str(step_id))
                    if observed is None:
                        continue  # skipped or not reached is neutral
                    is_side_effect = (
                        str(observed.operation_mode).casefold()
                        in SIDE_EFFECT_MODES
                    )
                    success = bool(
                        observed.technical_success
                        and not observed.needs_reconciliation
                        and (
                            not is_side_effect
                            or (
                                observed.business_success is True
                                and observed.verification_status
                                == VerificationStatus.VERIFIED
                            )
                        )
                    )
                    updated = agent_skill_manager.record_outcome(
                        user_id, str(skill_id), success=success
                    )
                    if updated is None:
                        continue
                    events.append(
                        {
                            "event": (
                                "agent_skill_execution_succeeded"
                                if success
                                else "agent_skill_disabled"
                                if updated.status.value == "disabled"
                                else "agent_skill_execution_failed"
                            ),
                            "data": {
                                "skill_id": updated.skill_id,
                                "step_id": str(step_id),
                                "status": updated.status.value,
                                "consecutive_failures": updated.consecutive_failures,
                            },
                        }
                    )
        except Exception as exc:
            logger.warning("Agent Skill completion failed: %s", exc)

        # Whole-workflow Skill automation has been retired. Step/Agent Skill
        # reflection, promotion and reuse accounting above is authoritative.
        return evidence, events

    # Initialize hook system (controlled by AUTO_RECOVERY_ENABLED)
    hook_engine = None
    if AUTO_RECOVERY_ENABLED:
        initialize_hook_system()
        hook_engine = HookEngine()

    # Prepare LLM client for handlers
    llm_client = get_llm_by_type("reasoning")

    yield {
        "event": "start_of_workflow",
        "data": {"workflow_id": workflow_id, "task_id": task_id, "input": initial_state["messages"], "resume_step": resume_step},
    }

    if initial_state.get("routing_decision"):
        yield {
            "event": "routing_decision",
            "data": {
                "workflow_id": workflow_id,
                "task_id": task_id,
                "task_profile": initial_state.get("task_profile", {}),
                "routing_decision": initial_state.get("routing_decision", {}),
            },
        }

    try:
        current_node = workflow.start_node
        state = State(**initial_state)

        if state.get("workflow_mode") == "production" and workflow_id in cache.cache:
            workflow_snapshot = cache.cache.get(workflow_id) or {}
            _restore_scenario_state_from_source(state, workflow_snapshot)

        if state.get("workflow_mode") == "production" and task_id:
            try:
                checkpoint_zero = checkpoint_manager.load_checkpoint(
                    workflow_id=workflow_id,
                    task_id=task_id,
                    step=0,
                )
                if checkpoint_zero and isinstance(checkpoint_zero.state, dict):
                    _restore_scenario_state_from_source(
                        state, checkpoint_zero.state)
            except Exception:
                pass

        # Historical checkpoints may contain whole-workflow Skill references.
        # They are intentionally ignored now that only Step/Agent Skills run.
        state["workflow_skill_match"] = {}
        state["reused_skill_id"] = ""
        state["reused_skill_owner_id"] = ""

        if not state.get("task_profile"):
            original_user_query = state.get(
                "original_user_query") or state.get("USER_QUERY", "")
            if original_user_query:
                state["USER_QUERY"] = original_user_query
            task_profile = await analyze_task_context(
                original_user_query,
                {
                    "workflow_mode": state.get("workflow_mode"),
                    "risk_profile": state.get("risk_profile", "LOW"),
                },
            )
            state["task_profile"] = task_profile
            state["task_profile_reason"] = task_profile.get("reason", "")
            state["scenario_tags"] = task_profile.get("scenario_tags", [])
            state["expected_capabilities"] = task_profile.get(
                "expected_capabilities", [])
            state["task_type"] = task_profile.get("task_type", "GENERAL")
            state["business_goal"] = task_profile.get(
                "business_goal", original_user_query)
            state["data_scope"] = task_profile.get("data_scope", "targeted")
            state["operation_mode"] = task_profile.get(
                "operation_mode", "read")
            state["risk_profile"] = task_profile.get("risk_profile", "LOW")
            state["scenario_fit_cache"] = {}
            state["TASK_PROFILE_TEXT"] = json.dumps(
                task_profile, ensure_ascii=False, indent=2)
            state["SCENARIO_TAGS_TEXT"] = ", ".join(
                task_profile.get("scenario_tags", []))
            state["EXPECTED_CAPABILITIES_TEXT"] = ", ".join(
                task_profile.get("expected_capabilities", []))

        if state.get("workflow_mode") == "launch" and not should_resume:
            workflow_snapshot = cache.cache.get(workflow_id)
            if isinstance(workflow_snapshot, dict):
                workflow_snapshot["task_profile"] = state.get("task_profile") or {}
                workflow_snapshot["workflow_skill_match"] = {}
                workflow_snapshot["reused_skill_id"] = ""
                workflow_snapshot["reused_skill_owner_id"] = ""

        if state.get("workflow_mode") == "production":
            if not should_resume or not getattr(task_logger, "planning_steps", []):
                try:
                    planning_snapshot = _normalize_planning_steps(
                        cache.get_planning_steps(workflow_id)
                    )
                except (KeyError, AttributeError):
                    # Some callers enter production with an explicit state but
                    # without a populated in-memory cache (for example after a
                    # process restart). Preserve the state plan instead of
                    # failing before the legacy/scheduler gate is evaluated.
                    planning_snapshot = _normalize_planning_steps(
                        state.get("planning_steps")
                    )
                task_logger.set_workflow_snapshot(
                    planning_snapshot,
                    state.get("task_profile") or {},
                )
                state["agent_skill_bindings"] = (
                    _agent_skill_bindings_from_steps(planning_snapshot)
                )
                if hasattr(task_logger, "set_agent_contract_fingerprints"):
                    task_logger.set_agent_contract_fingerprints(
                        _agent_contract_fingerprints(state.get("agent_cards"))
                    )
                if hasattr(task_logger, "set_agent_capability_bindings"):
                    task_logger.set_agent_capability_bindings(
                        _agent_capability_bindings(state.get("agent_cards"))
                    )

        if should_resume:
            try:
                # Load checkpoint from (resume_step - 1)
                checkpoint_step = resume_step - 1
                checkpoint = checkpoint_manager.load_checkpoint(
                    workflow_id=workflow_id, task_id=task_id, step=checkpoint_step)
                if checkpoint:
                    logger.info(
                        f"Resuming workflow {workflow_id} (task {task_id}) from checkpoint step {checkpoint.step}, will execute step {resume_step}")
                    if checkpoint.next_node:
                        current_node = checkpoint.next_node
                        state = State(**checkpoint.state)
                        # Runtime capabilities are authoritative at resume time. A
                        # checkpoint restores execution state, not stale prompt,
                        # tool, MCP, or remote-resource catalogs.
                        state.update(runtime_context)
                        step_count = resume_step
                        # Clean up stale checkpoints from previous failed runs
                        # Delete checkpoints with step >= resume_step (they may be from earlier failed attempts)
                        checkpoint_manager.clean_checkpoints_from_step(
                            task_id=task_id, from_step=resume_step)
                    else:
                        logger.warning(
                            "Checkpoint missing next_node, starting from scratch")
            except Exception as e:
                logger.warning(
                    f"Could not load checkpoint for resume, starting from scratch: {e}")

        if should_resume:
            # Checkpoints may contain the previous attempt's terminal evidence.
            # Clear it after all checkpoint/scenario restoration so completion
            # aggregates only evidence produced by this resumed attempt.
            _reset_resume_evidence(state)

        # Only log workflow_start for new executions, not for resume
        if not should_resume:
            task_logger.log_workflow_start(user_query=user_query)

        # Execution-engine (Phase 3): when enabled and the state carries an explicit
        # task graph, drive the TaskGraph scheduler instead of the legacy
        # publisher/while loop. Gated OFF by default -> B1 behavior is unchanged.
        if orchestration_scheduler_enabled:
            from src.orchestration.runtime import run_scheduler_workflow, scheduler_ready
            from src.orchestration.failure_mapper import make_failure
            from src.interface.task_graph import WorkflowStatus

            # Production execution: load + verify the approved PlanSnapshot and
            # inject its TaskGraph so the real Web/API path drives the scheduler.
            # Contract/produces for the rebuild gate come from the live trusted
            # registry (same source as the planner save path); if it is
            # unavailable the gate degrades fail-closed (snapshot rejected).
            trusted_registry_ok = True
            trusted_contracts: dict[str, Any] | None = None
            trusted_produces: dict[str, list[str]] | None = None
            try:
                trusted_contracts, trusted_produces = (
                    await _trusted_registry_contract_data(state.get("user_id"))
                )
            except Exception as exc:  # noqa: BLE001 - degraded gate stays fail-closed
                logger.warning(
                    "trusted registry unavailable for snapshot verification: %s",
                    exc,
                )
                trusted_registry_ok = False
            if trusted_registry_ok:
                load_production_task_graph(
                    state,
                    execution_phase,
                    current_agent_contracts=trusted_contracts,
                    current_agent_produces=trusted_produces,
                )
            elif (
                state.get("workflow_mode") == "production"
                and execution_phase == "execution"
            ):
                # Without the trusted registry the rebuild gate has no valid
                # contract source: refuse snapshot injection outright rather
                # than compare against stale agent_cards restored from a
                # checkpoint, and drop any caller/checkpoint-supplied graph so
                # the scheduler gate fails closed downstream.
                state.pop("task_graph", None)
                logger.warning(
                    "plan snapshot rejected for %s: trusted registry unavailable",
                    state.get("workflow_id"),
                )

            if should_resume:
                # Snapshot loading can restore the prior attempt's reference;
                # a resumed attempt starts without historical Skill binding.
                _strip_agent_skill_runtime_state(state)

            ready, category, detail = scheduler_ready(state)
            if category == "no_graph" and state.get("task_graph_rejection_reason"):
                category = "invalid"
                detail = str(state["task_graph_rejection_reason"])
            if ready:
                terminal_event = None
                async for scheduler_event in run_scheduler_workflow(
                    state,
                    task_id=task_id,
                    checkpoint_manager=checkpoint_manager,
                    task_logger=task_logger,
                    hook_engine=hook_engine,
                ):
                    if scheduler_event.get("event") == "end_of_workflow":
                        terminal_event = scheduler_event
                    else:
                        yield scheduler_event
                if state.get("workflow_mode") == "production":
                    try:
                        scheduler_failed = (
                            (terminal_event or {}).get("data", {}).get("status")
                            != WorkflowStatus.SUCCEEDED.value
                        )
                        _, skill_events = _complete_workflow_skill(
                            state,
                            execution_failed=scheduler_failed,
                        )
                        for skill_event in skill_events:
                            yield skill_event
                    except Exception as exc:
                        logger.warning(
                            "Scheduler workflow skill completion failed: %s", exc
                        )
                if terminal_event is not None:
                    yield terminal_event
                return

            # Three-way gate:
            #  - planning phase without a graph  -> enter the Planner (legacy).
            #  - invalid / unknown graph (any phase), OR a missing graph in the
            #    production execution phase       -> FAIL CLOSED (never fall back
            #    to the legacy executor, so an unclassified side effect / illegal
            #    graph / missing production graph never runs an Agent).
            is_production_exec = (
                state.get("workflow_mode") == "production"
                and execution_phase == "execution"
            )
            if category == "no_graph" and not is_production_exec:
                logger.info(
                    "scheduler gate: planning phase, entering planner (%s)", detail
                )
            else:
                logger.warning(
                    "scheduler gate: fail-closed (category=%s): %s", category, detail
                )
                state["workflow_execution_failed"] = True
                gate_code = {
                    "invalid": "TASK_GRAPH_INVALID",
                    "no_graph": "TASK_GRAPH_MISSING",
                    "unknown": "OPERATION_MODE_UNCLASSIFIED",
                }.get(category, "INTERNAL_SCHEDULER_ERROR")
                failure = make_failure(gate_code)
                if hasattr(task_logger, "log_failure"):
                    task_logger.log_failure(failure.model_dump(mode="json"))
                task_logger.log_workflow_terminal(
                    WorkflowStatus.FAILED,
                    error=failure.message,
                )
                if state.get("workflow_mode") == "production":
                    try:
                        _, skill_events = _complete_workflow_skill(
                            state,
                            execution_failed=True,
                        )
                        for skill_event in skill_events:
                            yield skill_event
                    except Exception as exc:
                        logger.warning(
                            "Scheduler gate skill failure recording failed: %s", exc
                        )
                yield {
                    "event": "end_of_workflow",
                    "data": {
                        "workflow_id": workflow_id,
                        "task_id": task_id,
                        "mode": "scheduler",
                        "status": WorkflowStatus.FAILED.value,
                        "error": failure.message,
                        "reason": "scheduler_gate_fail_closed",
                        "failures": [failure.model_dump(mode="json")],
                        "failed_steps": [],
                        "blocked_steps": [],
                    },
                }
                return

        while current_node != "__end__":
            agent_name = current_node
            logger.info(f"Started node: {agent_name}")

            # Store original node name to avoid being overwritten in message loop
            original_node_name = agent_name
            # A workflow can enter agent_proxy multiple times.  Each entry must
            # have its own event identity; otherwise the frontend treats all
            # remote-agent executions as one result card.
            node_event_id = f"{workflow_id}_{original_node_name}_{step_count}"

            # For agent_proxy, get the actual sub-agent name from state["next"]
            # Note: state["next"] is set by publisher in the previous iteration
            sub_agent_name = state.get(
                "next") if agent_name == "agent_proxy" else None
            task_logger.log_agent_start(
                node_name=original_node_name, step=step_count, sub_agent_name=sub_agent_name)

            # === Hook: NODE_START ===
            if hook_engine:
                hook_ctx = HookContext(
                    task_id=task_id,
                    workflow_id=workflow_id,
                    current_node=agent_name,
                    current_step=step_count,
                    state=dict(state),
                    history=task_logger.history,
                    hook_point=HookPoint.NODE_START,
                    user_query=user_query,
                )
                hook_result = await hook_engine.process(hook_ctx)
                if hook_result.modified_state:
                    state = State(**hook_result.modified_state)

            # Display name for frontend: agent_proxy【researcher】 format
            display_name = f"{agent_name}【{sub_agent_name}】" if sub_agent_name else agent_name
            yield {
                "event": "start_of_agent",
                "data": {
                    "agent_name": display_name,
                    "agent_id": node_event_id,
                    "sub_agent_name": sub_agent_name,
                },
            }
            node_func = workflow.nodes[current_node]
            state["task_id"] = task_id
            state["current_step"] = step_count
            command = None
            async for runtime_result in _execute_node_with_runtime_events(
                state,
                node_func,
                enable_runtime_events=agent_name == "planner",
            ):
                if hasattr(runtime_result, "goto"):
                    command = runtime_result
                else:
                    yield runtime_result

            if command is None:
                raise RuntimeError(
                    f"Node '{agent_name}' did not return a command")

            if hasattr(command, "update") and command.update:
                for key, value in command.update.items():
                    if key != "messages":
                        state[key] = value

                    if key == "messages" and isinstance(value, list) and value:
                        # State ignores coordinator messages, which not only lacks contextual benefits
                        # but may also cause other unpredictable effects.
                        if agent_name != "coordinator":
                            state["messages"] += value
                        last_message = value[-1]
                        if "content" in last_message:
                            if agent_name == "coordinator":
                                content = last_message["content"]
                                if content.startswith("handover"):
                                    # mark handoff, do not send maesages
                                    global is_handoff_case
                                    is_handoff_case = True
                                    continue
                            if agent_name in ["planner", "coordinator", "agent_proxy"]:
                                content = last_message["content"]
                                if not isinstance(content, str):
                                    try:
                                        content = json.dumps(
                                            content, ensure_ascii=False)
                                    except Exception:
                                        content = str(content)
                                # Log agent message to task log
                                task_logger.log_message(
                                    node_name=original_node_name, content=content, step=step_count)
                                chunk_size = 10  # send 10 words for each chunk
                                for i in range(0, len(content), chunk_size):
                                    chunk = content[i: i + chunk_size]
                                    # Use sub_agent_name for display if available
                                    msg_display_name = f"{original_node_name}【{state.get('processing_agent_name')}】" if original_node_name == "agent_proxy" and "processing_agent_name" in state else original_node_name

                                    yield {
                                        "event": "messages",
                                        "agent_name": msg_display_name,
                                        "data": {
                                            "message_id": f"{workflow_id}_{msg_display_name}_msg_{i}",
                                            "delta": {"content": chunk},
                                        },
                                    }
                                    await asyncio.sleep(0.01)

            next_node = command.goto

            # For agent_proxy, get the actual sub-agent name from state["processing_agent_name"]
            # Use original_node_name to ensure correct identification
            sub_agent_name = state.get(
                "processing_agent_name") if original_node_name == "agent_proxy" else None
            task_logger.log_agent_end(
                node_name=original_node_name, next_node=next_node, step=step_count, sub_agent_name=sub_agent_name)

            # Save checkpoint after node execution and state update
            try:
                checkpoint_manager.save_checkpoint(
                    workflow_id=workflow_id,
                    task_id=task_id,
                    step=step_count,
                    node_name=original_node_name,
                    next_node=next_node,
                    state=state
                )
                step_count += 1
            except Exception as e:
                logger.error(
                    f"Failed to save checkpoint at step {step_count}: {e}")

            # === Hook: NODE_END ===
            if hook_engine:
                hook_ctx = HookContext(
                    task_id=task_id,
                    workflow_id=workflow_id,
                    current_node=next_node,
                    current_step=step_count,
                    state=dict(state),
                    history=task_logger.history,
                    hook_point=HookPoint.NODE_END,
                    user_query=user_query,
                    last_message=content if 'content' in dir() else None,
                    last_agent=sub_agent_name,
                )
                hook_result = await hook_engine.process(hook_ctx)
                if hook_result.modified_state:
                    state = State(**hook_result.modified_state)
                # Handle recovery from hook result
                if hook_result.resume_step is not None and hook_result.modified_state:
                    # Recovery triggered, resume workflow
                    logger.info(
                        f"Hook triggered recovery, resuming from step {hook_result.resume_step}")
                    async for event_data in _process_workflow(
                        workflow,
                        hook_result.modified_state,
                        resume_step=hook_result.resume_step,
                        task_id=task_id,
                    ):
                        yield event_data
                    return

            # Use sub_agent_name for display in end_of_agent event
            end_display_name = f"{original_node_name}【{sub_agent_name}】" if sub_agent_name else original_node_name
            yield {
                "event": "end_of_agent",
                "data": {
                    "agent_name": end_display_name,
                    "agent_id": node_event_id,
                    "sub_agent_name": sub_agent_name,
                },
            }

            current_node = next_node

        execution_failed = bool(state.get("workflow_execution_failed"))
        skill_execution_evidence = None
        if state.get("workflow_mode") == "production":
            try:
                skill_execution_evidence = _resolve_skill_execution_evidence(
                    state,
                    execution_failed=execution_failed,
                )
                execution_failed = execution_failed or (
                    not skill_execution_evidence.technical_success
                    or skill_execution_evidence.business_success is False
                )
            except Exception as exc:
                logger.warning("Could not finalize workflow execution evidence: %s", exc)
        if execution_failed:
            task_logger.log_error(
                error="One or more Agent executions returned a non-success status",
                node_name="agent_proxy",
                step=step_count,
            )
        else:
            task_logger.log_workflow_end()

        # === Hook: WORKFLOW_END ===
        if hook_engine:
            hook_ctx = HookContext(
                task_id=task_id,
                workflow_id=workflow_id,
                current_node="__end__",
                current_step=step_count,
                state=dict(state),
                history=task_logger.history,
                hook_point=HookPoint.WORKFLOW_END,
                workflow_status="failed" if execution_failed else "completed",
                user_query=user_query,
            )
            # Inject dependencies for handlers
            hook_ctx.state["__llm_client__"] = llm_client
            hook_ctx.state["__checkpoint_manager__"] = checkpoint_manager

            hook_result = await hook_engine.process(hook_ctx)

            # Handle recovery from workflow_end hook
            if hook_result.resume_step is not None and hook_result.modified_state:
                logger.info(
                    f"Workflow end hook triggered recovery, resuming from step {hook_result.resume_step}")
                async for event_data in _process_workflow(
                    workflow,
                    hook_result.modified_state,
                    resume_step=hook_result.resume_step,
                    task_id=task_id,
                ):
                    yield event_data
                return

        if state.get("workflow_mode") == "production":
            try:
                skill_execution_evidence, skill_events = _complete_workflow_skill(
                    state,
                    execution_failed=execution_failed,
                )
                for skill_event in skill_events:
                    yield skill_event
            except Exception as exc:
                logger.warning("Workflow skill completion failed: %s", exc)

        cache.dump(workflow_id, initial_state["workflow_mode"])

        legacy_end_data = {
            "workflow_id": workflow_id,
            "task_id": task_id,
            "status": "FAILED" if execution_failed else "SUCCEEDED",
            "messages": [{
                "role": "user",
                "content": "workflow failed" if execution_failed else "workflow completed",
            }],
            "skill_execution_evidence": (
                skill_execution_evidence.model_dump(mode="json")
                if skill_execution_evidence is not None
                else None
            ),
        }
        yield {
            "event": "end_of_workflow",
            "data": legacy_end_data,
        }

    except PermissionDeniedError as e:
        logger.warning("S-ABAC permission denied: %s", str(e))
        payload = dict(e.payload)
        task_logger.log_error(
            error=f"S-ABAC permission denied: {payload.get('policy_result', {}).get('reason', str(e))}",
            node_name=current_node or "security",
            step=step_count,
        )
        failure_state = state if "state" in locals() else initial_state
        failure_state["workflow_execution_failed"] = True
        try:
            _resolve_skill_execution_evidence(
                failure_state,
                execution_failed=True,
            )
        except Exception as evidence_exc:
            logger.warning("Could not persist permission failure evidence: %s", evidence_exc)
        yield {
            "event": "permission_denied",
            "data": {
                "workflow_id": workflow_id,
                "task_id": task_id,
                "error": str(e),
                "policy_result": payload.get("policy_result", {}),
                "subject": payload.get("subject", {}),
                "object": payload.get("object", {}),
                "action": payload.get("action", {}),
                "scenario": payload.get("scenario", {}),
                "scenario_fit_result": (
                    payload.get("scenario", {})
                    .get("task_scenario", {})
                    .get("scenario_fit_result", {})
                ),
            },
        }

    except Exception as e:
        import traceback

        traceback.print_exc()
        logger.error("Error in Agent workflow: %s", str(e))
        task_logger.log_error(error=str(e), node_name=current_node or "system", step=step_count)
        failure_state = state if "state" in locals() else initial_state
        failure_state["workflow_execution_failed"] = True
        try:
            _resolve_skill_execution_evidence(
                failure_state,
                execution_failed=True,
            )
        except Exception as evidence_exc:
            logger.warning("Could not persist workflow failure evidence: %s", evidence_exc)
        # === Hook: ERROR ===
        if hook_engine:
            hook_ctx = HookContext(
                task_id=task_id,
                workflow_id=workflow_id,
                current_node=current_node,
                current_step=step_count,
                state=dict(state) if 'state' in dir() else {},
                history=task_logger.history,
                error=e,
                error_message=str(e),
                hook_point=HookPoint.ERROR,
                workflow_status="failed",
                user_query=user_query,
            )
            # Inject dependencies for handlers
            hook_ctx.state["__llm_client__"] = llm_client
            hook_ctx.state["__checkpoint_manager__"] = checkpoint_manager

            hook_result = await hook_engine.process(hook_ctx)

            # Handle recovery from error hook
            if hook_result.resume_step is not None and hook_result.modified_state:
                logger.info(
                    f"Error hook triggered recovery, resuming from step {hook_result.resume_step}")
                async for event_data in _process_workflow(
                    workflow,
                    hook_result.modified_state,
                    resume_step=hook_result.resume_step,
                    task_id=task_id,
                ):
                    yield event_data
                return

        from src.orchestration.failure_mapper import (
            make_failure,
            public_execution_reason,
        )

        public_reason = public_execution_reason(e) or (
            f"工作流节点 {current_node or 'system'} 发生未分类内部错误；"
            f"请使用任务编号 {task_id} 查看服务端日志。"
        )
        public_failure = make_failure(
            "INTERNAL_STEP_ERROR",
            step_id=str(current_node or "system"),
            message=public_reason,
            action=f"请使用任务编号 {task_id} 查看服务端日志并修复具体原因后重试。",
        )
        yield {
            "event": "error",
            "data": {
                "workflow_id": workflow_id,
                "task_id": task_id,
                "error": public_reason,
                "failure": public_failure.model_dump(mode="json"),
            },
        }
