import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List

from src.contracts.agent_contract import AgentContract, DataContractRef
from src.contracts.agent_schema_catalog import AGENT_SCHEMA_CATALOG
from src.interface.agent import Agent, AgentSource, LLMType
from src.interface.mcp import Tool
from src.manager.registry.resource_registry import ResourceRegistry, ResourceSpec
from src.orchestration.output_contracts import OUTPUT_SCHEMAS

logger = logging.getLogger(__name__)


def _safe_attr(obj: Any, name: str, default=None):
    return getattr(obj, name, default)


async def sync_local_resources(
    agent_registry,
    tool_registry,
    skills_manager,
    resource_registry: ResourceRegistry,
) -> int:
    """Sync local agents/tools/skills into ResourceRegistry."""

    count = 0

    # Agents
    agents = await agent_registry.list() if hasattr(agent_registry, "list") else []
    for agent in agents:
        spec = ResourceSpec(
            type="agent",
            name=_safe_attr(agent, "agent_name", ""),
            server_id="local",
            version="1.0.0",
            endpoint=_safe_attr(agent, "endpoint", None),
            protocol=_safe_attr(agent, "source", "local"),
            metadata={
                "description": _safe_attr(agent, "description", ""),
                "llm_type": str(_safe_attr(agent, "llm_type", LLMType.BASIC)),
                "prompt": _safe_attr(agent, "prompt", ""),
            },
        )
        await resource_registry.register(spec, persist=False)
        count += 1

    # Tools
    if tool_registry is not None and hasattr(tool_registry, "list_global_tools"):
        tool_metas = await tool_registry.list_global_tools()
        for meta in tool_metas:
            tool_obj = _safe_attr(meta, "tool", None)
            tool_name = _safe_attr(meta, "identifier", None)
            name = _safe_attr(tool_obj, "name", None) or _safe_attr(tool_name, "name", "")
            if not name:
                continue
            spec = ResourceSpec(
                type="tool",
                name=name,
                server_id="local",
                version=_safe_attr(meta, "version", "1.0.0"),
                protocol="local",
                metadata={
                    "description": _safe_attr(meta, "description", "") or _safe_attr(tool_obj, "description", ""),
                },
            )
            await resource_registry.register(spec, persist=False)
            count += 1

    # Skills
    skills = skills_manager.list_skills() if skills_manager else []
    for skill in skills:
        name = _safe_attr(skill, "name", "")
        if not name:
            continue
        spec = ResourceSpec(
            type="skill",
            name=name,
            server_id="local",
            version="1.0.0",
            protocol="local",
            metadata={
                "description": _safe_attr(skill, "description", ""),
                "category": _safe_attr(skill, "category", ""),
            },
        )
        await resource_registry.register(spec, persist=False)
        count += 1

    return count


