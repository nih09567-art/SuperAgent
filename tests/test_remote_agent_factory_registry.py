from __future__ import annotations

import json
from pathlib import Path

from remote_agents.factory import AgentFactory
from src.contracts.agent_contract import AgentContract, DataContractRef


def test_remote_agent_factory_implements_every_advertised_agent() -> None:
    registry_path = Path(__file__).resolve().parents[1] / "mock_remote_registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8-sig"))
    advertised = {
        item["name"]
        for item in registry.get("resources", [])
        if item.get("type") == "agent"
    }

    assert advertised == set(AgentFactory._agents)


def test_calendar_agent_is_available_to_remote_execution_server() -> None:
    agent = AgentFactory.get_agent("RemoteHRCalendarAgent")

    assert agent.name == "RemoteHRCalendarAgent"


def test_five_dynamic_agent_contracts_match_registry() -> None:
    registry_path = Path(__file__).resolve().parents[1] / "mock_remote_registry.json"
    entries = json.loads(registry_path.read_text(encoding="utf-8-sig"))["resources"]
    metadata_by_name = {
        item["name"]: item["metadata"]
        for item in entries
        if item.get("type") == "agent"
    }
    names = {
        "RemoteHRAssistantAgent",
        "RemoteKnowledgeAgent",
        "RemoteOfficeAssistantAgent",
        "RemoteReportAgent",
        "RemoteEmailDispatchAgent",
    }

    for name in names:
        metadata = metadata_by_name[name]
        optional_requires = set(metadata.get("optional_requires", []))
        optional_produces = set(metadata.get("optional_produces", []))
        registry_contract = AgentContract(
            contract_version=metadata["contract_version"],
            requires=[
                DataContractRef(
                    name=logical_name,
                    schema_ref=metadata["input_schema_refs"][logical_name],
                    required=logical_name not in optional_requires,
                )
                for logical_name in metadata.get("requires", [])
            ],
            produces=[
                DataContractRef(
                    name=logical_name,
                    schema_ref=metadata["output_schema_refs"][logical_name],
                    required=logical_name not in optional_produces,
                )
                for logical_name in metadata.get("produces", [])
            ],
        )
        agent_contract = AgentFactory.get_agent(name).planning_contract

        assert agent_contract is not None
        assert agent_contract.model_dump() == registry_contract.model_dump(), name
