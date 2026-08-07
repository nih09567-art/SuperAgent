from __future__ import annotations

import asyncio
import json
import threading
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.skills.agent_skill import (
    AgentSkillEvidence,
    AgentSkillManager,
    AgentSkillSettings,
    AgentSkillStatus,
    AgentSkillStore,
    bind_agent_skills,
    set_agent_skill_manager,
    slice_agent_skill_evidence,
)
from src.skills.execution_evidence import (
    SkillExecutionEvidence,
    StepExecutionEvidence,
    VerificationStatus,
)
from src.skills.reflection import SkillReflection
from src.skills.execution_trace import build_execution_trace, make_trace_event


class _ReflectionModel:
    def __init__(self, *, reusable: bool = True, malformed: bool = False):
        self.reusable = reusable
        self.malformed = malformed

    def invoke(self, _prompt):
        if self.malformed:
            return type("Response", (), {"content": "not-json", "tool_calls": []})()
        return type(
            "Response",
            (),
            {
                "content": {
                    "is_reusable": self.reusable,
                    "workflow_family": "routine_metrics_lookup",
                    "normalized_procedure": {
                        "steps": ["resolve_scope", "read_metrics", "return_typed_result"]
                    },
                    "confidence": 0.95,
                    "reasons": ["stable office procedure"],
                    "risk_notes": [],
                    "model_version": "test-reflector-v1",
                },
                "tool_calls": [],
            },
        )()


class _AggregateRejectModel(_ReflectionModel):
    def invoke(self, prompt):
        if "TRACES:" in prompt:
            self.reusable = False
        return super().invoke(prompt)


def _manager(tmp_path, **overrides) -> AgentSkillManager:
    values = {
        "enabled": True,
        "reuse_enabled": True,
        "auto_distill_enabled": True,
        "allow_side_effect_reuse": False,
        "match_threshold": 0.70,
        "match_margin": 0.08,
        "promotion_success_threshold": 2,
        "failure_disable_threshold": 2,
        "store_path": tmp_path / "agent-skills.sqlite3",
    }
    values.update(overrides)
    settings = AgentSkillSettings(**values)
    return AgentSkillManager(
        settings=settings,
        store=AgentSkillStore(settings.store_path),
        reflection=SkillReflection(_ReflectionModel()),
    )


def _read_evidence(
    task_id: str,
    *,
    user_id: str = "alice",
    contract: str = "reader-v1",
    step_id: str = "read_metrics",
) -> AgentSkillEvidence:
    return AgentSkillEvidence(
        evidence_id=f"evidence-{task_id}-{step_id}",
        user_id=user_id,
        task_id=task_id,
        workflow_id=f"wf-{task_id}",
        step_id=step_id,
        agent_name="MetricsReaderAgent",
        contract_fingerprint=contract,
        capability="metrics_retrieval",
        step_intent="retrieve_metrics",
        operation_mode="read",
        risk_level="LOW",
        data_scopes=("department_metrics",),
        input_bindings=(),
        expected_outputs=("metrics",),
        expected_schema_ref="schema://metrics/v1",
        verification_contract={"required": False, "method": "technical_result"},
        retry_policy={"max_attempts": 2, "fallback": "original_step"},
        execution_guidance=(
            "Execute capability metrics_retrieval for the current planned step "
            "using only current request data and bound upstream Artifacts."
        ),
        dependency_step_ids=(),
        dependency_success=True,
        technical_success=True,
        business_success=None,
        verification_status=VerificationStatus.NOT_REQUIRED,
        schema_valid=True,
        output_accepted=True,
        needs_reconciliation=False,
        artifact_refs=({"artifact_id": "artifact-1", "version": 1},),
        source_conversations=(
            {
                "turn_id": f"turn-{task_id}",
                "user_messages": [
                    {
                        "message_id": f"message-{task_id}",
                        "content": "查询本月部门指标",
                    }
                ],
                "assistant_messages": [
                    {"message_id": f"answer-{task_id}", "content": "指标查询完成"}
                ],
            },
        ),
        reflection_accepted=True,
        reflection_family="routine_metrics_lookup",
        reflection_procedure={"steps": ["resolve_scope", "read_metrics"]},
        reflection_confidence=0.95,
        reflection_reasons=("stable office procedure",),
        reflection_model_version="test-reflector-v1",
    )


