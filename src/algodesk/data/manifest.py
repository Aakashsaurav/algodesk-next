"""SQLite data manifest recording metadata for all partitioned files and instruments.

Transactional updates use explicit BEGIN IMMEDIATE for data integrity.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Sequence
from dataclasses import astuple, dataclass
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


@dataclass(frozen=True, slots=True)
class DatasetRecord:
    dataset_id: str
    instrument: str
    kind: str
    timeframe: str
    adjustment: str
    source: str
    schema_version: int = 1
    created_at: str = ""
    actions_epoch: str | None = None


@dataclass(frozen=True, slots=True)
class PartRecord:
    path: str
    dataset_id: str
    rows: int
    bytes: int
    first_ts: str
    last_ts: str
    sha256: str
    source: str
    source_version: str
    fetched_at: str


@dataclass(frozen=True, slots=True)
class CoverageDayRecord:
    dataset_id: str
    day: str
    status: str
    rows: int
    expected_rows: int
    part_path: str
    fetched_at: str
    note: str = ""


@dataclass(frozen=True, slots=True)
class FetchLogRecord:
    id: int
    chunk: str
    source: str
    started: str
    finished: str
    rows: int
    outcome: str
    error: str = ""


@dataclass(frozen=True, slots=True)
class QuarantineRecord:
    id: str
    chunk: str
    reason: str
    report_path: str
    created: str
    released_at: str | None = None
    purged_at: str | None = None
    status: str = "quarantined"


class DataManifest:
    """SQLite-backed metadata catalog with ACID transaction safety."""

    __slots__ = ("_conn", "_db_path", "_lock")

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self._db_path),
            autocommit=True,
            check_same_thread=False,
        )

        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        cur = self._conn.cursor()
        cur.execute("PRAGMA journal_mode = WAL")
        cur.execute(
            "CREATE TABLE IF NOT EXISTS schema_version "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        v_row = cur.execute("SELECT MAX(version) FROM schema_version").fetchone()
        current_v = v_row[0] if v_row and v_row[0] is not None else 0
        if (
            current_v == 0
            and cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='files'"
            ).fetchone()
        ):
            cur.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (1, datetime('now'))"
            )
            current_v = 1
        cur.executescript(
            "CREATE TABLE IF NOT EXISTS files "
            "(path TEXT PRIMARY KEY, instrument TEXT NOT NULL, kind TEXT NOT NULL, "
            "timeframe TEXT NOT NULL, date TEXT NOT NULL, rows INTEGER NOT NULL, "
            "bytes INTEGER NOT NULL, first_ts TEXT NOT NULL, last_ts TEXT NOT NULL, "
            "sha256 TEXT NOT NULL, source TEXT NOT NULL, state TEXT NOT NULL, "
            "created_ts TEXT NOT NULL);\n"
            "CREATE TABLE IF NOT EXISTS instruments "
            "(id TEXT PRIMARY KEY, exchange TEXT NOT NULL, segment TEXT NOT NULL, "
            "trading_symbol TEXT NOT NULL, kind TEXT NOT NULL, underlying TEXT, expiry TEXT);\n"
            "CREATE INDEX IF NOT EXISTS idx_files_inst_date ON files(instrument, date);\n"
            "CREATE INDEX IF NOT EXISTS idx_files_kind_state ON files(kind, state);\n"
            "CREATE INDEX IF NOT EXISTS idx_inst_sym ON instruments(trading_symbol);\n"
        )
        if current_v < 2:
            cur.executescript(
                "CREATE TABLE IF NOT EXISTS datasets "
                "(dataset_id TEXT PRIMARY KEY, instrument TEXT NOT NULL, kind TEXT NOT NULL, "
                "timeframe TEXT NOT NULL, adjustment TEXT NOT NULL, source TEXT NOT NULL, "
                "schema_version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, "
                "actions_epoch TEXT);\n"
                "CREATE TABLE IF NOT EXISTS parts "
                "(path TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, rows INTEGER NOT NULL, "
                "bytes INTEGER NOT NULL, first_ts TEXT NOT NULL, last_ts TEXT NOT NULL, "
                "sha256 TEXT NOT NULL, source TEXT NOT NULL, source_version TEXT NOT NULL, "
                "fetched_at TEXT NOT NULL);\n"
                "CREATE TABLE IF NOT EXISTS coverage_days "
                "(dataset_id TEXT NOT NULL, day TEXT NOT NULL, status TEXT NOT NULL, "
                "rows INTEGER NOT NULL, expected_rows INTEGER NOT NULL, part_path TEXT, "
                "fetched_at TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', "
                "PRIMARY KEY (dataset_id, day));\n"
                "CREATE TABLE IF NOT EXISTS fetch_log "
                "(id INTEGER PRIMARY KEY AUTOINCREMENT, chunk TEXT NOT NULL, "
                "source TEXT NOT NULL, started TEXT NOT NULL, finished TEXT NOT NULL, "
                "rows INTEGER NOT NULL, outcome TEXT NOT NULL, error TEXT NOT NULL DEFAULT '');\n"
                "CREATE TABLE IF NOT EXISTS quarantine "
                "(id TEXT PRIMARY KEY, chunk TEXT NOT NULL, reason TEXT NOT NULL, "
                "report_path TEXT NOT NULL, created TEXT NOT NULL, released_at TEXT, "
                "purged_at TEXT, status TEXT NOT NULL DEFAULT 'quarantined');\n"
                "CREATE INDEX IF NOT EXISTS idx_parts_dataset ON parts(dataset_id);\n"
                "CREATE INDEX IF NOT EXISTS idx_coverage_dataset_day"
                " ON coverage_days(dataset_id, day);\n"
                "CREATE INDEX IF NOT EXISTS idx_coverage_status ON coverage_days(status);\n"
                "CREATE INDEX IF NOT EXISTS idx_quarantine_status ON quarantine(status);\n"
                "INSERT OR REPLACE INTO schema_version (version, applied_at)"
                " VALUES (2, datetime('now'));\n"
            )
            try:
                cols = [r[1] for r in cur.execute("PRAGMA table_info(datasets)").fetchall()]
                if cols and "actions_epoch" not in cols:
                    cur.execute("ALTER TABLE datasets ADD COLUMN actions_epoch TEXT")
            except Exception:
                pass
        self._conn.commit()

    @property
    def schema_version(self) -> int:
        """Current schema version of manifest database."""
        cur = self._conn.execute("SELECT MAX(version) FROM schema_version")
        row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else 1

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
        return [FileRecord(**dict(r)) for r in cur.fetchall()]

    def get_instruments(self) -> list[InstrumentRecord]:
        """Fetch all recorded instruments."""
        cur = self._conn.execute("SELECT * FROM instruments ORDER BY id")
        return [InstrumentRecord(**dict(r)) for r in cur.fetchall()]

    def list_instruments(self) -> list[InstrumentRecord]:
        """Alias for get_instruments."""
        return self.get_instruments()

    def delete_file(self, path: str) -> None:
        """Delete single file record from manifest."""
        self.delete_files([path])

    def record_dataset(self, record: DatasetRecord) -> None:
        """Insert or replace dataset metadata row under transaction."""
        try:
            self._begin_immediate()
            self._conn.execute(
                """
                INSERT OR REPLACE INTO datasets
                (dataset_id, instrument, kind, timeframe, adjustment,
                 source, schema_version, created_at, actions_epoch)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                astuple(record),
            )
            self._commit()
        except Exception:
            self._rollback()
            raise

    def update_actions_epoch(self, dataset_id: str, actions_epoch: str) -> None:
        """Update actions_epoch for a dataset."""
        with self._lock:
            self._begin_immediate()
            try:
                self._conn.execute(
                    "UPDATE datasets SET actions_epoch = ? WHERE dataset_id = ?",
                    (actions_epoch, dataset_id),
                )
                self._commit()
            except Exception:
                self._rollback()
                raise

    def get_dataset(self, dataset_id: str) -> DatasetRecord | None:
        """Fetch dataset record by dataset_id."""
        cur = self._conn.execute("SELECT * FROM datasets WHERE dataset_id = ?", (dataset_id,))
        row = cur.fetchone()
        return DatasetRecord(**dict(row)) if row else None

    def list_datasets(
        self,
        instrument: str | None = None,
        kind: str | None = None,
        timeframe: str | None = None,
        source: str | None = None,
    ) -> list[DatasetRecord]:
        """Query datasets with optional filters."""
        query = "SELECT * FROM datasets WHERE 1=1"
        params: list[Any] = []
        for col, val in (
            ("instrument", instrument),
            ("kind", kind),
            ("timeframe", timeframe),
            ("source", source),
        ):
            if val:
                query += f" AND {col} = ?"
                params.append(val)
        query += " ORDER BY dataset_id"

        cur = self._conn.execute(query, params)
        return [DatasetRecord(**dict(r)) for r in cur.fetchall()]

    def record_part(self, record: PartRecord) -> None:
        """Insert or replace part file metadata row under transaction."""
        try:
            self._begin_immediate()
            self._conn.execute(
                """
                INSERT OR REPLACE INTO parts
                (path, dataset_id, rows, bytes, first_ts, last_ts,
                 sha256, source, source_version, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                astuple(record),
            )
            self._commit()
        except Exception:
            self._rollback()
            raise

    def get_parts(self, dataset_id: str | None = None) -> list[PartRecord]:
        """Query parts with optional dataset_id filter."""
        query = "SELECT * FROM parts WHERE 1=1"
        params: list[Any] = []
        if dataset_id:
            query += " AND dataset_id = ?"
            params.append(dataset_id)
        query += " ORDER BY path"
        cur = self._conn.execute(query, params)
        return [PartRecord(**dict(r)) for r in cur.fetchall()]

    def get_part_by_path(self, path: str) -> PartRecord | None:
        """Get single part record by file path."""
        cur = self._conn.execute("SELECT * FROM parts WHERE path = ?", (path,))
        r = cur.fetchone()
        return PartRecord(**dict(r)) if r else None

    def delete_part(self, path: str) -> None:
        """Delete part metadata row by path under transaction."""
        try:
            self._begin_immediate()
            self._conn.execute("DELETE FROM parts WHERE path = ?", (path,))
            self._commit()
        except Exception:
            self._rollback()
            raise

    def record_coverage_day(self, record: CoverageDayRecord) -> None:
        """Insert or replace single day coverage record."""
        self.record_coverage_days([record])

    def record_coverage_days(self, records: Sequence[CoverageDayRecord]) -> None:
        """Insert or replace coverage records in a single transaction."""
        if not records:
            return
        try:
            self._begin_immediate()
            for r in records:
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO coverage_days
                    (dataset_id, day, status, rows, expected_rows, part_path, fetched_at, note)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    astuple(r),
                )
            self._commit()
        except Exception:
            self._rollback()
            raise

    def get_coverage_days(
        self,
        dataset_id: str,
        start_day: str | None = None,
        end_day: str | None = None,
    ) -> list[CoverageDayRecord]:
        """Query coverage records for a dataset with optional date bounds."""
        query = "SELECT * FROM coverage_days WHERE dataset_id = ?"
        params: list[Any] = [dataset_id]
        if start_day:
            query += " AND day >= ?"
            params.append(start_day)
        if end_day:
            query += " AND day <= ?"
            params.append(end_day)
        query += " ORDER BY day"
        cur = self._conn.execute(query, params)
        return [CoverageDayRecord(**dict(r)) for r in cur.fetchall()]

    def record_fetch_log(
        self,
        chunk: str,
        source: str,
        started: str,
        finished: str,
        rows: int,
        outcome: str,
        error: str = "",
    ) -> int:
        """Append fetch operation entry to fetch_log."""
        try:
            self._begin_immediate()
            cur = self._conn.execute(
                """
                INSERT INTO fetch_log
                (chunk, source, started, finished, rows, outcome, error)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (chunk, source, started, finished, rows, outcome, error),
            )
            log_id = int(cur.lastrowid or 0)
            self._commit()
            return log_id
        except Exception:
            self._rollback()
            raise

    def get_fetch_logs(self, limit: int | None = None) -> list[FetchLogRecord]:
        """Fetch recent fetch_log entries in descending order."""
        if limit is not None:
            cur = self._conn.execute("SELECT * FROM fetch_log ORDER BY id DESC LIMIT ?", (limit,))
        else:
            cur = self._conn.execute("SELECT * FROM fetch_log ORDER BY id DESC")
        return [FetchLogRecord(**dict(r)) for r in cur.fetchall()]

    def record_quarantine(self, record: QuarantineRecord) -> None:
        """Record quarantined chunk entry under transaction."""
        try:
            self._begin_immediate()
            self._conn.execute(
                """
                INSERT OR REPLACE INTO quarantine
                (id, chunk, reason, report_path, created, released_at, purged_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                astuple(record),
            )
            self._commit()
        except Exception:
            self._rollback()
            raise

    def update_quarantine(
        self,
        id: str,
        status: str,
        released_at: str | None = None,
        purged_at: str | None = None,
    ) -> None:
        """Update quarantine record status, release timestamp, or purge timestamp."""
        try:
            self._begin_immediate()
            self._conn.execute(
                """
                UPDATE quarantine
                SET status = ?,
                    released_at = COALESCE(?, released_at),
                    purged_at = COALESCE(?, purged_at)
                WHERE id = ?
                """,
                (status, released_at, purged_at, id),
            )
            self._commit()
        except Exception:
            self._rollback()
            raise

    def get_quarantine(
        self,
        id: str | None = None,
        status: str | None = None,
    ) -> list[QuarantineRecord]:
        """Query quarantine records with optional id and status filters."""
        query = "SELECT * FROM quarantine WHERE 1=1"
        params: list[Any] = []
        if id:
            query += " AND id = ?"
            params.append(id)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created DESC"
        cur = self._conn.execute(query, params)
        return [QuarantineRecord(**dict(r)) for r in cur.fetchall()]

    def close(self) -> None:
        """Close SQLite database connection."""
        self._conn.close()
