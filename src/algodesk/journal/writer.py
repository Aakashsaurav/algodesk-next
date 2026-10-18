"""Append-only journal writer with lock file, buffering and backpressure.

INV-8: Single writer, write-ahead durability, tamper-evident audit trail.
"""

from __future__ import annotations

import collections
import re
import secrets
import sqlite3
import sys
import threading
import time
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from algodesk.api.errors import JournalError
from algodesk.api.scrub import Scrubber
from algodesk.journal.store import (
    GENESIS_HASH,
    canonical_json,
    compute_event_hash,
    init_journal_db,
)

_KIND_REGEX = re.compile(r"^[a-z][a-z0-9_.]*$")


def utcnow() -> datetime:
    """Return timezone-aware UTC datetime."""
    return datetime.now(UTC)


@dataclass(frozen=True)
class JournalSettings:
    """Settings for Journal writer. All required, no defaults."""

    flush_interval_ms: int
    max_buffer_events: int
    backpressure_timeout_ms: int


def encode_payload(val: Any) -> Any:
    """Encode payload value into JSON-compatible primitives."""
    if isinstance(val, (str, int, float, bool)) or val is None:
        return val
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=UTC).isoformat()
        return val.astimezone(UTC).isoformat()
    if isinstance(val, date):
        return val.isoformat()
    if isinstance(val, Decimal):
        return str(val)
    if isinstance(val, Enum):
        return val.value
    if isinstance(val, Path):
        return str(val)
    if isinstance(val, set):
        return sorted([encode_payload(x) for x in val])
    if isinstance(val, Mapping):
        return {str(k): encode_payload(v) for k, v in val.items()}
    if isinstance(val, (list, tuple)):
        return [encode_payload(x) for x in val]
    raise JournalError(
        what=f"Unsupported payload object of type '{type(val).__name__}'",
        where="journal.encoder",
        fix=(
            "Use supported payload types: datetime, date, Decimal, Enum, Path, "
            "set, Mapping, list, tuple or primitives"
        ),
    )


class WriterLock:
    """Single-writer lock file using fcntl (POSIX) or msvcrt (Windows)."""

    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path
        self._file: Any = None

    def acquire(self) -> None:
        try:
            self._file = open(self.lock_path, "w")
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            if self._file:
                try:
                    self._file.close()
                except Exception:
                    pass
                self._file = None
            raise JournalError(
                what=f"Journal database is locked by another writer process: {exc}",
                where=str(self.lock_path),
                fix="Ensure only one Journal writer process runs against this database",
            ) from exc

    def release(self) -> None:
        if self._file is not None:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    self._file.seek(0)
                    msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
                self._file.close()
            except Exception:
                pass
            finally:
                self._file = None


@dataclass
class _PendingEvent:
    seq: int
    run_id: str
    ts_utc: str
    mono_ns: int
    kind: str
    payload_json: str
    prev_hash: str
    hash: str
    durable_event: threading.Event | None = None


