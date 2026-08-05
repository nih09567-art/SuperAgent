"""Convert legacy scheduler failure signals to ``FailureDescriptor``.

This module is the single compatibility boundary for the scheduler's existing
``error``/``metrics`` representation.  It never publishes raw exception text,
remote responses, payloads, or schema-validator diagnostics.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any, Mapping

from src.contracts.workflow_failure import (
    FailureCategory,
    FailureCode,
    FailureDescriptor,
)


@dataclass(frozen=True)
class _FailureSpec:
    category: FailureCategory
    message: str
    retryable: bool
    action: str | None


def _spec(
    category: FailureCategory,
    message: str,
    *,
    retryable: bool = False,
    action: str | None = None,
) -> _FailureSpec:
    return _FailureSpec(category, message, retryable, action)


_SPECS: dict[str, _FailureSpec] = {
    FailureCode.ROUTING_FAILED: _spec(
        FailureCategory.ROUTING,
        "The step could not be routed to an Agent.",
        retryable=True,
        action="Check Agent availability and routing configuration, then retry.",
    ),
    FailureCode.NO_CAPABLE_AGENT: _spec(
        FailureCategory.ROUTING,
        "No capable Agent is available for this step.",
        action="Register a compatible Agent or revise the plan.",
    ),
    FailureCode.ROUTING_REJECTED: _spec(
        FailureCategory.ROUTING,
        "Routing policy rejected this step.",
        action="Review the routing policy and task requirements.",
    ),
    FailureCode.CLARIFICATION_REQUIRED: _spec(
        FailureCategory.PLANNING,
        "More information is required before this step can run.",
        action="Provide the requested clarification and run the workflow again.",
    ),
    FailureCode.CLARIFICATION_BLOCKED: _spec(
        FailureCategory.PLANNING,
        "This step did not run because workflow clarification is required.",
        action="Provide the requested clarification, then run the workflow again.",
    ),
    FailureCode.DISPATCH_AGENT_MISSING: _spec(
        FailureCategory.ROUTING,
        "The routing decision did not identify an Agent.",
        action="Fix the routing provider response.",
    ),
    FailureCode.AGENT_DISPATCH_DENIED: _spec(
        FailureCategory.PERMISSION,
        "Policy denied dispatching the selected Agent.",
        action="Review the user's Agent permissions and dispatch policy.",
    ),
    FailureCode.AGENT_EXECUTION_FAILED: _spec(
        FailureCategory.EXECUTION,
        "The Agent could not complete this step.",
        retryable=True,
        action="Check Agent health and retry when safe.",
    ),
    FailureCode.AGENT_BUSINESS_ERROR: _spec(
        FailureCategory.EXECUTION,
        "The Agent rejected the request for a business reason.",
        action="Correct the step input using the Agent's server-side diagnostic.",
    ),
    FailureCode.AGENT_RESULT_INVALID: _spec(
        FailureCategory.CONTRACT,
        "The Agent returned an invalid result envelope.",
        action="Upgrade the Agent to the declared result protocol.",
    ),
    FailureCode.AGENT_TIMEOUT: _spec(
        FailureCategory.TIMEOUT,
        "The Agent did not finish before the timeout.",
        retryable=True,
        action="Retry when safe or increase the configured timeout.",
    ),
    FailureCode.COMPLETION_CONDITION_FAILED: _spec(
        FailureCategory.EXECUTION,
        "The step result did not satisfy its completion condition.",
        action="Review the completion condition and Agent output.",
    ),
    FailureCode.CONTRACT_VERSION_MISMATCH: _spec(
        FailureCategory.CONTRACT,
        "The Agent result and Contract versions do not match.",
        action="Align the Agent and Contract versions.",
    ),
    FailureCode.PRODUCER_AGENT_MISMATCH: _spec(
        FailureCategory.CONTRACT,
        "The result producer does not match the selected Agent.",
        action="Check Agent registration and result metadata.",
    ),
    FailureCode.REROUTED_AGENT_CONTRACT_MISSING: _spec(
        FailureCategory.CONTRACT,
        "The rerouted Agent has no trusted contract to validate the result.",
        action="Register a trusted contract for the Agent or fix the routing.",
    ),
    FailureCode.MISSING_REQUIRED_OUTPUT: _spec(
        FailureCategory.CONTRACT,
        "The Agent result is missing a required output.",
        action="Fix the Agent output or revise its Contract.",
    ),
    FailureCode.UNDECLARED_OUTPUT: _spec(
        FailureCategory.CONTRACT,
        "The Agent returned an output not declared by its Contract.",
        action="Update the Agent or its Contract.",
    ),
    FailureCode.BUSINESS_RESULT_INCOMPLETE: _spec(
        FailureCategory.CONTRACT,
        "The Agent returned a partial result that cannot be published.",
        action="Complete the Agent result before retrying.",
    ),
    FailureCode.CONTRACT_NO_OUTPUTS: _spec(
        FailureCategory.CONTRACT,
        "The Agent Contract does not declare an output.",
        action="Declare at least one Contract output.",
    ),
    FailureCode.AMBIGUOUS_LEGACY_OUTPUT: _spec(
        FailureCategory.CONTRACT,
        "A legacy result cannot be mapped to the declared outputs.",
        action="Return a versioned result envelope.",
    ),
    FailureCode.SCHEMA_VALIDATION_FAILED: _spec(
        FailureCategory.SCHEMA,
        "The Agent output failed Schema validation.",
        action="Check the output fields and Contract Schema version.",
    ),
    FailureCode.UNREGISTERED_SCHEMA: _spec(
        FailureCategory.SCHEMA,
        "The required Schema is not registered.",
        action="Register the versioned Schema before retrying.",
    ),
    FailureCode.UPSTREAM_OUTPUT_MISSING: _spec(
        FailureCategory.ARTIFACT,
        "A required upstream output was not produced.",
        action="Inspect the upstream step and its declared outputs.",
    ),
    FailureCode.ARTIFACT_NOT_FOUND: _spec(
        FailureCategory.ARTIFACT,
        "A required Artifact could not be found.",
        action="Restore from a valid checkpoint or rerun the upstream step.",
    ),
    FailureCode.ARTIFACT_SCHEMA_INCOMPATIBLE: _spec(
        FailureCategory.SCHEMA,
        "An upstream Artifact has an incompatible Schema.",
        action="Align the upstream and downstream Schema versions.",
    ),
    FailureCode.ARTIFACT_SCHEMA_INVALID: _spec(
        FailureCategory.SCHEMA,
        "An upstream Artifact was not Schema-valid.",
        action="Fix and republish the upstream output.",
    ),
    FailureCode.ARTIFACT_SELECTOR_INVALID: _spec(
        FailureCategory.ARTIFACT,
        "The Artifact selector could not be resolved.",
        action="Correct the input binding selector.",
    ),
    FailureCode.ARTIFACT_ACCESS_DENIED: _spec(
        FailureCategory.PERMISSION,
        "Access to a required Artifact was denied.",
        action="Review Artifact access policy and Agent authorization.",
    ),
    FailureCode.UPSTREAM_STEP_FAILED: _spec(
        FailureCategory.ARTIFACT,
        "This step did not run because an upstream step failed.",
        action="Fix the blocked upstream steps, then resume from a safe checkpoint.",
    ),
    FailureCode.FAN_IN_BINDING_INVALID: _spec(
        FailureCategory.PLANNING,
        "The multi-source input binding is invalid.",
        action="Correct the plan's fan-in binding.",
    ),
    FailureCode.DUPLICATE_INPUT_PARAMETER: _spec(
        FailureCategory.PLANNING,
        "The plan binds the same input parameter more than once.",
        action="Remove the duplicate input binding.",
    ),
    FailureCode.PERSISTENCE_FAILED: _spec(
        FailureCategory.PERSISTENCE,
        "The step result could not be persisted safely.",
        retryable=True,
        action="Check storage health, then resume from a safe checkpoint.",
    ),
    FailureCode.ARTIFACT_STORE_CORRUPTION: _spec(
        FailureCategory.PERSISTENCE,
        "Artifact storage is corrupted or unreadable.",
        action="Repair or restore Artifact storage before resuming the workflow.",
    ),
    FailureCode.SIDE_EFFECT_UNCONFIRMED: _spec(
        FailureCategory.RECONCILIATION,
        "The outcome of a side effect could not be confirmed.",
        action="Verify the external operation manually; do not resend automatically.",
    ),
    FailureCode.INTERNAL_STEP_ERROR: _spec(
        FailureCategory.INTERNAL,
        "The step failed because of an internal error.",
        retryable=True,
        action="Check the server log using the workflow correlation id.",
    ),
    FailureCode.INTERNAL_SCHEDULER_ERROR: _spec(
        FailureCategory.INTERNAL,
        "The workflow scheduler encountered an internal error.",
        retryable=True,
        action="Check the server log using the workflow correlation id.",
    ),
    FailureCode.TASK_GRAPH_INVALID: _spec(
        FailureCategory.PLANNING,
        "The approved task graph is invalid.",
        action="Correct and approve a valid task graph before execution.",
    ),
    FailureCode.TASK_GRAPH_MISSING: _spec(
        FailureCategory.PLANNING,
        "No approved task graph is available for execution.",
        action="Complete planning and approve a task graph before execution.",
    ),
    FailureCode.OPERATION_MODE_UNCLASSIFIED: _spec(
        FailureCategory.PLANNING,
        "A step's operation mode could not be classified safely.",
        action="Declare whether each step is read-only or has side effects.",
    ),
}

_CODE_ALIASES = {
    "EXECUTION_FAILED": FailureCode.AGENT_EXECUTION_FAILED,
    "BUSINESS_RESULT_ERROR": FailureCode.AGENT_BUSINESS_ERROR,
    "INVALID_ENVELOPE": FailureCode.AGENT_RESULT_INVALID,
    "RESULT_SCHEMA_VERSION_MISMATCH": FailureCode.CONTRACT_VERSION_MISMATCH,
}

_INPUT_ERROR_CODES = {
    "artifact_not_produced": FailureCode.UPSTREAM_OUTPUT_MISSING,
    "required_contract_input_missing": FailureCode.UPSTREAM_OUTPUT_MISSING,
    "artifact_not_found": FailureCode.ARTIFACT_NOT_FOUND,
    "access_denied": FailureCode.ARTIFACT_ACCESS_DENIED,
    "selector_error": FailureCode.ARTIFACT_SELECTOR_INVALID,
    "schema_incompatible": FailureCode.ARTIFACT_SCHEMA_INCOMPATIBLE,
    "schema_invalid": FailureCode.ARTIFACT_SCHEMA_INVALID,
    "invalid_fan_in": FailureCode.FAN_IN_BINDING_INVALID,
    "duplicate_param": FailureCode.DUPLICATE_INPUT_PARAMETER,
}

_CONTRACT_RESULT_CODES = {
    "BUSINESS_RESULT_INCOMPLETE",
    "CONTRACT_NO_OUTPUTS",
    "CONTRACT_VERSION_MISMATCH",
    "AMBIGUOUS_LEGACY_OUTPUT",
    "MISSING_REQUIRED_OUTPUT",
    "PRODUCER_AGENT_MISMATCH",
    "RESULT_SCHEMA_VERSION_MISMATCH",
    "UNDECLARED_OUTPUT",
}


def public_execution_reason(error: Any) -> str | None:
    """Translate allow-listed execution diagnostics into a safe Chinese reason.

    Remote errors are untrusted and may contain credentials or business data,
    so arbitrary text is never copied into SSE.  Known platform/provider
    failure shapes are converted to an explicit reason; unknown text continues
    to use the catalog fallback and remains available only in server logs.
    """

    raw = str(error or "").strip()
    if not raw:
        return None
    lowered = raw.lower()

    timeout = re.search(r"timed out after\s+(\d+)s", lowered)
    if timeout:
        return f"远程执行器在 {timeout.group(1)} 秒内未返回结果，已按超时处理。"
    if "timeout" in lowered or "timed out" in lowered:
        return "远程执行器请求超时，未在规定时间内返回结果。"
    if "incomplete chunked read" in lowered or "peer closed connection" in lowered:
        return "上游模型的流式连接在响应完成前中断。"
    if "rate limit" in lowered or "too many requests" in lowered or "429" in lowered:
        return "远程服务触发限流，当前请求被服务端拒绝。"
    if any(marker in lowered for marker in (
        "network error",
        "connection refused",
        "connection reset",
        "service unavailable",
        "bad gateway",
    )):
        return "远程服务网络连接失败或暂时不可用。"
    if re.search(r"employee_id\s+or\s+employee_name\s+is\s+required", lowered):
        return "员工查询缺少员工姓名或员工编号，执行器无法确定查询对象。"
    if "missing endpoint" in lowered:
        return "执行器未配置远程服务地址，无法发起请求。"
    if "tool not found" in lowered:
        return "计划指定的工具未注册，执行器无法调用该工具。"
    if "not invokable" in lowered:
        return "计划指定的工具已注册，但当前不可调用。"
    if "invalid result payload" in lowered or "invalid result envelope" in lowered:
        return "远程执行器返回了无法解析的结果格式。"
    if "operation mode" in lowered and "not allowed" in lowered:
        return "当前操作模式不在该资源允许的模式范围内。"
    if any(marker in lowered for marker in (
        "permission denied",
        "not authorized",
        "authorization manifest",
    )):
        return "当前用户或执行器没有调用目标资源的权限。"
    if "validation failed" in lowered or "invalid input" in lowered:
        return "执行输入未通过校验，请检查该步骤所需字段。"
    return None


def public_policy_reason(payload: Mapping[str, Any] | None) -> str:
    """Return an explicit Chinese explanation for a policy-owned denial."""

    data = dict(payload or {})
    policy_result = data.get("policy_result")
    policy_result = policy_result if isinstance(policy_result, Mapping) else {}
    reason = str(policy_result.get("reason") or "").strip()
    lowered = reason.lower()

    if "human approval was rejected" in lowered:
        return "该操作此前的人工审批已被拒绝。"
    if "requires human approval" in lowered or "requires review" in lowered:
        return "该资源或操作风险要求人工审批后才能执行。"
    if "roles" in lowered and "not in allowed roles" in lowered:
        return "当前用户角色不在该资源允许的角色范围内。"
    if "job roles" in lowered and "not in allowed job roles" in lowered:
        return "当前用户职务角色不在该资源允许的职务范围内。"
    if "missing required grants" in lowered:
        return "当前用户缺少该资源要求的授权项。"
    if "operation mode" in lowered and "not in allowed modes" in lowered:
        return "当前步骤的操作模式不在该资源允许的模式范围内。"
    if "clearance insufficient" in lowered:
        return "当前用户的安全级别低于该资源的敏感级别要求。"
    if "outside working hours" in lowered:
        return "该操作仅允许在工作时间执行，当前时间不符合要求。"
    if "external network" in lowered:
        return "该操作仅允许从内部网络执行，当前网络区域不符合要求。"
    if "scenario" in lowered and any(word in lowered for word in ("mismatch", "do not align")):
        return "当前任务场景与目标资源声明的适用场景不匹配。"
    if "not in user" in lowered and "available agents" in lowered:
        return "所选 Agent 不在当前用户可调用的 Agent 列表中。"
    if "unknown security user" in lowered:
        return "当前用户未登记安全属性，系统无法完成权限判断。"
    if "no matching policy" in lowered or "default rule denied" in lowered:
        return "没有找到允许当前用户执行此操作的权限规则。"
    return "权限策略拒绝了当前操作，但没有返回可进一步细分的规则原因。"


def _code_text(code: str | FailureCode) -> str:
    raw = code.value if isinstance(code, FailureCode) else str(code)
    raw = raw.strip().upper()
    alias = _CODE_ALIASES.get(raw)
    return alias.value if isinstance(alias, FailureCode) else raw


def make_failure(
    code: str | FailureCode,
    category: str | FailureCategory | None = None,
    message: str | None = None,
    step_id: str | None = None,
    agent_id: str | None = None,
    retryable: bool | None = None,
    action: str | None = None,
    details_safe: Mapping[str, Any] | None = None,
    **context: Any,
) -> FailureDescriptor:
    """Build a descriptor using secure platform defaults.

    Explicit ``message``/``action`` values are intended for trusted platform
    callers.  The legacy mapper below deliberately uses catalog messages rather
    than forwarding untrusted ``error`` text.
    """

    code_text = _code_text(code)
    unknown_code = code_text not in _SPECS
    if unknown_code:
        code_text = FailureCode.INTERNAL_STEP_ERROR.value
    spec = _SPECS[code_text]
    safe_details = dict(details_safe or {})
    for key in (
        "actual_schema_ref",
        "attempts",
        "completion_condition",
        "expected_schema_ref",
        "logical_name",
        "missing_outputs",
        "reason_codes",
        "routing_decision",
        "schema_ref",
        "timeout_seconds",
        "undeclared_outputs",
    ):
        if key in context:
            safe_details[key] = context[key]

    blocked_by = context.get("blocked_by") or safe_details.pop("blocked_by", [])
    parameter_name = context.get("parameter_name", context.get("param"))
    source_step = context.get("source_step", context.get("source"))
    source_output = context.get("source_output")

    return FailureDescriptor(
        code=code_text,
        category=spec.category if unknown_code else (category or spec.category),
        message=message or spec.message,
        retryable=spec.retryable if retryable is None else retryable,
        action=spec.action if action is None else action,
        step_id=step_id,
        agent_id=agent_id,
        parameter_name=parameter_name,
        source_step=source_step,
        source_output=source_output,
        blocked_by=blocked_by,
        details_safe=safe_details,
    )


def failure_from_step_result(
    step_id: str,
    error: str | None,
    metrics: Mapping[str, Any] | None,
    agent_id: str | None = None,
) -> FailureDescriptor:
    """Map a legacy StepResult's error/metrics to the structured protocol."""

    values = dict(metrics or {})
    selected_agent = agent_id or values.get("selected_agent")
    retryable: bool | None = None
    details = {
        key: values[key]
        for key in (
            "attempts",
            "logical_name",
            "reason_codes",
            "routing_decision",
            "schema_ref",
        )
        if values.get(key) is not None
    }
    context = {
        "param": values.get("param"),
        "source": values.get("source"),
        "source_output": values.get("source_output"),
        "blocked_by": values.get("blocked_by") or [],
    }

    if values.get("needs_reconciliation"):
        code: str | FailureCode = FailureCode.SIDE_EFFECT_UNCONFIRMED
    elif values.get("persistence_failed"):
        code = FailureCode.PERSISTENCE_FAILED
    elif values.get("receipt_store_corrupt"):
        code = FailureCode.SIDE_EFFECT_UNCONFIRMED
    elif values.get("failure_code"):
        code = str(values["failure_code"])
    elif values.get("result_error"):
        raw_result_code = str(values["result_error"]).strip().upper()
        if raw_result_code in _CODE_ALIASES:
            code = _CODE_ALIASES[raw_result_code]
        elif raw_result_code in _CONTRACT_RESULT_CODES:
            code = _CODE_ALIASES.get(raw_result_code, raw_result_code)
        elif raw_result_code in {
            FailureCode.SCHEMA_VALIDATION_FAILED,
            FailureCode.UNREGISTERED_SCHEMA,
        }:
            code = raw_result_code
        elif raw_result_code in _SPECS:
            code = raw_result_code
        else:
            # Remote business codes are retained only in server logs.  They are
            # not trusted as part of the platform's public protocol.
            code = FailureCode.AGENT_BUSINESS_ERROR
        # ``result_retryable`` is set by the result adapter, not copied from
        # the raw remote payload.  It may only upgrade a business failure to
        # retryable; message/action stay platform-owned.
        if values.get("result_retryable") is True:
            retryable = True
    elif values.get("input_error"):
        code = _INPUT_ERROR_CODES.get(
            str(values["input_error"]).strip().lower(),
            FailureCode.FAN_IN_BINDING_INVALID,
        )
    else:
        routing = str(values.get("routing_decision") or "").upper()
        if routing == "CLARIFY" or values.get("clarify"):
            code = FailureCode.CLARIFICATION_REQUIRED
        elif routing == "NO_CAPABLE_AGENT":
            code = FailureCode.NO_CAPABLE_AGENT
        elif routing == "REJECT":
            code = FailureCode.ROUTING_REJECTED
        elif routing == "DISPATCH_NO_AGENT":
            code = FailureCode.DISPATCH_AGENT_MISSING
        elif routing == "ROUTING_ERROR":
            code = FailureCode.ROUTING_FAILED
        elif values.get("crashed"):
            code = FailureCode.INTERNAL_STEP_ERROR
        elif error and "timeout" in error.lower():
            code = FailureCode.AGENT_TIMEOUT
        elif error and error.lower().startswith("completion condition failed"):
            code = FailureCode.COMPLETION_CONDITION_FAILED
            details["completion_condition"] = error.partition(":")[2].strip()
        elif values.get("attempts") is not None or selected_agent:
            code = FailureCode.AGENT_EXECUTION_FAILED
        else:
            code = FailureCode.INTERNAL_STEP_ERROR

    public_reason = public_execution_reason(error)
    return make_failure(
        code,
        message=public_reason,
        step_id=step_id,
        agent_id=selected_agent,
        retryable=retryable,
        details_safe=details,
        **context,
    )


def failure_from_exception(
    exc: BaseException,
    *,
    step_id: str | None = None,
    agent_id: str | None = None,
    retryable: bool | None = None,
) -> FailureDescriptor:
    """Classify a caught exception without exposing its raw text."""

    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        code = FailureCode.AGENT_TIMEOUT
    elif isinstance(exc, PermissionError):
        code = FailureCode.ARTIFACT_ACCESS_DENIED
    else:
        code = FailureCode.INTERNAL_STEP_ERROR
    return make_failure(
        code,
        step_id=step_id,
        agent_id=agent_id,
        retryable=retryable,
    )


__all__ = [
    "failure_from_exception",
    "failure_from_step_result",
    "make_failure",
    "public_execution_reason",
    "public_policy_reason",
]
