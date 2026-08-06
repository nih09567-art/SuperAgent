from typing import Dict, List, AsyncGenerator, Optional
from dotenv import load_dotenv
import json
import re
from datetime import datetime, timezone
from pydantic import BaseModel

load_dotenv()
import logging
from src.interface.agent import *
from src.workflow.process import run_agent_workflow
from src.manager import agent_manager 
from src.manager.agents import NotFoundAgentError
from src.service.session import SessionManager
from src.interface.agent import RemoveAgentRequest
from src.workflow.cache import workflow_cache
from src.service.env import USE_MCP_TOOLS
from src.manager.mcp import get_mcp_hot_reload_manager
from src.manager.registry import ToolRegistry
from src.memory import (
    CurrentRequestOverflowError,
    PlanContextOverflowError,
    get_memory_manager,
)
from src.service.env import MEMORY_ENABLED
from src.memory.utils import redact_secrets
from src.orchestrator.context_resolver import resolve_conversation_request
from src.orchestrator.intent_recognition import memory_lookup_keys
from src.orchestration.plan_snapshot import plan_hash
from src.llm.agents import AGENT_LLM_MAP


logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)
session_manager = SessionManager()
_MAX_VISIBLE_RESULT_CHARS = 8000
_VISIBLE_RESULT_CAPTURE_CHARS = _MAX_VISIBLE_RESULT_CHARS + 512


def _bounded_visible_result(value: object) -> str:
    rendered = redact_secrets(str(value).strip())
    if len(rendered) <= _MAX_VISIBLE_RESULT_CHARS:
        return rendered
    return rendered[: _MAX_VISIBLE_RESULT_CHARS - 3] + "..."


def _active_compaction_model_type(request: AgentRequest) -> str:
    if request.deep_thinking_mode:
        return "reasoning"
    stage = "coordinator" if request.workmode == "production" else "planner"
    return AGENT_LLM_MAP[stage]


def _compact_execution_result(data: Dict) -> str:
    """Persist a bounded governed outcome, never raw child-Agent streams."""

    if not isinstance(data, dict):
        return ""
    result = data.get("result")
    if isinstance(result, str):
        rendered = result
    elif result is not None:
        rendered = json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)
    else:
        payload = {
            "workflow_status": data.get("workflow_status") or data.get("status"),
            "available": bool(data.get("available")),
            "unavailable_artifacts": data.get("unavailable_artifacts") or [],
        }
        rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return _bounded_visible_result(rendered)


def _is_visible_remote_agent(agent_name: str) -> bool:
    return bool(_visible_remote_agent_name(agent_name))


def _visible_remote_agent_name(agent_name: str) -> str:
    raw_name = str(agent_name or "").strip()
    normalized = raw_name.casefold()
    proxy_match = re.fullmatch(
        r"agent_proxy\s*(?:【(?P<cjk>[^】]+)】|\[(?P<bracket>[^\]]+)\]|\((?P<paren>[^)]+)\))",
        raw_name,
        flags=re.IGNORECASE,
    )
    if proxy_match:
        raw_name = next(
            value for value in proxy_match.groupdict().values() if value is not None
        ).strip()
        normalized = raw_name.casefold()
    elif normalized.startswith("agent_proxy"):
        return ""
    if not normalized or normalized in {
        "assistant",
        "coordinator",
        "planner",
        "publisher",
        "system",
        "tool",
    }:
        return ""
    if normalized.startswith("scheduler"):
        return ""
    return raw_name


def _assistant_memory_outputs(
    assistant_buffers: Dict[str, str],
    visible_remote_buffers: Dict[str, str],
) -> list[dict[str, object]]:
    outputs = [
        {"agent_name": name, "content": content, "user_visible": True}
        for name, content in assistant_buffers.items()
        if name != "execution_result" and content
    ]
    final_result = assistant_buffers.get("execution_result", "").strip()
    if final_result:
        outputs.append(
            {
                "agent_name": "execution_result",
                "content": final_result,
                "user_visible": True,
            }
        )
    elif visible_remote_buffers:
        agent_name = next(reversed(visible_remote_buffers))
        content = _bounded_visible_result(visible_remote_buffers[agent_name])
        agent_name = _visible_remote_agent_name(agent_name)
        if content:
            outputs.append(
                {
                    "agent_name": agent_name,
                    "content": content,
                    "user_visible": True,
                }
            )
    return outputs


