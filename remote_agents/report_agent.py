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

from .base_agent import BaseRemoteAgent

logger = logging.getLogger(__name__)

_ANNUAL_LEAVE_SOURCE_SCHEMAS = {
    "employee.info": "employee.info@v1",
    "policy.info": "policy.info@v2",
}


class _ReportOutputValidationError(ValueError):
    """The remote report tool returned a structurally unsafe result."""


def _validate_report_tool_result(result: Any) -> str:
    """Validate the tool business result before publishing an Artifact."""

    if not isinstance(result, dict):
        raise _ReportOutputValidationError("report tool result must be an object")
    if str(result.get("status") or "").strip().lower() != "success":
        raise _ReportOutputValidationError(
            "report tool result status must be success"
        )
    if "markdown" not in result:
        raise _ReportOutputValidationError(
            "report tool result is missing markdown"
        )
    markdown = result["markdown"]
    if not isinstance(markdown, str):
        raise _ReportOutputValidationError(
            "report tool markdown must be a string"
        )
    if not markdown.strip():
        raise _ReportOutputValidationError(
            "report tool markdown must not be empty"
        )
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


def _not_found_policy_report(title: str, source_count: int) -> dict[str, Any]:
    """Build a deterministic cautious report when policy retrieval missed."""

    markdown = (
        f"# {title}\n\n"
        "## 政策依据\n"
        "未检索到有效的年假政策依据。\n\n"
        "## 结论\n"
        "当前资料不足，无法据此判断可休年假天数。请补充有效政策来源或咨询人事部门；"
        "本报告不会根据工龄自行推断法定天数。"
    )
    payload = {
        "title": title,
        "markdown": markdown,
        "source_count": source_count,
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


def _resolved_report_sources(messages: List[Dict[str, Any]]) -> Dict[str, Any] | None:
    """Read the Scheduler-assembled report input without asking an LLM to copy it."""

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
        report_sources = (
            resolved.get("report.sources") if isinstance(resolved, dict) else None
        )
        if isinstance(report_sources, dict):
            return report_sources
    return None


def _validate_annual_leave_sources(sources: List[Dict[str, Any]]) -> None:
    """Fail closed when an annual-leave fan-in is incomplete or duplicated."""

    logical_names = [
        str(source.get("logical_name") or "")
        for source in sources
        if isinstance(source, dict)
    ]
    if not set(_ANNUAL_LEAVE_SOURCE_SCHEMAS).intersection(logical_names):
        return
    for logical_name, schema_ref in _ANNUAL_LEAVE_SOURCE_SCHEMAS.items():
        matches = [
            source
            for source in sources
            if isinstance(source, dict)
            and source.get("logical_name") == logical_name
        ]
        if len(matches) != 1 or matches[0].get("schema_ref") != schema_ref:
            raise ValueError("annual-leave report sources are incomplete or invalid")


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
                sources = report_sources.get("sources")
                if not isinstance(sources, list) or not sources:
                    raise ValueError(
                        "report.sources must contain at least one upstream source"
                    )
                _validate_annual_leave_sources(sources)
                arguments = {
                    "data": sources,
                    "title": str(report_sources.get("title") or "分析报告"),
                    "instruction": str(
                        report_sources.get("instruction") or "使用全部上游来源生成报告"
                    ),
                }
                source_count = len(sources)
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
                                arguments["title"], source_count
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
            markdown = _normalize_report_markdown(
                _validate_report_tool_result(result)
            )
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
