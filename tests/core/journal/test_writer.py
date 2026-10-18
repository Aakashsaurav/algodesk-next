"""Tests for journal writer, locking, backpressure and lifecycle (INV-8)."""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

import pytest

from algodesk.api.errors import JournalError
from algodesk.api.scrub import Scrubber
from algodesk.journal.store import verify_journal
from algodesk.journal.writer import Journal, JournalSettings


class StatusEnum(Enum):
    ACTIVE = "active"


def _settings(flush_ms: int = 20, max_buf: int = 100, timeout_ms: int = 1000) -> JournalSettings:
    return JournalSettings(
        flush_interval_ms=flush_ms,
        max_buffer_events=max_buf,
        backpressure_timeout_ms=timeout_ms,
    )


def test_writer_sequence_order(tmp_path: Path) -> None:
    db_path = tmp_path / "seq.db"
    settings = _settings()
    with Journal(db_path, settings=settings) as j:
        j.start_run("paper", "v1", "c1", "r1")
        s1 = j.append("test.event", {"idx": 1})
        s2 = j.append("test.event", {"idx": 2})
        s3 = j.append("test.event", {"idx": 3})
        assert s1 == 1
        assert s2 == 2
        assert s3 == 3
        j.end_run("ok")

    res = verify_journal(db_path)
    assert res.ok is True
    assert res.checked == 3


def test_durable_visible_to_second_connection_at_once(tmp_path: Path) -> None:
    db_path = tmp_path / "durable.db"
    settings = _settings(flush_ms=500)
    with Journal(db_path, settings=settings) as j:
        j.start_run("paper", "v1", "c1", "r1")
        s1 = j.append("event.normal", {"val": 10}, durable=False)
        s2 = j.append("event.durable", {"val": 20}, durable=True)
        assert s1 == 1
        assert s2 == 2

        # Second connection checks immediately without calling flush() on j
        conn2 = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        cur = conn2.cursor()
        cur.execute("SELECT seq, kind FROM events WHERE seq = 2;")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 2
        assert row[1] == "event.durable"
        conn2.close()

        j.end_run("ok")


def test_buffered_visible_after_flush(tmp_path: Path) -> None:
    db_path = tmp_path / "buffered.db"
    settings = _settings(flush_ms=5000)
    with Journal(db_path, settings=settings) as j:
        j.start_run("paper", "v1", "c1", "r1")
        j.append("event.buffered", {"val": 42}, durable=False)

        j.flush()

        conn2 = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        cur = conn2.cursor()
        cur.execute("SELECT seq FROM events WHERE seq = 1;")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 1
        conn2.close()
        j.end_run("ok")


def test_encoder_types_and_rejection(tmp_path: Path) -> None:
    db_path = tmp_path / "encoder.db"
    settings = _settings()
    with Journal(db_path, settings=settings) as j:
        j.start_run("paper", "v1", "c1", "r1")
        payload = {
            "dt": datetime(2026, 10, 18, 12, 0, tzinfo=UTC),
            "d": date(2026, 10, 18),
            "dec": Decimal("123.456"),
            "enum": StatusEnum.ACTIVE,
            "path": Path("/tmp/file.txt"),
            "st": {"b", "a", "c"},
        }
        seq = j.append("test.encoded", payload, durable=True)
        assert seq == 1

        class CustomObj:
            pass

        with pytest.raises(JournalError, match="Unsupported payload object"):
            j.append("test.invalid", {"bad": CustomObj()})

        j.end_run("ok")


def test_bad_kind_rejection(tmp_path: Path) -> None:
    db_path = tmp_path / "kind.db"
    settings = _settings()
    with Journal(db_path, settings=settings) as j:
        j.start_run("paper", "v1", "c1", "r1")
        bad_kinds = ["123startnum", "UpperEvent", "event with space", ""]
        for bad in bad_kinds:
            with pytest.raises(JournalError, match="Invalid event kind"):
                j.append(bad, {"x": 1})
        j.end_run("ok")


def test_scrubbed_payload_and_kind(tmp_path: Path) -> None:
    db_path = tmp_path / "scrub.db"
    scrubber = Scrubber()
    scrubber.register("super-secret-token-12345")
    settings = _settings()
    with Journal(db_path, settings=settings, scrubber=scrubber) as j:
        j.start_run("paper", "v1", "c1", "r1")
        j.append(
            "test.event",
            {"secret_field": "super-secret-token-12345", "token": "abc"},
            durable=True,
        )
        j.end_run("ok")

    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    cur = conn.cursor()
    cur.execute("SELECT payload_json FROM events WHERE seq = 1;")
    payload_json = cur.fetchone()[0]
    conn.close()

    assert "super-secret-token-12345" not in payload_json
    assert "***" in payload_json


