from __future__ import annotations

import logging
import os
from datetime import datetime, time


logger = logging.getLogger(__name__)
_DEFAULT_START = "09:00"
_DEFAULT_END = "18:00"


def _configured_time(name: str, default: str) -> time:
    raw = str(os.getenv(name, default) or default).strip()
    try:
        return datetime.strptime(raw, "%H:%M").time()
    except ValueError:
        logger.warning("Invalid %s=%r; using %s", name, raw, default)
        return datetime.strptime(default, "%H:%M").time()


def is_within_working_hours(now: datetime | None = None) -> bool:
    """Return whether local wall-clock time is inside [start, end)."""
    value = now or datetime.now()
    if callable(getattr(value, "time", None)):
        current = value.time()
    else:
        # Keep compatibility with lightweight clock doubles used by policy tests.
        current = time(
            int(getattr(value, "hour", 0)),
            int(getattr(value, "minute", 0)),
            int(getattr(value, "second", 0)),
        )
    start = _configured_time("S_ABAC_WORKING_HOURS_START", _DEFAULT_START)
    end = _configured_time("S_ABAC_WORKING_HOURS_END", _DEFAULT_END)
    if start <= end:
        return start <= current < end
    # Also support an overnight window such as 22:00-06:00.
    return current >= start or current < end
