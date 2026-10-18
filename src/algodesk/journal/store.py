"""Journal SQLite store, schema, triggers, verification and read APIs.

INV-8: Append-only durability and tamper-evident audit trail.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from algodesk.api.errors import JournalError

CURRENT_SCHEMA_VERSION = 1
GENESIS_HASH = "0" * 64


@dataclass(frozen=True)
class VerifyResult:
    """Result of journal verification."""

    ok: bool
    checked: int
    first_bad_seq: int | None = None
    reason: str | None = None


def canonical_json(payload: Mapping[str, Any]) -> str:
    """Serialize dictionary to canonical JSON with sorted keys and no whitespace."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def compute_event_hash(
    prev_hash: str,
    run_id: str,
    ts_utc: str,
    kind: str,
    canonical_payload_json: str,
) -> str:
    """Compute sha256 hash over event metadata and payload."""
    hasher = hashlib.sha256()
    hasher.update(prev_hash.encode("utf-8"))
    hasher.update(run_id.encode("utf-8"))
    hasher.update(ts_utc.encode("utf-8"))
    hasher.update(kind.encode("utf-8"))
    hasher.update(canonical_payload_json.encode("utf-8"))
    return hasher.hexdigest()


def _check_db(conn: sqlite3.Connection, path_str: str) -> None:
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA quick_check;")
        res = cur.fetchone()
        if res and res[0] != "ok":
            raise JournalError(
                what=f"Journal database integrity check failed: {res[0]}",
                where=path_str,
                fix="Repair SQLite database or restore from backup",
            )
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version';")
        if cur.fetchone():
            cur.execute("SELECT max(version) FROM schema_version;")
            row = cur.fetchone()
            if row and row[0] is not None and row[0] > CURRENT_SCHEMA_VERSION:
                raise JournalError(
                    what=(
                        f"Journal schema version {row[0]} is newer than supported "
                        f"{CURRENT_SCHEMA_VERSION}"
                    ),
                    where=path_str,
                    fix="Upgrade AlgoDesk to support this journal database",
                )
    except sqlite3.DatabaseError as exc:
        raise JournalError(
            what=f"Corrupt or unreadable journal database: {exc}",
            where=path_str,
            fix="Repair SQLite database or restore from backup",
        ) from exc


def init_journal_db(conn: sqlite3.Connection, busy_timeout_ms: int) -> None:
    """Initialize schema, tables and triggers on a read-write connection."""
    conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms};")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")

    with conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS runs ("
            "    run_id TEXT PRIMARY KEY,"
            "    started_utc TEXT NOT NULL,"
            "    mode TEXT NOT NULL,"
            "    code_version TEXT NOT NULL,"
            "    config_hash TEXT NOT NULL,"
            "    registry_hash TEXT NOT NULL,"
            "    status TEXT NOT NULL,"
            "    ended_utc TEXT"
            ");"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            "    seq INTEGER PRIMARY KEY,"
            "    run_id TEXT NOT NULL REFERENCES runs(run_id),"
            "    ts_utc TEXT NOT NULL,"
            "    mono_ns INTEGER NOT NULL,"
            "    kind TEXT NOT NULL,"
            "    payload_json TEXT NOT NULL,"
            "    prev_hash TEXT NOT NULL,"
            "    hash TEXT NOT NULL"
            ");"
        )
        _create_triggers(conn)
        conn.execute(
            "INSERT OR IGNORE INTO schema_version (version) VALUES (?);",
            (CURRENT_SCHEMA_VERSION,),
        )


def _create_triggers(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_events_no_update "
        "BEFORE UPDATE ON events BEGIN "
        "SELECT RAISE(ABORT, 'events table is append-only: UPDATE forbidden'); END;"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_events_no_delete "
        "BEFORE DELETE ON events BEGIN "
        "SELECT RAISE(ABORT, 'events table is append-only: DELETE forbidden'); END;"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_runs_no_delete "
        "BEFORE DELETE ON runs BEGIN "
        "SELECT RAISE(ABORT, 'runs table is append-only: DELETE forbidden'); END;"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_runs_limit_update "
        "BEFORE UPDATE ON runs BEGIN "
        "SELECT CASE "
        "    WHEN OLD.run_id != NEW.run_id "
        "      OR OLD.started_utc != NEW.started_utc "
        "      OR OLD.mode != NEW.mode "
        "      OR OLD.code_version != NEW.code_version "
        "      OR OLD.config_hash != NEW.config_hash "
        "      OR OLD.registry_hash != NEW.registry_hash "
        "    THEN RAISE(ABORT, 'runs may change only status and ended_utc') "
        "END; END;"
    )


def open_read_only_conn(path: Path | str, busy_timeout_ms: int) -> sqlite3.Connection:
    """Open read-only SQLite connection (mode=ro)."""
    p = Path(path).resolve()
    if not p.is_file():
        raise JournalError(
            what=f"Journal file does not exist: {p}",
            where=str(p),
            fix="Check database path or initialize journal first",
        )
    uri = f"file:{p.as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=busy_timeout_ms / 1000.0)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms};")
        conn.execute("PRAGMA foreign_keys = ON;")
        _check_db(conn, str(p))
        return conn
    except sqlite3.DatabaseError as exc:
        raise JournalError(
            what=f"Could not open journal read-only: {exc}",
            where=str(p),
            fix="Ensure journal file is valid SQLite database",
        ) from exc