def test_agent_skill_evidence_forbids_raw_execution_payloads():
    payload = _read_evidence("task-1").model_dump(mode="json")
    payload["raw_payload"] = {"department": "sales", "secret": "value"}

    with pytest.raises(ValidationError):
        AgentSkillEvidence.model_validate(payload)


def test_execution_trace_retains_observable_payload_and_redacts_secrets():
    event = make_trace_event(
        kind="remote_agent_response",
        request={
            "tool_name": "salary_lookup",
            "arguments": {"employee_id": "E-100", "auth": "Bearer abcdefghijklmnop"},
            "chain_of_thought": "private planner scratchpad",
        },
        response={"result": {"employee_id": "E-100", "amount": 42000}},
        agent_name="HrAgent",
        step_id="salary_lookup",
        status="succeeded",
    )

    serialized = str(event)
    assert "salary_lookup" in serialized
    assert "42000" in serialized
    assert "abcdefghijklmnop" not in serialized
    assert "private planner scratchpad" not in serialized
    assert event["payload_hash"]
    assert event["redaction_applied"] is True
    assert set(event["redaction_flags"]) == {
        "hidden_reasoning_omitted",
        "secret_redacted",
    }


def test_skill_persists_full_trace_in_audit_store_and_only_reference_in_card(
    tmp_path,
):
    manager = _manager(tmp_path)
    event = make_trace_event(
        kind="remote_agent_response",
        request={"tool_name": "metrics_lookup", "arguments": {"month": "2026-08"}},
        response={"result": {"revenue": 12345, "region": "east"}},
        agent_name="MetricsReaderAgent",
        step_id="read_metrics",
        status="succeeded",
    )
    trace = build_execution_trace(
        runtime_events=[event],
        planning_steps=[{"step_id": "read_metrics", "agent_name": "MetricsReaderAgent"}],
        task_profile={"task_type": "metrics_lookup"},
        task_id="task-audit",
        workflow_id="wf-audit",
    )
    evidence = _read_evidence("task-audit").model_copy(
        update={"execution_trace": trace}
    )

    result = manager.distill(evidence)
    stored = manager.store.list_evidence("alice")[0]
    trace_ref = stored.execution_trace
    audit_path = manager.store.path.parent / trace_ref["audit_ref"]

    assert audit_path.exists()
    audit_payload = json.loads(audit_path.read_text(encoding="utf-8"))
    remote_event = next(
        item
        for item in audit_payload["events"]
        if item["kind"] == "remote_agent_response"
    )
    assert remote_event["response"]["result"]["revenue"] == 12345
    assert trace_ref["audit_only"] is True
    assert trace_ref["trace_hash"] == audit_payload["trace_hash"]
    assert "events" not in trace_ref
    assert result.card.execution_traces == [trace_ref]
    assert (
        manager.store.read_execution_trace(
            user_id="alice", trace_id=trace_ref["trace_id"]
        )
        == audit_payload
    )
    assert (
        manager.store.read_execution_trace(
            user_id="bob", trace_id=trace_ref["trace_id"]
        )
        is None
    )


def test_reflection_prompt_excludes_audit_trace_body(tmp_path):
    class _PromptCaptureModel(_ReflectionModel):
        def __init__(self):
            super().__init__()
            self.prompt = ""

        def invoke(self, prompt):
            self.prompt = prompt
            return super().invoke(prompt)

    model = _PromptCaptureModel()
    settings = AgentSkillSettings(
        enabled=True,
        reuse_enabled=True,
        auto_distill_enabled=True,
        store_path=tmp_path / "prompt.sqlite3",
    )
    manager = AgentSkillManager(
        settings=settings,
        store=AgentSkillStore(settings.store_path),
        reflection=SkillReflection(model),
    )
    evidence = _read_evidence("task-prompt").model_copy(
        update={
            "execution_trace": build_execution_trace(
                runtime_events=[
                    make_trace_event(
                        kind="remote_agent_response",
                        response={"private_marker": "DO_NOT_INJECT_AUDIT_PAYLOAD"},
                    )
                ],
                task_id="task-prompt",
            )
        }
    )

    reflected = manager.reflect(
        evidence, source_conversations=evidence.source_conversations
    )

    assert reflected.reflection_accepted is True
    assert "DO_NOT_INJECT_AUDIT_PAYLOAD" not in model.prompt


