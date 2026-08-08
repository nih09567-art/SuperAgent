#!/usr/bin/env python
"""Report Agent - generates reports."""

from typing import Any, Dict, List
import hashlib
import json
import logging
import os
import re

from src.contracts.agent_contract import AgentContract, DataContractRef
from src.contracts.agent_result import AgentResultError
from src.contracts.agent_schema_catalog import (
    AGENT_SCHEMA_CATALOG,
    AGENT_SCHEMA_VALIDATORS,
)
from src.orchestration.schema_registry import get_schema_registry

from .base_agent import BaseRemoteAgent

logger = logging.getLogger(__name__)


class _ReportOutputValidationError(ValueError):
    """The remote report tool returned a structurally unsafe result."""


class _ReportSourceValidationError(ValueError):
    """The governed report fan-in is incomplete, duplicate, or unregistered."""


def _validate_report_tool_result(result: Any) -> str:
    """Validate the tool business result before publishing an Artifact."""

    if not isinstance(result, dict):
        raise _ReportOutputValidationError("report tool result must be an object")
    if str(result.get("status") or "").strip().lower() != "success":
        raise _ReportOutputValidationError("report tool result status must be success")
    if "markdown" not in result:
        raise _ReportOutputValidationError("report tool result is missing markdown")
    markdown = result["markdown"]
    if not isinstance(markdown, str):
        raise _ReportOutputValidationError("report tool markdown must be a string")
    if not markdown.strip():
        raise _ReportOutputValidationError("report tool markdown must not be empty")
    return markdown


def _normalize_report_markdown(markdown: str) -> str:
    """Normalize factual Chinese number/unit spacing for stable evidence checks."""

    normalized = re.sub(r"(?<=\d)\s+(?=[年月天])", "", markdown)
    normalized = re.sub(
        r"国务院令第\s*(\d+)\s*号",
        r"国务院令第\1号",
        normalized,
    )
    return normalized


def _not_found_policy_report(
    title: str,
    sources: List[Dict[str, Any]],
) -> dict[str, Any]:
    """Build a cautious, data-driven report when policy retrieval missed."""

    policy_topics = [
        str(source.get("payload", {}).get("query") or "")
        for source in sources
        if isinstance(source, dict)
        and source.get("logical_name") == "policy.info"
        and isinstance(source.get("payload"), dict)
    ]
    annual_leave = any(
        marker in topic for topic in policy_topics for marker in ("年假", "年休假")
    )
    conclusion = (
        "当前资料不足，无法据此判断可休年假天数。"
        if annual_leave
        else "当前资料不足，无法形成需要该政策依据支撑的业务结论。"
    )

    markdown = (
        f"# {title}\n\n"
        "## 政策依据\n"
        "未检索到有效的政策依据。\n\n"
        "## 结论\n"
        f"{conclusion}请补充有效政策来源或咨询相应业务部门；"
        "本报告不会根据其他来源自行推断缺失的政策结论。"
    )
    payload = {
        "title": title,
        "markdown": markdown,
        "source_count": len(sources),
    }
    payload["external_op_id"] = _report_external_operation_id(payload)
    return payload


def _report_external_operation_id(payload: dict[str, Any]) -> str:
    """Create a durable receipt ID for an otherwise non-side-effecting report."""

    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "report-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _execution_context_brief(messages: List[Dict[str, Any]]) -> Dict[str, Any] | None:
    """Read the newest Scheduler-created execution brief."""

    for message in reversed(messages or []):
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.startswith("EXECUTION_CONTEXT"):
            continue
        _, _, raw = content.partition("\n")
        try:
            brief = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(brief, dict):
            return brief
    return None


def _resolved_report_sources(messages: List[Dict[str, Any]]) -> Dict[str, Any] | None:
    """Read the Scheduler-assembled report input without asking an LLM to copy it."""

    brief = _execution_context_brief(messages)
    resolved = brief.get("resolved_inputs") if isinstance(brief, dict) else None
    report_sources = (
        resolved.get("report.sources") if isinstance(resolved, dict) else None
    )
    return report_sources if isinstance(report_sources, dict) else None


