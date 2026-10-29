"""Safety property tests: Kill-Switch durability and fail-closed invariants.

Invariants:
1. Trip persists atomically and writes durable journal event
2. File deletion reads as active, never as clear
3. File corruption reads as active, never as clear
4. Cross-process mtime sync detects changes
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from algodesk.engine.clock import SimClock
from algodesk.journal.store import JournalReader
from algodesk.journal.writer import Journal, JournalSettings
from algodesk.risk.killswitch import KillSwitch


def test_killswitch_trip_and_clear_lifecycle(tmp_path: Path) -> None:
    """Invariant 1: Tripping updates state and records journal event."""
    ks_file = tmp_path / "data" / "state" / "killswitch.json"
    j_db = tmp_path / "journal.db"
    journal = Journal(
        j_db,
        settings=JournalSettings(
            flush_interval_ms=50, max_buffer_events=1000, backpressure_timeout_ms=5000
        ),
    )
    journal.start_run(mode="paper", code_version="1.0", config_hash="h1", registry_hash="h2")
    clock = SimClock(datetime(2026, 10, 26, 9, 15, tzinfo=UTC))

    ks = KillSwitch(state_file=ks_file, journal=journal, clock=clock)
    assert not ks.is_active()

    # Trip
    ks.trip(reason="Daily loss limit breached", by="risk_monitor")
    assert ks.is_active()
    st = ks.status()
    assert st["active"] is True
    assert st["reason"] == "Daily loss limit breached"
    assert st["by"] == "risk_monitor"

    # Verify journal entry
    reader = JournalReader(j_db)
    evs = reader.events()
    assert len(evs) == 1
    assert evs[0]["kind"] == "killswitch.change"
    payload = json.loads(evs[0]["payload_json"])
    assert payload["active"] is True
    assert payload["reason"] == "Daily loss limit breached"


def test_killswitch_file_deletion_reads_as_active(tmp_path: Path) -> None:
    """Invariant 2: Deletion of an active killswitch state file reads as active, never as clear."""
    ks_file = tmp_path / "data" / "state" / "killswitch.json"
    clock = SimClock(datetime(2026, 10, 26, 9, 15, tzinfo=UTC))

    ks = KillSwitch(state_file=ks_file, clock=clock)
    ks.trip(reason="Market-wide circuit breaker", by="ops")
    assert ks.is_active() is True
    assert ks_file.is_file()

    # Adversarial deletion of state file
    ks_file.unlink()
    assert not ks_file.exists()

    # Check that it still reads as active (fails closed)
    assert ks.is_active() is True
    st = ks.status()
    assert st["active"] is True
    assert "fail-closed" in st.get("reason", "").lower()


def test_killswitch_file_corruption_reads_as_active(tmp_path: Path) -> None:
    """Invariant 3: Corruption of an active state file reads as active, never as clear."""
    ks_file = tmp_path / "data" / "state" / "killswitch.json"
    clock = SimClock(datetime(2026, 10, 26, 9, 15, tzinfo=UTC))

    ks = KillSwitch(state_file=ks_file, clock=clock)
    ks.trip(reason="Exchange connectivity failure", by="gateway")
    assert ks.is_active() is True

    # Adversarial corruption of state file with malformed JSON
    ks_file.write_text("{malformed: corrupted json data !@#$%", encoding="utf-8")

    # Invariant: Must remain active despite corrupted disk content
    assert ks.is_active() is True
    st = ks.status()
    assert st["active"] is True


def test_killswitch_cross_process_mtime_sync(tmp_path: Path) -> None:
    """Invariant 4: Second process detects disk modification via mtime check."""
    ks_file = tmp_path / "data" / "state" / "killswitch.json"
    clock = SimClock(datetime(2026, 10, 26, 9, 15, tzinfo=UTC))

    proc1 = KillSwitch(state_file=ks_file, clock=clock)
    proc2 = KillSwitch(state_file=ks_file, clock=clock)

    assert not proc1.is_active()
    assert not proc2.is_active()

    # Process 1 trips kill switch
    proc1.trip("Manual override", by="admin")
    assert proc1.is_active()

    # Process 2 syncs with disk and reads active state
    assert proc2.is_active()
    assert proc2.status()["reason"] == "Manual override"
