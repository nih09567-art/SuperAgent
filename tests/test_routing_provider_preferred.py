"""MainAgentRoutingProvider must honor a step's ``preferred_resource_id``.

Regression for the multi-agent misrouting bug: the candidate scoring runs
against the GLOBAL user_query (identical for every step), so without honoring
the per-step preferred agent, every step of a composite plan collapses onto the
same top-scoring agent (e.g. an HR-query step and a knowledge-lookup step both
routed to the knowledge agent -> two concurrent calls contend and one times
out). The provider must narrow to the plan's per-step agent WHEN it is an
authorized/capable candidate, and must never bypass a REJECT/CLARIFY verdict.
"""

import asyncio
from types import SimpleNamespace

import src.orchestrator as orchestrator_pkg
from src.contracts import ExcludedAgent, RoutingCandidate, RoutingDecision
from src.interface.task_graph import TaskStep
from src.orchestration.providers import MainAgentRoutingProvider


class _Profile:
    clarification_questions: list = []


def _decision(*, decision, selected, candidates):
    return RoutingDecision(
        decision_id="d1",
        task_id="t1",
        selected_agent=selected,
        candidate_agents=[
            RoutingCandidate(agent_id=a, score=s) for a, s in candidates
        ],
        decision=decision,
        confidence=0.9,
        reason_codes=["HIGH_CONFIDENCE_ROUTE"],
        trace_id="trace",
    )


def _patch(monkeypatch, decision, *, cards=()):
    async def _fake(**_kwargs):
        return _Profile(), list(cards), decision

    monkeypatch.setattr(orchestrator_pkg, "make_routing_decision", _fake)


def _decide(step):
    provider = MainAgentRoutingProvider()
    return asyncio.run(
        provider.decide(
            step,
            user_query="需要查询王强档案、年假制度并汇总",
            task_id="t1",
            workflow_id="wf1",
            agents=(),
            authorized_agent_ids={"RemoteHRAssistantAgent", "RemoteKnowledgeAgent"},
        )
    )


def test_honor_preferred_when_it_is_a_candidate(monkeypatch):
    # Global-query scoring picked Knowledge for everything, but this step's plan
    # assigned HR; HR is an authorized candidate -> honor it.
    _patch(
        monkeypatch,
        _decision(
            decision="DISPATCH",
            selected="RemoteKnowledgeAgent",
            candidates=[("RemoteKnowledgeAgent", 0.9), ("RemoteHRAssistantAgent", 0.7)],
        ),
    )
    step = TaskStep(step_id="step_1", preferred_resource_id="RemoteHRAssistantAgent")
    result = _decide(step)
    assert result.selected_agent == "RemoteHRAssistantAgent"
    assert "HONOR_PREFERRED_RESOURCE" in result.reason_codes


def test_keep_routing_pick_when_preferred_not_a_candidate(monkeypatch):
    # Preferred agent did not pass the permission/capability gate -> do NOT
    # override; keep the routing verdict (fail safe, never bypass the gate).
    _patch(
        monkeypatch,
        _decision(
            decision="DISPATCH",
            selected="RemoteKnowledgeAgent",
            candidates=[("RemoteKnowledgeAgent", 0.9)],
        ),
    )
    step = TaskStep(step_id="step_1", preferred_resource_id="RemoteHRAssistantAgent")
    result = _decide(step)
    assert result.selected_agent == "RemoteKnowledgeAgent"
    assert "HONOR_PREFERRED_RESOURCE" not in result.reason_codes


def test_honor_registered_authorized_preferred_when_top_k_omits_it(monkeypatch):
    # Composite workflows can have more eligible Agents than route_task's
    # candidate top_k. Omission is not an exclusion and must not collapse a
    # later plan step onto the global query's top-scoring Agent.
    decision = _decision(
        decision="DISPATCH",
        selected="RemoteHRAssistantAgent",
        candidates=[("RemoteHRAssistantAgent", 0.9)],
    )
    _patch(
        monkeypatch,
        decision,
        cards=[
            SimpleNamespace(agent_id="RemoteHRAssistantAgent"),
            SimpleNamespace(agent_id="RemoteReportAgent"),
        ],
    )
    provider = MainAgentRoutingProvider()
    result = asyncio.run(
        provider.decide(
            TaskStep(
                step_id="report",
                preferred_resource_id="RemoteReportAgent",
            ),
            user_query="查询员工、政策、记录并生成报告",
            task_id="t1",
            workflow_id="wf1",
            agents=(),
            authorized_agent_ids={
                "RemoteHRAssistantAgent",
                "RemoteReportAgent",
            },
        )
    )

    assert result.selected_agent == "RemoteReportAgent"
    assert "HONOR_PREFERRED_RESOURCE" in result.reason_codes


