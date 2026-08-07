"""Policy-bounded, tool-free consolidation of completed conversation turns."""

from __future__ import annotations

import inspect
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping, Sequence

from .models import LongTermMemory, MemoryMessage, utc_now
from .provenance import build_conversation_provenance
from .store import CURRENT_EXTRACTOR_VERSION, MemoryStore
from .utils import contains_secret, normalize_content, redact_secrets


EXTRACTOR_VERSION = CURRENT_EXTRACTOR_VERSION
LLM_EXTRACTOR_VERSION = EXTRACTOR_VERSION
ALLOWED_KINDS = {"fact", "preference", "constraint", "decision", "lesson"}
ALLOWED_SCOPES = {"user", "project", "task"}
USER_DURABILITY_BASES = {
    "explicit_remember",
    "default",
    "recurring",
    "correction",
    "stable_identity",
}
TRUSTED_DURABILITY_BASES = {"verified_workflow"}


@dataclass(frozen=True, slots=True)
class MemoryTagPolicy:
    kind: str
    scope: str
    decay_class: str
    label_prefix: str


OFFICE_MEMORY_TAXONOMY: dict[str, MemoryTagPolicy] = {
    "identity.name": MemoryTagPolicy("fact", "user", "pinned", "Preferred name"),
    "identity.role": MemoryTagPolicy("fact", "user", "slow", "Office role"),
    "organization.name": MemoryTagPolicy("fact", "user", "slow", "Organization"),
    "preference.language": MemoryTagPolicy(
        "preference", "user", "pinned", "Default response language"
    ),
    "preference.response_style": MemoryTagPolicy(
        "preference", "user", "slow", "Response style"
    ),
    "preference.report_style": MemoryTagPolicy(
        "preference", "user", "slow", "Report style"
    ),
    "preference.document_format": MemoryTagPolicy(
        "preference", "user", "slow", "Document format"
    ),
    "preference.communication_channel": MemoryTagPolicy(
        "preference", "user", "slow", "Communication channel"
    ),
    "constraint.approval": MemoryTagPolicy(
        "constraint", "user", "pinned", "Approval constraint"
    ),
    "constraint.privacy": MemoryTagPolicy(
        "constraint", "user", "pinned", "Privacy constraint"
    ),
    "constraint.schedule": MemoryTagPolicy(
        "constraint", "user", "medium", "Schedule constraint"
    ),
    "decision.workflow": MemoryTagPolicy(
        "decision", "user", "medium", "Workflow decision"
    ),
    "task.recurring": MemoryTagPolicy(
        "fact", "user", "medium", "Recurring task"
    ),
    "lesson.workflow": MemoryTagPolicy(
        "lesson", "user", "fast", "Workflow lesson"
    ),
}

_PROMPT_INJECTION_PATTERNS = (
    re.compile(
        r"(?:ignore|disregard|bypass|override).{0,40}"
        r"(?:instruction|system prompt|security|approval|permission|policy)",
        re.I,
    ),
    re.compile(
        r"(?:忽略|无视|绕过|覆盖|修改).{0,24}"
        r"(?:指令|系统提示|安全限制|安全策略|审批|权限|策略)"
    ),
    re.compile(r"(?:授予|提升|获取).{0,12}(?:权限|授权)"),
)

_SELF_ATTRIBUTION_PATTERNS = {
    "identity.name": re.compile(
        r"(?:\bmy name is\b|\bcall me\b|\u6211\u53eb|"
        r"\u6211\u7684\u540d\u5b57(?:\u662f|\u53eb)?|"
        r"\u8bf7\u79f0\u547c\u6211(?:\u4e3a)?|\u53eb\u6211)",
        re.I,
    ),
    "identity.role": re.compile(
        r"(?:\bmy role is\b|\bi work as\b|\u6211\u7684(?:\u804c\u4f4d|\u5c97\u4f4d)\u662f|"
        r"\u6211\u62c5\u4efb|\u6211\u662f)",
        re.I,
    ),
    "organization.name": re.compile(
        r"(?:\bmy (?:company|organization) is\b|\bi work at\b|"
        r"\u6211\u5c31\u804c\u4e8e|\u6211\u7684\u516c\u53f8\u662f|"
        r"\u6211\u5728)",
        re.I,
    ),
}


