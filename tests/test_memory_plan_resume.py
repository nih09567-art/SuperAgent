import asyncio
import json

from src.memory import MemoryManager, MemorySettings, MemoryStore
from src.orchestration.plan_snapshot import load_plan_snapshot, save_plan_snapshot
from src.orchestration.plan_to_task_graph import plan_to_task_graph
from src.robust.checkpoint import CheckpointManager


def test_compaction_and_restart_preserve_exact_plan_version_and_current_step(tmp_path):
    workflow_id = "wf-memory-resume"
    task_id = "task-memory-resume"
    steps = [
        {"agent_name": "AgentA", "title": "Collect"},
        {
            "agent_name": "AgentB",
            "title": "Publish",
            "inputs": [
                {
                    "parameter_name": "source",
                    "source_step": "AgentA",
                    "source_output": "result",
                }
            ],
        },
    ]
    graph = plan_to_task_graph(steps, task_id=workflow_id, subject="alice")
    snapshot = save_plan_snapshot(
        workflow_id=workflow_id,
        user_id="alice",
        planning_steps=steps,
        task_graph=graph.model_dump(),
        base_dir=tmp_path / "plans",
    )
    state = {
        "workflow_id": workflow_id,
        "user_id": "alice",
        "planning_steps": steps,
        "task_graph": graph.model_dump(),
        "plan_version": snapshot["schema_version"],
        "plan_hash": snapshot["plan_hash"],
        "current_step": 1,
        "completed_steps": ["step_1"],
        "step_results": {"step_1": {"status": "SUCCEEDED", "outputs": {}}},
    }
    checkpoints = CheckpointManager(tmp_path / "checkpoints")
    checkpoints.save_checkpoint(
        workflow_id=workflow_id,
        task_id=task_id,
        step=1,
        node_name="scheduler",
        next_node="scheduler",
        state=state,
    )

    settings = MemorySettings(
        enabled=True,
        long_term_enabled=False,
        auto_compact_enabled=True,
        trigger_tokens=1,
        target_tokens=80,
        max_context_tokens=1000,
        reserved_output_tokens=100,
        store_path=tmp_path / "memory.sqlite3",
    )
    manager = MemoryManager(settings=settings, store=MemoryStore(settings.store_path))
    asyncio.run(
        manager.prepare_context(
            user_id="alice",
            session_id="thread",
            incoming_messages=[
                    {
                        "role": "user",
                        "content": "run the approved plan " * 80,
                        "message_id": "u1",
                    }
            ],
            attachments={"current_plan": steps, "extra": {"plan_status": "active"}},
        )
    )
    asyncio.run(
        manager.record_assistant_outputs(
            user_id="alice",
            session_id="thread",
                outputs=[
                    {
                        "agent_name": "assistant",
                        "content": "execution started " * 100,
                        "message_id": "a1",
                    }
                ],
        )
    )
    asyncio.run(manager.compact_session(user_id="alice", session_id="thread"))

    restored_snapshot = load_plan_snapshot(workflow_id, base_dir=tmp_path / "plans")
    restored_checkpoint = CheckpointManager(tmp_path / "checkpoints").load_checkpoint(
        task_id=task_id,
        step=1,
    )
    compaction = MemoryStore(settings.store_path).latest_compaction("alice", "thread")

    assert restored_snapshot["schema_version"] == snapshot["schema_version"]
    assert restored_snapshot["plan_hash"] == snapshot["plan_hash"]
    assert restored_checkpoint.state["plan_version"] == snapshot["schema_version"]
    assert restored_checkpoint.state["plan_hash"] == snapshot["plan_hash"]
    assert restored_checkpoint.state["current_step"] == 1
    assert json.dumps(restored_checkpoint.state["task_graph"], sort_keys=True) == json.dumps(
        graph.model_dump(), sort_keys=True
    )
    assert compaction.attachments.current_plan is None
    assert snapshot["plan_hash"] not in compaction.summary
