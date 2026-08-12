from src.contracts import AgentCard, AgentContract, DataContractRef, TaskProfile
from src.orchestrator.department_router import route_task
from src.orchestration.contract_planning import contract_closure


def _research_report_profile() -> TaskProfile:
    return TaskProfile(
        task_id="research-report",
        intent="information_research",
        sub_intents=["information_research", "report_generation"],
        action="generate",
        expected_capabilities=["Research", "Document"],
        required_business_data=["research.markdown"],
        expected_deliverables=["report.markdown"],
        is_composite=True,
        subtasks=[
            {
                "id": "subtask_1",
                "intent": "information_research",
                "expected_capabilities": ["Research"],
                "scenario_tags": ["knowledge_lookup"],
            },
            {
                "id": "subtask_2",
                "intent": "report_generation",
                "expected_capabilities": ["Document"],
                "scenario_tags": ["report_generation"],
                "depends_on": ["subtask_1"],
            },
        ],
    )


def _agent_cards() -> list[AgentCard]:
    return [
        AgentCard(
            agent_id="researcher",
            name="researcher",
            capabilities=["Research"],
            intents=["information_research"],
            supported_actions=["read", "query"],
            accepted_data_scopes=["general"],
            planning_eligible=True,
            planning_agent_contract=AgentContract(
                contract_version="1.0",
                produces=[
                    DataContractRef(
                        name="research.markdown",
                        schema_ref="markdown_text_result@v1",
                    )
                ],
            ),
        ),
        AgentCard(
            agent_id="reporter",
            name="reporter",
            capabilities=["Document"],
            intents=["report_generation"],
            supported_actions=["generate"],
            accepted_data_scopes=["general"],
            risk_ceiling="MEDIUM",
            planning_eligible=True,
            planning_agent_contract=AgentContract(
                contract_version="1.0",
                produces=[
                    DataContractRef(
                        name="report.markdown",
                        schema_ref="markdown_text_result@v1",
                    )
                ],
            ),
        ),
    ]


def test_guest_is_rejected_when_report_agent_is_permission_blocked() -> None:
    decision = route_task(
        _research_report_profile(),
        _agent_cards(),
        authorized_agent_ids={"researcher"},
        workflow_id="guest:wf",
    )

    assert decision.decision == "REJECT"
    assert decision.selected_agent is None
    assert decision.reason_codes == [
        "COMPOSITE_ROUTE_INCOMPLETE",
        "PERMISSION_DENIED",
    ]
    assert any(
        item.agent_id == "reporter" and item.reason_code == "PERMISSION_DENIED"
        for item in decision.excluded_agents
    )


def test_researcher_user_dispatches_when_both_agents_are_authorized() -> None:
    decision = route_task(
        _research_report_profile(),
        _agent_cards(),
        authorized_agent_ids={"researcher", "reporter"},
        workflow_id="researcher_user:wf",
    )

    assert decision.decision == "DISPATCH"
    assert decision.reason_codes == ["COMPOSITE_ROUTE_COVERED"]
    assert decision.confidence == 1.0
    assert {item.agent_id for item in decision.candidate_agents} == {
        "researcher",
        "reporter",
    }


def test_contract_closure_selects_both_agents_only_for_authorized_researcher() -> None:
    profile = _research_report_profile()
    cards = _agent_cards()

    authorized = contract_closure(
        profile.to_legacy_scenario(),
        cards,
        authorized_agent_ids={"researcher", "reporter"},
    )
    guest = contract_closure(
        profile.to_legacy_scenario(),
        cards,
        authorized_agent_ids={"researcher"},
    )

    assert authorized.complete is True
    assert authorized.selected_agent_ids == ("researcher", "reporter")
    assert guest.complete is False
    assert guest.selected_agent_ids == ("researcher",)
    assert guest.missing_outputs == ("report.markdown",)