def test_second_writer_refused(tmp_path: Path) -> None:
    db_path = tmp_path / "locked.db"
    settings = _settings(flush_ms=50)
    j1 = Journal(db_path, settings=settings)
    try:
        with pytest.raises(JournalError, match="locked by another writer"):
            Journal(db_path, settings=settings)
    finally:
        j1.close()


def test_backpressure_error_with_stalled_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "backpressure.db"
    settings = _settings(flush_ms=1000, max_buf=2, timeout_ms=50)
    with Journal(db_path, settings=settings) as j:
        j.start_run("paper", "v1", "c1", "r1")

        stall_event = threading.Event()
        original_write = j._write_batch

        def stalled_write(batch: list[Any]) -> None:
            stall_event.wait(timeout=2.0)
            original_write(batch)

        monkeypatch.setattr(j, "_write_batch", stalled_write)

        j.append("test.event", {"idx": 1})
        time.sleep(0.05)

        j.append("test.event", {"idx": 2})
        j.append("test.event", {"idx": 3})

        with pytest.raises(JournalError, match="exceeded 2 events and backpressure timeout"):
            j.append("test.event", {"idx": 4})

        stall_event.set()


def test_writer_thread_failure_reraised(tmp_path: Path) -> None:
    db_path = tmp_path / "fail.db"
    settings = _settings()
    with Journal(db_path, settings=settings) as j:
        j.start_run("paper", "v1", "c1", "r1")
        j._writer_exception = sqlite3.OperationalError("Simulated disk I/O failure")
        with pytest.raises(JournalError, match="Simulated disk I/O failure"):
            j.append("test.event", {"x": 1})


def test_idempotent_close(tmp_path: Path) -> None:
    db_path = tmp_path / "close.db"
    settings = _settings()
    j = Journal(db_path, settings=settings)
    j.start_run("paper", "v1", "c1", "r1")
    j.end_run("ok")
    j.close()
    j.close()
    assert j._closed is True


def test_crash_recovery_marks_crashed_on_open(tmp_path: Path) -> None:
    db_path = tmp_path / "crash.db"
    settings = _settings()

    j1 = Journal(db_path, settings=settings)
    run1 = j1.start_run("paper", "v1", "c1", "r1")
    j1.append("event.one", {"a": 1}, durable=True)
    j1._conn.close()
    j1._lock_file.release()
    j1._closed = True

    with Journal(db_path, settings=settings):
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute("SELECT status FROM runs WHERE run_id = ?;", (run1,))
        assert cur.fetchone()[0] == "crashed"

        cur.execute("SELECT kind, payload_json FROM events WHERE seq = 2;")
        row = cur.fetchone()
        assert row[0] == "journal.run_recovered"
        assert run1 in row[1]
        conn.close()

    res = verify_journal(db_path)
    assert res.ok is True
    assert res.checked == 2