async def sync_remote_agents(resource_registry: ResourceRegistry, agent_registry, default_user_id: str = "share") -> int:
    """Create in-memory Agent entries from remote ResourceSpec entries."""

    count = 0
    remote_specs = await resource_registry.list(type="agent")

    for spec in remote_specs:
        if spec.server_id == "local":
            continue
        if not spec.endpoint:
            continue

        agent_name = spec.name
        existing = await agent_registry.get(agent_name)
        if existing is not None:
            continue

        metadata = dict(spec.metadata or {})
        description = metadata.get("description", f"Remote agent from {spec.server_id}")
        llm_type = metadata.get("llm_type", LLMType.BASIC)
        prompt = metadata.get("prompt", "remote agent")
        tools = metadata.get("selected_tools", [])
        selected_tools: List[Tool] = []
        for item in tools:
            if isinstance(item, Tool):
                selected_tools.append(item)
            elif isinstance(item, dict):
                selected_tools.append(
                    Tool(
                        name=item.get("name", ""),
                        description=item.get("description", ""),
                        parameters=item.get("parameters")
                    )
                )

        # Extract the explicit v1 contract while retaining legacy string lists.
        contract_version = metadata.get("contract_version")
        requires = metadata.get("requires", [])
        produces = metadata.get("produces", [])
        # Legacy logical names kept for pre-contract planner dependencies
        # (e.g. employee.id/employee.name). They join Agent.produces but are
        # deliberately excluded from the strict contract below.
        legacy_produces = metadata.get("legacy_produces", [])
        optional_requires = {
            str(name) for name in metadata.get("optional_requires", []) or []
        }
        optional_produces = {
            str(name) for name in metadata.get("optional_produces", []) or []
        }
        input_schema_refs = metadata.get("input_schema_refs", {})
        output_schema_refs = metadata.get("output_schema_refs", {})
        parameter_mapping = metadata.get("parameter_mapping", {})
        contract_activation = metadata.get("contract_activation", "runtime")
        planning_selected_tools = metadata.get("planning_selected_tools", [])
        agent_contract = None
        if contract_version:
            missing_input_refs = [
                name for name in requires if name not in input_schema_refs
            ]
            missing_output_refs = [
                name for name in produces if name not in output_schema_refs
            ]
            unknown_optional_requires = optional_requires - set(requires)
            unknown_optional_produces = optional_produces - set(produces)
            referenced_schemas = set(input_schema_refs.values()) | set(
                output_schema_refs.values()
            )
            known_schemas = set(AGENT_SCHEMA_CATALOG) | set(OUTPUT_SCHEMAS)
            unknown_schema_refs = sorted(referenced_schemas - known_schemas)
            selected_tool_names = {tool.name for tool in selected_tools}
            unknown_planning_tools = sorted(
                set(planning_selected_tools) - selected_tool_names
            )
            if (
                missing_input_refs
                or missing_output_refs
                or unknown_optional_requires
                or unknown_optional_produces
                or unknown_schema_refs
                or unknown_planning_tools
                or contract_activation not in {"runtime", "planning"}
            ):
                # Fail closed for this Agent only: refuse to register it, but
                # never let one bad registry entry break the whole batch.
                logger.error(
                    "Invalid Agent contract for %s: missing schema refs for "
                    "requires=%s, produces=%s; unknown optional requires=%s, "
                    "produces=%s; unknown schemas=%s; unknown planning "
                    "tools=%s; activation=%r; "
                    "agent not registered",
                    agent_name,
                    missing_input_refs,
                    missing_output_refs,
                    sorted(unknown_optional_requires),
                    sorted(unknown_optional_produces),
                    unknown_schema_refs,
                    unknown_planning_tools,
                    contract_activation,
                )
                continue
            agent_contract = AgentContract(
                contract_version=contract_version,
                requires=[
                    DataContractRef(
                        name=name,
                        schema_ref=input_schema_refs[name],
                        required=name not in optional_requires,
                    )
                    for name in requires
                ],
                produces=[
                    DataContractRef(
                        name=name,
                        schema_ref=output_schema_refs[name],
                        required=name not in optional_produces,
                    )
                    for name in produces
                ],
            )

        runtime_requires = (
            requires
            if contract_activation == "runtime"
            else metadata.get("legacy_requires", [])
        )
        runtime_produces = (
            produces
            if contract_activation == "runtime"
            else []
        )
        agent = Agent(
            user_id=default_user_id,
            agent_name=agent_name,
            nick_name=agent_name,
            description=description,
            llm_type=llm_type,
            selected_tools=selected_tools,
            prompt=prompt,
            source=AgentSource.REMOTE,
            endpoint=spec.endpoint,
            api_key=(spec.auth or {}).get("api_key"),
            requires=runtime_requires,
            produces=runtime_produces
            + [name for name in legacy_produces if name not in runtime_produces],
            contract_version=contract_version,
            input_schema_refs=input_schema_refs,
            output_schema_refs=output_schema_refs,
            agent_contract=(
                agent_contract if contract_activation == "runtime" else None
            ),
            planning_agent_contract=agent_contract,
            planning_selected_tools=planning_selected_tools,
            parameter_mapping=parameter_mapping,
        )

        await agent_registry.register(agent, persist=False)
        count += 1

    return count
