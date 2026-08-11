import asyncio
from types import SimpleNamespace

import src.workflow.process as process
from src.interface.agent import WorkMode
from src.robust.hooks.base import Action, ActionType, HookContext, HookPoint
from src.robust.hooks.handlers.failure_attribution import FailureAttributionHandler
from langgraph.types import Command


def test_auto_recovery_hooks_are_disabled_for_planning(monkeypatch):
    monkeypatch.setattr(process, "AUTO_RECOVERY_ENABLED", True)

    assert process._auto_recovery_hooks_enabled(
        {"workflow_mode": WorkMode.LAUNCH}, "re_planning"
    ) is False
    assert process._auto_recovery_hooks_enabled(
        {"workflow_mode": WorkMode.PRODUCTION}, "execution"
    ) is True


def test_hook_recovery_is_bounded_and_rejects_checkpoint_zero():
    invalid = SimpleNamespace(resume_step=0, modified_state={"value": 1})
    assert process._bounded_hook_recovery({}, invalid, source="test") is None

    valid = SimpleNamespace(resume_step=1, modified_state={"value": 1})
    recovery = process._bounded_hook_recovery({}, valid, source="test")
    assert recovery == ({"value": 1, "__auto_recovery_attempted": True}, 1)
    assert (
        process._bounded_hook_recovery(
            {"__auto_recovery_attempted": True}, valid, source="test"
        )
        is None
    )


def test_launch_workflow_does_not_run_execution_recovery_hooks(tmp_path, monkeypatch):
    import src.robust.checkpoint as checkpoint_module
    import src.robust.task_logger as task_logger_module

    monkeypatch.setattr(process, "AUTO_RECOVERY_ENABLED", True)
    monkeypatch.setattr(process, "orchestration_scheduler_enabled", False)
    monkeypatch.setattr(process, "get_llm_by_type", lambda _kind: None)
    monkeypatch.setattr(process.cache, "dump", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        process,
        "initialize_hook_system",
        lambda: (_ for _ in ()).throw(AssertionError("planning enabled recovery hooks")),
    )
    monkeypatch.setattr(
        task_logger_module, "checkpoints_dir", tmp_path / "checkpoints"
    )
    monkeypatch.setattr(checkpoint_module, "checkpoints_dir", tmp_path / "checkpoints")

    async def finish_plan(_state):
        return Command(update={"planning_steps": []}, goto="__end__")

    workflow = SimpleNamespace(start_node="planner", nodes={"planner": finish_plan})
    state = {
        "user_id": "admin",
        "workflow_id": "workflow-planning",
        "workflow_mode": WorkMode.LAUNCH,
        "USER_QUERY": "remember project ALPHA-731",
        "original_user_query": "remember project ALPHA-731",
        "messages": [{"role": "user", "content": "remember project ALPHA-731"}],
        "task_profile": {"task_type": "GENERAL"},
        "workflow_execution_failed": False,
    }

    async def collect_events():
        return [
            event
            async for event in process._process_workflow(
                workflow,
                state,
                task_id="planning-task",
                execution_phase="re_planning",
            )
        ]

    events = asyncio.run(collect_events())
    assert events[-1]["event"] == "end_of_workflow"
    assert events[-1]["data"]["status"] == "SUCCEEDED"


def test_failure_handler_resumes_after_retained_checkpoint(monkeypatch):
    handler = FailureAttributionHandler()
    attribution = SimpleNamespace(
        is_succeed=False,
        mistake_step=1,
        mistake_node="planner",
    )
    rollback_target = SimpleNamespace(
        rollback_step=0,
        checkpoint=SimpleNamespace(node_name="coordinator"),
    )
    injection_result = SimpleNamespace(
        patched_state={"workflow_mode": "production"},
        injection_text="retry with correction",
        target_node="planner",
    )

    async def fake_attribute(_self, _task_id):
        return attribution

    def fake_find(_self, **_kwargs):
        return rollback_target

    async def fake_apply(_self, **_kwargs):
        return injection_result

    monkeypatch.setattr(
        "src.robust.hooks.handlers.failure_attribution.FailureAttributor.attribute",
        fake_attribute,
    )
    monkeypatch.setattr(
        "src.robust.hooks.handlers.failure_attribution.RollbackController.find_rollback_point",
        fake_find,
    )
    monkeypatch.setattr(
        "src.robust.hooks.handlers.failure_attribution.CorrectionInjector.apply",
        fake_apply,
    )
    monkeypatch.setattr(
        "src.robust.hooks.handlers.failure_attribution.RollbackController.save_patched_checkpoint",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.robust.hooks.handlers.failure_attribution.TaskLogger.load",
        lambda _task_id: None,
    )

    result = asyncio.run(
        handler.handle(
            HookContext(
                task_id="task-1",
                workflow_id="workflow-1",
                hook_point=HookPoint.ERROR,
                state={
                    "__llm_client__": object(),
                    "__checkpoint_manager__": object(),
                },
            ),
            Action(type=ActionType.ROLLBACK, target_step=1),
        )
    )

    assert result.resume_step == 1
    assert result.metadata["rollback_step"] == 0
    assert result.metadata["resume_step"] == 1
