"""Tests for journal hardening: R-01, R-16, R-18."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from algodesk.api.errors import JournalError
from algodesk.journal.store import canonical_json, verify_journal
from algodesk.journal.writer import Journal, JournalSettings, WriterLock, encode_payload


def _settings(
    *,
    flush_interval_ms: int = 5,
    max_buffer_events: int = 100,
    backpressure_timeout_ms: int = 100,
) -> JournalSettings:
    return JournalSettings(
        flush_interval_ms=flush_interval_ms,
        max_buffer_events=max_buffer_events,
        backpressure_timeout_ms=backpressure_timeout_ms,
    )


def test_r01_sticky_failure_and_clean_close(tmp_path: Path) -> None:
    """R-01: After writer failure, all operations fail sticky; close releases lock and raises."""
    db_path = tmp_path / "sticky.db"
    settings = _settings(backpressure_timeout_ms=100)
    j = Journal(db_path, settings=settings)
    _run_id = j.start_run("paper", "v1", "c1", "r1")
    seq1 = j.append("order.first", {"val": 1})

    j.flush()
    assert seq1 == 1

    # Inject failure
    j._writer_exception = sqlite3.OperationalError("Simulated disk failure")

    # Later append raises
    with pytest.raises(JournalError, match="Simulated disk failure"):
        j.append("order.second", {"val": 2})

    # Sticky: NEXT append STILL raises
    with pytest.raises(JournalError, match="Simulated disk failure"):
        j.append("order.third", {"val": 3})

    # flush raises within timeout
    with pytest.raises(JournalError):
        j.flush()

    # start_run and end_run raise
    with pytest.raises(JournalError):
        j.start_run("paper", "v1", "c1", "r1")
    with pytest.raises(JournalError):
        j.end_run("ok")

    # close releases lock and then raises
    with pytest.raises(JournalError):
        j.close()

    # Lock must be free now: another writer can acquire it
    lock = WriterLock(db_path.with_name(f"{db_path.name}.lock"))
    lock.acquire()
    lock.release()

    # Events committed before failure still verify
    res = verify_journal(db_path)
    assert res.ok is True
    assert res.checked == 1


def test_r16_verify_journal_never_uses_fetchall(tmp_path: Path) -> None:
    """R-16: verify_journal iterates the cursor directly and never calls fetchall()."""
    db_path = tmp_path / "verify.db"
    settings = _settings()
    with Journal(db_path, settings=settings) as j:
        j.start_run("paper", "v1", "c1", "r1")
        j.append("ev.1", {"x": 1})
        j.append("ev.2", {"x": 2})
        j.flush()

    from algodesk.journal import store

    real_open = store.open_read_only_conn

    class CursorProxy:
        def __init__(self, cur: sqlite3.Cursor) -> None:
            self._cur = cur

        def execute(self, *args: Any, **kwargs: Any) -> CursorProxy:
            self._cur.execute(*args, **kwargs)
            return self

        def __iter__(self) -> Any:
            return iter(self._cur)

        def fetchall(self) -> list[object]:
            raise AssertionError("fetchall was called on sqlite3.Cursor during verify_journal!")

        def __getattr__(self, name: str) -> object:
            return getattr(self._cur, name)

    class ConnProxy:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self._conn = conn

        def cursor(self) -> CursorProxy:
            return CursorProxy(self._conn.cursor())

        def close(self) -> None:
            self._conn.close()

        def __getattr__(self, name: str) -> object:
            return getattr(self._conn, name)

    with patch(
        "algodesk.journal.store.open_read_only_conn",
        # pyrefly: ignore[implicit-any-lambda]
        side_effect=lambda p, b: ConnProxy(real_open(p, b)),
    ):
        res = verify_journal(db_path)
        assert res.ok is True
        assert res.checked == 2


def test_r18_nan_and_infinity_rejected() -> None:
    """R-18: NaN and infinity payloads raise JournalError."""
    with pytest.raises(JournalError, match="NaN"):
        encode_payload({"val": float("nan")})

    with pytest.raises(JournalError, match="infinity"):
        encode_payload({"val": float("inf")})

    with pytest.raises(JournalError, match="infinity"):
        encode_payload({"val": float("-inf")})

    with pytest.raises(JournalError):
        canonical_json({"val": float("nan")})


def test_r18_init_failure_closes_lock_and_connection(tmp_path: Path) -> None:
    """R-18: Failure during Journal.__init__ releases lock."""
    db_path = tmp_path / "init_fail.db"
    settings = _settings()

    with patch("algodesk.journal.writer.init_journal_db", side_effect=RuntimeError("init failed")):
        with pytest.raises(RuntimeError, match="init failed"):
            Journal(db_path, settings=settings)

    # Lock must be released so that another acquire succeeds
    lock = WriterLock(db_path.with_name(f"{db_path.name}.lock"))
    lock.acquire()
    lock.release()
