import asyncio

import src.orchestrator.main_agent as main_agent


def test_routing_uses_approved_task_profile_without_reprofiling(monkeypatch):
    async def _unexpected_profile(*_args, **_kwargs):
        raise AssertionError("production execution must not re-profile the approved task")

    monkeypatch.setattr(main_agent, "profile_task", _unexpected_profile)

    approved = {
        "task_id": "planning-task",
        "intent": "message_or_email_send",
        "intents": ["message_or_email_send"],
        "task_type": "COMMUNICATION",
        "business_goal": "发送已确认报告",
        "action": "send",
        "operation_mode": "send",
        "data_scope": "communication.recipient,communication.content",
        "scenario_tags": ["notification_send"],
        "expected_capabilities": ["Communication"],
        "risk_profile": "HIGH",
        "subtasks": [
            {
                "id": "subtask_1",
                "intent": "message_or_email_send",
                "action": "send",
            }
        ],
        "is_composite": False,
    }

    profile, cards, decision = asyncio.run(
        main_agent.make_routing_decision(
            user_query="执行期间语义服务可能给出不同结果",
            task_id="execution-task",
            workflow_id="wf-approved-profile",
            agents=(),
            authorized_agent_ids=set(),
            task_profile_override=approved,
        )
    )

    assert profile.task_id == "planning-task"
    assert profile.action == "send"
    assert profile.data_scope == [
        "communication.recipient",
        "communication.content",
    ]
    assert [item["id"] for item in profile.subtasks] == ["subtask_1"]
    assert cards == []
    assert decision.decision == "REJECT"
