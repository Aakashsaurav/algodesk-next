"""Tests for ParamStore SQLite persistence, triggers, integrity, and schema checks."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


def test_corrupt_database_raises_param_error(tmp_path: Path) -> None:
    """Opening a corrupt database file must raise ParamError with fix hint."""
    from algodesk.api.errors import ParamError
    from algodesk.params.store import ParamStore

    bad_db = tmp_path / "corrupt.db"
    bad_db.write_bytes(b"not a valid sqlite file contents")

    with pytest.raises(ParamError) as exc_info:
        ParamStore(bad_db)
    assert "corrupt" in exc_info.value.what.lower()
    assert "restore" in exc_info.value.fix.lower()


def test_newer_schema_version_raises(tmp_path: Path) -> None:
    """Opening a database with newer schema_version must raise ParamError with fix hint."""
    from algodesk.api.errors import ParamError
    from algodesk.params.store import ParamStore

    db_file = tmp_path / "future.db"
    conn = sqlite3.connect(db_file, autocommit=True)
    conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY);")
    conn.execute("INSERT INTO schema_version (version) VALUES (99);")
    conn.close()

    with pytest.raises(ParamError) as exc_info:
        ParamStore(db_file)
    assert "newer than supported" in exc_info.value.what
    assert "upgrade" in exc_info.value.fix.lower()


def test_history_triggers_prevent_update(tmp_path: Path) -> None:
    """Triggers must forbid UPDATE on param_history table."""
    from algodesk.params.store import ParamStore

    db_file = tmp_path / "hist.db"
    store = ParamStore(db_file)
    store.add_history(
        ts_utc="2026-10-18T00:00:00+00:00",
        name="test.limit",
        scope="global",
        old_value=None,
        new_value=10,
        by="admin",
        channel="cli",
        reason="initial setup",
        confirmed=None,
        status="applied",
    )
    store.close()

    conn = sqlite3.connect(db_file, autocommit=True)
    with pytest.raises(sqlite3.DatabaseError) as exc_info:
        conn.execute("UPDATE param_history SET reason = 'tampered' WHERE id = 1;")
    assert "param_history is append-only: UPDATE forbidden" in str(exc_info.value)
    conn.close()


def test_history_triggers_prevent_delete(tmp_path: Path) -> None:
    """Triggers must forbid DELETE on param_history table."""
    from algodesk.params.store import ParamStore

    db_file = tmp_path / "hist.db"
    store = ParamStore(db_file)
    store.add_history(
        ts_utc="2026-10-18T00:00:00+00:00",
        name="test.limit",
        scope="global",
        old_value=None,
        new_value=10,
        by="admin",
        channel="cli",
        reason="initial setup",
        confirmed=None,
        status="applied",
    )
    store.close()

    conn = sqlite3.connect(db_file, autocommit=True)
    with pytest.raises(sqlite3.DatabaseError) as exc_info:
        conn.execute("DELETE FROM param_history WHERE id = 1;")
    assert "param_history is append-only: DELETE forbidden" in str(exc_info.value)
    conn.close()


def test_wal_mode_and_pragmas(tmp_path: Path) -> None:
    """ParamStore enables WAL mode, busy_timeout 5000, and foreign keys."""
    from algodesk.params.store import ParamStore

    db_file = tmp_path / "pragmas.db"
    store = ParamStore(db_file)
    conn = store._conn
    journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
    foreign_keys = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
    busy_timeout = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
    store.close()

    assert journal_mode.lower() == "wal"
    assert foreign_keys == 1
    assert busy_timeout == 5000


def test_store_value_and_pending_crud(tmp_path: Path) -> None:
    """ParamStore handles values, pending entries, and sequence increments."""
    from algodesk.params.store import ParamStore

    db_file = tmp_path / "crud.db"
    store = ParamStore(db_file)

    seq1 = store.set_value("test.val", "global", 42, "2026-10-18T01:00:00Z")
    assert seq1 == 1

    values = store.load_all_values()
    assert ("test.val", "global") in values
    val, updated = values[("test.val", "global")]
    assert val == 42
    assert updated == "2026-10-18T01:00:00Z"

    store.add_pending("test.val", "global", 50, "2026-10-18T01:05:00Z", "session_start")
    pending = store.load_all_pending()
    assert len(pending) == 1
    assert pending[0]["value"] == 50

    seq2 = store.delete_value("test.val", "global")
    assert seq2 == 2
    assert store.load_all_values() == {}
    assert store.load_all_pending() == []
    store.close()