def _has_self_attributed_value(tag: str, value: Any, source_text: str) -> bool:
    pattern = _SELF_ATTRIBUTION_PATTERNS.get(tag)
    if pattern is None:
        return True
    if not isinstance(value, str) or not value.strip():
        return False
    normalized_value = normalize_content(value)
    for match in pattern.finditer(source_text):
        window = source_text[match.start() : match.end() + 120]
        if normalized_value in normalize_content(window):
            return True
    return False

Extractor = Callable[
    [Sequence[MemoryMessage]],
    Sequence[Mapping[str, Any]] | Awaitable[Sequence[Mapping[str, Any]]],
]


class MemoryExtractionError(RuntimeError):
    """Base error for retryable durable-memory extraction failures."""


class MemoryExtractionContractError(MemoryExtractionError):
    """The model response did not satisfy the structured extraction contract."""


class MemoryExtractionToolCallError(MemoryExtractionError):
    """The tool-free extraction model attempted to call a tool."""


class _MemoryCandidatePolicyRejection(ValueError):
    """A well-formed candidate that policy requires us to discard."""


def _llm_content_text(content: Any) -> str:
    """Normalize text-only chat-model responses without accepting tool blocks."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        parts: list[str] = []
        for block in content:
            if isinstance(block, Mapping):
                block_type = str(block.get("type") or "text").casefold()
                if block_type not in {"text", "output_text"}:
                    raise MemoryExtractionContractError(
                        "memory extractor returned a non-text content block"
                    )
                value = block.get("text")
            else:
                value = getattr(block, "text", None)
            if value:
                parts.append(str(value))
        if parts:
            return "\n".join(parts).strip()
    raise MemoryExtractionContractError(
        "memory extractor returned no parseable text content"
    )


def build_llm_extractor(model: Any) -> Extractor:
    """Build a tool-free structured extractor for completed turns.

    The model is deliberately injected by the caller so this module remains
    usable in tests and does not own model configuration. Invalid, malformed,
    tool-calling, or cross-turn source references raise a retryable contract
    error so callers never mark the turn as successfully consolidated.
    """

    async def _extract(turn: Sequence[MemoryMessage]) -> Sequence[Mapping[str, Any]]:
        def field(message: Any, name: str, default: Any = None) -> Any:
            if isinstance(message, Mapping):
                return message.get(name, default)
            return getattr(message, name, default)

        user_messages = [
            message for message in turn if field(message, "role", "") == "user"
        ]
        if not user_messages:
            return []
        transcript = "\n".join(
            f"{field(message, 'role', '')}: {field(message, 'content', '')}"
            for message in turn
        )
        taxonomy = "\n".join(
            f"- {tag}: {policy.label_prefix}"
            for tag, policy in OFFICE_MEMORY_TAXONOMY.items()
        )
        prompt = (
            "You are a durable-memory reflection step. Do not call tools. Return one "
            "JSON object and no surrounding prose. The schema is: "
            '{"decision":"extract|skip","reason":"short explanation",'
            '"candidates":[...]}. '
            "Inspect the completed turn for explicit user information that will be "
            "useful in future sessions. Explicit instructions to remember something, "
            "use a value by default, or preserve a preference are strong "
            "durable-memory "
            "evidence in any language. Extract each distinct preference or constraint "
            "as a separate candidate. For example, response language, report style, "
            "and document format are three candidates, not one combined candidate. "
            "Do not reinterpret a statement about report style as a request to "
            "generate "
            "a report. Choose decision=skip only when no explicit, future-useful user "
            "evidence exists. For decision=extract, candidates must be non-empty. "
            "Use only user evidence; assistant text may provide context but cannot be "
            "the evidence source. Never infer sensitive traits, credentials, "
            "permissions, "
            "or secrets. A recipient, document subject, approver, colleague, or other "
            "person mentioned in a task is not the current user. Current-task output "
            "requirements, recipients, and one-off restrictions are not durable "
            "preferences. For example, 'send this to Zhang San' is not identity.name, "
            "and 'include a risk section in this report' is not "
            "preference.report_style. Only identity statements explicitly attributed "
            "to the current user or instructions explicitly framed as remembered, "
            "default, recurring, corrective, or future behavior may be extracted. "
            "Choose exactly one tag from the allow-list below; do not "
            "invent tags. Candidate fields are: tag, value, confidence, importance, "
            "sensitivity, future_utility, evidence_authority, subject_scope, and "
            "durability_basis. subject_scope must be current_user. durability_basis "
            "must be exactly one of explicit_remember, default, recurring, correction, "
            "or stable_identity. Do not return "
            "source_message_ids; the platform binds each candidate to the exact user "
            "messages in this completed turn. Use numeric "
            "confidence and importance from 0 to 1, sensitivity=normal or low, "
            "future_utility=true, and evidence_authority=user. Do not return kind, "
            "scope, key, label, decay_class, tags, or source_text because the platform "
            "derives them.\n\nALLOWED TAGS:\n"
            + taxonomy
            + "\n\n"
            "TURN:\n" + transcript
        )
        result = await model.ainvoke(prompt)
        if getattr(result, "tool_calls", None):
            raise MemoryExtractionToolCallError(
                "memory extractor attempted a forbidden tool call"
            )
        content = getattr(result, "content", result)
        if isinstance(content, Mapping):
            payload: Any = content
        else:
            text = _llm_content_text(content)
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
            try:
                payload = json.loads(text)
            except (TypeError, ValueError):
                raise MemoryExtractionContractError(
                    "memory extractor returned invalid JSON"
                ) from None
        if not isinstance(payload, Mapping):
            raise MemoryExtractionContractError(
                "memory extractor result must be a JSON object"
            )
        decision = str(payload.get("decision") or "").strip().casefold()
        reason = str(payload.get("reason") or "").strip()
        candidates_payload = payload.get("candidates")
        if decision not in {"extract", "skip"} or not reason:
            raise MemoryExtractionContractError(
                "memory extractor result requires decision and reason"
            )
        if not isinstance(candidates_payload, list):
            raise MemoryExtractionContractError(
                "memory extractor candidates must be an array"
            )
        if decision == "extract" and not candidates_payload:
            raise MemoryExtractionContractError(
                "memory extractor chose extract without candidates"
            )
        if decision == "skip" and candidates_payload:
            raise MemoryExtractionContractError(
                "memory extractor chose skip with candidates"
            )
        if decision == "skip":
            return []
        user_evidence = {
            str(field(message, "message_id", "")): str(field(message, "content", ""))
            for message in user_messages
        }
        candidates: list[Mapping[str, Any]] = []
        for item in candidates_payload:
            if not isinstance(item, Mapping):
                raise MemoryExtractionContractError(
                    "memory extractor candidate must be an object"
                )
            tag = normalize_memory_key(str(item.get("tag") or ""))
            if tag not in OFFICE_MEMORY_TAXONOMY:
                raise MemoryExtractionContractError(
                    "memory extractor candidate used an unsupported tag"
                )
            subject_scope = str(item.get("subject_scope") or "").strip().casefold()
            if subject_scope != "current_user":
                raise MemoryExtractionContractError(
                    "memory extractor candidate is not attributed to the current user"
                )
            durability_basis = str(
                item.get("durability_basis") or ""
            ).strip().casefold()
            if durability_basis not in USER_DURABILITY_BASES:
                raise MemoryExtractionContractError(
                    "memory extractor candidate lacks a supported durability basis"
                )
            source_ids = tuple(user_evidence)
            source_text = "\n".join(
                user_evidence[source_id] for source_id in source_ids
            )
            if not _has_self_attributed_value(
                tag, item.get("value"), source_text
            ):
                raise MemoryExtractionContractError(
                    "memory extractor identity candidate lacks user self-attribution"
                )
            candidate = dict(item)
            candidate["tag"] = tag
            candidate["subject_scope"] = subject_scope
            candidate["durability_basis"] = durability_basis
            candidate["source_message_ids"] = source_ids
            candidate["source_text"] = source_text
            candidate.setdefault("extractor_version", LLM_EXTRACTOR_VERSION)
            candidate.setdefault("future_utility", True)
            candidate.setdefault("evidence_authority", "user")
            candidates.append(candidate)
        return candidates

    return _extract


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    kind: str
    scope: str
    key: str
    value: Any
    label: str
    source_text: str
    source_message_ids: tuple[str, ...]
    confidence: float
    importance: float
    decay_class: str
    sensitivity: str = "normal"
    future_utility: bool = True
    evidence_authority: str = "user"
    subject_scope: str = ""
    durability_basis: str = ""
    created_at: datetime = field(default_factory=utc_now)
    last_reinforced_at: datetime = field(default_factory=utc_now)
    reinforcement_count: int = 0
    extractor_version: str = EXTRACTOR_VERSION
    tags: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MemoryCandidate":
        tag = normalize_memory_key(str(data.get("tag") or data.get("key") or ""))
        policy = OFFICE_MEMORY_TAXONOMY.get(tag)
        if policy is None:
            raise ValueError("unsupported office-memory tag")
        value = data.get("value")
        raw_source = str(data.get("source_text", "")).strip()
        raw_value = json.dumps(value, ensure_ascii=False, default=str)
        if any(contains_secret(item) for item in (raw_source, raw_value)):
            raise _MemoryCandidatePolicyRejection(
                "credential-shaped memory candidate"
            )
        display_value = (
            " ".join(value.split())
            if isinstance(value, str)
            else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        )
        if not display_value:
            raise ValueError("memory candidate value is required")
        display_value = display_value[:500]

        def parsed_time(name: str) -> datetime:
            item = data.get(name)
            if isinstance(item, str):
                return datetime.fromisoformat(item.replace("Z", "+00:00"))
            return item if isinstance(item, datetime) else utc_now()

        return cls(
            kind=policy.kind,
            scope=policy.scope,
            key=tag,
            value=value,
            label=redact_secrets(f"{policy.label_prefix}: {display_value}"),
            source_text=redact_secrets(raw_source),
            source_message_ids=tuple(
                str(item) for item in data.get("source_message_ids") or ()
            ),
            confidence=float(data.get("confidence", 0.0)),
            importance=float(data.get("importance", 0.0)),
            decay_class=policy.decay_class,
            sensitivity=str(data.get("sensitivity", "normal") or "normal").casefold(),
            future_utility=bool(data.get("future_utility", False)),
            evidence_authority=str(
                data.get("evidence_authority", "") or ""
            ).casefold(),
            subject_scope=str(data.get("subject_scope", "") or "").casefold(),
            durability_basis=str(
                data.get("durability_basis", "") or ""
            ).casefold(),
            created_at=parsed_time("created_at"),
            last_reinforced_at=parsed_time("last_reinforced_at"),
            reinforcement_count=int(data.get("reinforcement_count", 0) or 0),
            extractor_version=str(
                data.get("extractor_version", EXTRACTOR_VERSION)
                or EXTRACTOR_VERSION
            ),
            tags=(tag,),
        )


def normalize_memory_key(value: str) -> str:
    pieces = re.findall(r"[a-z0-9_]+", value.casefold().replace("-", "_"))
    return ".".join(pieces)


def contains_prompt_injection(value: Any) -> bool:
    """Detect narrow, security-relevant instructions that must not persist."""
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    normalized = " ".join(text.split())
    return any(pattern.search(normalized) for pattern in _PROMPT_INJECTION_PATTERNS)


def _has_self_attributed_identity(candidate: MemoryCandidate) -> bool:
    return _has_self_attributed_value(
        candidate.key, candidate.value, candidate.source_text
    )


class MemoryConsolidator:
    """Extract and persist conservative durable-memory candidates."""

    def __init__(
        self,
        store: MemoryStore,
        *,
        extractor: Extractor | None = None,
        min_confidence: float = 0.70,
        min_importance: float = 0.60,
    ) -> None:
        self.store = store
        self.extractor = extractor
        self.min_confidence = min_confidence
        self.min_importance = min_importance

    async def extract(self, turn: Sequence[MemoryMessage]) -> list[MemoryCandidate]:
        if self.extractor is None:
            raise MemoryExtractionError("durable-memory extractor is unavailable")
        result = self.extractor(turn)
        if inspect.isawaitable(result):
            result = await result
        candidates: list[MemoryCandidate] = []
        for item in result or ():
            try:
                payload = asdict(item) if isinstance(item, MemoryCandidate) else item
                candidates.append(MemoryCandidate.from_dict(payload))
            except _MemoryCandidatePolicyRejection:
                continue
            except (TypeError, ValueError, KeyError) as exc:
                raise MemoryExtractionContractError(
                    "memory extractor candidate failed platform validation"
                ) from exc
        return candidates

    @staticmethod
    def accepts(candidate: MemoryCandidate) -> bool:
        if candidate.kind not in ALLOWED_KINDS or candidate.scope not in ALLOWED_SCOPES:
            return False
        if not candidate.key or not candidate.label or not candidate.source_message_ids:
            return False
        if not candidate.future_utility:
            return False
        if candidate.evidence_authority not in {"user", "trusted_task"}:
            return False
        if candidate.evidence_authority == "user":
            if candidate.subject_scope != "current_user":
                return False
            if candidate.durability_basis not in USER_DURABILITY_BASES:
                return False
            if not _has_self_attributed_identity(candidate):
                return False
        elif (
            candidate.subject_scope != "workflow"
            or candidate.durability_basis not in TRUSTED_DURABILITY_BASES
        ):
            return False
        if candidate.sensitivity not in {"normal", "low"}:
            return False
        if any(
            contains_prompt_injection(value)
            for value in (candidate.value, candidate.label, candidate.source_text)
        ):
            return False
        return not contains_secret(candidate.source_text) and not contains_secret(
            candidate.label
        )

    async def consolidate(
        self,
        turn: Sequence[MemoryMessage],
        *,
        workflow_id: str | None = None,
    ) -> list[LongTermMemory]:
        if not turn:
            return []
        accepted: list[LongTermMemory] = []
        user_id = turn[0].user_id
        session_id = turn[0].session_id
        latest_user = next(
            (message for message in reversed(turn) if message.role == "user"),
            None,
        )
        source_conversation = build_conversation_provenance(
            turn,
            turn_id=(
                str(latest_user.metadata.get("turn_id") or latest_user.message_id)
                if latest_user is not None
                else None
            ),
        )
        for candidate in await self.extract(turn):
            if (
                not self.accepts(candidate)
                or candidate.confidence < self.min_confidence
                or candidate.importance < self.min_importance
            ):
                continue
            current = next(
                (
                    record
                    for record in self.store.list_long_term(user_id)
                    if record.memory_key == candidate.key
                    and record.scope == candidate.scope
                ),
                None,
            )
            is_conflict = current is not None and normalize_content(
                current.content
            ) != normalize_content(str(candidate.value))
            authoritative_correction = (
                candidate.evidence_authority == "user"
                and candidate.confidence >= 0.90
            )
            lifecycle_status = (
                "pending"
                if is_conflict and not authoritative_correction
                else "active"
            )
            accepted.append(
                self.store.remember(
                    user_id=user_id,
                    content=str(candidate.value),
                    kind=("episodic" if candidate.kind == "lesson" else candidate.kind),
                    memory_key=candidate.key,
                    scope=candidate.scope,
                    confidence=candidate.confidence,
                    importance=candidate.importance,
                    value=candidate.value,
                    label=candidate.label,
                    decay_class=candidate.decay_class,
                    last_reinforced_at=candidate.last_reinforced_at,
                    reinforcement_count=candidate.reinforcement_count,
                    source_message_ids=candidate.source_message_ids,
                    sensitivity=candidate.sensitivity,
                    extractor_version=candidate.extractor_version,
                    tags=candidate.tags or (candidate.key,),
                    status=lifecycle_status,
                    workflow_id=workflow_id,
                    session_id=session_id,
                    provenance={
                        "source": "completed_turn",
                        "source_message_ids": list(candidate.source_message_ids),
                        "source_turn_id": source_conversation.get("turn_id"),
                        "extractor_version": candidate.extractor_version,
                        "evidence_authority": candidate.evidence_authority,
                        "subject_scope": candidate.subject_scope,
                        "durability_basis": candidate.durability_basis,
                    },
                    metadata={
                        "source_text": candidate.source_text,
                        "source_conversations": [source_conversation],
                        "future_utility": candidate.future_utility,
                        "subject_scope": candidate.subject_scope,
                        "durability_basis": candidate.durability_basis,
                    },
                )
            )
        return accepted


__all__ = [
    "ALLOWED_KINDS",
    "ALLOWED_SCOPES",
    "USER_DURABILITY_BASES",
    "EXTRACTOR_VERSION",
    "LLM_EXTRACTOR_VERSION",
    "MemoryTagPolicy",
    "OFFICE_MEMORY_TAXONOMY",
    "MemoryCandidate",
    "MemoryConsolidator",
    "MemoryExtractionContractError",
    "MemoryExtractionError",
    "MemoryExtractionToolCallError",
    "build_llm_extractor",
    "contains_prompt_injection",
    "normalize_memory_key",
]
