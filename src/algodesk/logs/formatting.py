"""IST time conversion and presentation formatting helpers (ADR-047, NFR-6).

Central layer-legal formatting utilities for rendering human-facing timestamps
in Indian Standard Time (IST, UTC+05:30) with mandatory 'IST' suffix, preserving
underlying UTC storage and data integrity.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def to_ist(val: Any) -> datetime | None:
    """Convert a datetime, epoch timestamp, or ISO string to an IST datetime."""
    if val is None or val == "" or val == "N/A":
        return None

    if isinstance(val, (int, float)):
        return datetime.fromtimestamp(val, tz=UTC).astimezone(IST)

    if isinstance(val, str):
        cleaned = val.strip()
        if not cleaned or cleaned.upper() == "N/A":
            return None
        is_already_ist = cleaned.endswith(" IST")
        if is_already_ist:
            cleaned = cleaned[:-4].strip()
        try:
            if "T" in cleaned or " " in cleaned or ":" in cleaned:
                dt = datetime.fromisoformat(cleaned)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=IST if is_already_ist else UTC)
                return dt.astimezone(IST)
            d = date.fromisoformat(cleaned)
            return datetime.combine(d, time.min, tzinfo=IST)
        except ValueError:
            return None

    if isinstance(val, datetime):
        dt = val if val.tzinfo is not None else val.replace(tzinfo=UTC)
        return dt.astimezone(IST)

    if isinstance(val, date):
        return datetime.combine(val, time.min, tzinfo=IST)

    return None


def format_ist(
    val: Any,
    *,
    date_only: bool = False,
    include_ms: bool = False,
    include_suffix: bool = True,
) -> str:
    """Format any timestamp or datetime in Indian Standard Time (IST).

    Guarantees:
    - Never outputs +00:00 or UTC offsets for human display.
    - Appends 'IST' suffix when include_suffix is True and not date_only.
    - Daily candles anchored at 18:30 UTC cleanly display the next calendar date.
    - Returns 'N/A' on None or unparseable input.
    """
    dt_ist = to_ist(val)
    if dt_ist is None:
        return "N/A"

    if date_only:
        return dt_ist.strftime("%Y-%m-%d")

    suffix = " IST" if include_suffix else ""
    if include_ms:
        ms = f"{int(dt_ist.microsecond / 1000):03d}"
        return dt_ist.strftime("%Y-%m-%d %H:%M:%S.") + ms + suffix

    return dt_ist.strftime("%Y-%m-%d %H:%M:%S") + suffix