class Journal:
    """Single-writer append-only journal implementing api.JournalWriter."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        settings: JournalSettings,
        scrubber: Scrubber | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.db_path = Path(db_path).resolve()
        self.settings = settings
        self.scrubber = scrubber
        self._clock = clock

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file = WriterLock(self.db_path.with_name(f"{self.db_path.name}.lock"))
        self._lock_file.acquire()

        self._db_lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self.db_path),
            timeout=self.settings.backpressure_timeout_ms / 1000.0,
            check_same_thread=False,
        )
        with self._db_lock:
            init_journal_db(self._conn, self.settings.backpressure_timeout_ms)

        self._cond = threading.Condition()

        self._queue: collections.deque[_PendingEvent] = collections.deque()
        self._writer_exception: Exception | None = None
        self._stopped = False
        self._closed = False
        self._last_seq: int = 0
        self._last_hash: str = GENESIS_HASH
        self._committed_seq: int = 0
        self._current_run_id: str | None = None

        self._init_state_and_recover()
        self._worker_thread = threading.Thread(
            target=self._writer_loop,
            name="JournalWriterThread",
            daemon=True,
        )
        self._worker_thread.start()

    def _init_state_and_recover(self) -> None:
        cur = self._conn.cursor()
        cur.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1;")
        row = cur.fetchone()
        if row:
            self._last_seq = int(row[0])
            self._last_hash = str(row[1])
            self._committed_seq = int(row[0])
        else:
            self._last_seq = 0
            self._last_hash = GENESIS_HASH
            self._committed_seq = 0

        # Mark unclosed runs crashed and append journal.run_recovered event
        cur.execute("SELECT run_id FROM runs WHERE status = 'running';")
        unclosed = [r[0] for r in cur.fetchall()]
        now_ts = self._clock().astimezone(UTC).isoformat()
        for r_id in unclosed:
            cur.execute(
                "UPDATE runs SET status = 'crashed', ended_utc = ? WHERE run_id = ?;",
                (now_ts, r_id),
            )
            self._last_seq += 1
            payload = {"recovered_run_id": r_id, "marked_status": "crashed"}
            canon = canonical_json(payload)
            kind = "journal.run_recovered"
            h = compute_event_hash(self._last_hash, r_id, now_ts, kind, canon)
            cur.execute(
                "INSERT INTO events (seq, run_id, ts_utc, mono_ns, kind, payload_json, "
                "prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?);",
                (
                    self._last_seq,
                    r_id,
                    now_ts,
                    time.monotonic_ns(),
                    kind,
                    canon,
                    self._last_hash,
                    h,
                ),
            )

            self._last_hash = h
            self._committed_seq = self._last_seq
        self._conn.commit()

    def start_run(
        self,
        mode: str,
        code_version: str,
        config_hash: str,
        registry_hash: str,
    ) -> str:
        """Start a new run and record run record in runs table."""
        self._check_writer_error()
        now = self._clock().astimezone(UTC)
        ts_part = now.strftime("%Y%m%d_%H%M%S")
        rand_part = secrets.token_hex(4)
        run_id = f"{ts_part}_{rand_part}"
        started_utc = now.isoformat()

        with self._cond:
            with self._db_lock:
                self._conn.execute(
                    "INSERT INTO runs (run_id, started_utc, mode, code_version, config_hash, "
                    "registry_hash, status, ended_utc) VALUES (?, ?, ?, ?, ?, ?, 'running', NULL);",
                    (run_id, started_utc, mode, code_version, config_hash, registry_hash),
                )
                self._conn.commit()
            self._current_run_id = run_id
        return run_id

    def end_run(self, status: str) -> None:
        """End current run with status ('ok', 'error', 'aborted')."""
        if status not in ("ok", "error", "aborted"):
            raise JournalError(
                what=f"Invalid run status '{status}': must be 'ok', 'error', or 'aborted'",
                where="journal.end_run",
                fix="Pass one of 'ok', 'error', or 'aborted'",
            )
        self.flush()
        with self._cond:
            if self._current_run_id is None:
                raise JournalError(
                    what="No active run to end",
                    where="journal.end_run",
                    fix="Call start_run() before end_run()",
                )
            now_ts = self._clock().astimezone(UTC).isoformat()
            with self._db_lock:
                self._conn.execute(
                    "UPDATE runs SET status = ?, ended_utc = ? WHERE run_id = ?;",
                    (status, now_ts, self._current_run_id),
                )
                self._conn.commit()
            self._current_run_id = None

    def _check_writer_error(self) -> None:
        if self._writer_exception is not None:
            exc = self._writer_exception
            self._writer_exception = None
            if isinstance(exc, JournalError):
                raise exc
            raise JournalError(
                what=f"Journal background writer encountered error: {exc}",
                where="journal.writer_thread",
                fix="Inspect disk space, SQLite database integrity or logs",
            ) from exc

    def append(
        self,
        kind: str,
        payload: Mapping[str, object],
        *,
        durable: bool = False,
    ) -> int:
        """Append an event to the journal, returning the sequence number immediately."""
        self._check_writer_error()
        if not isinstance(kind, str) or not _KIND_REGEX.match(kind):
            raise JournalError(
                what=f"Invalid event kind '{kind}': must match ^[a-z][a-z0-9_.]*$",
                where="journal.append",
                fix="Event kind must start with a lowercase letter and contain only [a-z0-9_.]",
            )
        if not isinstance(payload, Mapping):
            raise JournalError(
                what=f"Payload must be a Mapping, got '{type(payload).__name__}'",
                where="journal.append",
                fix="Pass a dict or Mapping as event payload",
            )

        encoded = encode_payload(payload)
        if self.scrubber is not None:
            kind = self.scrubber.scrub(kind)
            encoded = self.scrubber.scrub_obj(encoded)
        canon_json = canonical_json(encoded)

        now_dt = self._clock().astimezone(UTC)
        ts_utc = now_dt.isoformat()
        mono_ns = time.monotonic_ns()
        durable_event = threading.Event() if durable else None

        with self._cond:
            if self._current_run_id is None:
                raise JournalError(
                    what="Cannot append event: no active run",
                    where="journal.append",
                    fix="Call start_run() before appending events",
                )
            self._apply_backpressure_under_lock()
            seq: int = self._last_seq + 1
            h = compute_event_hash(
                self._last_hash,
                self._current_run_id,
                ts_utc,
                kind,
                canon_json,
            )
            ev = _PendingEvent(
                seq=seq,
                run_id=self._current_run_id,
                ts_utc=ts_utc,
                mono_ns=mono_ns,
                kind=kind,
                payload_json=canon_json,
                prev_hash=self._last_hash,
                hash=h,
                durable_event=durable_event,
            )
            self._queue.append(ev)
            self._last_seq = seq
            self._last_hash = h
            self._cond.notify_all()

        if durable and durable_event is not None:
            durable_event.wait()
            self._check_writer_error()
        return seq

    def _apply_backpressure_under_lock(self) -> None:
        timeout = self.settings.backpressure_timeout_ms / 1000.0
        deadline = time.monotonic() + timeout
        while len(self._queue) >= self.settings.max_buffer_events:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                warnings.warn(
                    f"Journal buffer full ({len(self._queue)} events); backpressure timeout "
                    f"of {self.settings.backpressure_timeout_ms}ms exceeded",
                    stacklevel=2,
                )
                raise JournalError(
                    what=(
                        f"Journal buffer exceeded {self.settings.max_buffer_events} events "
                        f"and backpressure timeout of {self.settings.backpressure_timeout_ms}ms"
                    ),
                    where="journal.append",
                    fix="Increase max_buffer_events or check disk write performance",
                )
            self._cond.wait(remaining)

    def _writer_loop(self) -> None:
        while True:
            batch: list[_PendingEvent] = []
            with self._cond:
                while not self._stopped and not self._queue:
                    self._cond.wait(self.settings.flush_interval_ms / 1000.0)
                if not self._queue:
                    if self._stopped:
                        break
                    continue
                while self._queue:
                    batch.append(self._queue.popleft())
                self._cond.notify_all()

            if batch:
                self._write_batch(batch)

    def _write_batch(self, batch: list[_PendingEvent]) -> None:
        has_durable = any(item.durable_event is not None for item in batch)
        try:
            params = [
                (
                    e.seq,
                    e.run_id,
                    e.ts_utc,
                    e.mono_ns,
                    e.kind,
                    e.payload_json,
                    e.prev_hash,
                    e.hash,
                )
                for e in batch
            ]
            with self._db_lock:
                if has_durable:
                    self._conn.execute("PRAGMA synchronous = FULL;")
                self._conn.executemany(
                    "INSERT INTO events (seq, run_id, ts_utc, mono_ns, kind, payload_json, "
                    "prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?);",
                    params,
                )
                self._conn.commit()
                if has_durable:
                    self._conn.execute("PRAGMA synchronous = NORMAL;")

            with self._cond:
                self._committed_seq = batch[-1].seq
                self._cond.notify_all()
        except Exception as exc:
            self._writer_exception = exc
        finally:
            for item in batch:
                if item.durable_event is not None:
                    item.durable_event.set()

    def flush(self) -> None:
        """Block until all queued events are committed to disk."""
        self._check_writer_error()
        with self._cond:
            target = self._last_seq
            self._cond.notify_all()
            while self._committed_seq < target and not self._stopped:
                self._check_writer_error()
                self._cond.wait(self.settings.flush_interval_ms / 1000.0)
        self._check_writer_error()

    def close(self) -> None:
        """Flush pending events, stop background thread, and release lock."""
        if self._closed:
            return
        try:
            self.flush()
        except Exception:
            pass

        with self._cond:
            self._stopped = True
            self._cond.notify_all()

        if self._worker_thread.is_alive():
            self._worker_thread.join(timeout=2.0)

        with self._db_lock:
            try:
                self._conn.close()
            except Exception:
                pass
        self._lock_file.release()
        self._closed = True

    def __enter__(self) -> Journal:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