def test_registered_but_excluded_preferred_is_never_honored(monkeypatch):
    decision = _decision(
        decision="DISPATCH",
        selected="RemoteKnowledgeAgent",
        candidates=[("RemoteKnowledgeAgent", 0.9)],
    ).model_copy(
        update={
            "excluded_agents": [
                ExcludedAgent(
                    agent_id="RemoteReportAgent",
                    reason="capability mismatch",
                    reason_code="CAPABILITY_MISMATCH",
                )
            ]
        }
    )
    _patch(
        monkeypatch,
        decision,
        cards=[SimpleNamespace(agent_id="RemoteReportAgent")],
    )
    provider = MainAgentRoutingProvider()
    result = asyncio.run(
        provider.decide(
            TaskStep(
                step_id="report",
                preferred_resource_id="RemoteReportAgent",
            ),
            user_query="查询员工、政策、记录并生成报告",
            task_id="t1",
            workflow_id="wf1",
            agents=(),
            authorized_agent_ids={
                "RemoteKnowledgeAgent",
                "RemoteReportAgent",
            },
        )
    )

    assert result.selected_agent == "RemoteKnowledgeAgent"
    assert "HONOR_PREFERRED_RESOURCE" not in result.reason_codes


def test_reject_is_never_overridden_by_preferred(monkeypatch):
    # A non-DISPATCH verdict must clear the agent even if the plan preferred one.
    _patch(
        monkeypatch,
        _decision(
            decision="REJECT",
            selected=None,
            candidates=[("RemoteHRAssistantAgent", 0.7)],
        ),
    )
    step = TaskStep(step_id="step_1", preferred_resource_id="RemoteHRAssistantAgent")
    result = _decide(step)
    assert result.selected_agent is None
    assert result.decision == "REJECT"


def test_no_preferred_keeps_routing_pick(monkeypatch):
    _patch(
        monkeypatch,
        _decision(
            decision="DISPATCH",
            selected="RemoteKnowledgeAgent",
            candidates=[("RemoteKnowledgeAgent", 0.9), ("RemoteHRAssistantAgent", 0.7)],
        ),
    )
    step = TaskStep(step_id="step_2")  # no preferred_resource_id
    result = _decide(step)
    assert result.selected_agent == "RemoteKnowledgeAgent"


def test_trusted_profile_routes_each_step_without_reprofiling_global_query(
    monkeypatch,
):
    async def _must_not_reprofile(**_kwargs):
        raise AssertionError("execution routing must reuse the trusted TaskProfile")

    monkeypatch.setattr(
        orchestrator_pkg,
        "make_routing_decision",
        _must_not_reprofile,
    )
    trusted_profile = {
        "task_id": "t1",
        "entities": {"employee_name": "王强", "recipient": "hr@example.test"},
        "missing_fields": [],
        "needs_clarification": False,
        "confidence": 0.93,
        "subtasks": [
            {
                "id": "subtask_2",
                "intent": "knowledge_lookup",
                "task_type": "KNOWLEDGE",
                "goal": "查询年假政策",
                "action": "read",
                "expected_capabilities": ["Knowledge"],
                "scenario_tags": ["knowledge_lookup"],
                "data_scope": ["knowledge.internal"],
                "required_business_data": ["policy.info"],
                "expected_deliverables": [],
            },
            {
                "id": "subtask_4",
                "intent": "report_generation",
                "task_type": "DOCUMENT",
                "goal": "生成报告",
                "action": "generate",
                "expected_capabilities": ["Document"],
                "scenario_tags": ["reporting"],
                "data_scope": ["document.generated"],
                "required_business_data": [],
                "expected_deliverables": ["report.markdown"],
            },
        ],
    }
    agents = [
        SimpleNamespace(agent_name="RemoteKnowledgeAgent"),
        SimpleNamespace(agent_name="RemoteReportAgent"),
        SimpleNamespace(agent_name="RemoteEmailDispatchAgent"),
    ]
    provider = MainAgentRoutingProvider()

    async def _run():
        common = {
            "user_query": "查询王强的工龄和年假政策，生成报告，发送给邮箱 hr@example.test",
            "task_id": "t1",
            "workflow_id": "wf1",
            "agents": agents,
            "authorized_agent_ids": {
                "RemoteKnowledgeAgent",
                "RemoteReportAgent",
                "RemoteEmailDispatchAgent",
            },
            "task_profile": trusted_profile,
        }
        knowledge = await provider.decide(
            TaskStep(
                step_id="step_2",
                subtask_ids=["subtask_2"],
                operation_mode="read",
                preferred_resource_id="RemoteKnowledgeAgent",
            ),
            **common,
        )
        report = await provider.decide(
            TaskStep(
                step_id="step_4",
                subtask_ids=["subtask_4"],
                operation_mode="generate",
                preferred_resource_id="RemoteReportAgent",
            ),
            **common,
        )
        return knowledge, report

    knowledge, report = asyncio.run(_run())

    assert knowledge.decision == "DISPATCH"
    assert knowledge.selected_agent == "RemoteKnowledgeAgent"
    assert report.decision == "DISPATCH"
    assert report.selected_agent == "RemoteReportAgent"


def test_invalid_trusted_profile_binding_fails_closed():
    provider = MainAgentRoutingProvider()
    result = asyncio.run(
        provider.decide(
            TaskStep(step_id="step_2", subtask_ids=["missing"]),
            user_query="查询年假政策",
            task_id="t1",
            workflow_id="wf1",
            agents=[SimpleNamespace(agent_name="RemoteKnowledgeAgent")],
            authorized_agent_ids={"RemoteKnowledgeAgent"},
            task_profile={
                "task_id": "t1",
                "subtasks": [{"id": "subtask_2", "intent": "knowledge_lookup"}],
            },
        )
    )

    assert result.decision == "ROUTING_ERROR"
    assert result.selected_agent is None
    assert result.reason_codes[0].startswith("TRUSTED_STEP_PROFILE_INVALID")