def _validate_report_sources(report_sources: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Validate a Scheduler-assembled, scenario-neutral report input."""

    registry = get_schema_registry()
    for schema_ref, schema in AGENT_SCHEMA_CATALOG.items():
        semantic_validator = AGENT_SCHEMA_VALIDATORS.get(schema_ref)
        if not registry.has(schema_ref):
            registry.register(
                schema_ref,
                schema,
                semantic_validator=semantic_validator,
            )
        elif semantic_validator is not None:
            registry.set_semantic_validator(schema_ref, semantic_validator)
    valid, _errors = registry.validate(report_sources, "report.sources@v1")
    if not valid:
        raise _ReportSourceValidationError("report.sources failed schema validation")

    sources = report_sources.get("sources")
    if not isinstance(sources, list):
        raise _ReportSourceValidationError("report.sources must contain a source list")
    for source in sources:
        schema_ref = str(source.get("schema_ref") or "")
        if not registry.has(schema_ref):
            raise _ReportSourceValidationError(
                "report source uses an unregistered schema"
            )
    return sources


def _resolved_legacy_report_data(messages: List[Dict[str, Any]]) -> List[Any]:
    """Collect legacy resolved inputs when the report.sources contract is absent."""

    for message in reversed(messages or []):
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.startswith("EXECUTION_CONTEXT"):
            continue
        _, _, raw = content.partition("\n")
        try:
            brief = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        resolved = brief.get("resolved_inputs") if isinstance(brief, dict) else None
        if not isinstance(resolved, dict):
            continue
        data: List[Any] = []
        for value in resolved.values():
            if isinstance(value, dict) and isinstance(value.get("records"), list):
                data.extend(value["records"])
            else:
                data.append(value)
        return data
    return []


class RemoteReportAgent(BaseRemoteAgent):
    """Report Agent for generating reports."""

    def __init__(self):
        super().__init__(
            name="RemoteReportAgent",
            prompt="You are a report generator that creates comprehensive reports.",
            contract=AgentContract(
                contract_version="1.0",
                requires=[
                    DataContractRef(
                        name="report.sources",
                        schema_ref="report.sources@v1",
                    )
                ],
                produces=[
                    DataContractRef(
                        name="report.markdown",
                        schema_ref="report.markdown@v1",
                    )
                ],
            ),
        )

    async def execute(
        self,
        tools: List[Dict[str, Any]],
        messages: List[Dict[str, Any]],
        context: Dict[str, Any],
        parameter_extractor: Any,
    ) -> Dict[str, Any]:
        """Execute report generation - single tool agent."""
        if not tools or len(tools) == 0:
            return self.result_envelope(
                error=AgentResultError(
                    code="NO_TOOL_PROVIDED",
                    message="No report tool was provided",
                    retryable=False,
                )
            )

        tool = tools[0]
        tool_name = tool.get("name", "unknown")

        try:
            # Artifact fan-in is a governed data-plane input. The extractor may
            # not remove, replace or rewrite the actual upstream payloads
            # assembled by the Scheduler.
            report_sources = _resolved_report_sources(messages)
            if report_sources is not None:
                sources = _validate_report_sources(report_sources)
                arguments = {
                    "data": sources,
                    "title": str(report_sources.get("title") or "分析报告"),
                    "instruction": (
                        "事实边界：只能使用 data 中实际存在的来源事实，不得补造。"
                        + str(
                            report_sources.get("instruction")
                            or "使用全部上游来源生成报告"
                        )
                    ),
                }
                policy_not_found = any(
                    isinstance(source, dict)
                    and source.get("logical_name") == "policy.info"
                    and isinstance(source.get("payload"), dict)
                    and source["payload"].get("not_found") is True
                    for source in sources
                )
                if policy_not_found:
                    # Do not ask an LLM to infer statutory numbers from
                    # employee tenure when the structured policy Artifact says
                    # no policy was found.
                    return self.result_envelope(
                        outputs={
                            "report.markdown": _not_found_policy_report(
                                arguments["title"], sources
                            )
                        }
                    )
            else:
                # Legacy/direct invocation has no Scheduler data-plane input.
                logger.info(f"[{self.name}] Extracting parameters for {tool_name}")
                arguments = await parameter_extractor.extract(
                    agent_name=self.name,
                    agent_prompt=self.prompt,
                    tool=tool,
                    messages=messages,
                )
                if not isinstance(arguments, dict):
                    arguments = {}
                if not arguments.get("data") and not arguments.get("sections"):
                    legacy_data = _resolved_legacy_report_data(messages)
                    if legacy_data:
                        arguments["data"] = legacy_data

            llm_timeout = int(os.getenv("REMOTE_REPORT_LLM_TIMEOUT", "120"))
            tool_timeout = int(os.getenv("REMOTE_REPORT_TOOL_TIMEOUT", "150"))
            if tool_timeout <= llm_timeout:
                raise ValueError(
                    "REMOTE_REPORT_TOOL_TIMEOUT must be greater than REMOTE_REPORT_LLM_TIMEOUT"
                )
            arguments["llm_timeout_sec"] = llm_timeout

            logger.info(f"[{self.name}] Calling {tool_name}")
            result = await self.call_tool(
                tool_name=tool_name,
                arguments=arguments,
                timeout=tool_timeout,
            )
            markdown = _normalize_report_markdown(_validate_report_tool_result(result))
            sources = arguments.get("sources")
            if not isinstance(sources, list):
                sources = arguments.get("data")
            source_count = len(sources) if isinstance(sources, list) else 0
            payload = {
                "title": str(arguments.get("title") or "分析报告"),
                "markdown": markdown,
                "source_count": source_count,
            }
            payload["external_op_id"] = _report_external_operation_id(payload)
            return self.result_envelope(outputs={"report.markdown": payload})
        except Exception as exc:
            # Keep the full exception in server logs, but never expose a raw
            # URL, traceback, secret, or provider-specific error text through
            # the AgentResult envelope consumed by SSE/FailureDescriptor.
            logger.exception("[%s] report execution failed", self.name)
            if isinstance(exc, TimeoutError):
                code = "REPORT_TOOL_TIMEOUT"
                message = "Report tool request timed out"
                retryable = True
            elif isinstance(exc, _ReportOutputValidationError):
                code = "INVALID_REPORT_OUTPUT"
                message = "Report tool returned invalid output"
                retryable = False
            elif isinstance(exc, _ReportSourceValidationError):
                code = "INVALID_REPORT_SOURCES"
                message = "Report sources failed validation"
                retryable = False
            else:
                code = "REPORT_TOOL_ERROR"
                message = "Report generation failed"
                retryable = False
            return self.result_envelope(
                error=AgentResultError(
                    code=code,
                    message=message,
                    retryable=retryable,
                    details={"tool": tool_name},
                )
            )