def test_benchmark_10000_buffered_events(tmp_path: Path) -> None:
    """Append 10,000 buffered events and measure median and 99th percentile latency."""
    db_path = tmp_path / "bench.db"
    settings = _settings(flush_ms=10, max_buf=20000, timeout_ms=5000)
    latencies: list[float] = []

    with Journal(db_path, settings=settings) as j:
        j.start_run("paper", "bench", "cfg", "reg")
        for i in range(10000):
            t0 = time.perf_counter()
            j.append("bench.event", {"idx": i, "data": "benchmark payload"})
            t1 = time.perf_counter()
            latencies.append((t1 - t0) * 1000.0)
        j.end_run("ok")

    latencies.sort()
    median_ms = latencies[len(latencies) // 2]
    p99_ms = latencies[int(len(latencies) * 0.99)]
    print(f"\nBENCHMARK: 10,000 events | median: {median_ms:.4f} ms | p99: {p99_ms:.4f} ms")

    assert median_ms < 5.0, f"Median append latency too high: {median_ms} ms"
    assert p99_ms < 50.0, f"p99 append latency too high: {p99_ms} ms"

    res = verify_journal(db_path)
    assert res.ok is True
    assert res.checked == 10000


def test_append_without_start_run_raises_journal_error(tmp_path: Path) -> None:
    db_path = tmp_path / "no_run.db"
    settings = _settings()
    with Journal(db_path, settings=settings) as j:
        with pytest.raises(JournalError, match="no active run"):
            j.append("test.event", {"k": "v"})


def test_end_run_without_start_run_raises_journal_error(tmp_path: Path) -> None:
    db_path = tmp_path / "no_end.db"
    settings = _settings()
    with Journal(db_path, settings=settings) as j:
        with pytest.raises(JournalError, match="No active run to end"):
            j.end_run("ok")


def test_end_run_with_invalid_status_raises_journal_error(tmp_path: Path) -> None:
    db_path = tmp_path / "bad_status.db"
    settings = _settings()
    with Journal(db_path, settings=settings) as j:
        j.start_run("paper", "v1", "c1", "r1")
        with pytest.raises(JournalError, match="Invalid run status"):
            j.end_run("complete")


def test_end_run_with_aborted_and_error_status(tmp_path: Path) -> None:
    db_path = tmp_path / "aborted_error.db"
    settings = _settings()
    with Journal(db_path, settings=settings) as j:
        run1 = j.start_run("paper", "v1", "c1", "r1")
        j.end_run("aborted")

        run2 = j.start_run("paper", "v1", "c1", "r1")
        j.end_run("error")

    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    cur = conn.cursor()
    cur.execute("SELECT status FROM runs WHERE run_id = ?;", (run1,))
    assert cur.fetchone()[0] == "aborted"
    cur.execute("SELECT status FROM runs WHERE run_id = ?;", (run2,))
    assert cur.fetchone()[0] == "error"
    conn.close()


def test_append_non_mapping_payload_raises_journal_error(tmp_path: Path) -> None:
    db_path = tmp_path / "non_map.db"
    settings = _settings()
    with Journal(db_path, settings=settings) as j:
        j.start_run("paper", "v1", "c1", "r1")
        with pytest.raises(JournalError, match="Payload must be a Mapping"):
            j.append("test.event", "not a dict")  # type: ignore[arg-type]


def test_encoder_naive_datetime_handling(tmp_path: Path) -> None:
    db_path = tmp_path / "naive_dt.db"
    settings = _settings()
    with Journal(db_path, settings=settings) as j:
        j.start_run("paper", "v1", "c1", "r1")
        dt_naive = datetime(2026, 1, 1, 10, 30, 0)
        j.append("test.dt", {"ts": dt_naive}, durable=True)
        j.end_run("ok")

    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    cur = conn.cursor()
    cur.execute("SELECT payload_json FROM events WHERE seq = 1;")
    payload = cur.fetchone()[0]
    conn.close()
    assert "2026-01-01T10:30:00+00:00" in payload


def test_encoder_timezone_offset_datetime_handling(tmp_path: Path) -> None:
    from datetime import timedelta, timezone

    db_path = tmp_path / "tz_offset.db"
    settings = _settings()
    with Journal(db_path, settings=settings) as j:
        j.start_run("paper", "v1", "c1", "r1")
        # IST is UTC+5:30
        ist = timezone(timedelta(hours=5, minutes=30))
        dt_ist = datetime(2026, 1, 1, 15, 30, 0, tzinfo=ist)
        j.append("test.dt", {"ts": dt_ist}, durable=True)
        j.end_run("ok")

    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    cur = conn.cursor()
    cur.execute("SELECT payload_json FROM events WHERE seq = 1;")
    payload = cur.fetchone()[0]
    conn.close()
    assert "2026-01-01T10:00:00+00:00" in payload


def test_encoder_nested_sets_and_decimals(tmp_path: Path) -> None:
    db_path = tmp_path / "nested_enc.db"
    settings = _settings()
    with Journal(db_path, settings=settings) as j:
        j.start_run("paper", "v1", "c1", "r1")
        payload = {
            "basket": [
                {"prices": [Decimal("10.5"), Decimal("20.25")]},
                {"tags": {"b", "a"}},
            ]
        }
        j.append("test.basket", payload, durable=True)
        j.end_run("ok")

    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    cur = conn.cursor()
    cur.execute("SELECT payload_json FROM events WHERE seq = 1;")
    payload_str = cur.fetchone()[0]
    conn.close()
    assert '"10.5"' in payload_str
    assert '["a","b"]' in payload_str


def test_multiple_runs_in_same_database(tmp_path: Path) -> None:
    db_path = tmp_path / "multi_run.db"
    settings = _settings()
    with Journal(db_path, settings=settings) as j:
        j.start_run("paper", "v1", "c1", "r1")
        s1 = j.append("run1.event", {"r": 1})
        j.end_run("ok")

        j.start_run("paper", "v1", "c1", "r1")
        s2 = j.append("run2.event", {"r": 2})
        j.end_run("ok")

    assert s1 == 1
    assert s2 == 2
    res = verify_journal(db_path)
    assert res.ok is True
    assert res.checked == 2


def test_flush_idempotent_when_queue_empty(tmp_path: Path) -> None:
    db_path = tmp_path / "flush_empty.db"
    settings = _settings()
    with Journal(db_path, settings=settings) as j:
        j.start_run("paper", "v1", "c1", "r1")
        j.flush()
        j.flush()
        j.end_run("ok")


def test_append_durable_with_multiple_events(tmp_path: Path) -> None:
    db_path = tmp_path / "multi_durable.db"
    settings = _settings()
    with Journal(db_path, settings=settings) as j:
        j.start_run("paper", "v1", "c1", "r1")
        for i in range(5):
            j.append("test.event", {"i": i}, durable=True)
        j.end_run("ok")

    res = verify_journal(db_path)
    assert res.ok is True
    assert res.checked == 5


def test_recovery_of_multiple_unclosed_runs(tmp_path: Path) -> None:
    db_path = tmp_path / "multi_crash.db"
    settings = _settings()

    # Create unclosed run 1
    j1 = Journal(db_path, settings=settings)
    r1 = j1.start_run("paper", "v1", "c1", "r1")
    j1._conn.close()
    j1._lock_file.release()
    j1._closed = True

    # Artificially insert a second unclosed run
    conn = sqlite3.connect(str(db_path))
    r2 = "20261018_120000_22222222"
    conn.execute(
        "INSERT INTO runs (run_id, started_utc, mode, code_version, config_hash, "
        "registry_hash, status, ended_utc) "
        "VALUES (?, '2026-10-18T12:00:00Z', 'paper', 'v1', 'c1', 'r1', 'running', NULL);",
        (r2,),
    )
    conn.commit()
    conn.close()

    # Reopen to recover both
    with Journal(db_path, settings=settings):
        pass

    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    cur = conn.cursor()
    cur.execute("SELECT status FROM runs WHERE run_id = ?;", (r1,))
    assert cur.fetchone()[0] == "crashed"
    cur.execute("SELECT status FROM runs WHERE run_id = ?;", (r2,))
    assert cur.fetchone()[0] == "crashed"
    conn.close()

    res = verify_journal(db_path)
    assert res.ok is True
    assert res.checked == 2


def test_custom_clock_injection(tmp_path: Path) -> None:
    fixed_time = datetime(2026, 5, 20, 15, 30, 45, tzinfo=UTC)
    db_path = tmp_path / "clock.db"
    settings = _settings()
    with Journal(db_path, settings=settings, clock=lambda: fixed_time) as j:
        j.start_run("paper", "v1", "c1", "r1")
        j.append("test.event", {"k": "v"}, durable=True)
        j.end_run("ok")

    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    cur = conn.cursor()
    cur.execute("SELECT ts_utc FROM events WHERE seq = 1;")
    ts = cur.fetchone()[0]
    conn.close()
    assert ts == "2026-05-20T15:30:45+00:00"


def test_run_id_format_matches_timestamp_and_hex(tmp_path: Path) -> None:
    import re

    db_path = tmp_path / "run_id_fmt.db"
    settings = _settings()
    with Journal(db_path, settings=settings) as j:
        r_id = j.start_run("paper", "v1", "c1", "r1")
        j.end_run("ok")

    assert re.match(r"^\d{8}_\d{6}_[0-9a-f]{8}$", r_id) is not None


def test_writer_preserves_order_across_multiple_flushes(tmp_path: Path) -> None:
    db_path = tmp_path / "order_flush.db"
    settings = _settings(flush_ms=10)
    with Journal(db_path, settings=settings) as j:
        j.start_run("paper", "v1", "c1", "r1")
        for i in range(1, 20):
            j.append("test.ordered", {"i": i})
            if i % 5 == 0:
                j.flush()
        j.end_run("ok")

    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    cur = conn.cursor()
    cur.execute("SELECT seq FROM events ORDER BY seq ASC;")
    seqs = [r[0] for r in cur.fetchall()]
    conn.close()
    assert seqs == list(range(1, 20))