def test_agent_skill_reflection_rejection_is_fail_closed(tmp_path):
    manager = _manager(tmp_path)
    rejected = manager.reflect(
        _read_evidence("task-rejected"),
        source_conversations=_read_evidence("task-rejected").source_conversations,
    )
    assert rejected.reflection_accepted is True

    rejecting_manager = AgentSkillManager(
        settings=manager.settings,
        store=AgentSkillStore(tmp_path / "reject.sqlite3"),
        reflection=SkillReflection(_ReflectionModel(reusable=False)),
    )
    rejected = rejecting_manager.reflect(_read_evidence("task-one"))
    assert rejected.reflection_accepted is False
    with pytest.raises(ValueError, match="accepted LLM reflection"):
        rejecting_manager.distill(rejected)


def test_agent_skill_reflection_invalid_output_is_fail_closed(tmp_path):
    settings = AgentSkillSettings(
        enabled=True,
        reuse_enabled=True,
        auto_distill_enabled=True,
        store_path=tmp_path / "invalid.sqlite3",
    )
    manager = AgentSkillManager(
        settings=settings,
        store=AgentSkillStore(settings.store_path),
        reflection=SkillReflection(_ReflectionModel(malformed=True)),
    )
    rejected = manager.reflect(_read_evidence("task-invalid"))
    assert rejected.reflection_accepted is False
    assert "reflection_invalid" in rejected.reflection_reasons[0]


def test_reflection_timeouts_use_bounded_shared_workers():
    release = threading.Event()
    lock = threading.Lock()
    started = 0

    class BlockingModel:
        def invoke(self, _prompt):
            nonlocal started
            with lock:
                started += 1
            release.wait(timeout=1.0)
            return _ReflectionModel().invoke(_prompt)

    reflection = SkillReflection(BlockingModel(), timeout_seconds=0.01)
    try:
        results = [reflection.reflect_trace({}) for _ in range(6)]
    finally:
        release.set()

    assert all(result.valid is False for result in results)
    assert all(result.reasons == ("reflection_timeout",) for result in results)
    assert 1 <= started <= 2


def test_aggregate_reflection_can_keep_two_traces_as_candidate(tmp_path):
    settings = AgentSkillSettings(
        enabled=True,
        reuse_enabled=True,
        auto_distill_enabled=True,
        store_path=tmp_path / "aggregate.sqlite3",
    )
    manager = AgentSkillManager(
        settings=settings,
        store=AgentSkillStore(settings.store_path),
        reflection=SkillReflection(_AggregateRejectModel()),
    )
    first = manager.distill(_read_evidence("task-a"))
    second = manager.distill(_read_evidence("task-b"))
    assert first.card.status == AgentSkillStatus.CANDIDATE
    assert second.card.status == AgentSkillStatus.CANDIDATE
    assert second.card.aggregate_reflection_accepted is False


def test_agent_skill_candidate_promotes_from_two_distinct_tasks_and_is_idempotent(
    tmp_path,
):
    manager = _manager(tmp_path, promotion_success_threshold=1)

    first = manager.distill(_read_evidence("task-1"))
    duplicate = manager.distill(_read_evidence("task-1"))
    second = manager.distill(_read_evidence("task-2"))

    assert first.card.status == AgentSkillStatus.CANDIDATE
    assert duplicate.card.skill_id == first.card.skill_id
    assert duplicate.card.evidence_count == 1
    assert second.card.status == AgentSkillStatus.ACTIVE
    assert second.card.evidence_count == 2
    assert second.card.provenance.source_task_ids == ["task-1", "task-2"]
    assert second.card.aggregate_reflection_accepted is True
    assert {item["turn_id"] for item in second.card.source_conversations} == {
        "turn-task-1",
        "turn-task-2",
    }
    assert len(manager.store.list_evidence("alice")) == 2


def test_agent_skill_store_is_user_scoped_and_versions_contract_drift(tmp_path):
    manager = _manager(tmp_path)
    alice_v1 = manager.distill(_read_evidence("task-a1")).card
    alice_v2 = manager.distill(
        _read_evidence("task-a2", contract="reader-v2")
    ).card
    bob = manager.distill(_read_evidence("task-b1", user_id="bob")).card

    assert {item.skill_id for item in manager.store.list("alice")} == {
        alice_v1.skill_id,
        alice_v2.skill_id,
    }
    assert [item.skill_id for item in manager.store.list("bob")] == [bob.skill_id]
    assert alice_v1.family_signature == alice_v2.family_signature
    assert alice_v1.signature != alice_v2.signature
    assert {alice_v1.version, alice_v2.version} == {1, 2}


