"""Policy-bounded, tool-free consolidation of completed conversation turns."""

from __future__ import annotations

import inspect
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping, Sequence

from .models import LongTermMemory, MemoryMessage, utc_now
from .store import MemoryStore
from .utils import contains_secret, normalize_content, redact_secrets


EXTRACTOR_VERSION = "heuristic-v1"
LLM_EXTRACTOR_VERSION = "llm-json-v1"
ALLOWED_KINDS = {"fact", "preference", "constraint", "decision", "lesson"}
ALLOWED_SCOPES = {"user", "project", "task"}

Extractor = Callable[
    [Sequence[MemoryMessage]],
    Sequence[Mapping[str, Any]] | Awaitable[Sequence[Mapping[str, Any]]],
]


def build_llm_extractor(model: Any) -> Extractor:
    """Build a tool-free structured extractor for completed turns.

    The model is deliberately injected by the caller so this module remains
    usable in tests and does not own model configuration.  Invalid, malformed,
    tool-calling, or cross-turn source references are discarded as a whole.
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
            f"[{field(message, 'message_id', '')}] {field(message, 'role', '')}: "
            f"{field(message, 'content', '')}"
            for message in turn
        )
        prompt = (
            "You are a durable-memory extractor. Do not call tools. Return JSON "
            "only as an array of candidate objects. Extract zero or more facts, "
            "preferences, constraints, decisions, or reusable lessons that are "
            "likely useful in a future session. Use only explicit user evidence; "
            "never infer sensitive traits, credentials, permissions, or secrets. "
            "Every candidate must cite one or more user message IDs from this turn. "
            "Required fields: kind, scope, key, value, label, source_text, "
            "source_message_ids, confidence, importance, decay_class, sensitivity, "
            "future_utility, evidence_authority, tags. The label must be compact "
            "and must not contain instructions.\n\n"
            "TURN:\n" + transcript
        )
        result = await model.ainvoke(prompt)
        if getattr(result, "tool_calls", None):
            return []
        content = getattr(result, "content", result)
        if isinstance(content, Mapping):
            payload: Any = content
        else:
            text = str(content or "").strip()
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
            try:
                payload = json.loads(text)
            except (TypeError, ValueError):
                return []
        if isinstance(payload, Mapping):
            payload = payload.get("candidates", [])
        if not isinstance(payload, list):
            return []
        user_ids = {str(field(message, "message_id", "")) for message in user_messages}
        candidates: list[Mapping[str, Any]] = []
        for item in payload:
            if not isinstance(item, Mapping):
                continue
            source_ids = tuple(str(value) for value in item.get("source_message_ids") or ())
            if not source_ids or not set(source_ids).issubset(user_ids):
                continue
            candidate = dict(item)
            candidate["source_message_ids"] = source_ids
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
    created_at: datetime = field(default_factory=utc_now)
    last_reinforced_at: datetime = field(default_factory=utc_now)
    reinforcement_count: int = 0
    extractor_version: str = EXTRACTOR_VERSION
    tags: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MemoryCandidate":
        payload = dict(data)
        raw_label = str(payload.get("label", "")).strip()
        raw_source = str(payload.get("source_text", "")).strip()
        raw_value = json.dumps(payload.get("value"), ensure_ascii=False, default=str)
        if any(contains_secret(value) for value in (raw_label, raw_source, raw_value)):
            raise ValueError("credential-shaped memory candidate")
        payload["kind"] = str(payload.get("kind", "fact")).casefold()
        payload["scope"] = str(payload.get("scope", "user")).casefold()
        payload["key"] = normalize_memory_key(str(payload.get("key", "")))
        payload["label"] = redact_secrets(raw_label)
        payload["source_text"] = redact_secrets(raw_source)
        payload["source_message_ids"] = tuple(
            str(item) for item in payload.get("source_message_ids") or ()
        )
        payload["confidence"] = float(payload.get("confidence", 0.0))
        payload["importance"] = float(payload.get("importance", 0.0))
        payload["decay_class"] = str(
            payload.get("decay_class", "medium") or "medium"
        ).casefold()
        payload["sensitivity"] = str(
            payload.get("sensitivity", "normal") or "normal"
        ).casefold()
        payload["future_utility"] = bool(payload.get("future_utility", False))
        payload["evidence_authority"] = str(
            payload.get("evidence_authority", "") or ""
        ).casefold()
        payload["reinforcement_count"] = int(
            payload.get("reinforcement_count", 0) or 0
        )
        payload["extractor_version"] = str(
            payload.get("extractor_version", EXTRACTOR_VERSION)
            or EXTRACTOR_VERSION
        )
        payload["tags"] = tuple(
            normalize_memory_key(str(item))
            for item in payload.get("tags") or ()
            if str(item).strip()
        )
        for key in ("created_at", "last_reinforced_at"):
            value = payload.get(key)
            if isinstance(value, str):
                payload[key] = datetime.fromisoformat(value.replace("Z", "+00:00"))
            elif value is None:
                payload[key] = utc_now()
        return cls(**payload)


def normalize_memory_key(value: str) -> str:
    pieces = re.findall(r"[a-z0-9_]+", value.casefold().replace("-", "_"))
    return ".".join(pieces)


def candidate_from_user_message(message: MemoryMessage) -> MemoryCandidate | None:
    text = " ".join(message.content.strip().split())
    if not text or contains_secret(text):
        return None

    explicit_patterns = (
        re.compile(r"^(?:please\s+)?remember(?:\s+that)?[\s,:]+(?P<value>.+)$", re.I),
        re.compile(r"^\s*\u8bf7\u8bb0\u4f4f[\s,:\uff1a\uff0c]*(?P<value>.+)$"),
    )
    preference_patterns = (
        re.compile(r"^i\s+(?:usually\s+)?prefer[\s,:]+(?P<value>.+)$", re.I),
        re.compile(r"^\s*\u6211(?:\u66f4)?(?:\u504f\u597d|\u559c\u6b22)[\s,:\uff1a\uff0c]*(?P<value>.+)$"),
    )
    match = next((pattern.match(text) for pattern in explicit_patterns if pattern.match(text)), None)
    explicit = match is not None
    if match is None:
        match = next((pattern.match(text) for pattern in preference_patterns if pattern.match(text)), None)
    if match is None:
        return None

    value = match.group("value").strip()
    if not value or contains_secret(value):
        return None
    normalized_value = value.casefold()
    preference_evidence = not explicit or any(
        token in normalized_value
        for token in ("prefer", "preference", "\u504f\u597d", "\u559c\u6b22")
    )
    if preference_evidence and any(
        token in normalized_value
        for token in (
            "language",
            "chinese",
            "english",
            "\u4e2d\u6587",
            "\u82f1\u6587",
            "\u8bed\u8a00",
        )
    ):
        key = "preference.language"
    elif preference_evidence and any(
        token in normalized_value
        for token in ("report", "concise", "detailed", "\u62a5\u544a", "\u7b80\u6d01", "\u8be6\u7ec6")
    ):
        key = "preference.report_style"
    else:
        digest = hashlib.sha256(normalized_value.encode("utf-8")).hexdigest()[:12]
        key = f"{'preference.general' if preference_evidence else 'fact.explicit'}.{digest}"
    kind = "preference" if preference_evidence else "fact"
    label = f"{key}: {value}"
    return MemoryCandidate(
        kind=kind,
        scope="user",
        key=key,
        value=value,
        label=label,
        source_text=text,
        source_message_ids=(message.message_id,),
        confidence=1.0 if explicit else 0.9,
        importance=0.9 if explicit else 0.8,
        decay_class="pinned" if explicit else "slow",
        sensitivity="normal",
        future_utility=True,
        evidence_authority="user",
        tags=(key,),
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
            return [
                candidate
                for message in turn
                if message.role == "user"
                for candidate in [candidate_from_user_message(message)]
                if candidate is not None
            ]
        result = self.extractor(turn)
        if inspect.isawaitable(result):
            result = await result
        candidates: list[MemoryCandidate] = []
        for item in result or ():
            try:
                candidates.append(
                    item if isinstance(item, MemoryCandidate) else MemoryCandidate.from_dict(item)
                )
            except (TypeError, ValueError, KeyError):
                continue
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
        if candidate.sensitivity not in {"normal", "low"}:
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
                        "extractor_version": candidate.extractor_version,
                        "evidence_authority": candidate.evidence_authority,
                    },
                    metadata={
                        "source_text": candidate.source_text,
                        "future_utility": candidate.future_utility,
                    },
                )
            )
        return accepted


__all__ = [
    "ALLOWED_KINDS",
    "ALLOWED_SCOPES",
    "EXTRACTOR_VERSION",
    "LLM_EXTRACTOR_VERSION",
    "MemoryCandidate",
    "MemoryConsolidator",
    "build_llm_extractor",
    "candidate_from_user_message",
    "normalize_memory_key",
]
