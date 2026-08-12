"""Audit-only integration between ToolRegistry and the generic selector."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Iterable, List, Mapping, Optional

from src.manager.registry.tool_registry import ToolRegistry
from src.manager.registry.tool_semantics import selection_candidate


logger = logging.getLogger(__name__)


def _message_text(messages: Iterable[Any]) -> str:
    for message in reversed(list(messages)):
        if isinstance(message, dict):
            content = message.get("content")
        else:
            content = getattr(message, "content", None)
        if content not in (None, ""):
            return str(content)
    return ""


def _known_inputs(metadata: Mapping[str, Any]) -> Dict[str, Any]:
    explicit = metadata.get("known_inputs")
    if isinstance(explicit, dict):
        return dict(explicit)
    resolved = metadata.get("resolved_inputs")
    if not isinstance(resolved, dict):
        return {}
    flattened: Dict[str, Any] = {}

    def visit(value: Any) -> None:
        if not isinstance(value, dict):
            return
        for key, item in value.items():
            if isinstance(item, dict):
                visit(item)
            elif item not in (None, "", [], {}):
                flattened.setdefault(str(key), item)

    visit(resolved)
    return flattened


class ToolSelectionService:
    """Produce audit metadata without altering selected tools or parameters."""

    def __init__(self, registry: Optional[ToolRegistry] = None) -> None:
        self._registry = registry

    @staticmethod
    def configured_mode() -> str:
        return str(os.getenv("MCP_TOOL_SELECTION_MODE") or "off").strip().lower()

    @staticmethod
    def top_k() -> int:
        value = int(os.getenv("MCP_TOOL_SELECTION_TOP_K") or "3")
        return max(1, value)

    async def audit(
        self,
        *,
        agent_name: str,
        messages: Iterable[Any],
        context: Any,
        actual_tool_names: Iterable[str],
    ) -> Optional[Dict[str, Any]]:
        requested_mode = self.configured_mode()
        if requested_mode == "off":
            return None
        # Production enforce is deliberately unavailable in this phase.
        effective_mode = "audit"
        actual_names = [str(name) for name in actual_tool_names if str(name)]
        if context.metadata is None:
            context.metadata = {}
        try:
            registry = self._registry or await ToolRegistry.get_instance()
            metadata = list((await registry.mcp_metadata_view()).values())
            candidates = [selection_candidate(item) for item in metadata]
            from src.tools.office_mcp.selector import select_tools

            decision = select_tools(
                task_text=_message_text(messages),
                target_agent=agent_name,
                known_inputs=_known_inputs(context.metadata or {}),
                candidates=candidates,
                top_k=self.top_k(),
                mode=effective_mode,
            )
            selected_candidate = decision["candidates"][0] if decision["selected_tool"] else None
            compatible_legacy = (
                selected_candidate.get("legacy_tool_name") if selected_candidate else None
            )
            matching_actual = next(
                (
                    name
                    for name in actual_names
                    if name in {decision["selected_tool"], compatible_legacy}
                ),
                None,
            )
            report = {
                **decision,
                "requested_mode": requested_mode,
                "mode": effective_mode,
                "enforce_blocked": requested_mode == "enforce",
                "agent_name": agent_name,
                "recommended_mcp_tool": decision["selected_tool"],
                "compatible_legacy_tool": compatible_legacy,
                "actual_legacy_tool": matching_actual,
                "actual_tool_names": actual_names,
                "recommendation_matches_actual": bool(
                    decision["selected_tool"] and matching_actual
                ),
            }
        except Exception as exc:  # audit must never change execution behavior
            logger.warning("MCP tool-selection audit failed for %s: %s", agent_name, exc)
            report = {
                "requested_mode": requested_mode,
                "mode": effective_mode,
                "enforce_blocked": requested_mode == "enforce",
                "agent_name": agent_name,
                "actual_tool_names": actual_names,
                "error": str(exc),
            }
        context.metadata["tool_selection_audit"] = report
        logger.info(
            "MCP tool audit agent=%s before=%s after=%s top_k=%s",
            agent_name,
            report.get("candidate_count_before_filter"),
            report.get("candidate_count_after_filter"),
            [item.get("tool_key") for item in report.get("candidates", [])],
        )
        return report