def test_stable_new_contract_version_can_replace_previous_active_version(tmp_path):
    manager = _manager(tmp_path)
    v1 = manager.distill(_read_evidence("task-v1-1")).card
    v1 = manager.distill(_read_evidence("task-v1-2")).card
    assert v1.status == AgentSkillStatus.ACTIVE

    v2 = manager.distill(_read_evidence("task-v2-1", contract="reader-v2")).card
    v2 = manager.distill(_read_evidence("task-v2-2", contract="reader-v2")).card

    assert v2.version == 2
    assert v2.status == AgentSkillStatus.ACTIVE
    assert manager.store.get("alice", v1.skill_id).status == AgentSkillStatus.DISABLED


def test_record_outcome_disables_only_the_failing_agent_skill(tmp_path):
    manager = _manager(tmp_path)
    first = manager.distill(_read_evidence("task-1")).card
    second = manager.distill(
        AgentSkillEvidence(
            **{
                **_read_evidence("task-2", step_id="read_customers").model_dump(),
                "evidence_id": "evidence-task-2-read-customers",
                "agent_name": "CustomerReaderAgent",
                "contract_fingerprint": "customer-v1",
                "capability": "customer_retrieval",
                "expected_outputs": ("customers",),
                "expected_schema_ref": "schema://customers/v1",
            }
        )
    ).card
    manager.store.activate("alice", first.skill_id)
    manager.store.activate("alice", second.skill_id)

    manager.record_outcome("alice", first.skill_id, success=False)
    failed = manager.record_outcome("alice", first.skill_id, success=False)

    assert failed is not None
    assert failed.status == AgentSkillStatus.DISABLED
    assert manager.store.get("alice", second.skill_id).status == AgentSkillStatus.ACTIVE


def test_step_slicer_learns_verified_read_from_later_failed_workflow():
    evidence = SkillExecutionEvidence(
        task_id="task-1",
        workflow_id="wf-1",
        workflow_status="FAILED",
        technical_success=False,
        step_coverage=1.0,
        steps=[
            StepExecutionEvidence(
                step_id="read_metrics",
                agent_name="MetricsReaderAgent",
                operation_mode="read",
                technical_success=True,
                verification_status=VerificationStatus.NOT_REQUIRED,
                schema_valid=True,
                artifact_refs=[{"artifact_id": "artifact-1", "version": 1}],
            ),
            StepExecutionEvidence(
                step_id="send_report",
                agent_name="NotificationAgent",
                operation_mode="send",
                technical_success=False,
                verification_status=VerificationStatus.FAILED,
                error="delivery failed",
            ),
        ],
    )
    planning_steps = [
        {
            "step_id": "read_metrics",
            "agent_name": "MetricsReaderAgent",
            "capability": "metrics_retrieval",
            "intents": ["retrieve_metrics"],
            "operation_mode": "read",
            "risk_level": "LOW",
            "expected_outputs": ["metrics"],
            "expected_schema_ref": "schema://metrics/v1",
        },
        {
            "step_id": "send_report",
            "agent_name": "NotificationAgent",
            "capability": "notification_delivery",
            "operation_mode": "send",
            "risk_level": "HIGH",
            "depends_on": ["read_metrics"],
        },
    ]

    sliced = slice_agent_skill_evidence(
        user_id="alice",
        evidence=evidence,
        planning_steps=planning_steps,
        task_profile={"data_scope": ["department_metrics"]},
        agent_contracts={
            "MetricsReaderAgent": "reader-v1",
            "NotificationAgent": "notify-v1",
        },
        agent_capabilities={
            "MetricsReaderAgent": ["metrics_retrieval"],
            "NotificationAgent": ["notification_delivery"],
        },
    )

    assert [item.step_id for item in sliced] == ["read_metrics"]
    serialized = str(sliced[0].model_dump(mode="json"))
    assert "delivery failed" not in serialized
    assert "department_metrics" in serialized


