"""SQLite persistence store for parameter registry."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from algodesk.api.errors import ParamError

SCHEMA_VERSION = 1


def _check_db_integrity(conn: sqlite3.Connection, db_path: Path) -> None:
    """Verify SQLite database integrity and handle corrupt databases."""
    try:
        cursor = conn.execute("PRAGMA quick_check;")
        rows = cursor.fetchall()
        if not rows or rows[0][0] != "ok":
            raise ParamError(
                what="Database file is corrupt or failed integrity check",
                where=str(db_path),
                fix="Restore params.db from backup or delete and re-initialize",
            )
    except sqlite3.DatabaseError as exc:
        raise ParamError(
            what=f"Database file is corrupt: {exc}",
            where=str(db_path),
            fix="Restore params.db from backup or delete and re-initialize",
        ) from None


def _check_schema_version(conn: sqlite3.Connection, db_path: Path) -> None:
    """Verify or insert schema version, raising if newer than supported."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);")
    cursor = conn.execute("SELECT MAX(version) FROM schema_version;")
    row = cursor.fetchone()
    current_version = row[0] if row and row[0] is not None else None
    if current_version is not None and current_version > SCHEMA_VERSION:
        raise ParamError(
            what=(
                f"Database schema version {current_version} "
                f"is newer than supported {SCHEMA_VERSION}"
            ),
            where=str(db_path),
            fix="Upgrade algodesk or run database migration",
        )
    if current_version is None:
        conn.execute("INSERT INTO schema_version (version) VALUES (?);", (SCHEMA_VERSION,))


def _create_tables(conn: sqlite3.Connection) -> None:
    """Create param_spec, param_value, param_pending, param_history and meta tables."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS param_spec (name TEXT PRIMARY KEY, spec_json TEXT NOT NULL);"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS param_value ("
        "name TEXT NOT NULL, scope TEXT NOT NULL, "
        "value_json TEXT NOT NULL, updated_utc TEXT NOT NULL, "
        "PRIMARY KEY (name, scope));"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS param_pending ("
        "name TEXT NOT NULL, scope TEXT NOT NULL, "
        "value_json TEXT NOT NULL, requested_utc TEXT NOT NULL, apply_at TEXT NOT NULL, "
        "PRIMARY KEY (name, scope));"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS param_history ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL, "
        "name TEXT NOT NULL, scope TEXT NOT NULL, old_json TEXT, "
        "new_json TEXT NOT NULL, by TEXT NOT NULL, channel TEXT NOT NULL, "
        "reason TEXT NOT NULL, confirmed TEXT, status TEXT NOT NULL);"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL);")
    conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('change_seq', 0);")


def _create_triggers(conn: sqlite3.Connection) -> None:
    """Create append-only triggers on param_history."""
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS param_history_no_update
        BEFORE UPDATE ON param_history
        BEGIN
            SELECT RAISE(ABORT, 'param_history is append-only: UPDATE forbidden');
        END;
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS param_history_no_delete
        BEFORE DELETE ON param_history
        BEGIN
            SELECT RAISE(ABORT, 'param_history is append-only: DELETE forbidden');
        END;
        """
    )