def _verify_row(
    row: sqlite3.Row,
    expected_seq: int,
    expected_prev_hash: str,
) -> tuple[bool, str, str | None]:
    seq, run_id, ts_utc, kind, payload_json, prev_hash, stored_hash = (
        row["seq"],
        row["run_id"],
        row["ts_utc"],
        row["kind"],
        row["payload_json"],
        row["prev_hash"],
        row["hash"],
    )
    if seq != expected_seq:
        return (
            False,
            f"Sequence gap or removed row: expected seq {expected_seq}, got {seq}",
            None,
        )
    if prev_hash != expected_prev_hash:
        return (
            False,
            f"Broken hash link: expected prev_hash {expected_prev_hash}, got {prev_hash}",
            None,
        )

    try:
        canon = canonical_json(json.loads(payload_json))
    except Exception as exc:
        return False, f"Invalid payload JSON: {exc}", None
    computed_hash = compute_event_hash(prev_hash, run_id, ts_utc, kind, canon)
    if stored_hash != computed_hash:
        return False, f"Hash mismatch: stored {stored_hash}, computed {computed_hash}", None
    return True, "", stored_hash


def verify_journal(path: Path | str, *, busy_timeout_ms: int = 5000) -> VerifyResult:
    """Verify hash chain integrity and sequence order of the journal."""
    p = Path(path).resolve()
    conn = open_read_only_conn(p, busy_timeout_ms)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT seq, run_id, ts_utc, kind, payload_json, prev_hash, hash "
            "FROM events ORDER BY seq ASC;"
        )
        rows = cur.fetchall()
        expected_seq = 1
        expected_prev_hash = GENESIS_HASH

        for row in rows:
            ok, reason, next_hash = _verify_row(row, expected_seq, expected_prev_hash)
            if not ok or next_hash is None:
                return VerifyResult(
                    ok=False,
                    checked=expected_seq - 1,
                    first_bad_seq=row["seq"],
                    reason=reason,
                )
            expected_seq += 1
            expected_prev_hash = next_hash

        return VerifyResult(ok=True, checked=len(rows))
    finally:
        conn.close()


class JournalReader:
    """Read APIs for journal using a read-only connection."""

    def __init__(self, db_path: Path | str, *, busy_timeout_ms: int = 5000) -> None:
        self.db_path = Path(db_path).resolve()
        self.busy_timeout_ms = busy_timeout_ms
        self._conn = open_read_only_conn(self.db_path, self.busy_timeout_ms)

    def tail(self, n: int) -> list[dict[str, Any]]:
        """Return the last n events ordered by sequence."""
        cur = self._conn.cursor()
        cur.execute(
            "SELECT seq, run_id, ts_utc, mono_ns, kind, payload_json, prev_hash, hash "
            "FROM (SELECT * FROM events ORDER BY seq DESC LIMIT ?) ORDER BY seq ASC;",
            (n,),
        )
        return [dict(r) for r in cur.fetchall()]

    def events(
        self,
        *,
        run_id: str | None = None,
        kind: str | None = None,
        since_seq: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Query events matching optional run_id, kind, and sequence filters."""
        query = (
            "SELECT seq, run_id, ts_utc, mono_ns, kind, payload_json, prev_hash, hash "
            "FROM events WHERE 1=1"
        )
        params: list[Any] = []
        if run_id is not None:
            query += " AND run_id = ?"
            params.append(run_id)
        if kind is not None:
            query += " AND kind = ?"
            params.append(kind)
        if since_seq is not None:
            query += " AND seq > ?"
            params.append(since_seq)
        query += " ORDER BY seq ASC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        cur = self._conn.cursor()
        cur.execute(query, tuple(params))
        return [dict(r) for r in cur.fetchall()]

    def stats(self) -> dict[str, Any]:
        """Return aggregate statistics about the journal."""
        cur = self._conn.cursor()
        cur.execute("SELECT count(*), min(seq), max(seq) FROM events;")
        ev_count, min_seq, max_seq = cur.fetchone()
        cur.execute("SELECT count(*) FROM runs;")
        runs_count = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM runs WHERE status = 'running';")
        unclosed = cur.fetchone()[0]
        return {
            "events_count": ev_count,
            "first_seq": min_seq,
            "last_seq": max_seq,
            "runs_count": runs_count,
            "unclosed_runs_count": unclosed,
        }

    def unclosed_runs(self) -> list[dict[str, Any]]:
        """Return all runs currently marked as running."""
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM runs WHERE status = 'running' ORDER BY started_utc ASC;")
        return [dict(r) for r in cur.fetchall()]

    def close(self) -> None:
        """Close read-only connection."""
        self._conn.close()

    def __enter__(self) -> JournalReader:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


def tail(path: Path | str, n: int, *, busy_timeout_ms: int = 5000) -> list[dict[str, Any]]:
    """Return the last n events from journal at path."""
    with JournalReader(path, busy_timeout_ms=busy_timeout_ms) as reader:
        return reader.tail(n)


def events(
    path: Path | str,
    *,
    run_id: str | None = None,
    kind: str | None = None,
    since_seq: int | None = None,
    limit: int | None = None,
    busy_timeout_ms: int = 5000,
) -> list[dict[str, Any]]:
    """Query events from journal at path."""
    with JournalReader(path, busy_timeout_ms=busy_timeout_ms) as reader:
        return reader.events(run_id=run_id, kind=kind, since_seq=since_seq, limit=limit)


def stats(path: Path | str, *, busy_timeout_ms: int = 5000) -> dict[str, Any]:
    """Return journal statistics from path."""
    with JournalReader(path, busy_timeout_ms=busy_timeout_ms) as reader:
        return reader.stats()


def unclosed_runs(path: Path | str, *, busy_timeout_ms: int = 5000) -> list[dict[str, Any]]:
    """Return unclosed runs from journal at path."""
    with JournalReader(path, busy_timeout_ms=busy_timeout_ms) as reader:
        return reader.unclosed_runs()