def test_step_slicer_requires_successful_dependencies_and_verified_side_effects():
    evidence = SkillExecutionEvidence(
        task_id="task-1",
        workflow_id="wf-1",
        workflow_status="PARTIAL_FAILED",
        technical_success=False,
        steps=[
            StepExecutionEvidence(
                step_id="source",
                agent_name="SourceAgent",
                operation_mode="read",
                technical_success=False,
                verification_status=VerificationStatus.FAILED,
            ),
            StepExecutionEvidence(
                step_id="send",
                agent_name="NotificationAgent",
                operation_mode="send",
                technical_success=True,
                business_success=True,
                verification_status=VerificationStatus.VERIFIED,
                verification_method="platform_receipt",
                idempotency_key="idem-1",
            ),
        ],
    )
    planning_steps = [
        {"step_id": "source", "agent_name": "SourceAgent", "operation_mode": "read"},
        {
            "step_id": "send",
            "agent_name": "NotificationAgent",
            "capability": "notification_delivery",
            "operation_mode": "send",
            "depends_on": ["source"],
            "verification_contract": {
                "required": True,
                "method": "platform_receipt",
            },
        },
    ]

    assert slice_agent_skill_evidence(
        user_id="alice",
        evidence=evidence,
        planning_steps=planning_steps,
        task_profile={},
        agent_contracts={"SourceAgent": "v1", "NotificationAgent": "v1"},
    ) == []


def test_side_effect_without_business_identity_never_promotes(tmp_path):
    manager = _manager(tmp_path, allow_side_effect_reuse=True)
    base = _read_evidence("task-1").model_dump()
    base.update(
        {
            "evidence_id": "send-task-1",
            "step_id": "send",
            "agent_name": "NotificationAgent",
            "contract_fingerprint": "notify-v1",
            "capability": "notification_delivery",
            "step_intent": "send_notification",
            "operation_mode": "send",
            "risk_level": "HIGH",
            "expected_outputs": (),
            "expected_schema_ref": None,
            "verification_contract": {
                "required": True,
                "method": "platform_receipt",
            },
            "technical_success": True,
            "business_success": True,
            "verification_status": VerificationStatus.VERIFIED,
            "schema_valid": None,
            "output_accepted": True,
            "idempotency_key_present": False,
            "external_operation_id_present": False,
        }
    )
    first = manager.distill(AgentSkillEvidence(**base))
    base["evidence_id"] = "send-task-2"
    base["task_id"] = "task-2"
    second = manager.distill(AgentSkillEvidence(**base))

    assert first.card.status == AgentSkillStatus.CANDIDATE
    assert second.card.status == AgentSkillStatus.CANDIDATE
    assert "business_identity_missing" in second.decision.reasons


def test_binding_is_partial_additive_and_contract_safe(tmp_path):
    manager = _manager(tmp_path)
    manager.distill(_read_evidence("task-1"))
    active = manager.distill(_read_evidence("task-2")).card
    original = [
        {
            "step_id": "read_metrics",
            "agent_name": "MetricsReaderAgent",
            "capability": "metrics_retrieval",
            "intents": ["retrieve_metrics"],
            "description": "Read metrics for the current request",
            "operation_mode": "read",
            "risk_level": "LOW",
            "expected_outputs": ["metrics"],
            "expected_schema_ref": "schema://metrics/v1",
            "verification_contract": {
                "required": False,
                "method": "technical_result",
            },
        },
        {
            "step_id": "write_report",
            "agent_name": "ReportWriterAgent",
            "description": "Write the report",
            "operation_mode": "write",
            "risk_level": "MEDIUM",
        },
    ]
    snapshot = deepcopy(original)

    result = bind_agent_skills(
        manager,
        user_id="alice",
        planning_steps=original,
        task_profile={"data_scope": ["department_metrics"]},
        agent_contracts={
            "MetricsReaderAgent": "reader-v1",
            "ReportWriterAgent": "writer-v1",
        },
        agent_capabilities={"MetricsReaderAgent": ["metrics_retrieval"]},
    )

    assert result.bindings == {"read_metrics": active.skill_id}
    assert result.steps[0]["agent_skill_binding"]["skill_id"] == active.skill_id
    assert result.steps[0]["description"] == snapshot[0]["description"]
    assert result.steps[0]["agent_name"] == snapshot[0]["agent_name"]
    assert result.steps[0].get("depends_on", []) == []
    assert "agent_skill_guidance" not in result.steps[0]
    assert result.steps[1] == snapshot[1]
    assert original == snapshot

    resolved = manager.resolve_binding(
        user_id="alice",
        binding=result.steps[0]["agent_skill_binding"],
        agent_name="MetricsReaderAgent",
        contract_fingerprint="reader-v1",
        operation_mode="read",
        step=result.steps[0],
        task_profile={"data_scope": ["department_metrics"]},
        agent_capabilities={"MetricsReaderAgent": ["metrics_retrieval"]},
    )
    assert resolved is not None
    assert resolved.execution_guidance == active.recipe.execution_guidance
    assert manager.resolve_binding(
        user_id="alice",
        binding={
            **result.steps[0]["agent_skill_binding"],
            "signature": "forged-signature",
        },
        agent_name="MetricsReaderAgent",
        contract_fingerprint="reader-v1",
        operation_mode="read",
        step=result.steps[0],
        task_profile={"data_scope": ["department_metrics"]},
        agent_capabilities={"MetricsReaderAgent": ["metrics_retrieval"]},
    ) is None
    assert manager.resolve_binding(
        user_id="alice",
        binding=result.steps[0]["agent_skill_binding"],
        agent_name="MetricsReaderAgent",
        contract_fingerprint="reader-v1",
        operation_mode="read",
        step={**result.steps[0], "intents": ["retrieve_customer_records"]},
        task_profile={"data_scope": ["department_metrics"]},
        agent_capabilities={"MetricsReaderAgent": ["metrics_retrieval"]},
    ) is None

    drift = bind_agent_skills(
        manager,
        user_id="alice",
        planning_steps=original,
        task_profile={"data_scope": ["department_metrics"]},
        agent_contracts={"MetricsReaderAgent": "reader-v2"},
        agent_capabilities={"MetricsReaderAgent": ["metrics_retrieval"]},
    )
    assert drift.bindings == {}
    assert drift.steps == original


