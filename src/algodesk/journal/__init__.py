"""Append-only journal with integrity hash chain."""

from algodesk.journal.kinds import (
    REGISTERED_EVENT_KINDS,
    is_registered_kind,
    register_event_kind,
)
from algodesk.journal.store import (
    GENESIS_HASH,
    JournalReader,
    VerifyResult,
    events,
    stats,
    tail,
    unclosed_runs,
    verify_journal,
)
from algodesk.journal.writer import (
    Journal,
    JournalSettings,
)

__all__ = [
    "GENESIS_HASH",
    "Journal",
    "JournalReader",
    "JournalSettings",
    "REGISTERED_EVENT_KINDS",
    "VerifyResult",
    "events",
    "is_registered_kind",
    "register_event_kind",
    "stats",
    "tail",
    "unclosed_runs",
    "verify_journal",
]
