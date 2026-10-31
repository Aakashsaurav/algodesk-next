"""Event kinds catalogue and registration for append-only journal.

INV-8: Append-only durability and tamper-evident audit trail.
M1 Design 8.1: Registered event catalogue.
"""

from __future__ import annotations

import re

_KIND_REGEX = re.compile(r"^[a-z][a-z0-9_.]*$")

M1_DESIGN_8_1_KINDS: frozenset[str] = frozenset(
    {
        "run.start",
        "config.effective",
        "registry.snapshot",
        "instruments.loaded",
        "feed.connected",
        "tick.stats",
        "bar.stats",
        "signal",
        "intent",
        "risk.decision",
        "order.created",
        "order.event",
        "fill",
        "portfolio.snapshot",
        "error",
        "run.end",
    }
)

NEW_OBSERVABILITY_KINDS: frozenset[str] = frozenset(
    {
        "proc.stats",
        "latency.stats",
    }
)

REGISTERED_EVENT_KINDS: set[str] = {
    # Session lifecycle & configuration
    "run.start",
    "config.effective",
    "registry.snapshot",
    "instruments.loaded",
    "feed.connected",
    "feed.disconnected",
    "feed.gap",
    "feed.late_tick",
    "recorder.file",
    "tick.stats",
    "bar.stats",
    "proc.stats",
    "latency.stats",
    "run.end",
    # Trading events
    "signal",
    "intent",
    "risk.decision",
    "order.created",
    "order.event",
    "fill",
    "position.change",
    "portfolio.snapshot",
    # Operational & risk events
    "alert.sent",
    "alert.failed",
    "killswitch.change",
    "killswitch.trip",
    "killswitch.clear",
    "timer",
    "error",
    # Recovery & data events
    "journal.run_recovered",
    "data.validation",
    "data.write",
    "data.quarantine",
}


def register_event_kind(kind: str) -> None:
    """Register a new event kind into the valid catalogue."""
    if not isinstance(kind, str) or not _KIND_REGEX.match(kind):
        raise ValueError(f"Invalid event kind format: {kind!r}")
    REGISTERED_EVENT_KINDS.add(kind)


def is_registered_kind(kind: str) -> bool:
    """Check if an event kind is in the registered catalogue."""
    return kind in REGISTERED_EVENT_KINDS