def test_binding_never_reads_another_users_active_skill(tmp_path):
    manager = _manager(tmp_path)
    manager.distill(_read_evidence("task-b1", user_id="bob"))
    manager.distill(_read_evidence("task-b2", user_id="bob"))
    step = {
        "step_id": "read_metrics",
        "agent_name": "MetricsReaderAgent",
        "capability": "metrics_retrieval",
        "intents": ["retrieve_metrics"],
        "operation_mode": "read",
        "risk_level": "LOW",
        "expected_outputs": ["metrics"],
        "expected_schema_ref": "schema://metrics/v1",
        "verification_contract": {"required": False, "method": "technical_result"},
    }

    result = bind_agent_skills(
        manager,
        user_id="alice",
        planning_steps=[step],
        task_profile={"data_scope": ["department_metrics"]},
        agent_contracts={"MetricsReaderAgent": "reader-v1"},
        agent_capabilities={"MetricsReaderAgent": ["metrics_retrieval"]},
    )

    assert result.bindings == {}
    assert result.steps == [step]


def test_planner_post_validation_binding_emits_partial_match(tmp_path, monkeypatch):
    import src.service.env as env
    import src.workflow.coor_task as coor_task

    manager = _manager(tmp_path)
    manager.distill(_read_evidence("task-1"))
    active = manager.distill(_read_evidence("task-2")).card
    events = []
    validation_calls = []

    async def validate(steps, user_id):
        validation_calls.append((deepcopy(steps), user_id))
        return True, []

    async def emit(event):
        events.append(event)

    monkeypatch.setattr(env, "AGENT_SKILL_ENABLED", True)
    monkeypatch.setattr(env, "AGENT_SKILL_REUSE_ENABLED", True)
    monkeypatch.setattr(coor_task, "_validate_plan_data_flow", validate)
    monkeypatch.setattr(
        coor_task, "_validate_plan_against_task_profile", lambda _steps, _state: []
    )
    steps = [
        {
            "step_id": "read_metrics",
            "agent_name": "MetricsReaderAgent",
            "capability": "metrics_retrieval",
            "intents": ["retrieve_metrics"],
            "description": "Read current metrics",
            "operation_mode": "read",
            "risk_level": "LOW",
            "expected_outputs": ["metrics"],
            "expected_schema_ref": "schema://metrics/v1",
            "verification_contract": {
                "required": False,
                "method": "technical_result",
            },
        },
        {
            "step_id": "write_report",
            "agent_name": "ReportWriterAgent",
            "description": "Write report",
            "operation_mode": "write",
        },
    ]
    binding_state = {
        "user_id": "alice",
        "task_profile": {"data_scope": ["department_metrics"]},
        "agent_contract_fingerprints": {
            "MetricsReaderAgent": "reader-v1",
            "ReportWriterAgent": "writer-v1",
        },
        "agent_capability_bindings": {
            "MetricsReaderAgent": ["metrics_retrieval"]
        },
        "runtime_event_handler": emit,
    }
    set_agent_skill_manager(manager)
    try:
        bound, bindings = asyncio.run(
            coor_task._bind_validated_agent_skills(
                steps,
                binding_state,
            )
        )
        opted_out_steps, opted_out_bindings = asyncio.run(
            coor_task._bind_validated_agent_skills(
                steps,
                {**binding_state, "skill_reuse_enabled": False},
            )
        )
    finally:
        set_agent_skill_manager(None)

    assert bindings == {"read_metrics": active.skill_id}
    assert bound[0]["agent_skill_binding"]["skill_id"] == active.skill_id
    assert bound[1] == steps[1]
    assert len(validation_calls) == 1
    assert events[-1]["event"] == "agent_skill_matched"
    assert opted_out_steps == steps
    assert opted_out_bindings == {}


