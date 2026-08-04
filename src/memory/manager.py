"""High-level orchestration for short-term and long-term Agent Memory."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import NAMESPACE_URL, uuid4, uuid5

from config.global_variables import memory_dir
from src.service import env

from .compaction import (
    CompactionEngine,
    CompactionToolCallError,
    CompactionValidationError,
    build_bounded_emergency_context,
    completed_turns,
    render_compaction_segments,
    select_recent_turns,
    summary_user_message_ids,
)
from .consolidation import (
    EXTRACTOR_VERSION,
    MemoryConsolidator,
    build_llm_extractor,
    candidate_from_user_message,
    contains_prompt_injection,
)
from .models import (
    CompactionRecord,
    MemoryContextMetadata,
    MemoryMessage,
    PreparedMemoryContext,
    RecoveryAttachments,
)
from .retrieval import (
    MemoryRetriever,
    TaggedMemoryRetriever,
    format_untrusted_memories,
    project_model_memories,
    select_model_memories,
)
from .store import MemoryStore, MemoryStoreError, SecretDetectedError
from .utils import (
    build_provenance,
    contains_secret,
    derive_session_id,
    estimate_tokens,
    redact_secrets,
)


logger = logging.getLogger(__name__)


class PlanContextOverflowError(RuntimeError):
    def __init__(
        self, *, plan_tokens: int, current_request_tokens: int, input_budget: int
    ) -> None:
        self.plan_tokens = int(plan_tokens)
        self.current_request_tokens = int(current_request_tokens)
        self.input_budget = int(input_budget)
        super().__init__(
            "plan_context_overflow: required="
            f"{self.plan_tokens + self.current_request_tokens}, "
            f"budget={self.input_budget}"
        )


class CurrentRequestOverflowError(RuntimeError):
    def __init__(self, *, current_request_tokens: int, input_budget: int) -> None:
        self.current_request_tokens = int(current_request_tokens)
        self.input_budget = int(input_budget)
        super().__init__(
            "current_request_context_overflow: required="
            f"{self.current_request_tokens}, budget={self.input_budget}"
        )


_REMEMBER_PATTERNS = (
    re.compile(r"^\s*(?:请)?记住(?:一下)?[：:,，\s]*(?P<content>.+)$", re.DOTALL),
    re.compile(r"^\s*remember(?:\s+that)?[：:,\s]+(?P<content>.+)$", re.I | re.DOTALL),
)
_SIMPLE_GREETING_PATTERN = re.compile(
    r"^\s*(?:hi|hello|hey|thanks|thank you|你好|您好|嗨|谢谢|你是谁|who are you)[!.。！?？\s]*$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class MemorySettings:
    enabled: bool = True
    long_term_enabled: bool = True
    auto_compact_enabled: bool = True
    llm_compaction_enabled: bool = False
    max_context_tokens: int = 32768
    reserved_output_tokens: int = 4096
    trigger_tokens: int = 21504
    target_tokens: int = 10752
    long_term_top_k: int = 5
    long_term_token_budget: int = 512
    max_record_chars: int = 8000
    recent_turns: int = 2
    auto_consolidation_enabled: bool = True
    consolidation_llm_enabled: bool = False
    markdown_projection_enabled: bool = True
    store_path: Path = memory_dir

    @property
    def input_budget(self) -> int:
        return max(1, self.max_context_tokens - self.reserved_output_tokens)

    @classmethod
    def from_env(cls) -> "MemorySettings":
        configured = env.MEMORY_STORE_DIR or env.MEMORY_DB_PATH
        path = Path(configured) if configured else memory_dir
        return cls(
            enabled=env.MEMORY_ENABLED,
            long_term_enabled=env.MEMORY_LONG_TERM_ENABLED,
            auto_compact_enabled=env.MEMORY_AUTO_COMPACT_ENABLED,
            llm_compaction_enabled=env.MEMORY_COMPACTION_LLM_ENABLED,
            max_context_tokens=env.MEMORY_MAX_CONTEXT_TOKENS,
            reserved_output_tokens=env.MEMORY_RESERVED_OUTPUT_TOKENS,
            trigger_tokens=env.MEMORY_COMPACTION_TRIGGER_TOKENS,
            target_tokens=env.MEMORY_COMPACTION_TARGET_TOKENS,
            long_term_top_k=env.MEMORY_LONG_TERM_TOP_K,
            long_term_token_budget=getattr(
                env, "MEMORY_LONG_TERM_TOKEN_BUDGET", 512
            ),
            max_record_chars=env.MEMORY_MAX_RECORD_CHARS,
            recent_turns=getattr(env, "MEMORY_RECENT_TURNS", 2),
            auto_consolidation_enabled=getattr(
                env, "MEMORY_AUTO_CONSOLIDATION_ENABLED", True
            ),
            consolidation_llm_enabled=getattr(
                env, "MEMORY_CONSOLIDATION_LLM_ENABLED", False
            ),
            markdown_projection_enabled=getattr(
                env, "MEMORY_MARKDOWN_PROJECTION_ENABLED", True
            ),
            store_path=path,
        )


class MemoryManager:
    def __init__(
        self,
        *,
        settings: MemorySettings | None = None,
        store: MemoryStore | None = None,
        compactor: CompactionEngine | None = None,
        retriever: MemoryRetriever | None = None,
        consolidator: MemoryConsolidator | None = None,
    ) -> None:
        self.settings = settings or MemorySettings.from_env()
        self.store = store or MemoryStore(self.settings.store_path)
        self.retriever = retriever or TaggedMemoryRetriever()
        self.compactor = compactor or CompactionEngine(
            summarizer=self._build_summarizer(),
            trigger_tokens=self.settings.trigger_tokens,
            target_tokens=self.settings.target_tokens,
        )
        self.consolidator = consolidator or MemoryConsolidator(
            self.store, extractor=self._build_memory_extractor()
        )

    def _build_summarizer(self, model_type: str = "basic") -> Any | None:
        if not self.settings.llm_compaction_enabled:
            return None
        try:
            from src.llm.llm import get_llm_by_type

            # The raw chat model is deliberately not bound to tools.
            return get_llm_by_type(model_type or "basic")
        except Exception as exc:
            logger.warning("Memory compaction LLM unavailable: %s", type(exc).__name__)
            return None

    def _build_memory_extractor(self) -> Any | None:
        if not self.settings.consolidation_llm_enabled:
            return None
        try:
            from src.llm.llm import get_llm_by_type

            # No tools are bound to the model used for durable-memory
            # extraction.  The extractor accepts only a strict JSON result.
            return build_llm_extractor(get_llm_by_type("basic"))
        except Exception as exc:
            logger.warning("Memory consolidation LLM unavailable: %s", type(exc).__name__)
            return None

    def resolve_session_id(
        self,
        user_id: str,
        *,
        session_id: str | None = None,
    ) -> str:
        # Do not derive the conversation from workflow_id: Launch generates that
        # ID from message content, while Production reuses it later.
        return derive_session_id(user_id, session_id=session_id)

    async def prepare_context(
        self,
        *,
        user_id: str,
        incoming_messages: Sequence[Mapping[str, Any]],
        session_id: str | None = None,
        workflow_id: str | None = None,
        request_enabled: bool | None = None,
        retrieval_query: str | None = None,
        attachments: RecoveryAttachments | Mapping[str, Any] | None = None,
        hook_results: Sequence[Mapping[str, Any]] | None = None,
        intent_tags: Sequence[str] | None = None,
        memory_keys: Sequence[str] | None = None,
        compaction_model_type: str | None = None,
    ) -> PreparedMemoryContext:
        fallback_messages = tuple(self._sanitize_message(message) for message in incoming_messages)
        resolved = self.resolve_session_id(user_id, session_id=session_id)
        if not self.settings.enabled or request_enabled is False:
            return PreparedMemoryContext(
                messages=fallback_messages,
                metadata=MemoryContextMetadata(
                    session_id=resolved,
                    token_estimate=estimate_tokens(fallback_messages),
                    warning="memory_disabled",
                ),
            )

        normalized_attachments = (
            attachments
            if isinstance(attachments, RecoveryAttachments)
            else RecoveryAttachments.from_dict(attachments)
        )
        extra = dict(normalized_attachments.extra)
        extra.setdefault("rebuild_runtime_capabilities", True)
        normalized_attachments = RecoveryAttachments(
            recent_files=normalized_attachments.recent_files,
            current_plan=normalized_attachments.current_plan,
            active_skills=normalized_attachments.active_skills,
            async_tasks=normalized_attachments.async_tasks,
            extra=extra,
        )
        plan_status = str(extra.get("plan_status") or "").casefold()
        plan_is_resumable = plan_status in {
            "active",
            "waiting_input",
            "waiting_approval",
            "paused",
            "resumable_failed",
        }
        request_tokens = estimate_tokens(fallback_messages)
        if request_tokens > self.settings.input_budget:
            raise CurrentRequestOverflowError(
                current_request_tokens=request_tokens,
                input_budget=self.settings.input_budget,
            )
        if plan_is_resumable and normalized_attachments.current_plan is not None:
            plan_tokens = estimate_tokens(normalized_attachments.current_plan)
            if plan_tokens + request_tokens > self.settings.input_budget:
                raise PlanContextOverflowError(
                    plan_tokens=plan_tokens,
                    current_request_tokens=request_tokens,
                    input_budget=self.settings.input_budget,
                )

        try:
            stored = await asyncio.to_thread(
                self._append_incoming,
                user_id,
                resolved,
                incoming_messages,
                workflow_id,
            )
            if self.settings.long_term_enabled:
                await self._promote_explicit_requests(stored, workflow_id)
            if self.settings.auto_consolidation_enabled:
                await self._consolidate_pending(user_id, resolved, workflow_id)

            latest, tail = await asyncio.to_thread(
                self.store.messages_after_compaction, user_id, resolved
            )
            all_messages = await asyncio.to_thread(
                self.store.list_messages, user_id, resolved
            )
            projection = self._project(latest, tail if latest else all_messages)

            if (
                self.settings.auto_compact_enabled
                and estimate_tokens(projection) >= self.settings.trigger_tokens
            ):
                conversation_only_attachments = RecoveryAttachments(
                    recent_files=normalized_attachments.recent_files,
                    current_plan=None,
                    active_skills=(),
                    async_tasks=(),
                    extra={
                        "workflow_id": extra.get("workflow_id"),
                        "plan_status": plan_status or None,
                        "plan_hash": extra.get("plan_hash"),
                        "rebuild_runtime_capabilities": True,
                    },
                )
                latest = await self._compact_for_request(
                    user_id=user_id,
                    session_id=resolved,
                    latest=latest,
                    all_messages=all_messages,
                    tail=tail,
                    attachments=conversation_only_attachments,
                    hook_results=hook_results,
                    active_plan=(
                        normalized_attachments.current_plan if plan_is_resumable else None
                    ),
                    compaction_model_type=compaction_model_type,
                )
                if latest is not None:
                    _, tail = await asyncio.to_thread(
                        self.store.messages_after_compaction, user_id, resolved
                    )
                    projection = self._project(latest, tail)

            query = (
                redact_secrets(retrieval_query).strip()
                if retrieval_query
                else self._latest_user_content(stored)
                or self._latest_user_content(all_messages)
            )
            retrieved = []
            selected_retrieved = []
            retrieved_memories: tuple[dict[str, Any], ...] = ()
            if (
                query
                and self.settings.long_term_enabled
                and not _SIMPLE_GREETING_PATTERN.fullmatch(query)
            ):
                long_term = await asyncio.to_thread(self.store.list_long_term, user_id)
                normalized_memory_keys = {
                    str(key).strip() for key in memory_keys or () if str(key).strip()
                }
                if normalized_memory_keys:
                    # Explicit preference questions select named keys before Top-K
                    # ranking so unrelated preferences cannot crowd them out.
                    long_term = [
                        record
                        for record in long_term
                        if str(record.memory_key or record.kind) in normalized_memory_keys
                    ]
                try:
                    retrieved = self.retriever.retrieve(
                        query,
                        long_term,
                        user_id=user_id,
                        top_k=self.settings.long_term_top_k,
                        intent_tags=tuple(intent_tags or extra.get("intent_tags") or ()),
                        project_id=str(extra.get("project_id") or "") or None,
                    )
                except TypeError:
                    retrieved = self.retriever.retrieve(
                        query,
                        long_term,
                        user_id=user_id,
                        top_k=self.settings.long_term_top_k,
                    )
                selected_retrieved = select_model_memories(
                    retrieved, token_budget=self.settings.long_term_token_budget
                )
                retrieved_memories = project_model_memories(selected_retrieved)
                reference = format_untrusted_memories(
                    selected_retrieved,
                )
                if reference:
                    projection.insert(
                        0,
                        {
                            "role": "assistant",
                            "content": reference,
                            "metadata": {"memory_type": "long_term_reference"},
                        },
                    )

            token_estimate = estimate_tokens(projection)
            warning = None
            if token_estimate > self.settings.input_budget:
                projection = build_bounded_emergency_context(
                    all_messages, self.settings.input_budget
                )
                token_estimate = estimate_tokens(projection)
                warning = "context_budget_emergency_projection"
            compactions = await asyncio.to_thread(
                self.store.list_compactions, user_id, resolved
            )
            consolidation_watermark = await asyncio.to_thread(
                self.store.get_consolidation_watermark,
                user_id,
                resolved,
                extractor_version=EXTRACTOR_VERSION,
            )
            metadata = MemoryContextMetadata(
                session_id=resolved,
                token_estimate=token_estimate,
                compaction_id=latest.compaction_id if latest else None,
                compaction_generation=len(compactions),
                retrieved_memory_ids=tuple(
                    item.memory.memory_id for item in selected_retrieved
                ),
                retrieved_memories=retrieved_memories,
                attachment_references=normalized_attachments.recent_files,
                warning=warning,
                retained_turn_count=(
                    latest.boundary.retained_turn_count if latest else 0
                ),
                plan_status=plan_status or None,
                plan_hash=(str(extra.get("plan_hash")) if extra.get("plan_hash") else None),
                consolidation_watermark=consolidation_watermark,
                markdown_projection_path=(
                    str(self.store.markdown_path(user_id))
                    if self.settings.markdown_projection_enabled
                    else None
                ),
                compaction_markdown_path=(
                    str(self.store.compaction_markdown_path(user_id, resolved))
                    if latest is not None
                    else None
                ),
            )
            return PreparedMemoryContext(
                messages=tuple(projection),
                metadata=metadata,
            )
        except Exception as exc:
            if isinstance(exc, (PlanContextOverflowError, CurrentRequestOverflowError)):
                raise
            correlation = uuid4().hex[:12]
            logger.warning(
                "Memory soft failure correlation=%s type=%s",
                correlation,
                type(exc).__name__,
            )
            return PreparedMemoryContext(
                messages=fallback_messages,
                metadata=MemoryContextMetadata(
                    session_id=resolved,
                    token_estimate=estimate_tokens(fallback_messages),
                    warning=f"memory_soft_failure:{correlation}",
                ),
            )

    def _append_incoming(
        self,
        user_id: str,
        session_id: str,
        incoming_messages: Sequence[Mapping[str, Any]],
        workflow_id: str | None,
    ) -> list[MemoryMessage]:
        stored = []
        for index, message in enumerate(incoming_messages):
            metadata = dict(message.get("metadata") or {})
            role = str(message.get("role", "user"))
            content = redact_secrets(str(message.get("content", "")))
            identifier = message.get("message_id")
            if not identifier:
                # Older Web clients resend the whole transcript without IDs.
                # Derive the ID from its stable position and content so a retry
                # is idempotent while two identical messages in one request stay
                # distinct.
                identifier = uuid5(
                    NAMESPACE_URL,
                    f"superagent-input:{user_id}:{session_id}:{index}:"
                    f"{role}:{content}",
                ).hex
                metadata.setdefault("generated_message_id", True)
            if role == "user":
                metadata.setdefault("turn_id", str(identifier))
                metadata.setdefault("main_visible", True)
            if contains_secret(str(message.get("content", ""))):
                metadata["secret_redacted"] = True
            stored.append(
                self.store.append_message(
                    user_id=user_id,
                    session_id=session_id,
                    role=role,
                    content=content,
                    message_id=str(identifier),
                    workflow_id=workflow_id,
                    metadata=metadata,
                )
            )
        return stored

    async def _promote_explicit_requests(
        self, messages: Sequence[MemoryMessage], workflow_id: str | None
    ) -> None:
        for message in messages:
            if message.role != "user":
                continue
            if message.metadata.get("secret_redacted"):
                continue
            candidate = self._extract_explicit_memory(message.content)
            if candidate is None:
                continue
            if contains_prompt_injection(candidate):
                logger.warning("Rejected instruction-like explicit memory request")
                continue
            structured = candidate_from_user_message(message)
            try:
                await self.remember(
                    user_id=message.user_id,
                    content=candidate,
                    kind=(
                        structured.kind
                        if structured is not None
                        else (
                            "preference"
                            if self._looks_like_preference(candidate)
                            else "fact"
                        )
                    ),
                    confidence=1.0,
                    memory_key=(structured.key if structured is not None else None),
                    value=(structured.value if structured is not None else candidate),
                    label=(structured.label if structured is not None else candidate),
                    importance=(structured.importance if structured is not None else 1.0),
                    decay_class=(
                        structured.decay_class if structured is not None else "pinned"
                    ),
                    source_message_ids=(message.message_id,),
                    extractor_version=(
                        structured.extractor_version
                        if structured is not None
                        else EXTRACTOR_VERSION
                    ),
                    tags=(structured.tags if structured is not None else ()),
                    workflow_id=workflow_id,
                    session_id=message.session_id,
                    provenance=build_provenance(
                        "explicit_user_request",
                        message_id=message.message_id,
                        workflow_id=workflow_id,
                        session_id=message.session_id,
                        actor="user",
                    ),
                )
            except SecretDetectedError:
                logger.warning("Rejected secret-looking explicit memory request")

    @staticmethod
    def _extract_explicit_memory(content: str) -> str | None:
        for pattern in _REMEMBER_PATTERNS:
            match = pattern.match(content)
            if match:
                candidate = match.group("content").strip()
                return candidate or None
        return None

    @staticmethod
    def _looks_like_preference(content: str) -> bool:
        normalized = content.casefold()
        return any(token in normalized for token in ("prefer", "preference", "偏好", "喜欢"))

    async def _compact_for_request(
        self,
        *,
        user_id: str,
        session_id: str,
        latest: CompactionRecord | None,
        all_messages: Sequence[MemoryMessage],
        tail: Sequence[MemoryMessage],
        attachments: RecoveryAttachments,
        hook_results: Sequence[Mapping[str, Any]] | None,
        active_plan: Any = None,
        compaction_model_type: str | None = None,
    ) -> CompactionRecord | None:
        source = self._compaction_source(latest, all_messages, tail)
        # Preserve the active request verbatim as the post-boundary tail.
        current_request: MemoryMessage | None = None
        if source and source[-1].role == "user" and not _is_synthetic_summary(source[-1]):
            current_request = source[-1]
            source = source[:-1]
        if not source:
            return latest
        if _is_synthetic_summary(source[-1]):
            return latest
        if latest is None:
            source_context_messages = self._project(None, source)
        else:
            retained_tail = [
                message
                for message in tail
                if current_request is None
                or message.message_id != current_request.message_id
            ]
            source_context_messages = self._project(latest, retained_tail)

        turns = completed_turns(
            [message for message in source if not _is_synthetic_summary(message)]
        )
        required_tokens = estimate_tokens(current_request.content) if current_request else 0
        if active_plan is not None:
            required_tokens += estimate_tokens(active_plan)
        retained_turns = select_recent_turns(
            turns,
            available_tokens=max(0, self.settings.input_budget - required_tokens),
            summary_target_tokens=self.settings.target_tokens,
            max_turns=self.settings.recent_turns,
        )
        retained_ids = {
            message.message_id
            for turn in retained_turns
            for message in turn
            if isinstance(message, MemoryMessage)
        }
        summary_source = [
            message for message in source if message.message_id not in retained_ids
        ]
        if not summary_source:
            return latest
        if _is_synthetic_summary(summary_source[-1]):
            return latest
        covered_user_ids = [
            message.message_id
            for message in summary_source
            if message.role == "user" and not _is_synthetic_summary(message)
        ]
        for message in summary_source:
            if _is_synthetic_summary(message):
                covered_user_ids[:0] = list(summary_user_message_ids(message.content))
        try:
            record = await self.compactor.compact(
                summary_source,
                trigger="auto",
                attachments=attachments,
                hook_results=hook_results,
                retained_message_ids=tuple(
                    message.message_id
                    for turn in retained_turns
                    for message in turn
                    if isinstance(message, MemoryMessage)
                ),
                retained_messages=tuple(
                    message for turn in retained_turns for message in turn
                ),
                source_context_messages=source_context_messages,
                retained_turn_count=len(retained_turns),
                covered_user_message_ids=covered_user_ids,
                summarizer_override=(
                    self._build_summarizer(compaction_model_type)
                    if compaction_model_type
                    else None
                ),
            )
        except CompactionToolCallError:
            logger.warning("Memory compaction discarded because summarizer called a tool")
            return latest
        except CompactionValidationError as exc:
            logger.info("Memory compaction candidate discarded: %s", exc)
            return latest
        watermark = max((message.sequence for message in all_messages), default=0)
        record = replace(
            record,
            metadata={
                **record.metadata,
                "transcript_watermark_sequence": watermark,
            },
        )
        try:
            return await asyncio.to_thread(self.store.save_compaction, record)
        except MemoryStoreError as exc:
            logger.info("Memory compaction candidate discarded: %s", exc)
            return latest

    @staticmethod
    def _compaction_source(
        latest: CompactionRecord | None,
        all_messages: Sequence[MemoryMessage],
        tail: Sequence[MemoryMessage],
    ) -> list[MemoryMessage]:
        if latest is None:
            return list(all_messages)
        synthetic = MemoryMessage(
            message_id=f"summary:{latest.compaction_id}",
            user_id=latest.user_id,
            session_id=latest.session_id,
            sequence=latest.boundary.last_sequence,
            role="assistant",
            content=latest.summary,
            metadata={
                "memory_type": "prior_summary",
                "covered_message_ids": list(
                    latest.metadata.get("covered_message_ids")
                    or latest.metadata.get("covered_user_message_ids")
                    or ()
                ),
            },
        )
        return [synthetic, *tail]

    @staticmethod
    def _project(
        latest: CompactionRecord | None, messages: Sequence[MemoryMessage]
    ) -> list[dict[str, Any]]:
        projection = render_compaction_segments(latest) if latest else []
        projection.extend(
            {
                "role": message.role,
                "content": message.content,
                "metadata": {
                    **message.metadata,
                    "message_id": message.message_id,
                    "sequence": message.sequence,
                },
            }
            for message in messages
        )
        return projection

    @staticmethod
    def _latest_user_content(messages: Sequence[MemoryMessage]) -> str:
        for message in reversed(messages):
            if message.role == "user":
                return message.content
        return ""

    @staticmethod
    def _sanitize_message(message: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "role": str(message.get("role", "user")),
            "content": redact_secrets(str(message.get("content", ""))),
            **(
                {"metadata": dict(message.get("metadata") or {})}
                if message.get("metadata")
                else {}
            ),
        }

    async def record_assistant_outputs(
        self,
        *,
        user_id: str,
        session_id: str,
        outputs: Sequence[Mapping[str, Any]],
        workflow_id: str | None = None,
    ) -> list[MemoryMessage]:
        history = await asyncio.to_thread(
            self.store.list_messages, user_id, session_id
        )
        turn_id = next(
            (
                message.metadata.get("turn_id") or message.message_id
                for message in reversed(history)
                if message.role == "user"
            ),
            None,
        )
        messages = []
        for output in outputs:
            if output.get("user_visible") is False:
                continue
            content = str(output.get("content", "")).strip()
            if not content:
                continue
            agent_name = str(output.get("agent_name", "assistant"))
            if "agent_proxy" in agent_name or agent_name.startswith("scheduler"):
                continue
            identifier = output.get("message_id") or uuid5(
                NAMESPACE_URL,
                f"superagent-output:{workflow_id}:{session_id}:{agent_name}:{content}",
            ).hex
            messages.append(
                MemoryMessage(
                    message_id=str(identifier),
                    user_id=user_id,
                    session_id=session_id,
                    sequence=0,
                    role="assistant",
                    content=content,
                    workflow_id=workflow_id,
                    metadata={
                        "agent_name": agent_name,
                        "turn_complete": bool(output.get("turn_complete", True)),
                        "main_visible": True,
                        "turn_id": turn_id,
                    },
                )
            )
        if not messages or not self.settings.enabled:
            return []
        stored = await asyncio.to_thread(self.store.append_messages, messages)
        if self.settings.auto_consolidation_enabled:
            try:
                await self._consolidate_pending(user_id, session_id, workflow_id)
            except Exception as exc:
                logger.warning(
                    "Memory consolidation deferred after response: %s",
                    type(exc).__name__,
                )
        return stored

    async def _consolidate_pending(
        self, user_id: str, session_id: str, workflow_id: str | None
    ) -> None:
        watermark = await asyncio.to_thread(
            self.store.get_consolidation_watermark,
            user_id,
            session_id,
            extractor_version=EXTRACTOR_VERSION,
        )
        messages = await asyncio.to_thread(
            self.store.list_messages,
            user_id,
            session_id,
            after_sequence=watermark,
        )
        for turn in completed_turns(messages):
            await self.consolidator.consolidate(turn, workflow_id=workflow_id)
            last_sequence = max(message.sequence for message in turn)
            await asyncio.to_thread(
                self.store.advance_consolidation_watermark,
                user_id,
                session_id,
                last_sequence,
                extractor_version=EXTRACTOR_VERSION,
            )
        if self.settings.markdown_projection_enabled:
            await asyncio.to_thread(self.store.project_markdown, user_id)

    async def compact_session(
        self,
        *,
        user_id: str,
        session_id: str | None = None,
        attachments: RecoveryAttachments | Mapping[str, Any] | None = None,
        hook_results: Sequence[Mapping[str, Any]] | None = None,
    ) -> CompactionRecord:
        resolved = self.resolve_session_id(user_id, session_id=session_id)
        latest, tail = await asyncio.to_thread(
            self.store.messages_after_compaction, user_id, resolved
        )
        all_messages = await asyncio.to_thread(
            self.store.list_messages, user_id, resolved
        )
        source = self._compaction_source(latest, all_messages, tail)
        if not source:
            raise ValueError("session has no messages to compact")
        if latest is not None and not tail:
            return latest
        turns = completed_turns(
            [message for message in source if not _is_synthetic_summary(message)]
        )
        retained_turns = select_recent_turns(
            turns,
            available_tokens=self.settings.input_budget,
            summary_target_tokens=self.settings.target_tokens,
            max_turns=self.settings.recent_turns,
        )
        retained_ids = {
            message.message_id for turn in retained_turns for message in turn
        }
        summary_source = [
            message for message in source if message.message_id not in retained_ids
        ]
        if not summary_source:
            summary_source = list(source)
            retained_turns = ()
        if _is_synthetic_summary(summary_source[-1]):
            return latest
        record = await self.compactor.compact(
            summary_source,
            trigger="manual",
            attachments=attachments,
            hook_results=hook_results,
            retained_message_ids=tuple(
                message.message_id for turn in retained_turns for message in turn
            ),
            retained_messages=tuple(
                message for turn in retained_turns for message in turn
            ),
            source_context_messages=self._project(
                latest, tail if latest is not None else all_messages
            ),
            retained_turn_count=len(retained_turns),
            covered_user_message_ids=tuple(
                dict.fromkeys(
                    [
                        message_id
                        for message in summary_source
                        if _is_synthetic_summary(message)
                        for message_id in summary_user_message_ids(message.content)
                    ]
                    + [
                        message.message_id
                        for message in summary_source
                        if message.role == "user" and not _is_synthetic_summary(message)
                    ]
                )
            ),
        )
        record = replace(
            record,
            metadata={
                **record.metadata,
                "transcript_watermark_sequence": max(
                    (message.sequence for message in all_messages), default=0
                ),
            },
        )
        return await asyncio.to_thread(self.store.save_compaction, record)

    async def compact_if_needed(
        self,
        *,
        user_id: str,
        session_id: str,
        workflow_id: str | None = None,
        current_step_id: str | None = None,
        compaction_model_type: str | None = None,
    ) -> CompactionRecord | None:
        """Compact conversation history at a durable workflow safe point.

        The caller must invoke this only after the StepResult and checkpoint are
        durable.  Plan/TaskGraph state is intentionally represented only by
        identifiers here and remains authoritative in workflow storage.
        """

        if not self.settings.enabled or not self.settings.auto_compact_enabled:
            return None
        resolved = self.resolve_session_id(user_id, session_id=session_id)
        latest, tail = await asyncio.to_thread(
            self.store.messages_after_compaction, user_id, resolved
        )
        all_messages = await asyncio.to_thread(
            self.store.list_messages, user_id, resolved
        )
        source = self._compaction_source(latest, all_messages, tail)
        if not source or not self.compactor.should_compact(source):
            return None
        candidate = await self._compact_for_request(
            user_id=user_id,
            session_id=resolved,
            latest=latest,
            all_messages=all_messages,
            tail=tail,
            attachments=RecoveryAttachments(
                extra={
                    "workflow_id": workflow_id,
                    "current_step_id": current_step_id,
                    "safe_point": "between_steps",
                    "rebuild_runtime_capabilities": True,
                }
            ),
            hook_results=None,
            active_plan=None,
            compaction_model_type=compaction_model_type,
        )
        if candidate is None or (
            latest is not None and candidate.compaction_id == latest.compaction_id
        ):
            return None
        return candidate

    async def remember(self, **kwargs: Any):
        if not self.settings.enabled or not self.settings.long_term_enabled:
            raise RuntimeError("long-term memory is disabled")
        content = str(kwargs.get("content", ""))
        if len(content) > self.settings.max_record_chars:
            raise ValueError("memory content exceeds configured size limit")
        record = await asyncio.to_thread(self.store.remember, **kwargs)
        if self.settings.markdown_projection_enabled:
            try:
                await asyncio.to_thread(self.store.project_markdown, record.user_id)
            except Exception as exc:
                logger.warning(
                    "MEMORY.md projection deferred: %s", type(exc).__name__
                )
        return record

    async def list_long_term(
        self,
        user_id: str,
        *,
        query: str | None = None,
        project_id: str | None = None,
    ):
        records = await asyncio.to_thread(self.store.list_long_term, user_id)
        if not query:
            return records
        return self.retriever.retrieve(
            query,
            records,
            user_id=user_id,
            top_k=self.settings.long_term_top_k,
            project_id=project_id,
        )

    async def recall_labels(
        self,
        *,
        user_id: str,
        query: str,
        intent_tags: Sequence[str] = (),
        scopes: Sequence[str] | None = None,
        project_id: str | None = None,
        memory_keys: Sequence[str] | None = None,
    ) -> tuple[str, tuple[str, ...]]:
        reference, memory_ids, _entries = await self.recall_context(
            user_id=user_id,
            query=query,
            intent_tags=intent_tags,
            scopes=scopes,
            project_id=project_id,
            memory_keys=memory_keys,
        )
        return reference, memory_ids

    async def recall_context(
        self,
        *,
        user_id: str,
        query: str,
        intent_tags: Sequence[str] = (),
        scopes: Sequence[str] | None = None,
        project_id: str | None = None,
        memory_keys: Sequence[str] | None = None,
    ) -> tuple[str, tuple[str, ...], tuple[dict[str, Any], ...]]:
        if not self.settings.enabled or not self.settings.long_term_enabled:
            return "", (), ()
        records = await asyncio.to_thread(self.store.list_long_term, user_id)
        normalized_memory_keys = {
            str(key).strip() for key in memory_keys or () if str(key).strip()
        }
        if normalized_memory_keys:
            records = [
                record
                for record in records
                if str(record.memory_key or record.kind) in normalized_memory_keys
            ]
        try:
            retrieved = self.retriever.retrieve(
                query,
                records,
                user_id=user_id,
                top_k=self.settings.long_term_top_k,
                scopes=scopes,
                intent_tags=intent_tags,
                project_id=project_id,
            )
        except TypeError:
            retrieved = self.retriever.retrieve(
                query,
                records,
                user_id=user_id,
                top_k=self.settings.long_term_top_k,
                scopes=scopes,
            )
        selected = select_model_memories(
            retrieved, token_budget=self.settings.long_term_token_budget
        )
        return (
            format_untrusted_memories(selected),
            tuple(item.memory.memory_id for item in selected),
            project_model_memories(selected),
        )

    async def forget(self, user_id: str, memory_id: str) -> bool:
        return await asyncio.to_thread(self.store.delete_long_term, user_id, memory_id)

    async def list_session_messages(
        self, user_id: str, session_id: str | None = None
    ) -> list[MemoryMessage]:
        resolved = self.resolve_session_id(user_id, session_id=session_id)
        return await asyncio.to_thread(self.store.list_messages, user_id, resolved)


_manager: MemoryManager | None = None


def _is_synthetic_summary(message: MemoryMessage) -> bool:
    return message.message_id.startswith("summary:")


def get_memory_manager() -> MemoryManager:
    global _manager
    if _manager is None:
        _manager = MemoryManager()
    return _manager


def set_memory_manager(manager: MemoryManager | None) -> None:
    global _manager
    _manager = manager


__all__ = [
    "MemoryManager",
    "MemorySettings",
    "PlanContextOverflowError",
    "get_memory_manager",
    "set_memory_manager",
]
