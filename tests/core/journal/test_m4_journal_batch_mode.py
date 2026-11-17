"""Tests for Step 11: Journal batch mode for backtest only, hash integrity, and refusals."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from algodesk.api.errors import SafetyViolation
from algodesk.journal.store import verify_journal
from algodesk.journal.writer import Journal, JournalSettings


def test_paper_and_live_refuse_batch_mode(tmp_path: Path) -> None:
    db_path = tmp_path / "test_refuse.db"
    settings = JournalSettings(
        flush_interval_ms=50,
        max_buffer_events=1000,
        backpressure_timeout_ms=5000,
        batch_mode=True,
        batch_size=100,
    )
    journal = Journal(db_path=db_path, settings=settings)

    # Paper mode must refuse batch mode
    with pytest.raises(SafetyViolation, match="Journal batch mode is forbidden in 'paper' mode"):
        journal.start_run("paper", "0.1.0", "cfg", "reg")

    # Live mode must refuse batch mode
    with pytest.raises(SafetyViolation, match="Journal batch mode is forbidden in 'live' mode"):
        journal.start_run("live", "0.1.0", "cfg", "reg")

    journal.close()


def test_batch_mode_commits_in_batches_and_preserves_hash_chain(tmp_path: Path) -> None:
    db_path = tmp_path / "test_batch.db"
    settings = JournalSettings(
        flush_interval_ms=500,
        max_buffer_events=5000,
        backpressure_timeout_ms=5000,
        batch_mode=True,
        batch_size=50,
    )
    journal = Journal(db_path=db_path, settings=settings)

    journal.start_run("backtest", "0.1.0", "cfg", "reg")

    # Append 120 events
    for i in range(120):
        journal.append("test.event", {"idx": i})

    # Flush and end run
    journal.end_run("ok")
    journal.close()

    # Verify total events in SQLite
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM events WHERE kind = 'test.event';")
    count = cur.fetchone()[0]
    assert count == 120
    conn.close()

    # Strict hash chain verification passes completely
    verified = verify_journal(db_path)
    assert verified.ok is True
    assert verified.checked >= 120