def _init_tables(conn: sqlite3.Connection, db_path: Path) -> None:
    """Initialize SQLite tables, triggers, and schema version."""
    _check_db_integrity(conn, db_path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        _check_schema_version(conn, db_path)
        _create_tables(conn)
        _create_triggers(conn)
        conn.execute("COMMIT;")
    except Exception as exc:
        try:
            conn.execute("ROLLBACK;")
        except Exception:
            pass
        if isinstance(exc, ParamError):
            raise
        raise ParamError(
            what=f"Failed to initialize database: {exc}",
            where=str(db_path),
            fix="Ensure database file is valid and writable",
        ) from None


class ParamStore:
    """Persistence manager wrapping SQLite for parameter storage."""

    def __init__(self, db_path: Path | str) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = sqlite3.connect(
                str(self._path),
                timeout=5.0,
                autocommit=True,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode = WAL;")
            self._conn.execute("PRAGMA busy_timeout = 5000;")
            self._conn.execute("PRAGMA foreign_keys = ON;")
        except sqlite3.DatabaseError as exc:
            raise ParamError(
                what=f"Database file is corrupt or invalid: {exc}",
                where=str(self._path),
                fix="Restore params.db from backup or delete and re-initialize",
            ) from None
        _init_tables(self._conn, self._path)

    @contextmanager
    def transaction(self) -> Generator[None]:
        """Context manager executing explicit transaction (BEGIN IMMEDIATE / COMMIT / ROLLBACK)."""

        already_in_tx = self._conn.in_transaction
        if not already_in_tx:
            self._conn.execute("BEGIN IMMEDIATE;")
        try:
            yield
            if not already_in_tx:
                self._conn.execute("COMMIT;")
        except Exception:
            if not already_in_tx:
                try:
                    self._conn.execute("ROLLBACK;")
                except Exception:
                    pass
            raise

    def close(self) -> None:
        """Close SQLite connection."""
        self._conn.close()

    def get_change_seq(self) -> int:
        """Fetch current change sequence number from meta."""
        cursor = self._conn.execute("SELECT value FROM meta WHERE key = 'change_seq';")
        row = cursor.fetchone()
        return int(row[0]) if row else 0

    def bump_change_seq(self) -> int:
        """Increment and return the next change sequence number."""
        cursor = self._conn.execute(
            """
            UPDATE meta SET value = value + 1 WHERE key = 'change_seq'
            RETURNING value;
            """
        )
        row = cursor.fetchone()
        return int(row[0]) if row else 1

    def save_spec_json(self, name: str, spec_json: str) -> None:
        """Insert or update a parameter spec JSON string."""
        with self.transaction():
            self._conn.execute(
                """
                INSERT INTO param_spec (name, spec_json) VALUES (?, ?)
                ON CONFLICT(name) DO UPDATE SET spec_json = excluded.spec_json;
                """,
                (name, spec_json),
            )

    def load_all_specs_json(self) -> dict[str, str]:
        """Return all parameter specs as name -> spec_json dict."""
        cursor = self._conn.execute("SELECT name, spec_json FROM param_spec;")
        return {row["name"]: row["spec_json"] for row in cursor.fetchall()}

    def load_all_values(self) -> dict[tuple[str, str], tuple[Any, str]]:
        """Return map of (name, scope) -> (parsed_value, updated_utc)."""
        cursor = self._conn.execute("SELECT name, scope, value_json, updated_utc FROM param_value;")
        result: dict[tuple[str, str], tuple[Any, str]] = {}
        for row in cursor.fetchall():
            val = json.loads(row["value_json"])
            result[(row["name"], row["scope"])] = (val, row["updated_utc"])
        return result

    def set_value(
        self,
        name: str,
        scope: str,
        value: Any,
        updated_utc: str,
    ) -> int:
        """Set override value in param_value and increment change_seq."""
        val_json = json.dumps(value)
        with self.transaction():
            self._conn.execute(
                """
                INSERT INTO param_value (name, scope, value_json, updated_utc)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name, scope) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_utc = excluded.updated_utc;
                """,
                (name, scope, val_json, updated_utc),
            )
            # Remove any pending value for this (name, scope)
            self._conn.execute(
                "DELETE FROM param_pending WHERE name = ? AND scope = ?;",
                (name, scope),
            )
            return self.bump_change_seq()

    def delete_value(self, name: str, scope: str) -> int:
        """Delete override from param_value and bump change_seq."""
        with self.transaction():
            self._conn.execute(
                "DELETE FROM param_value WHERE name = ? AND scope = ?;",
                (name, scope),
            )
            self._conn.execute(
                "DELETE FROM param_pending WHERE name = ? AND scope = ?;",
                (name, scope),
            )
            return self.bump_change_seq()

    def add_pending(
        self,
        name: str,
        scope: str,
        value: Any,
        requested_utc: str,
        apply_at: str,
    ) -> None:
        """Record a pending parameter change."""
        val_json = json.dumps(value)
        with self.transaction():
            self._conn.execute(
                """
                INSERT INTO param_pending (name, scope, value_json, requested_utc, apply_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(name, scope) DO UPDATE SET
                    value_json = excluded.value_json,
                    requested_utc = excluded.requested_utc,
                    apply_at = excluded.apply_at;
                """,
                (name, scope, val_json, requested_utc, apply_at),
            )

    def load_all_pending(self) -> list[dict[str, Any]]:
        """Return all pending parameter changes."""
        cursor = self._conn.execute(
            "SELECT name, scope, value_json, requested_utc, apply_at FROM param_pending;"
        )
        rows: list[dict[str, Any]] = []
        for r in cursor.fetchall():
            rows.append(
                {
                    "name": r["name"],
                    "scope": r["scope"],
                    "value": json.loads(r["value_json"]),
                    "requested_utc": r["requested_utc"],
                    "apply_at": r["apply_at"],
                }
            )
        return rows

    def clear_pending(self, name: str, scope: str) -> None:
        """Delete pending record for a specific parameter and scope."""
        with self.transaction():
            self._conn.execute(
                "DELETE FROM param_pending WHERE name = ? AND scope = ?;",
                (name, scope),
            )

    def add_history(
        self,
        *,
        ts_utc: str,
        name: str,
        scope: str,
        old_value: Any,
        new_value: Any,
        by: str,
        channel: str,
        reason: str,
        confirmed: str | None,
        status: str,
    ) -> int:
        """Append an entry to param_history and return its id."""
        old_json = json.dumps(old_value) if old_value is not None else None
        new_json = json.dumps(new_value)
        with self.transaction():
            cursor = self._conn.execute(
                """
                INSERT INTO param_history
                (ts_utc, name, scope, old_json, new_json, by, channel, reason, confirmed, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    ts_utc,
                    name,
                    scope,
                    old_json,
                    new_json,
                    by,
                    channel,
                    reason,
                    confirmed,
                    status,
                ),
            )
            return cursor.lastrowid or 0

    def query_history(
        self,
        name: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Query parameter change history in reverse chronological order."""
        query = (
            "SELECT id, ts_utc, name, scope, old_json, new_json, "
            "by, channel, reason, confirmed, status FROM param_history "
        )
        params: list[Any] = []
        if name is not None:
            query += "WHERE name = ? "
            params.append(name)
        query += "ORDER BY id DESC LIMIT ?;"
        params.append(limit)

        cursor = self._conn.execute(query, params)
        records: list[dict[str, Any]] = []
        for row in cursor.fetchall():
            records.append(
                {
                    "id": row["id"],
                    "ts_utc": row["ts_utc"],
                    "name": row["name"],
                    "scope": row["scope"],
                    "old_value": json.loads(row["old_json"])
                    if row["old_json"] is not None
                    else None,
                    "new_value": json.loads(row["new_json"]),
                    "by": row["by"],
                    "channel": row["channel"],
                    "reason": row["reason"],
                    "confirmed": row["confirmed"],
                    "status": row["status"],
                }
            )
        return records
