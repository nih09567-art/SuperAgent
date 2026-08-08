"""Tool-free LLM reflection used to gate Step/Agent Skill distillation."""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


# Reflection is allowed to time out, but a timed-out synchronous model call may
# continue running in its worker. Keep those workers bounded across all skills
# instead of creating one executor (and one potentially leaked thread) per call.
_REFLECTION_EXECUTOR = ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="agent-skill-reflection"
)


@dataclass(frozen=True, slots=True)
class SkillReflectionResult:
    is_reusable: bool
    workflow_family: str
    normalized_procedure: dict[str, Any]
    confidence: float
    reasons: tuple[str, ...] = ()
    risk_notes: tuple[str, ...] = ()
    model_version: str = "unknown"
    valid: bool = True


def _parse_content(result: Any) -> Any:
    if getattr(result, "tool_calls", None):
        raise ValueError("skill reflection attempted a tool call")
    content = getattr(result, "content", result)
    if isinstance(content, Mapping):
        return content
    text = str(content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    return json.loads(text)


def _coerce(payload: Any, *, model_version: str = "unknown") -> SkillReflectionResult:
    if not isinstance(payload, Mapping):
        raise ValueError("reflection output must be an object")
    reusable = payload.get("is_reusable")
    confidence = payload.get("confidence")
    if not isinstance(reusable, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("reflection output has invalid decision fields")
    confidence = max(0.0, min(1.0, float(confidence)))
    family = str(payload.get("workflow_family") or "").strip()[:120]
    procedure = payload.get("normalized_procedure")
    if not isinstance(procedure, Mapping):
        procedure = {}
    reasons = tuple(str(item)[:500] for item in payload.get("reasons") or ())
    risk_notes = tuple(str(item)[:500] for item in payload.get("risk_notes") or ())
    return SkillReflectionResult(
        is_reusable=reusable,
        workflow_family=family,
        normalized_procedure=dict(procedure),
        confidence=confidence,
        reasons=reasons,
        risk_notes=risk_notes,
        model_version=str(payload.get("model_version") or model_version),
    )


class SkillReflection:
    """Adapter around the configured reasoning model; failures fail closed."""

    def __init__(
        self,
        model: Any = None,
        *,
        min_confidence: float = 0.75,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.model = model
        self.min_confidence = float(min_confidence)
        self.timeout_seconds = max(0.1, float(timeout_seconds))

    @classmethod
    def from_default_model(cls) -> "SkillReflection":
        try:
            from src.llm.llm import get_llm_by_type

            return cls(get_llm_by_type("reasoning"))
        except Exception:
            return cls(None)

    def _invoke(self, prompt: str) -> SkillReflectionResult:
        if self.model is None:
            return SkillReflectionResult(
                False, "", {}, 0.0, ("reflection_model_unavailable",), valid=False
            )
        future = None
        try:
            future = _REFLECTION_EXECUTOR.submit(self.model.invoke, prompt)
            raw_result = future.result(timeout=self.timeout_seconds)
            result = _coerce(_parse_content(raw_result))
            accepted = result.is_reusable and result.confidence >= self.min_confidence
            if not accepted:
                return result
            return result
        except FutureTimeoutError:
            if future is not None:
                # This removes queued work. A call already running cannot be
                # forcefully stopped, but remains bounded by the shared pool.
                future.cancel()
            return SkillReflectionResult(
                False, "", {}, 0.0, ("reflection_timeout",), valid=False
            )
        except Exception as exc:
            return SkillReflectionResult(
                False,
                "",
                {},
                0.0,
                (f"reflection_invalid:{type(exc).__name__}",),
                valid=False,
            )

    @staticmethod
    def _evidence_json(evidence: Any) -> str:
        if hasattr(evidence, "model_dump"):
            evidence = evidence.model_dump(mode="json")
        return json.dumps(evidence, ensure_ascii=False, sort_keys=True, default=str)

    def reflect_trace(
        self,
        evidence: Any,
        *,
        source_conversations: Sequence[Mapping[str, Any]] = (),
    ) -> SkillReflectionResult:
        prompt = (
            "You are a workflow skill reviewer. Do not call tools. Return JSON only. "
            "Decide whether this single Agent step is a conventional, repeatable office "
            "procedure (for example leave approval, salary lookup, meeting notification), "
            "not a one-off answer. Ignore concrete people, dates, IDs and values; describe "
            "parameterizable slots and stable preconditions. Do not grant permissions or "
            "claim business success. Required JSON fields: is_reusable (boolean), "
            "workflow_family (short string), normalized_procedure (object), confidence "
            "(0..1), reasons (array), risk_notes (array), model_version (string).\n\n"
            "STEP EVIDENCE:\n"
            + self._evidence_json(evidence)
            + "\nSOURCE CONVERSATIONS:\n"
            + self._evidence_json(list(source_conversations))
        )
        return self._invoke(prompt)

    def reflect_aggregate(
        self,
        evidences: Sequence[Any],
        *,
        source_conversations: Sequence[Mapping[str, Any]] = (),
    ) -> SkillReflectionResult:
        prompt = (
            "You are reviewing multiple executions for one Agent Skill. Do not call tools. "
            "Return JSON only. Decide whether all traces represent one conventional reusable "
            "office procedure with parameterizable inputs, rather than coincidental structural "
            "similarity. Reject if business semantics differ or the procedure depends on one-off "
            "facts. Use the same required JSON fields as single-trace reflection.\n\n"
            "TRACES:\n"
            + self._evidence_json([item.model_dump(mode="json") if hasattr(item, "model_dump") else item for item in evidences])
            + "\nSOURCE CONVERSATIONS:\n"
            + self._evidence_json(list(source_conversations))
        )
        return self._invoke(prompt)


__all__ = ["SkillReflection", "SkillReflectionResult"]
