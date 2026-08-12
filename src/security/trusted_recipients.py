"""Platform-owned recipient resolution for remote email authorization."""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Iterable

from src.utils.path_utils import get_project_root


class TrustedRecipientResolutionError(ValueError):
    """A semantic recipient could not be resolved without expanding access."""


class AmbiguousTrustedRecipientError(TrustedRecipientResolutionError):
    """A recipient label maps to more than one trusted mailbox."""


class UnknownTrustedRecipientError(TrustedRecipientResolutionError):
    """A requested recipient is absent from the trusted directory."""


def _normalized(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


def _normalized_directory_label(value: Any) -> str:
    """Normalize a business label without changing an explicit email address."""

    normalized = _normalized(value)
    # Entity extraction may retain a harmless business wrapper, for example
    # ``邮箱hr@example.test``.  Resolve the explicit address itself, while still
    # requiring that address to exist in the platform-owned directory below.
    email_matches = re.findall(
        r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?",
        normalized,
    )
    if len(email_matches) == 1:
        return email_matches[0]
    if "@" in normalized:
        return normalized
    for suffix in ("的邮箱地址", "邮箱地址", "的邮箱", "邮箱"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)].strip()
            break
    return normalized


def _recipient_values(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in str(value or "").replace(";", ",").split(",") if part.strip()]


def _matches_name_title_alias(entry: dict[str, Any], recipient: str) -> bool:
    """Match a unique directory name prefix plus business title."""

    name = _normalized(entry.get("name"))
    positions = {
        _normalized(entry.get("position")),
        _normalized(entry.get("alternate_position")),
    }
    for title in ("负责人", "经理", "主管", "秘书", "行长"):
        normalized_title = _normalized(title)
        if not recipient.endswith(normalized_title):
            continue
        name_prefix = recipient[: -len(normalized_title)]
        return bool(name_prefix) and name.startswith(name_prefix) and any(
            normalized_title in position for position in positions if position
        )
    return False


def _matches_explicit_alias(entry: dict[str, Any], recipient: str) -> bool:
    aliases = entry.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    return recipient in {
        _normalized(alias) for alias in aliases if _normalized(alias)
    }


def _trusted_directory() -> list[dict[str, Any]]:
    root = get_project_root() / "assets"
    entries: list[dict[str, Any]] = []
    contacts_path = root / "contacts.json"
    if contacts_path.exists():
        data = json.loads(contacts_path.read_text(encoding="utf-8-sig"))
        entries.extend(item for item in data.get("contacts", []) if isinstance(item, dict))
    people_path = root / "person_info_sample.json"
    if people_path.exists():
        data = json.loads(people_path.read_text(encoding="utf-8-sig"))
        for person in data.get("personInfoList", []):
            if not isinstance(person, dict):
                continue
            entries.append({
                "name": person.get("adtEmpeNm"),
                "position": person.get("tcoPostNm") or person.get("nwgntPstNm"),
                "alternate_position": person.get("nwgntPstNm"),
                "email": person.get("internalMaiBox"),
            })
    return entries


def resolve_trusted_recipient_addresses(recipients: Any) -> list[str]:
    """Resolve each recipient to exactly one platform-controlled mailbox.

    Exact directory email addresses are valid identities.  Semantic labels
    (name or position) must resolve uniquely; returning every match for an
    ambiguous title would silently widen the send authorization.
    """

    requested = [item for item in _recipient_values(recipients) if _normalized(item)]
    if not requested:
        return []
    directory = _trusted_directory()
    resolved: set[str] = set()
    for raw_recipient in requested:
        recipient = _normalized_directory_label(raw_recipient)
        email_matches = {
            str(entry.get("email") or "").strip()
            for entry in directory
            if _normalized(entry.get("email")) == recipient
            and str(entry.get("email") or "").strip()
        }
        matches = email_matches
        if not matches:
            matches = {
                str(entry.get("email") or "").strip()
                for entry in directory
                if str(entry.get("email") or "").strip()
                and recipient
                in {
                    _normalized(entry.get("name")),
                    _normalized(entry.get("position")),
                    _normalized(entry.get("alternate_position")),
                }
            }
        if not matches:
            matches = {
                str(entry.get("email") or "").strip()
                for entry in directory
                if str(entry.get("email") or "").strip()
                and _matches_name_title_alias(entry, recipient)
            }
        if not matches:
            matches = {
                str(entry.get("email") or "").strip()
                for entry in directory
                if str(entry.get("email") or "").strip()
                and _matches_explicit_alias(entry, recipient)
            }
        if len(matches) > 1:
            raise AmbiguousTrustedRecipientError(
                f"trusted recipient is ambiguous: {raw_recipient!r} matches "
                f"{len(matches)} mailboxes"
            )
        if not matches:
            raise UnknownTrustedRecipientError(
                f"trusted recipient not found: {raw_recipient!r}"
            )
        resolved.update(matches)
    return sorted(resolved, key=str.casefold)
