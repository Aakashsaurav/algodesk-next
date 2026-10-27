"""SQLite data manifest recording metadata for all partitioned files and instruments.

Transactional updates use explicit BEGIN IMMEDIATE for data integrity.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class FileRecord:
    path: str
    instrument: str
    kind: str
    timeframe: str
    date: str
    rows: int
    bytes: int
    first_ts: str
    last_ts: str
    sha256: str
    source: str
    state: str
    created_ts: str


@dataclass(frozen=True, slots=True)
class InstrumentRecord:
    id: str
    exchange: str
    segment: str
    trading_symbol: str
    kind: str
    underlying: str | None = None
    expiry: str | None = None


class DataManifest:
    """SQLite-backed metadata catalog with ACID transaction safety."""

    __slots__ = ("_conn", "_db_path")

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._db_path),
            autocommit=True,
        )

        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        cur = self._conn.cursor()
        cur.execute("PRAGMA journal_mode = WAL")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY,
                instrument TEXT NOT NULL,
                kind TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                date TEXT NOT NULL,
                rows INTEGER NOT NULL,
                bytes INTEGER NOT NULL,
                first_ts TEXT NOT NULL,
                last_ts TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                source TEXT NOT NULL,
                state TEXT NOT NULL,
                created_ts TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS instruments (
                id TEXT PRIMARY KEY,
                exchange TEXT NOT NULL,
                segment TEXT NOT NULL,
                trading_symbol TEXT NOT NULL,
                kind TEXT NOT NULL,
                underlying TEXT,
                expiry TEXT
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_files_inst_date ON files(instrument, date)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_files_kind_state ON files(kind, state)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_inst_sym ON instruments(trading_symbol)")
        self._conn.commit()

    def _begin_immediate(self) -> None:
        if not self._conn.in_transaction:
            self._conn.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        if self._conn.in_transaction:
            self._conn.execute("COMMIT")

    def _rollback(self) -> None:
        if self._conn.in_transaction:
            self._conn.execute("ROLLBACK")

    def record_file(self, record: FileRecord) -> None:
        """Insert or replace file metadata row under explicit BEGIN IMMEDIATE."""
        try:
            self._begin_immediate()
            self._conn.execute(
                """
                INSERT OR REPLACE INTO files
                (path, instrument, kind, timeframe, date, rows, bytes,
                 first_ts, last_ts, sha256, source, state, created_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.path,
                    record.instrument,
                    record.kind,
                    record.timeframe,
                    record.date,
                    record.rows,
                    record.bytes,
                    record.first_ts,
                    record.last_ts,
                    record.sha256,
                    record.source,
                    record.state,
                    record.created_ts,
                ),
            )
            self._commit()
        except Exception:
            self._rollback()
            raise

    def replace_parts_with_day(
        self,
        instrument: str,
        date_str: str,
        part_paths: Sequence[str],
        day_record: FileRecord,
    ) -> None:
        """Atomically delete part files and insert day.parquet row."""
        try:
            self._begin_immediate()
            for p in part_paths:
                self._conn.execute("DELETE FROM files WHERE path = ?", (p,))
            self._conn.execute(
                """
                INSERT OR REPLACE INTO files
                (path, instrument, kind, timeframe, date, rows, bytes,
                 first_ts, last_ts, sha256, source, state, created_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    day_record.path,
                    day_record.instrument,
                    day_record.kind,
                    day_record.timeframe,
                    day_record.date,
                    day_record.rows,
                    day_record.bytes,
                    day_record.first_ts,
                    day_record.last_ts,
                    day_record.sha256,
                    day_record.source,
                    day_record.state,
                    day_record.created_ts,
                ),
            )
            self._commit()
        except Exception:
            self._rollback()
            raise

    def delete_files(self, paths: Sequence[str]) -> int:
        """Delete files by path under explicit BEGIN IMMEDIATE transaction."""
        if not paths:
            return 0
        try:
            self._begin_immediate()
            deleted = 0
            for p in paths:
                cur = self._conn.execute("DELETE FROM files WHERE path = ?", (p,))
                deleted += cur.rowcount
            self._commit()
            return deleted
        except Exception:
            self._rollback()
            raise

    def record_instruments(self, records: Sequence[InstrumentRecord]) -> None:
        """Record instruments under explicit BEGIN IMMEDIATE transaction."""
        if not records:
            return
        try:
            self._begin_immediate()
            for r in records:
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO instruments
                    (id, exchange, segment, trading_symbol, kind, underlying, expiry)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (r.id, r.exchange, r.segment, r.trading_symbol, r.kind, r.underlying, r.expiry),
                )
            self._commit()
        except Exception:
            self._rollback()
            raise

    def get_files(
        self,
        instrument: str | None = None,
        date_str: str | None = None,
        kind: str | None = None,
        state: str | None = None,
    ) -> list[FileRecord]:
        """Query file metadata with optional filters."""
        query = "SELECT * FROM files WHERE 1=1"
        params: list[Any] = []
        if instrument:
            query += " AND instrument = ?"
            params.append(instrument)
        if date_str:
            query += " AND date = ?"
            params.append(date_str)
        if kind:
            query += " AND kind = ?"
            params.append(kind)
        if state:
            query += " AND state = ?"
            params.append(state)
        query += " ORDER BY instrument, date, path"

        cur = self._conn.execute(query, params)
        rows = cur.fetchall()
        return [
            FileRecord(
                path=r["path"],
                instrument=r["instrument"],
                kind=r["kind"],
                timeframe=r["timeframe"],
                date=r["date"],
                rows=r["rows"],
                bytes=r["bytes"],
                first_ts=r["first_ts"],
                last_ts=r["last_ts"],
                sha256=r["sha256"],
                source=r["source"],
                state=r["state"],
                created_ts=r["created_ts"],
            )
            for r in rows
        ]

    def get_instruments(self) -> list[InstrumentRecord]:
        """Fetch all recorded instruments."""
        cur = self._conn.execute("SELECT * FROM instruments ORDER BY id")
        rows = cur.fetchall()
        return [
            InstrumentRecord(
                id=r["id"],
                exchange=r["exchange"],
                segment=r["segment"],
                trading_symbol=r["trading_symbol"],
                kind=r["kind"],
                underlying=r["underlying"],
                expiry=r["expiry"],
            )
            for r in rows
        ]

    def list_instruments(self) -> list[InstrumentRecord]:
        """Alias for get_instruments."""
        return self.get_instruments()

    def delete_file(self, path: str) -> None:
        """Delete single file record from manifest."""
        self.delete_files([path])

    def close(self) -> None:
        """Close SQLite database connection."""
        self._conn.close()
