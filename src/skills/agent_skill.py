"""Evidence-based distillation and reuse of one planned Agent step.

Agent skills remain invocation recipes. Complete observable execution traces
are retained separately as audit-only payloads and are never replayed into an
Agent prompt.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field

from src.skills.execution_evidence import (
    SIDE_EFFECT_MODES,
    SkillExecutionEvidence,
    StepExecutionEvidence,
    VerificationStatus,
)
from src.skills.reflection import SkillReflection, SkillReflectionResult
from src.skills.execution_trace import normalize_execution_trace, trace_summary
from src.memory.utils import redact_secrets


_RISK_LEVEL = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
_TRUSTED_VERIFIER_METHODS = {
    "trusted_business_verifier",
    "trusted_verifier",
    "provider_query",
}
_DATA_PATH_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _sanitize_snapshot(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _sanitize_snapshot(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_snapshot(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_snapshot(item) for item in value]
    if isinstance(value, str):
        return redact_secrets(value)[:12000]
    return value


def _normalize_token(value: Any, default: str = "") -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value or "").casefold()).strip("_")
    return normalized or default


def _normalize_values(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        source: Iterable[Any] = [value]
    elif isinstance(value, Iterable) and not isinstance(value, Mapping):
        source = value
    else:
        source = []
    return tuple(
        dict.fromkeys(
            token
            for item in source
            if (token := _normalize_token(item))
        )
    )


def _operation_mode(value: Any) -> str:
    mode = _normalize_token(value, "read")
    aliases = {"query": "read", "lookup": "read", "search": "read"}
    return aliases.get(mode, mode)


def _risk(value: Any) -> str:
    candidate = str(value or "LOW").upper()
    return candidate if candidate in _RISK_LEVEL else "LOW"


def _step_id(index: int, step: Mapping[str, Any]) -> str:
    return str(step.get("step_id") or step.get("subtask_id") or f"step_{index + 1}")


def _safe_ref(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    artifact_id = str(raw.get("artifact_id") or "").strip()
    if not artifact_id:
        return None
    result: dict[str, Any] = {"artifact_id": artifact_id}
    if isinstance(raw.get("version"), int):
        result["version"] = raw["version"]
    schema_ref = raw.get("expected_schema_ref") or raw.get("schema_ref")
    if schema_ref:
        result["schema_ref"] = str(schema_ref)
    return result


def _normalize_input_bindings(step: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
    bindings: list[dict[str, str]] = []
    for raw in step.get("inputs") or step.get("input_bindings") or ():
        if not isinstance(raw, Mapping):
            continue
        parameter_name = str(raw.get("parameter_name") or "").strip()
        if not parameter_name or not _DATA_PATH_RE.fullmatch(parameter_name):
            continue
        source_artifacts = raw.get("source_artifacts")
        sources = source_artifacts if isinstance(source_artifacts, list) else [raw]
        for source in sources:
            if not isinstance(source, Mapping):
                continue
            source_output = str(source.get("source_output") or "").strip()
            if not source_output or not _DATA_PATH_RE.fullmatch(source_output):
                continue
            bindings.append(
                {
                    "parameter_name": parameter_name,
                    "source_output": source_output,
                }
            )
    bindings.sort(key=lambda item: (item["parameter_name"], item["source_output"]))
    return tuple(bindings)


def _verification_contract(raw: Any) -> dict[str, Any]:
    source = raw if isinstance(raw, Mapping) else {}
    result: dict[str, Any] = {}
    for field in ("required", "trusted_verifier_required"):
        if isinstance(source.get(field), bool):
            result[field] = source[field]
    for field in ("method", "schema_ref"):
        value = source.get(field)
        if isinstance(value, (str, int)) and str(value).strip():
            result[field] = str(value).strip()
    return result


def _retry_policy(step: Mapping[str, Any], operation_mode: str) -> dict[str, Any]:
    attempts = max(1, int(step.get("retry") or 0) + 1)
    if operation_mode != "read":
        attempts = 1
    return {
        "max_attempts": attempts,
        "fallback": "original_step" if operation_mode == "read" else "reconciliation",
    }


def _capability(
    step: Mapping[str, Any],
    agent_capabilities: Mapping[str, Sequence[str]] | None = None,
    task_profile: Mapping[str, Any] | None = None,
) -> str:
    explicit = (
        step.get("capability")
        or step.get("expected_capability")
        or step.get("operation")
    )
    if explicit:
        return _normalize_token(explicit, "general")
    required = step.get("required_capabilities")
    if isinstance(required, list) and required:
        return _normalize_token(required[0], "general")
    agent_name = str(step.get("agent_name") or step.get("agent") or "")
    declared = _normalize_values((agent_capabilities or {}).get(agent_name, ()))
    expected = set(_normalize_values((task_profile or {}).get("expected_capabilities")))
    for item in declared:
        if not expected or item in expected:
            return item
    return _normalize_token(agent_name, "general")


def _step_intent(step: Mapping[str, Any], capability: str) -> str:
    raw = step.get("intents") or step.get("intent")
    if isinstance(raw, str):
        return _normalize_token(raw, capability)
    if isinstance(raw, list) and raw:
        return _normalize_token(raw[0], capability)
    return capability


def _expected_outputs(step: Mapping[str, Any]) -> tuple[str, ...]:
    raw = step.get("expected_outputs") or step.get("produces") or ()
    if isinstance(raw, str):
        raw = [raw]
    return tuple(
        item
        for item in (str(value).strip() for value in raw)
        if item and _DATA_PATH_RE.fullmatch(item)
    )


def _data_scopes(task_profile: Mapping[str, Any] | None) -> tuple[str, ...]:
    return _normalize_values((task_profile or {}).get("data_scope")) or ("general",)


def agent_contract_fingerprints(agent_cards: Any) -> dict[str, str]:
    """Hash current Agent invocation contracts without retaining card payloads."""

    fingerprints: dict[str, str] = {}
    for card in agent_cards if isinstance(agent_cards, list) else ():
        if not isinstance(card, Mapping):
            continue
        agent_name = str(card.get("agent_id") or card.get("name") or "").strip()
        if not agent_name:
            continue
        contract = {
            "capabilities": card.get("capabilities") or [],
            "intents": card.get("intents") or [],
            "supported_actions": card.get("supported_actions") or [],
            "accepted_data_scopes": card.get("accepted_data_scopes") or [],
            "risk_ceiling": card.get("risk_ceiling") or "LOW",
            "input_schema": card.get("input_schema") or {},
            "output_schema": card.get("output_schema") or {},
            "contract_version": card.get("contract_version"),
            "requires": card.get("requires") or [],
            "produces": card.get("produces") or [],
            "input_schema_refs": card.get("input_schema_refs") or {},
            "output_schema_refs": card.get("output_schema_refs") or {},
            "version": card.get("version") or "1.0.0",
        }
        fingerprints[agent_name] = hashlib.sha256(
            json.dumps(
                contract,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
    return fingerprints


def agent_capability_bindings(agent_cards: Any) -> dict[str, list[str]]:
    bindings: dict[str, list[str]] = {}
    for card in agent_cards if isinstance(agent_cards, list) else ():
        if not isinstance(card, Mapping):
            continue
        agent_name = str(card.get("agent_id") or card.get("name") or "").strip()
        if agent_name:
            bindings[agent_name] = [
                str(item) for item in card.get("capabilities") or [] if str(item)
            ]
    return bindings


class AgentSkillStatus(str, Enum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    DISABLED = "disabled"


class AgentSkillSettings(BaseModel):
    enabled: bool = True
    reuse_enabled: bool = False
    auto_distill_enabled: bool = True
    reflection_enabled: bool = True
    reflection_min_confidence: float = 0.75
    allow_side_effect_reuse: bool = False
    match_threshold: float = 0.75
    match_margin: float = 0.08
    promotion_success_threshold: int = 2
    failure_disable_threshold: int = 2
    minimum_structure_consistency: float = 1.0
    store_path: Path = Path("store/skills/agent_skills.sqlite3")

    @classmethod
    def from_env(cls) -> "AgentSkillSettings":
        from src.service import env

        return cls(
            enabled=getattr(env, "AGENT_SKILL_ENABLED", True),
            reuse_enabled=getattr(env, "AGENT_SKILL_REUSE_ENABLED", False),
            auto_distill_enabled=getattr(env, "AGENT_SKILL_AUTO_DISTILL_ENABLED", True),
            reflection_enabled=getattr(env, "AGENT_SKILL_REFLECTION_ENABLED", True),
            reflection_min_confidence=getattr(
                env, "AGENT_SKILL_REFLECTION_MIN_CONFIDENCE", 0.75
            ),
            allow_side_effect_reuse=getattr(
                env, "AGENT_SKILL_SIDE_EFFECT_REUSE_ENABLED", False
            ),
            match_threshold=getattr(env, "AGENT_SKILL_MATCH_THRESHOLD", 0.75),
            match_margin=getattr(env, "AGENT_SKILL_MATCH_MARGIN", 0.08),
            promotion_success_threshold=max(
                2, getattr(env, "AGENT_SKILL_PROMOTION_THRESHOLD", 2)
            ),
            failure_disable_threshold=max(
                1, getattr(env, "AGENT_SKILL_FAILURE_THRESHOLD", 2)
            ),
            store_path=Path(
                getattr(env, "AGENT_SKILL_DB_PATH", None)
                or "store/skills/agent_skills.sqlite3"
            ),
        )


class AgentSkillEvidence(BaseModel):
    """Sanitized evidence for one platform-visible Agent invocation."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    user_id: str
    task_id: str
    workflow_id: str = ""
    step_id: str
    agent_name: str
    contract_fingerprint: str
    capability: str
    step_intent: str
    operation_mode: str = "read"
    risk_level: str = "LOW"
    data_scopes: tuple[str, ...] = ("general",)
    input_bindings: tuple[dict[str, str], ...] = ()
    expected_outputs: tuple[str, ...] = ()
    expected_schema_ref: str | None = None
    verification_contract: dict[str, Any] = Field(default_factory=dict)
    retry_policy: dict[str, Any] = Field(default_factory=dict)
    execution_guidance: str
    dependency_step_ids: tuple[str, ...] = ()
    dependency_success: bool = True
    technical_success: bool = False
    business_success: bool | None = None
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    verification_method: str | None = None
    schema_valid: bool | None = None
    output_accepted: bool = False
    idempotency_key_present: bool = False
    external_operation_id_present: bool = False
    needs_reconciliation: bool = False
    artifact_refs: tuple[dict[str, Any], ...] = ()
    source_conversations: tuple[dict[str, Any], ...] = ()
    execution_trace: dict[str, Any] = Field(default_factory=dict)
    reflection_accepted: bool = False
    reflection_family: str = ""
    reflection_procedure: dict[str, Any] = Field(default_factory=dict)
    reflection_confidence: float = 0.0
    reflection_reasons: tuple[str, ...] = ()
    reflection_model_version: str = ""
    created_at: str = Field(default_factory=_now)

    @property
    def is_side_effect(self) -> bool:
        return self.operation_mode.casefold() in SIDE_EFFECT_MODES