def test_task_graph_preserves_only_agent_skill_reference():
    from src.orchestration.plan_to_task_graph import plan_to_task_graph

    binding = {
        "skill_id": "askill-1",
        "version": 2,
        "signature": "signature-1",
        "contract_fingerprint": "contract-1",
    }
    graph = plan_to_task_graph(
        [
            {
                "step_id": "read_metrics",
                "agent_name": "MetricsReaderAgent",
                "operation_mode": "read",
                "agent_skill_binding": binding,
                "agent_skill_guidance": "caller-controlled text must be dropped",
            }
        ],
        task_id="task-1",
    )

    assert graph.steps[0].agent_skill_binding == binding
    assert not hasattr(graph.steps[0], "agent_skill_guidance")


def test_resume_strips_previous_attempt_agent_skill_state():
    from src.workflow.process import _strip_agent_skill_runtime_state

    state = {
        "workflow_id": "alice:wf",
        "user_id": "alice",
        "planning_steps": [
            {
                "step_id": "read_metrics",
                "agent_name": "MetricsReaderAgent",
                "agent_skill_binding": {"skill_id": "askill-1"},
            }
        ],
        "task_graph": {
            "steps": [
                {
                    "step_id": "read_metrics",
                    "agent_skill_binding": {"skill_id": "askill-1"},
                }
            ]
        },
        "agent_skill_bindings": {"read_metrics": "askill-1"},
        "agent_skill_applied_steps": {"read_metrics": "askill-1"},
    }

    _strip_agent_skill_runtime_state(state)

    assert "agent_skill_binding" not in state["planning_steps"][0]
    assert "agent_skill_binding" not in state["task_graph"]["steps"][0]
    assert state["agent_skill_bindings"] == {}
    assert state["agent_skill_applied_steps"] == {}


def test_agent_skill_admin_api_is_user_scoped(tmp_path, monkeypatch):
    import src.service.web_app as web_app

    manager = _manager(tmp_path)
    candidate = manager.distill(_read_evidence("task-api")).card
    set_agent_skill_manager(manager)
    monkeypatch.setattr(web_app, "WORKFLOW_SKILL_ADMIN_API_KEY", "test-key")
    headers = {"Authorization": "Bearer test-key"}
    try:
        with TestClient(web_app.app) as client:
            listed = client.get(
                "/api/agent-skills",
                params={"user_id": "alice"},
                headers=headers,
            )
            assert listed.status_code == 200
            assert listed.json()[0]["skill_id"] == candidate.skill_id

            evidence = client.get(
                "/api/agent-skills/evidence",
                params={"user_id": "alice"},
                headers=headers,
            )
            assert evidence.status_code == 200
            assert evidence.json()[0]["task_id"] == "task-api"

            activated = client.post(
                f"/api/agent-skills/{candidate.skill_id}/activate",
                json={"user_id": "alice"},
                headers=headers,
            )
            assert activated.status_code == 200
            assert activated.json()["skill"]["status"] == "active"

            forbidden = client.get(
                f"/api/agent-skills/{candidate.skill_id}",
                params={"user_id": "bob"},
                headers=headers,
            )
            assert forbidden.status_code == 404

            disabled = client.post(
                f"/api/agent-skills/{candidate.skill_id}/disable",
                json={"user_id": "alice"},
                headers=headers,
            )
            assert disabled.status_code == 200
            assert disabled.json()["event"] == "agent_skill_disabled"
    finally:
        set_agent_skill_manager(None)