def _memory_compacted_event(record) -> dict:
    covered_ids = list(
        record.metadata.get("covered_message_ids")
        or record.metadata.get("covered_user_message_ids")
        or ()
    )
    return {
        "event": "memory_compacted",
        "data": {
            "compaction_id": record.compaction_id,
            "session_id": record.session_id,
            "generation": int(record.metadata.get("compaction_generation") or 0),
            "covered_message_ids": covered_ids,
            "covered_message_count": len(covered_ids),
            "retained_turn_count": record.boundary.retained_turn_count,
            "token_count_before": record.boundary.token_count_before,
            "token_count_after": record.boundary.token_count_after,
            "summary_mode": record.metadata.get("summary_mode") or "unknown",
            "fallback_reason": record.metadata.get("fallback_reason"),
            "markdown_path": record.metadata.get("markdown_projection_path"),
        },
    }

class Server:
    def __init__(self, host="0.0.0.0", port=8001) -> None:
        self.host = host
        self.port = port

    def _process_request(self, request: "AgentRequest") -> List[Dict[str, str]]:
        return [{"role": message.role, "content": message.content} for message in request.messages]

    @staticmethod
    async def _trigger_mcp_reload(force: bool = False) -> None:
        if USE_MCP_TOOLS:
            hot_manager = await get_mcp_hot_reload_manager()
            await hot_manager.reload(force=force)

    @staticmethod
    async def _run_agent_workflow(
            request: "AgentRequest"
    ) -> AsyncGenerator[str, None]:
        await agent_manager.ensure_initialized()
        await Server._trigger_mcp_reload(force=False)

        session = session_manager.get_session(request.user_id)
        for message in request.messages:
            session.add_message(message.role, message.content)

        incoming_messages = [
            {
                "role": message.role,
                "content": message.content,
                "message_id": message.message_id,
                "metadata": message.metadata,
            }
            for message in request.messages
        ]
        latest_user_message = next(
            (
                str(item["content"])
                for item in reversed(incoming_messages)
                if str(item.get("role") or "").lower() == "user"
                and str(item.get("content") or "").strip()
            ),
            "",
        )
        if request.workmode == "production":
            resolved_request = resolve_conversation_request(
                current_message=(
                    request.resolved_request
                    or request.original_user_query
                    or request.instruction
                    or latest_user_message
                ),
                context_entities=(
                    request.current_request_entities
                    or request.context_entities
                ),
                context_artifacts=request.context_artifacts,
            )
        else:
            resolved_request = resolve_conversation_request(
                current_message=request.instruction or latest_user_message,
                turn_type=request.turn_type,
                clarification_context=request.clarification_context,
                context_entities=request.context_entities,
                context_artifacts=request.context_artifacts,
            )
        if request.resolved_request:
            resolved_request.resolved_message = str(request.resolved_request).strip()
        if request.current_request_entities:
            resolved_request.entity_overrides = {
                **resolved_request.entity_overrides,
                **dict(request.current_request_entities),
            }
        if request.context_references:
            from src.contracts import ContextReference
            request_context_references = [
                ContextReference.model_validate(item)
                for item in request.context_references
                if isinstance(item, dict)
            ]
            existing_reference_keys = {
                (
                    item.kind,
                    item.key,
                    json.dumps(
                        item.value,
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    ),
                )
                for item in resolved_request.context_references
            }
            resolved_request.context_references.extend(
                item
                for item in request_context_references
                if (
                    item.kind,
                    item.key,
                    json.dumps(
                        item.value,
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    ),
                ) not in existing_reference_keys
            )
        memory_manager = None
        memory_metadata = {}
        memory_session_id = ""
        memory_turn_id = next(
            (
                str(item.get("message_id"))
                for item in reversed(incoming_messages)
                if str(item.get("role") or "").casefold() == "user"
                and item.get("message_id")
            ),
            None,
        )
        memory_active = MEMORY_ENABLED and request.memory_enabled is not False
        if memory_active:
            memory_manager = get_memory_manager()
            try:
                current_plan = (
                    workflow_cache.get_planning_steps(request.workflow_id)
                    if request.workflow_id
                    else request.instruction_history
                )
            except Exception:
                current_plan = request.instruction_history
            if request.workmode == "production":
                plan_status = "active"
            elif request.workflow_id and getattr(request, "stop_after_planner", False):
                plan_status = "waiting_approval"
            else:
                plan_status = "planning"
            current_plan_hash = (
                plan_hash(current_plan)
                if isinstance(current_plan, list)
                and all(isinstance(item, dict) for item in current_plan)
                else None
            )
            try:
                prepared = await memory_manager.prepare_context(
                    user_id=request.user_id,
                    incoming_messages=incoming_messages,
                    session_id=request.memory_session_id or request.session_id,
                    workflow_id=request.workflow_id,
                    request_enabled=request.memory_enabled,
                    retrieval_query=resolved_request.resolved_message,
                    attachments={
                        "current_plan": current_plan,
                        "extra": {
                            "workflow_id": request.workflow_id,
                            "plan_status": plan_status,
                            "plan_hash": current_plan_hash,
                            "project_id": request.project_id,
                            "intent_tags": [
                                f"entity.{key}"
                                for key in sorted(resolved_request.entity_overrides)
                            ],
                        },
                    },
                    intent_tags=[
                        f"entity.{key}"
                        for key in sorted(resolved_request.entity_overrides)
                    ],
                    memory_keys=memory_lookup_keys(resolved_request.resolved_message),
                    compaction_model_type=_active_compaction_model_type(request),
                )
            except PlanContextOverflowError as exc:
                yield {
                    "event": "workflow_error",
                    "data": {
                        "workflow_id": request.workflow_id,
                        "reason_code": "PLAN_CONTEXT_OVERFLOW",
                        "reason": "The confirmed Plan and current request exceed the model input budget",
                        "plan_tokens": exc.plan_tokens,
                        "current_request_tokens": exc.current_request_tokens,
                        "input_budget": exc.input_budget,
                    },
                }
                return
            except CurrentRequestOverflowError as exc:
                yield {
                    "event": "workflow_error",
                    "data": {
                        "workflow_id": request.workflow_id,
                        "reason_code": "CURRENT_REQUEST_CONTEXT_OVERFLOW",
                        "reason": (
                            "The current request exceeds the model input budget; "
                            "shorten it or provide large content as an attachment"
                        ),
                        "current_request_tokens": exc.current_request_tokens,
                        "input_budget": exc.input_budget,
                    },
                }
                return
            session_messages = list(prepared.messages)
            memory_metadata = prepared.metadata.to_dict()
            memory_metadata["long_term_reference"] = next(
                (
                    str(message.get("content") or "")
                    for message in prepared.messages
                    if (message.get("metadata") or {}).get("memory_type")
                    == "long_term_reference"
                ),
                "",
            )
            memory_session_id = prepared.metadata.session_id
            memory_turn_id = prepared.metadata.current_turn_id or memory_turn_id
            if prepared.metadata.warning:
                yield {
                    "event": "memory_warning",
                    "data": {
                        "warning": prepared.metadata.warning,
                        "session_id": memory_session_id,
                    },
                }
        else:
            session_messages = session.history[-3:]

        response_stream = run_agent_workflow(
            user_id=request.user_id,
            user_input_messages=session_messages,
            debug=request.debug,
            deep_thinking_mode=request.deep_thinking_mode,
            search_before_planning=request.search_before_planning,
            coor_agents=request.coor_agents,
            workmode=request.workmode,
            workflow_id=request.workflow_id,
            stop_after_planner=getattr(request, "stop_after_planner", False),
            instruction=getattr(request, "instruction", None),
            instruction_history=getattr(request, "instruction_history", None),
            original_user_query=getattr(request, "original_user_query", None),
            memory_session_id=memory_session_id,
            memory_enabled=memory_active,
            memory_context=memory_metadata,
            project_id=request.project_id,
            compaction_model_type=_active_compaction_model_type(request),
            skill_reuse_enabled=request.skill_reuse_enabled,
            current_request=resolved_request.resolved_message,
            raw_request=resolved_request.raw_message,
            entity_overrides=resolved_request.entity_overrides,
            context_references=[
                item.model_dump()
                for item in resolved_request.context_references
            ],
            context_artifacts=resolved_request.artifact_inputs,
            conversation_context={
                "entities": dict(request.context_entities or {}),
                "artifacts": list(resolved_request.artifact_inputs),
            },
            request_input_messages=[
                {
                    "role": item["role"],
                    "content": redact_secrets(item["content"]),
                    "message_id": item.get("message_id"),
                    "metadata": dict(item.get("metadata") or {}),
                }
                for item in incoming_messages
            ],
        )
        assistant_buffers: Dict[str, str] = {}
        visible_remote_buffers: Dict[str, str] = {}
        actual_workflow_id = request.workflow_id
        stream_completed = False
        workflow_ended = False
        compaction_record = None
        try:
            async for res in response_stream:
                try:
                    event_type = res.get("event")
                    data = res.get("data") or {}
                    if data.get("workflow_id"):
                        actual_workflow_id = data.get("workflow_id")
                    if event_type == "messages":
                        agent_name = str(res.get("agent_name") or "assistant")
                        delta = (data.get("delta") or {}).get("content", "")
                        if agent_name in {"planner", "coordinator", "assistant"}:
                            assistant_buffers[agent_name] = (
                                assistant_buffers.get(agent_name, "") + str(delta)
                            )
                        else:
                            visible_agent_name = _visible_remote_agent_name(agent_name)
                            if visible_agent_name and delta:
                                visible_remote_buffers[visible_agent_name] = (
                                    visible_remote_buffers.get(visible_agent_name, "")
                                    + str(delta)
                                )[:_VISIBLE_RESULT_CAPTURE_CHARS]
                    elif event_type == "final_result":
                        compact_result = _compact_execution_result(data)
                        if compact_result:
                            assistant_buffers["execution_result"] = compact_result
                    # replace agent_obj with agent_json
                    if event_type == "new_agent_created" and "agent_obj" in data:
                        agent_obj: BaseModel = data["agent_obj"]
                        agent_json = agent_obj.model_dump_json(indent=2) if agent_obj else None
                        if agent_json:
                            data["agent_obj"] = agent_json
                        else:
                            logger.warning("Could not serialize agent object for new_agent_created event.")
                            data.pop("agent_obj", None)
                    if event_type == "end_of_workflow":
                        workflow_ended = True
                    yield res
                except (TypeError, ValueError, json.JSONDecodeError) as e:
                    logging.error(f"Error serializing event: {e}", exc_info=True)
            stream_completed = True
        finally:
            outputs = _assistant_memory_outputs(
                assistant_buffers, visible_remote_buffers
            )
            if memory_manager is not None and outputs:
                try:
                    await memory_manager.record_assistant_outputs(
                        user_id=request.user_id,
                        session_id=memory_session_id,
                        workflow_id=actual_workflow_id,
                        outputs=outputs,
                        turn_id=memory_turn_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to persist streamed assistant memory: %s",
                        type(exc).__name__,
                    )
                else:
                    if stream_completed or workflow_ended:
                        try:
                            compaction_record = await memory_manager.compact_if_needed(
                                user_id=request.user_id,
                                session_id=memory_session_id,
                                workflow_id=actual_workflow_id,
                                current_step_id="assistant_persisted",
                                compaction_model_type=_active_compaction_model_type(request),
                            )
                        except Exception as exc:
                            logger.warning(
                                "Post-assistant memory compaction failed: %s",
                                type(exc).__name__,
                            )
        if stream_completed and compaction_record is not None:
            yield _memory_compacted_event(compaction_record)

    async def _run_agent_workflow_with_resume(
            self,
            request: "AgentRequest",
            resume_step: int = None,
            task_id: str = None
    ) -> AsyncGenerator[str, None]:
        """Run agent workflow with resume capability from a specific checkpoint step."""
        if agent_manager is None:
             logger.error("Agent workflow called before AgentManager was initialized.")

        await agent_manager.ensure_initialized()
        await Server._trigger_mcp_reload(force=False)

        # Resume场景：直接使用request.messages（来自step=0 checkpoint）
        # 不依赖session，因为resume可能在很久之后执行，session已过期
        # request.messages是AgentMessage列表，需要转换为dict列表
        session_messages = [
            {
                "role": message.role,
                "content": message.content,
                "message_id": message.message_id,
                "metadata": dict(message.metadata or {}),
            }
            for message in request.messages
        ]
        resume_turn_id = next(
            (
                str(
                    (message.metadata or {}).get("turn_id")
                    or message.message_id
                )
                for message in reversed(request.messages)
                if str(message.role or "").casefold() == "user"
                and ((message.metadata or {}).get("turn_id") or message.message_id)
            ),
            None,
        )
        memory_session_id = ""
        memory_metadata = {}
        memory_manager = None
        memory_active = MEMORY_ENABLED and request.memory_enabled is True
        if memory_active:
            memory_manager = get_memory_manager()
            memory_session_id = memory_manager.resolve_session_id(
                request.user_id,
                session_id=request.memory_session_id or request.session_id,
            )
            memory_metadata = {
                "session_id": memory_session_id,
                "resume_uses_checkpoint_state": True,
                "current_turn_id": resume_turn_id,
            }

        response_stream = run_agent_workflow(
            user_id=request.user_id,
            user_input_messages=session_messages,
            debug=request.debug,
            deep_thinking_mode=request.deep_thinking_mode,
            search_before_planning=request.search_before_planning,
            coor_agents=request.coor_agents,
            workmode=request.workmode,
            workflow_id=request.workflow_id,
            resume_step=resume_step,
            task_id=task_id,
            stop_after_planner=getattr(request, "stop_after_planner", False),
            instruction=getattr(request, "instruction", None),
            instruction_history=getattr(request, "instruction_history", None),
            original_user_query=getattr(request, "original_user_query", None),
            memory_session_id=memory_session_id,
            memory_enabled=memory_active,
            memory_context=memory_metadata,
            project_id=request.project_id,
            compaction_model_type=_active_compaction_model_type(request),
            skill_reuse_enabled=request.skill_reuse_enabled,
            request_input_messages=session_messages,
        )
        assistant_buffers: Dict[str, str] = {}
        visible_remote_buffers: Dict[str, str] = {}
        actual_workflow_id = request.workflow_id
        stream_completed = False
        workflow_ended = False
        compaction_record = None
        try:
            async for res in response_stream:
                try:
                    event_type = res.get("event")
                    data = res.get("data") or {}
                    if data.get("workflow_id"):
                        actual_workflow_id = data.get("workflow_id")
                    if event_type == "messages":
                        agent_name = str(res.get("agent_name") or "assistant")
                        delta = (data.get("delta") or {}).get("content", "")
                        if agent_name in {"planner", "coordinator", "assistant"}:
                            assistant_buffers[agent_name] = (
                                assistant_buffers.get(agent_name, "") + str(delta)
                            )
                        else:
                            visible_agent_name = _visible_remote_agent_name(agent_name)
                            if visible_agent_name and delta:
                                visible_remote_buffers[visible_agent_name] = (
                                    visible_remote_buffers.get(visible_agent_name, "")
                                    + str(delta)
                                )[:_VISIBLE_RESULT_CAPTURE_CHARS]
                    elif event_type == "final_result":
                        compact_result = _compact_execution_result(data)
                        if compact_result:
                            assistant_buffers["execution_result"] = compact_result
                    if event_type == "new_agent_created" and "agent_obj" in data:
                        agent_obj: BaseModel = data["agent_obj"]
                        agent_json = agent_obj.model_dump_json(indent=2) if agent_obj else None
                        if agent_json:
                            data["agent_obj"] = agent_json
                        else:
                            logger.warning("Could not serialize agent object for new_agent_created event.")
                            data.pop("agent_obj", None)
                    if event_type == "end_of_workflow":
                        workflow_ended = True
                    yield res
                except (TypeError, ValueError, json.JSONDecodeError) as e:
                    logging.error(f"Error serializing event: {e}", exc_info=True)
            stream_completed = True
        finally:
            outputs = _assistant_memory_outputs(
                assistant_buffers, visible_remote_buffers
            )
            if outputs and not resume_turn_id:
                logger.warning(
                    "Skipping resumed assistant memory persistence because the "
                    "checkpoint has no trusted user turn id"
                )
            elif memory_manager is not None and outputs:
                try:
                    await memory_manager.record_assistant_outputs(
                        user_id=request.user_id,
                        session_id=memory_session_id,
                        workflow_id=actual_workflow_id,
                        outputs=outputs,
                        turn_id=resume_turn_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to persist resumed assistant memory: %s",
                        type(exc).__name__,
                    )
                else:
                    if stream_completed or workflow_ended:
                        try:
                            compaction_record = await memory_manager.compact_if_needed(
                                user_id=request.user_id,
                                session_id=memory_session_id,
                                workflow_id=actual_workflow_id,
                                current_step_id="assistant_persisted",
                                compaction_model_type=_active_compaction_model_type(request),
                            )
                        except Exception as exc:
                            logger.warning(
                                "Post-resume assistant memory compaction failed: %s",
                                type(exc).__name__,
                            )
        if stream_completed and compaction_record is not None:
            yield _memory_compacted_event(compaction_record)

    @staticmethod
    async def _list_agents(
         request: "listAgentRequest"
    ) -> AsyncGenerator[str, None]:
        await agent_manager.ensure_initialized()
        try:
            agents: List[Agent] = await agent_manager.agent_registry.list(
                user_id=request.user_id,
                match=request.match,
            )
            for agent in agents:
                yield agent.model_dump_json() + "\n"
        except Exception as e:
            logger.error(f"Error listing agents: {e}", exc_info=True)
            raise Exception(f"Error listing agents: {e}")

    @staticmethod
    async def _list_agents_json(user_id: str, match: Optional[str] = None):
        await agent_manager.ensure_initialized()
        try:
            agents: List[Agent] = await agent_manager.agent_registry.list(
                user_id=user_id,
                match=match,
            )
            return [agent.model_dump() for agent in agents]
        except Exception as e:
            raise Exception(f"Error listing agents: {e}")

    @staticmethod
    async def _workflow_draft(user_id: str, match: str):
        try:
            workflows = workflow_cache.list_workflows(user_id, match)
            return workflows[0]
        except Exception as e:
            raise Exception(f"Error listing workflows: {e}")
        
    @staticmethod
    async def _list_workflow_json(user_id: str, match: Optional[str] = None):
        try:
            workflows = workflow_cache.list_workflows(user_id, match)
            default_workflows = workflow_cache.list_workflows('share')
            workflows.extend(default_workflows)
            workflowJsons = []
            for workflow in workflows:
                updated_at = workflow.get("updated_at")
                created_at = workflow.get("created_at")
                if not updated_at:
                    try:
                        workflow_user_id, polish_id = str(
                            workflow["workflow_id"]).split(":", 1)
                        workflow_path = (
                            workflow_cache.workflow_dir
                            / workflow_user_id
                            / f"{polish_id}.json"
                        )
                        updated_at = datetime.fromtimestamp(
                            workflow_path.stat().st_mtime,
                            tz=timezone.utc,
                        ).isoformat()
                    except (KeyError, OSError, ValueError):
                        updated_at = None
                workflowJsons.append({
                    "workflow_id": workflow["workflow_id"],
                    "version": workflow["version"],
                    "lap": workflow["lap"],
                    "user_input_messages": workflow["user_input_messages"],
                    "deep_thinking_mode": workflow["deep_thinking_mode"],
                    "search_before_planning": workflow["search_before_planning"],
                    "created_at": created_at,
                    "updated_at": updated_at,
                })
            return [workflowJson for workflowJson in workflowJsons]
        except Exception as e:
            raise Exception(f"Error listing workflows: {e}")
        
    @staticmethod
    def _list_workflow(
         request: "listAgentRequest"
    ) -> AsyncGenerator[str, None]:
        if workflow_cache is None:
             logger.error("Workflow cache not initialized.")
             raise Exception("Workflow cache not initialized.")
        try:
            workflows = workflow_cache.list_workflows(request.user_id, request.match)
            return workflows
        except Exception as e:
            logger.error(f"Error listing workflows: {e}", exc_info=True)
            raise Exception(f"Error listing workflows: {e}")

    @staticmethod
    async def _list_user_all_agents(user_id: str):
        await agent_manager.ensure_initialized()
        try:
            agents = await agent_manager.agent_registry.list(user_id=user_id)
            return [agent.model_dump() for agent in agents]
        except Exception as e:
            raise Exception(f"Error listing user all agents: {e}")
        
    @staticmethod
    async def _list_default_agents_json():
        await agent_manager.ensure_initialized()
        try:
            agents = await agent_manager.agent_registry.list(user_id="share")
            return [agent.model_dump() for agent in agents]
        except Exception as e:
            raise Exception(f"Error listing default agents: {e}")
        
    @staticmethod
    async def _edit_workflow(user_id: str, workflow):
        await agent_manager.ensure_initialized()
        try:
            nodes = workflow["nodes"]
            for _, node in nodes.items():
                if node["component_type"] == "agent" and node["config"]["type"] == "execution_agent":
                    if "add" in node and node["add"] == "1":
                        agent_name = node["name"]
                        agents = await agent_manager._list_user_all_agents(user_id)
                        for agent in agents:
                            if agent.agent_name == node["name"]:
                                from datetime import datetime
                                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                                agent_name = f"{node['name']}_{timestamp}"
                                break
                        _tools = []
                        for tool in node["config"]["tools"]:
                            _tools.append(Tool(
                                name=tool["config"]["name"],
                                description=tool["config"]["description"],
                            ))
                        agent = Agent(
                            user_id=user_id,
                            agent_name=agent_name,
                            nick_name=node["label"],
                            description=node["config"]["description"],
                            llm_type=node["config"]["llm_type"],
                            selected_tools=_tools,
                            prompt=node["config"]["prompt"]
                        )
                        await agent_manager._save_agent(agent, flush=True)
                        for graph in workflow["graph"]:
                            if graph["component_type"] == "agent" and graph["config"]["node_type"] == "execution_agent":
                                if graph["name"] == node["name"]:
                                    graph["name"] = agent_name
                                    break
                        workflow_cache.save_workflow(user_id, workflow)
                    else:
                        _tools = []
                        for tool in node["config"]["tools"]:
                            _tools.append(Tool(
                                name=tool["config"]["name"],
                                description=tool["config"]["description"],
                            ))
                        agent = Agent(
                            user_id=user_id,
                            agent_name=node["name"],
                            nick_name=node["label"],
                            description=node["config"]["description"],
                            llm_type=node["config"]["llm_type"],
                            selected_tools=_tools,
                            prompt=node["config"]["prompt"]
                        )
                        await agent_manager._edit_agent(agent)
                        workflow_cache.save_workflow(user_id, workflow)
            return workflow
        except Exception as e:
            raise Exception(f"Error editing workflow: {e}")

    @staticmethod
    async def _list_default_agents() -> AsyncGenerator[str, None]:
        await agent_manager.ensure_initialized()
        try:
            agents = await agent_manager.agent_registry.list(user_id="share")
            for agent in agents:
                yield agent.model_dump_json() + "\n"
        except Exception as e:
            logger.error(f"Error listing default agents: {e}", exc_info=True)
            raise Exception(f"Error listing default agents: {e}")

    @staticmethod
    async def _list_default_tools() -> AsyncGenerator[str, None]:
        await agent_manager.ensure_initialized()
        try:
            await Server._trigger_mcp_reload(force=False)
            registry = await ToolRegistry.get_instance()
            tools = await registry.list_global_tools()
            for meta in tools:
                tool = meta.tool
                tool_name = getattr(tool, "name", "")
                if not tool_name:
                    continue
                payload = Tool(
                    name=tool_name,
                    description=meta.description or getattr(tool, "description", ""),
                )
                yield payload.model_dump_json() + "\n"
        except Exception as e:
            logger.error(f"Error listing default tools: {e}", exc_info=True)
            raise Exception(f"Error listing default tools: {e}")

    @staticmethod
    async def _edit_agent(
        request: "Agent"
    ) -> AsyncGenerator[str, None]:
        await agent_manager.ensure_initialized()
        try:
            result = await agent_manager._edit_agent(request)
            yield json.dumps({"result": result}) + "\n"
        except NotFoundAgentError as e:
            logger.warning(f"Edit agent failed: {e}")
            yield json.dumps({"result": "agent not found", "error": str(e)}) + "\n"
        except Exception as e:
            logger.error(f"Error editing agent {request.agent_name}: {e}", exc_info=True)
            yield json.dumps({"result": "error", "error": str(e)}) + "\n"

    @staticmethod
    async def _edit_planning_steps(
        request: "EditStepsRequest"
    ) -> AsyncGenerator[str, None]:
        await agent_manager.ensure_initialized()
        try:
            workflow_cache.save_planning_steps(request.workflow_id,request.planning_steps)
            yield json.dumps({"result": "success"}) + "\n"
        except Exception as e:
            logger.error(f"Error editing planning steps : {e}", exc_info=True)
            yield json.dumps({"result": "error", "error": str(e)}) + "\n"

    @staticmethod
    async def _remove_agent(request: RemoveAgentRequest) -> AsyncGenerator[str, None]:
        await agent_manager.ensure_initialized()
        try:
            await agent_manager._remove_agent(request.agent_name)
            yield json.dumps({"result": "success", "message": f"Agent '{request.agent_name}' deleted successfully."}) + "\n"
        except Exception as e:
            logger.error(f"Error removing agent {request.agent_name}: {e}", exc_info=True)
            yield json.dumps({"result": "error", "message": f"Error removing Agent '{request.agent_name}': {str(e)}"}) + "\n"