class AgentSkillDecision(BaseModel):
    contributes: bool
    promotion_ready: bool
    reasons: list[str] = Field(default_factory=list)


def evaluate_agent_skill_evidence(evidence: AgentSkillEvidence) -> AgentSkillDecision:
    hard_reasons: list[str] = []
    promotion_reasons: list[str] = []
    if not evidence.agent_name or not evidence.capability:
        hard_reasons.append("agent_or_capability_missing")
    if not evidence.contract_fingerprint:
        hard_reasons.append("agent_contract_missing")
    if not evidence.technical_success:
        hard_reasons.append("step_not_technically_successful")
    if not evidence.dependency_success:
        hard_reasons.append("dependency_failed_or_missing")
    if evidence.needs_reconciliation:
        hard_reasons.append("reconciliation_required")

    if evidence.is_side_effect:
        if evidence.business_success is not True:
            promotion_reasons.append("business_outcome_not_verified")
        if evidence.verification_status != VerificationStatus.VERIFIED:
            promotion_reasons.append("verification_missing")
        if not (
            evidence.idempotency_key_present
            or evidence.external_operation_id_present
        ):
            promotion_reasons.append("business_identity_missing")
        if evidence.operation_mode.casefold() in {"approve", "delete"}:
            trusted_required = bool(
                evidence.verification_contract.get("trusted_verifier_required", True)
            )
            if trusted_required and str(evidence.verification_method or "").casefold() not in _TRUSTED_VERIFIER_METHODS:
                promotion_reasons.append("trusted_verifier_missing")
    elif not (
        evidence.schema_valid is True
        or bool(evidence.expected_schema_ref)
        or evidence.output_accepted
    ):
        promotion_reasons.append("typed_or_accepted_output_missing")

    contributes = not hard_reasons
    return AgentSkillDecision(
        contributes=contributes,
        promotion_ready=contributes and not promotion_reasons,
        reasons=[*hard_reasons, *promotion_reasons],
    )


