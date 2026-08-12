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


def test_confirmed_profile_is_projected_to_the_current_step(monkeypatch):
    captured = {}

    async def _fake(**kwargs):
        captured.update(kwargs)
        return (
            _Profile(),
            [SimpleNamespace(agent_id="RemoteReportAgent")],
            _decision(
                decision="DISPATCH",
                selected="RemoteReportAgent",
                candidates=[("RemoteReportAgent", 0.9)],
            ),
        )

    monkeypatch.setattr(orchestrator_pkg, "make_routing_decision", _fake)
    approved_profile = {
        "task_id": "approved-task",
        "intent": "risk_analysis",
        "intents": ["risk_analysis", "report_generation", "message_or_email_send"],
        "task_type": "COMPOSITE",
        "action": "send",
        "missing_fields": ["recipient"],
        "needs_clarification": True,
        "clarification_questions": ["recipient?"],
        "subtasks": [
            {"id": "subtask_1", "intent": "risk_analysis", "action": "read"},
            {
                "id": "subtask_2",
                "intent": "report_generation",
                "task_type": "DOCUMENT",
                "action": "generate",
                "expected_capabilities": ["Document"],
                "scenario_tags": ["reporting"],
                "data_scope": ["document.generated"],
            },
            {"id": "subtask_3", "intent": "message_or_email_send", "action": "send"},
        ],
    }
    step = TaskStep(
        step_id="report",
        subtask_ids=["subtask_2"],
        intents=["report_generation"],
        operation_mode="generate",
        preferred_resource_id="RemoteReportAgent",
        expected_outputs=["report.markdown"],
    )

    result = asyncio.run(
        MainAgentRoutingProvider().decide(
            step,
            user_query="global composite request",
            task_id="t1",
            workflow_id="wf1",
            agents=(),
            authorized_agent_ids={"RemoteReportAgent"},
            metadata={
                "scenario_tags": ["global"],
                "approved_task_profile": approved_profile,
            },
        )
    )

    override = captured["task_profile_override"]
    assert result.selected_agent == "RemoteReportAgent"
    assert override["task_id"] == "approved-task"
    assert override["intent"] == "report_generation"
    assert override["intents"] == ["report_generation"]
    assert override["action"] == "generate"
    assert override["subtasks"] == [approved_profile["subtasks"][1]]
    assert override["missing_fields"] == []
    assert override["needs_clarification"] is False
    assert "approved_task_profile" not in captured["metadata"]
