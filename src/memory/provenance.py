"""Bounded, redaction-aware conversation provenance shared by memory and skills."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .models import MemoryMessage
from .utils import redact_secrets


MAX_SNAPSHOT_CHARS = 12_000
MAX_MESSAGE_CHARS = 6_000


def _text(value: Any) -> str:
    return redact_secrets(str(value or "").strip())


def build_conversation_provenance(
    messages: Sequence[MemoryMessage | Mapping[str, Any]],
    *,
    turn_id: str | None = None,
    assistant_outputs: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Keep only user turns and final visible assistant content for audit."""

    def get(message: Any, key: str, default: Any = None) -> Any:
        if isinstance(message, Mapping):
            return message.get(key, default)
        return getattr(message, key, default)

    user_items: list[dict[str, Any]] = []
    assistant_items: list[dict[str, Any]] = []
    for message in messages:
        role = str(get(message, "role", "")).casefold()
        content = _text(get(message, "content", ""))
        if not content:
            continue
        metadata = get(message, "metadata", {})
        metadata = metadata if isinstance(metadata, Mapping) else {}
        item = {
            "message_id": str(get(message, "message_id", "")),
            "content": content[:MAX_MESSAGE_CHARS],
            "created_at": str(get(message, "created_at", "")),
            "truncated": len(content) > MAX_MESSAGE_CHARS,
        }
        if role == "user":
            user_items.append(item)
        elif role == "assistant" and (
            bool(metadata.get("main_visible", False))
            or str(get(message, "tool", "")).casefold()
            in {"agent_proxy", "publisher", "assistant"}
        ):
            assistant_items.append(item)

    for output in assistant_outputs or ():
        if not isinstance(output, Mapping):
            continue
        content = _text(output.get("content"))
        if content:
            assistant_items.append(
                {
                    "message_id": str(output.get("message_id") or ""),
                    "content": content[:MAX_MESSAGE_CHARS],
                    "created_at": str(output.get("created_at") or ""),
                    "truncated": len(content) > MAX_MESSAGE_CHARS,
                }
            )

    resolved_turn_id = str(turn_id or "")
    if not resolved_turn_id and user_items:
        resolved_turn_id = user_items[-1]["message_id"]
    payload = {
        "turn_id": resolved_turn_id,
        "user_messages": user_items,
        "assistant_messages": assistant_items,
        "redaction_applied": True,
    }
    # Bound the serialized snapshot without dropping the user turn identifier.
    serialized_length = len(str(payload))
    if serialized_length > MAX_SNAPSHOT_CHARS:
        for item in assistant_items:
            item["content"] = item["content"][:1000]
            item["truncated"] = True
    return payload


__all__ = ["MAX_MESSAGE_CHARS", "MAX_SNAPSHOT_CHARS", "build_conversation_provenance"]