class AgentSkillApplicability(BaseModel):
    capability: str
    step_intent: str
    operation_mode: str
    max_risk: str = "LOW"
    data_scopes: tuple[str, ...] = ("general",)
    input_bindings: tuple[dict[str, str], ...] = ()
    expected_outputs: tuple[str, ...] = ()
    expected_schema_ref: str | None = None
    verification_contract: dict[str, Any] = Field(default_factory=dict)


class AgentSkillRecipe(BaseModel):
    agent_name: str
    contract_fingerprint: str
    request_placeholder: str = "{{CURRENT_REQUEST}}"
    execution_guidance: str
    retry_policy: dict[str, Any] = Field(default_factory=dict)


class AgentSkillQuality(BaseModel):
    support_count: int = 0
    promotion_evidence_count: int = 0
    structure_consistency: float = 0.0
    contract_stability: float = 1.0
    typed_output_rate: float = 0.0
    business_verification_rate: float | None = None
    execution_success_rate: float = 1.0


class AgentSkillProvenance(BaseModel):
    source_task_ids: list[str] = Field(default_factory=list)
    source_evidence_ids: list[str] = Field(default_factory=list)
    distilled_at: str = Field(default_factory=_now)


class AgentSkillCard(BaseModel):
    model_config = ConfigDict(extra="allow")

    skill_id: str
    user_id: str
    name: str
    description: str
    schema_version: int = 1
    status: AgentSkillStatus = AgentSkillStatus.CANDIDATE
    version: int = 1
    family_signature: str
    signature: str
    applicability: AgentSkillApplicability
    recipe: AgentSkillRecipe
    quality: AgentSkillQuality = Field(default_factory=AgentSkillQuality)
    evidence_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    consecutive_failures: int = 0
    confidence: float = 0.5
    provenance: AgentSkillProvenance = Field(default_factory=AgentSkillProvenance)
    source_conversations: list[dict[str, Any]] = Field(default_factory=list)
    execution_traces: list[dict[str, Any]] = Field(default_factory=list)
    aggregate_reflection_accepted: bool = False
    aggregate_reflection_reasons: list[str] = Field(default_factory=list)
    aggregate_reflection_model_version: str = ""
    reflection_family: str = ""
    reflection_procedure: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)
    last_used_at: str | None = None


class AgentSkillDistillationResult(BaseModel):
    card: AgentSkillCard
    decision: AgentSkillDecision
    evidence_saved: bool
    created: bool
    promoted: bool


class AgentSkillMatch(BaseModel):
    skill: AgentSkillCard
    score: float
    reason: str
    checks: dict[str, bool]
    bound_step: dict[str, Any]


class ResolvedAgentSkillBinding(BaseModel):
    skill_id: str
    version: int
    signature: str
    execution_guidance: str
    retry_policy: dict[str, Any] = Field(default_factory=dict)


class AgentSkillBindingResult(BaseModel):
    steps: list[dict[str, Any]]
    bindings: dict[str, str] = Field(default_factory=dict)
    matches: list[AgentSkillMatch] = Field(default_factory=list)
    rejections: list[dict[str, Any]] = Field(default_factory=list)


def _family_signature(evidence: AgentSkillEvidence) -> str:
    return _hash(
        {
            "capability": evidence.capability,
            "step_intent": evidence.step_intent,
            "operation_mode": evidence.operation_mode,
            "input_bindings": list(evidence.input_bindings),
            "expected_outputs": evidence.expected_outputs,
            "expected_schema_ref": evidence.expected_schema_ref,
            "verification_class": _verification_contract(
                evidence.verification_contract
            ),
        }
    )


def _implementation_signature(evidence: AgentSkillEvidence) -> str:
    return _hash(
        {
            "family": _family_signature(evidence),
            "agent_name": evidence.agent_name,
            "contract_fingerprint": evidence.contract_fingerprint,
            "execution_guidance": evidence.execution_guidance,
            "retry_policy": evidence.retry_policy,
            "verification_contract": _verification_contract(
                evidence.verification_contract
            ),
        }
    )


class AgentSkillStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.audit_dir = self.path.parent / "agent_skill_audit"
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path), timeout=30, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS agent_skills (
                    skill_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    family_signature TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(user_id, signature)
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_skills_user "
                "ON agent_skills(user_id)"
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS agent_skill_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    family_signature TEXT NOT NULL,
                    implementation_signature TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(user_id, task_id, step_id, implementation_signature)
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_skill_evidence_bucket "
                "ON agent_skill_evidence(user_id, family_signature, "
                "implementation_signature)"
            )

    @staticmethod
    def _card(row: sqlite3.Row) -> AgentSkillCard:
        return AgentSkillCard.model_validate(json.loads(row["payload"]))

    @staticmethod
    def _evidence(row: sqlite3.Row) -> AgentSkillEvidence:
        return AgentSkillEvidence.model_validate(json.loads(row["payload"]))

    def get(self, user_id: str, skill_id: str) -> AgentSkillCard | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_skills WHERE user_id=? AND skill_id=?",
                (user_id, skill_id),
            ).fetchone()
        return self._card(row) if row else None

    def get_by_signature(self, user_id: str, signature: str) -> AgentSkillCard | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_skills WHERE user_id=? AND signature=?",
                (user_id, signature),
            ).fetchone()
        return self._card(row) if row else None

    def list(self, user_id: str) -> list[AgentSkillCard]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_skills WHERE user_id=? ORDER BY created_at",
                (user_id,),
            ).fetchall()
        return [self._card(row) for row in rows]

    def list_active(self, user_id: str) -> list[AgentSkillCard]:
        return [
            card for card in self.list(user_id)
            if card.status == AgentSkillStatus.ACTIVE
        ]

    def save_evidence(
        self,
        evidence: AgentSkillEvidence,
        family_signature: str,
        implementation_signature: str,
    ) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO agent_skill_evidence
                (evidence_id,user_id,task_id,step_id,family_signature,
                 implementation_signature,payload,created_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (
                    evidence.evidence_id,
                    evidence.user_id,
                    evidence.task_id,
                    evidence.step_id,
                    family_signature,
                    implementation_signature,
                    _json(evidence.model_dump(mode="json")),
                    evidence.created_at,
                ),
            )
            return cursor.rowcount > 0

    def save_execution_trace(
        self,
        *,
        user_id: str,
        evidence_id: str,
        trace: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Persist a sanitized audit payload outside the reusable Skill card."""

        if not trace or trace.get("audit_only") is not True:
            return {}
        user_bucket = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16]
        safe_evidence_id = re.sub(r"[^A-Za-z0-9_.-]", "_", evidence_id)[:180]
        relative = Path("agent_skill_audit") / user_bucket / f"{safe_evidence_id}.json"
        target = self.path.parent / relative
        payload = normalize_execution_trace(trace)
        with self._lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".tmp")
            temporary.write_text(_json(payload), encoding="utf-8")
            temporary.replace(target)
        return trace_summary(payload, relative.as_posix())

    def read_execution_trace(
        self, *, user_id: str, trace_id: str
    ) -> dict[str, Any] | None:
        """Read one audit payload while enforcing the user bucket boundary."""

        user_bucket = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16]
        bucket = self.audit_dir / user_bucket
        if not bucket.exists():
            return None
        with self._lock:
            for path in bucket.glob("*.json"):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                expires_at = str(payload.get("retention_expires_at") or "")
                if expires_at:
                    try:
                        if datetime.fromisoformat(expires_at) <= datetime.now(UTC):
                            continue
                    except ValueError:
                        continue
                if (
                    isinstance(payload, dict)
                    and str(payload.get("trace_id") or "") == str(trace_id)
                ):
                    return payload
        return None

    def list_evidence(
        self,
        user_id: str,
        *,
        family_signature: str | None = None,
        implementation_signature: str | None = None,
    ) -> list[AgentSkillEvidence]:
        clauses = ["user_id=?"]
        values: list[Any] = [user_id]
        if family_signature:
            clauses.append("family_signature=?")
            values.append(family_signature)
        if implementation_signature:
            clauses.append("implementation_signature=?")
            values.append(implementation_signature)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_skill_evidence WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at",
                values,
            ).fetchall()
        return [self._evidence(row) for row in rows]

    def save_candidate(
        self,
        card: AgentSkillCard,
        *,
        promotion_threshold: int,
        allow_side_effect_activation: bool,
        minimum_structure_consistency: float,
    ) -> AgentSkillCard:
        threshold = max(2, promotion_threshold)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM agent_skills WHERE user_id=? AND signature=?",
                (card.user_id, card.signature),
            ).fetchone()
            if row:
                existing = self._card(row)
                card.skill_id = existing.skill_id
                card.version = existing.version
                card.created_at = existing.created_at
                card.status = existing.status
                card.failure_count = existing.failure_count
                card.consecutive_failures = existing.consecutive_failures
                card.last_used_at = existing.last_used_at
            else:
                family_rows = connection.execute(
                    "SELECT * FROM agent_skills WHERE user_id=? AND family_signature=?",
                    (card.user_id, card.family_signature),
                ).fetchall()
                card.version = max(
                    (self._card(item).version for item in family_rows), default=0
                ) + 1

            side_effect = card.applicability.operation_mode in SIDE_EFFECT_MODES
            promotion_ready = (
                card.schema_version >= 1
                and card.quality.promotion_evidence_count >= threshold
                and card.quality.structure_consistency >= minimum_structure_consistency
                and card.aggregate_reflection_accepted
                and (not side_effect or allow_side_effect_activation)
            )
            if card.status != AgentSkillStatus.DISABLED and promotion_ready:
                card.status = AgentSkillStatus.ACTIVE
            if card.status == AgentSkillStatus.ACTIVE:
                sibling_rows = connection.execute(
                    "SELECT * FROM agent_skills WHERE user_id=? AND family_signature=?",
                    (card.user_id, card.family_signature),
                ).fetchall()
                for sibling_row in sibling_rows:
                    sibling = self._card(sibling_row)
                    if (
                        sibling.skill_id != card.skill_id
                        and sibling.status == AgentSkillStatus.ACTIVE
                    ):
                        sibling.status = AgentSkillStatus.DISABLED
                        sibling.updated_at = _now()
                        connection.execute(
                            "UPDATE agent_skills SET payload=?,updated_at=? "
                            "WHERE user_id=? AND skill_id=?",
                            (
                                _json(sibling.model_dump(mode="json")),
                                sibling.updated_at,
                                sibling.user_id,
                                sibling.skill_id,
                            ),
                        )
            card.updated_at = _now()
            connection.execute(
                """INSERT INTO agent_skills
                (skill_id,user_id,signature,family_signature,payload,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(user_id,signature) DO UPDATE SET
                  skill_id=excluded.skill_id,
                  family_signature=excluded.family_signature,
                  payload=excluded.payload,
                  updated_at=excluded.updated_at""",
                (
                    card.skill_id,
                    card.user_id,
                    card.signature,
                    card.family_signature,
                    _json(card.model_dump(mode="json")),
                    card.created_at,
                    card.updated_at,
                ),
            )
            connection.commit()
        return card

    def _set_status(
        self, user_id: str, skill_id: str, status: AgentSkillStatus
    ) -> AgentSkillCard:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM agent_skills WHERE user_id=? AND skill_id=?",
                (user_id, skill_id),
            ).fetchone()
            if not row:
                raise KeyError(f"agent skill not found: {skill_id}")
            card = self._card(row)
            card.status = status
            card.updated_at = _now()
            if status == AgentSkillStatus.ACTIVE:
                siblings = connection.execute(
                    "SELECT * FROM agent_skills WHERE user_id=? AND family_signature=?",
                    (user_id, card.family_signature),
                ).fetchall()
                for sibling_row in siblings:
                    sibling = self._card(sibling_row)
                    if sibling.skill_id != skill_id and sibling.status == AgentSkillStatus.ACTIVE:
                        sibling.status = AgentSkillStatus.DISABLED
                        sibling.updated_at = _now()
                        connection.execute(
                            "UPDATE agent_skills SET payload=?,updated_at=? "
                            "WHERE user_id=? AND skill_id=?",
                            (
                                _json(sibling.model_dump(mode="json")),
                                sibling.updated_at,
                                user_id,
                                sibling.skill_id,
                            ),
                        )
            connection.execute(
                "UPDATE agent_skills SET payload=?,updated_at=? "
                "WHERE user_id=? AND skill_id=?",
                (
                    _json(card.model_dump(mode="json")),
                    card.updated_at,
                    user_id,
                    skill_id,
                ),
            )
            connection.commit()
        return card

    def activate(self, user_id: str, skill_id: str) -> AgentSkillCard:
        return self._set_status(user_id, skill_id, AgentSkillStatus.ACTIVE)

    def disable(self, user_id: str, skill_id: str) -> AgentSkillCard:
        return self._set_status(user_id, skill_id, AgentSkillStatus.DISABLED)

    def record_outcome(
        self,
        user_id: str,
        skill_id: str,
        *,
        success: bool,
        failure_threshold: int,
    ) -> AgentSkillCard | None:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM agent_skills WHERE user_id=? AND skill_id=?",
                (user_id, skill_id),
            ).fetchone()
            if not row:
                return None
            card = self._card(row)
            card.last_used_at = _now()
            if success:
                card.success_count += 1
                card.consecutive_failures = 0
            else:
                card.failure_count += 1
                card.consecutive_failures += 1
                if card.consecutive_failures >= max(1, failure_threshold):
                    card.status = AgentSkillStatus.DISABLED
            total = card.success_count + card.failure_count
            card.quality.execution_success_rate = (
                card.success_count / total if total else 0.0
            )
            card.updated_at = _now()
            connection.execute(
                "UPDATE agent_skills SET payload=?,updated_at=? "
                "WHERE user_id=? AND skill_id=?",
                (
                    _json(card.model_dump(mode="json")),
                    card.updated_at,
                    user_id,
                    skill_id,
                ),
            )
            connection.commit()
        return card


class AgentSkillManager:
    def __init__(
        self,
        settings: AgentSkillSettings | None = None,
        store: AgentSkillStore | None = None,
        reflection: SkillReflection | None = None,
    ) -> None:
        self.settings = settings or AgentSkillSettings.from_env()
        self.store = store or AgentSkillStore(self.settings.store_path)
        default_reflection = reflection or SkillReflection.from_default_model()
        self.reflection = SkillReflection(
            default_reflection.model,
            min_confidence=self.settings.reflection_min_confidence,
            timeout_seconds=default_reflection.timeout_seconds,
        )

    def reflect(
        self,
        evidence: AgentSkillEvidence,
        *,
        source_conversations: Sequence[Mapping[str, Any]] = (),
    ) -> AgentSkillEvidence:
        """Attach a fail-closed LLM reflection to one step evidence record."""

        if not self.settings.reflection_enabled:
            return evidence.model_copy(
                update={
                    "source_conversations": tuple(
                        dict(item) for item in source_conversations
                    ),
                    "reflection_reasons": ("reflection_disabled",),
                }
            )

        sanitized_sources = tuple(
            _sanitize_snapshot(dict(item))
            for item in source_conversations
            if isinstance(item, Mapping)
        )
        evidence = evidence.model_copy(update={"source_conversations": sanitized_sources})
        reflection_evidence = evidence.model_copy(update={"execution_trace": {}})
        result = self.reflection.reflect_trace(
            reflection_evidence, source_conversations=sanitized_sources
        )
        return evidence.model_copy(
            update={
                "source_conversations": sanitized_sources,
                "reflection_accepted": bool(
                    result.valid
                    and result.is_reusable
                    and result.confidence >= self.reflection.min_confidence
                ),
                "reflection_family": result.workflow_family,
                "reflection_procedure": dict(result.normalized_procedure),
                "reflection_confidence": result.confidence,
                "reflection_reasons": result.reasons,
                "reflection_model_version": result.model_version,
            }
        )

    def distill(self, evidence: AgentSkillEvidence) -> AgentSkillDistillationResult:
        if not self.settings.enabled or not self.settings.auto_distill_enabled:
            raise ValueError("agent skill distillation is disabled")
        if not evidence.reflection_accepted:
            raise ValueError("agent step evidence lacks an accepted LLM reflection")
        decision = evaluate_agent_skill_evidence(evidence)
        if not decision.contributes:
            raise ValueError(
                "agent step evidence is not eligible: " + ",".join(decision.reasons)
            )
        if evidence.execution_trace.get("events"):
            summary = self.store.save_execution_trace(
                user_id=evidence.user_id,
                evidence_id=evidence.evidence_id,
                trace=evidence.execution_trace,
            )
            evidence = evidence.model_copy(update={"execution_trace": summary})
        family_signature = _family_signature(evidence)
        signature = _implementation_signature(evidence)
        before = self.store.get_by_signature(evidence.user_id, signature)
        previous_status = before.status if before else None
        evidence_saved = self.store.save_evidence(
            evidence, family_signature, signature
        )
        supporting = self.store.list_evidence(
            evidence.user_id,
            family_signature=family_signature,
            implementation_signature=signature,
        )
        source_task_ids = list(dict.fromkeys(item.task_id for item in supporting))
        promotion_count = len(
            {
                item.task_id
                for item in supporting
                if evaluate_agent_skill_evidence(item).promotion_ready
                and item.reflection_accepted
            }
        )
        aggregate_reflection = SkillReflectionResult(
            False, "", {}, 0.0, ("promotion_threshold_not_reached",), valid=False
        )
        if promotion_count >= max(2, self.settings.promotion_success_threshold):
            aggregate_reflection = self.reflection.reflect_aggregate(
                supporting,
                source_conversations=[
                    conversation
                    for item in supporting
                    for conversation in item.source_conversations
                ],
            )
        typed_count = sum(
            bool(item.schema_valid is True or item.expected_schema_ref or item.output_accepted)
            for item in supporting
        )
        side_effects = [item for item in supporting if item.is_side_effect]
        verified_count = sum(
            item.verification_status == VerificationStatus.VERIFIED
            and item.business_success is True
            for item in side_effects
        )
        applicability = AgentSkillApplicability(
            capability=evidence.capability,
            step_intent=evidence.step_intent,
            operation_mode=evidence.operation_mode,
            max_risk=evidence.risk_level,
            data_scopes=evidence.data_scopes,
            input_bindings=evidence.input_bindings,
            expected_outputs=evidence.expected_outputs,
            expected_schema_ref=evidence.expected_schema_ref,
            verification_contract=_verification_contract(
                evidence.verification_contract
            ),
        )
        card = AgentSkillCard(
            skill_id=before.skill_id if before else f"askill_{uuid.uuid4().hex}",
            user_id=evidence.user_id,
            name=f"agent_{evidence.capability}",
            description=(
                f"Reusable {evidence.capability.replace('_', ' ')} invocation "
                f"for {evidence.agent_name}"
            ),
            family_signature=family_signature,
            signature=signature,
            applicability=applicability,
            recipe=AgentSkillRecipe(
                agent_name=evidence.agent_name,
                contract_fingerprint=evidence.contract_fingerprint,
                execution_guidance=evidence.execution_guidance,
                retry_policy=dict(evidence.retry_policy),
            ),
            quality=AgentSkillQuality(
                support_count=len(source_task_ids),
                promotion_evidence_count=promotion_count,
                structure_consistency=(
                    1.0 if supporting else 0.0
                ),
                contract_stability=1.0,
                typed_output_rate=typed_count / len(supporting),
                business_verification_rate=(
                    verified_count / len(side_effects) if side_effects else None
                ),
                execution_success_rate=(
                    before.quality.execution_success_rate if before else 1.0
                ),
            ),
            evidence_count=len(source_task_ids),
            success_count=before.success_count if before else len(source_task_ids),
            failure_count=before.failure_count if before else 0,
            consecutive_failures=before.consecutive_failures if before else 0,
            confidence=min(0.95, 0.55 + 0.1 * len(source_task_ids)),
            provenance=AgentSkillProvenance(
                source_task_ids=source_task_ids,
                source_evidence_ids=[item.evidence_id for item in supporting],
                distilled_at=(
                    before.provenance.distilled_at if before else _now()
                ),
            ),
            created_at=before.created_at if before else _now(),
            last_used_at=before.last_used_at if before else None,
            source_conversations=[
                conversation
                for item in supporting
                for conversation in item.source_conversations
            ][:10],
            execution_traces=[
                dict(item.execution_trace)
                for item in supporting
                if item.execution_trace
            ][:10],
            aggregate_reflection_accepted=bool(
                aggregate_reflection.valid
                and aggregate_reflection.is_reusable
                and aggregate_reflection.confidence >= self.reflection.min_confidence
            ),
            aggregate_reflection_reasons=list(aggregate_reflection.reasons),
            aggregate_reflection_model_version=aggregate_reflection.model_version,
            reflection_family=(
                aggregate_reflection.workflow_family or evidence.reflection_family
            ),
            reflection_procedure=dict(
                aggregate_reflection.normalized_procedure
                or evidence.reflection_procedure
            ),
        )
        saved = self.store.save_candidate(
            card,
            promotion_threshold=self.settings.promotion_success_threshold,
            allow_side_effect_activation=self.settings.allow_side_effect_reuse,
            minimum_structure_consistency=self.settings.minimum_structure_consistency,
        )
        return AgentSkillDistillationResult(
            card=saved,
            decision=decision,
            evidence_saved=evidence_saved,
            created=before is None,
            promoted=(
                previous_status != AgentSkillStatus.ACTIVE
                and saved.status == AgentSkillStatus.ACTIVE
            ),
        )

    def record_outcome(
        self, user_id: str, skill_id: str, *, success: bool
    ) -> AgentSkillCard | None:
        return self.store.record_outcome(
            user_id,
            skill_id,
            success=success,
            failure_threshold=self.settings.failure_disable_threshold,
        )

    def resolve_binding(
        self,
        *,
        user_id: str,
        binding: Mapping[str, Any],
        agent_name: str,
        contract_fingerprint: str,
        operation_mode: str,
        step: Mapping[str, Any],
        task_profile: Mapping[str, Any],
        agent_capabilities: Mapping[str, Sequence[str]] | None = None,
    ) -> ResolvedAgentSkillBinding | None:
        """Resolve a plan reference against current trusted runtime metadata.

        The plan is editable client input.  Never execute guidance copied from
        it; reload the Active card and verify every immutable identity field.
        """

        if not self.settings.enabled or not self.settings.reuse_enabled:
            return None
        skill_id = str(binding.get("skill_id") or "")
        if not skill_id:
            return None
        card = self.store.get(user_id, skill_id)
        if card is None or card.status != AgentSkillStatus.ACTIVE:
            return None
        capability = _capability(step, agent_capabilities, task_profile)
        step_intent = _step_intent(step, capability)
        risk_level = _risk(step.get("risk_level"))
        scopes = _data_scopes(task_profile)
        inputs = _normalize_input_bindings(step)
        outputs = _expected_outputs(step)
        schema_ref = step.get("expected_schema_ref") or step.get(
            "output_schema_ref"
        )
        verification = _verification_contract(step.get("verification_contract"))
        applicability = card.applicability
        current_required = bool(verification.get("required", False))
        skill_required = bool(
            applicability.verification_contract.get("required", False)
        )
        current_trusted = bool(
            verification.get("trusted_verifier_required", False)
        )
        skill_trusted = bool(
            applicability.verification_contract.get(
                "trusted_verifier_required", False
            )
        )
        checks = (
            str(binding.get("signature") or "") == card.signature,
            int(binding.get("version") or 0) == card.version,
            str(binding.get("contract_fingerprint") or "")
            == card.recipe.contract_fingerprint,
            card.recipe.agent_name == agent_name,
            bool(contract_fingerprint)
            and card.recipe.contract_fingerprint == contract_fingerprint,
            card.applicability.operation_mode == _operation_mode(operation_mode),
            applicability.capability == capability,
            applicability.step_intent == step_intent,
            _RISK_LEVEL[risk_level] <= _RISK_LEVEL[_risk(applicability.max_risk)],
            set(scopes).issubset(set(applicability.data_scopes)),
            tuple(applicability.input_bindings) == inputs,
            (
                not applicability.expected_outputs
                or set(applicability.expected_outputs).issubset(set(outputs))
            ),
            (applicability.expected_schema_ref or None)
            == (str(schema_ref) if schema_ref else None),
            (
                (not skill_required or current_required)
                and (not skill_trusted or current_trusted)
                and (
                    not skill_required
                    or applicability.verification_contract.get("method")
                    == verification.get("method")
                )
            ),
            (
                card.applicability.operation_mode not in SIDE_EFFECT_MODES
                or self.settings.allow_side_effect_reuse
            ),
        )
        if not all(checks):
            return None
        return ResolvedAgentSkillBinding(
            skill_id=card.skill_id,
            version=card.version,
            signature=card.signature,
            execution_guidance=card.recipe.execution_guidance,
            retry_policy=dict(card.recipe.retry_policy),
        )

    def match_step(
        self,
        *,
        user_id: str,
        step: Mapping[str, Any],
        task_profile: Mapping[str, Any],
        agent_contracts: Mapping[str, str],
        agent_capabilities: Mapping[str, Sequence[str]] | None = None,
    ) -> AgentSkillMatch | None:
        if not self.settings.enabled or not self.settings.reuse_enabled:
            return None
        agent_name = str(step.get("agent_name") or "")
        current_contract = str(agent_contracts.get(agent_name) or "")
        capability = _capability(step, agent_capabilities, task_profile)
        step_intent = _step_intent(step, capability)
        operation_mode = _operation_mode(step.get("operation_mode"))
        risk_level = _risk(step.get("risk_level"))
        scopes = _data_scopes(task_profile)
        inputs = _normalize_input_bindings(step)
        outputs = _expected_outputs(step)
        schema_ref = step.get("expected_schema_ref") or step.get("output_schema_ref")
        verification = _verification_contract(step.get("verification_contract"))

        candidates: list[AgentSkillMatch] = []
        for card in self.store.list_active(user_id):
            skill = card.applicability
            current_required = bool(verification.get("required", False))
            skill_required = bool(skill.verification_contract.get("required", False))
            current_trusted = bool(
                verification.get("trusted_verifier_required", False)
            )
            skill_trusted = bool(
                skill.verification_contract.get("trusted_verifier_required", False)
            )
            checks = {
                "agent": card.recipe.agent_name == agent_name,
                "contract": bool(current_contract)
                and card.recipe.contract_fingerprint == current_contract,
                "capability": skill.capability == capability,
                "intent": skill.step_intent == step_intent,
                "operation_mode": skill.operation_mode == operation_mode,
                "risk": _RISK_LEVEL[risk_level] <= _RISK_LEVEL[_risk(skill.max_risk)],
                "data_scope": set(scopes).issubset(set(skill.data_scopes)),
                "input_shape": tuple(skill.input_bindings) == inputs,
                "output_shape": tuple(skill.expected_outputs) == outputs,
                "schema": (skill.expected_schema_ref or None)
                == (str(schema_ref) if schema_ref else None),
                "verification": (
                    (not skill_required or current_required)
                    and (not skill_trusted or current_trusted)
                    and (
                        not skill_required
                        or skill.verification_contract.get("method")
                        == verification.get("method")
                    )
                ),
                "side_effect_enabled": (
                    operation_mode not in SIDE_EFFECT_MODES
                    or self.settings.allow_side_effect_reuse
                ),
            }
            if not all(checks.values()):
                continue
            score = 1.0
            if score < self.settings.match_threshold:
                continue
            bound = deepcopy(dict(step))
            bound["agent_skill_binding"] = {
                "skill_id": card.skill_id,
                "version": card.version,
                "signature": card.signature,
                "contract_fingerprint": card.recipe.contract_fingerprint,
            }
            candidates.append(
                AgentSkillMatch(
                    skill=card,
                    score=score,
                    reason="all_structural_checks_passed",
                    checks=checks,
                    bound_step=bound,
                )
            )
        candidates.sort(key=lambda item: item.score, reverse=True)
        if not candidates:
            return None
        if (
            len(candidates) > 1
            and candidates[0].score - candidates[1].score
            < self.settings.match_margin
        ):
            return None
        return candidates[0]


def slice_agent_skill_evidence(
    *,
    user_id: str,
    evidence: SkillExecutionEvidence,
    planning_steps: Sequence[Mapping[str, Any]],
    task_profile: Mapping[str, Any] | None,
    agent_contracts: Mapping[str, str],
    agent_capabilities: Mapping[str, Sequence[str]] | None = None,
    source_conversations: Sequence[Mapping[str, Any]] = (),
    execution_trace: Mapping[str, Any] | None = None,
) -> list[AgentSkillEvidence]:
    plan: dict[str, Mapping[str, Any]] = {}
    agent_to_steps: dict[str, list[str]] = {}
    aliases: dict[str, str] = {}
    for index, raw in enumerate(planning_steps):
        if not isinstance(raw, Mapping):
            continue
        identifier = _step_id(index, raw)
        plan[identifier] = raw
        aliases[identifier] = identifier
        if raw.get("subtask_id"):
            aliases[str(raw["subtask_id"])] = identifier
        agent_name = str(raw.get("agent_name") or "")
        if agent_name:
            agent_to_steps.setdefault(agent_name, []).append(identifier)

    step_evidence = {item.step_id: item for item in evidence.steps}
    resolved_evidence: dict[str, StepExecutionEvidence] = {}
    for item in evidence.steps:
        identifier = aliases.get(item.step_id)
        if identifier is None:
            candidates = agent_to_steps.get(item.agent_name, [])
            if len(candidates) == 1:
                identifier = candidates[0]
        if identifier and identifier in plan:
            resolved_evidence[identifier] = item

    results: list[AgentSkillEvidence] = []
    for identifier, raw in plan.items():
        observed = resolved_evidence.get(identifier)
        if observed is None:
            continue
        agent_name = str(raw.get("agent_name") or observed.agent_name or "")
        contract = str(agent_contracts.get(agent_name) or "")
        capability = _capability(raw, agent_capabilities, task_profile)
        intent = _step_intent(raw, capability)
        dependencies: list[str] = []
        raw_deps = raw.get("depends_on") or ()
        if isinstance(raw_deps, str):
            raw_deps = [raw_deps]
        for dependency in raw_deps:
            resolved = aliases.get(str(dependency), str(dependency))
            if resolved not in dependencies:
                dependencies.append(resolved)
        for binding in raw.get("inputs") or ():
            if isinstance(binding, Mapping) and binding.get("source_step"):
                resolved = aliases.get(
                    str(binding["source_step"]), str(binding["source_step"])
                )
                if resolved not in dependencies:
                    dependencies.append(resolved)
        dependency_success = all(
            resolved_evidence.get(dep) is not None
            and resolved_evidence[dep].technical_success
            for dep in dependencies
        )
        expected_schema_ref = raw.get("expected_schema_ref") or raw.get(
            "output_schema_ref"
        )
        refs = tuple(
            item
            for raw_ref in observed.artifact_refs
            if (item := _safe_ref(raw_ref)) is not None
        )
        candidate = AgentSkillEvidence(
            evidence_id=f"aevidence_{uuid.uuid4().hex}",
            user_id=user_id,
            task_id=evidence.task_id,
            workflow_id=evidence.workflow_id,
            step_id=identifier,
            agent_name=agent_name,
            contract_fingerprint=contract,
            capability=capability,
            step_intent=intent,
            operation_mode=_operation_mode(observed.operation_mode),
            risk_level=_risk(observed.risk_level or raw.get("risk_level")),
            data_scopes=_data_scopes(task_profile),
            input_bindings=_normalize_input_bindings(raw),
            expected_outputs=_expected_outputs(raw),
            expected_schema_ref=(
                str(expected_schema_ref) if expected_schema_ref else None
            ),
            verification_contract=_verification_contract(
                raw.get("verification_contract")
            ),
            retry_policy=_retry_policy(raw, _operation_mode(observed.operation_mode)),
            execution_guidance=(
                f"Execute capability {capability} for the current planned step "
                "using only current request data and bound upstream Artifacts."
            ),
            dependency_step_ids=tuple(dependencies),
            dependency_success=dependency_success,
            technical_success=observed.technical_success,
            business_success=observed.business_success,
            verification_status=observed.verification_status,
            verification_method=observed.verification_method,
            schema_valid=observed.schema_valid,
            output_accepted=bool(
                observed.schema_valid is True or expected_schema_ref or refs
            ),
            idempotency_key_present=bool(observed.idempotency_key),
            external_operation_id_present=bool(observed.external_operation_id),
            needs_reconciliation=observed.needs_reconciliation,
            artifact_refs=refs,
            source_conversations=tuple(
                dict(item) for item in source_conversations if isinstance(item, Mapping)
            ),
            execution_trace=(
                dict(execution_trace) if isinstance(execution_trace, Mapping) else {}
            ),
        )
        if evaluate_agent_skill_evidence(candidate).contributes:
            results.append(candidate)
    return results


def bind_agent_skills(
    manager: AgentSkillManager,
    *,
    user_id: str,
    planning_steps: Sequence[Mapping[str, Any]],
    task_profile: Mapping[str, Any],
    agent_contracts: Mapping[str, str],
    agent_capabilities: Mapping[str, Sequence[str]] | None = None,
) -> AgentSkillBindingResult:
    original: list[dict[str, Any]] = []
    for step in planning_steps:
        if not isinstance(step, Mapping):
            continue
        sanitized = deepcopy(dict(step))
        # The Planner/Web plan is editable input. Rebuild binding references
        # from current Active cards instead of preserving caller-supplied data.
        sanitized.pop("agent_skill_binding", None)
        sanitized.pop("agent_skill_guidance", None)
        original.append(sanitized)
    if not manager.settings.enabled or not manager.settings.reuse_enabled:
        return AgentSkillBindingResult(steps=original)
    bound_steps: list[dict[str, Any]] = []
    bindings: dict[str, str] = {}
    matches: list[AgentSkillMatch] = []
    for index, step in enumerate(original):
        match = manager.match_step(
            user_id=user_id,
            step=step,
            task_profile=task_profile,
            agent_contracts=agent_contracts,
            agent_capabilities=agent_capabilities,
        )
        if match is None:
            bound_steps.append(step)
            continue
        identifier = _step_id(index, step)
        bound_steps.append(match.bound_step)
        bindings[identifier] = match.skill.skill_id
        matches.append(match)
    return AgentSkillBindingResult(
        steps=bound_steps,
        bindings=bindings,
        matches=matches,
    )


_manager: AgentSkillManager | None = None


def get_agent_skill_manager() -> AgentSkillManager:
    global _manager
    if _manager is None:
        _manager = AgentSkillManager()
    return _manager


def set_agent_skill_manager(manager: AgentSkillManager | None) -> None:
    global _manager
    _manager = manager


__all__ = [
    "AgentSkillApplicability",
    "AgentSkillBindingResult",
    "AgentSkillCard",
    "AgentSkillDecision",
    "AgentSkillDistillationResult",
    "AgentSkillEvidence",
    "AgentSkillManager",
    "AgentSkillMatch",
    "AgentSkillProvenance",
    "AgentSkillQuality",
    "AgentSkillRecipe",
    "ResolvedAgentSkillBinding",
    "AgentSkillSettings",
    "AgentSkillStatus",
    "AgentSkillStore",
    "bind_agent_skills",
    "agent_capability_bindings",
    "agent_contract_fingerprints",
    "evaluate_agent_skill_evidence",
    "get_agent_skill_manager",
    "set_agent_skill_manager",
    "slice_agent_skill_evidence",
]
