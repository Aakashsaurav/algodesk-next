"""Tests for journal store, schema, triggers, verification and read APIs (INV-8)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from algodesk.api.errors import JournalError
from algodesk.journal.store import (
    GENESIS_HASH,
    canonical_json,
    compute_event_hash,
    events,
    init_journal_db,
    stats,
    tail,
    unclosed_runs,
    verify_journal,
)


def _populate_sample_journal(db_path: Path) -> str:
    conn = sqlite3.connect(str(db_path))
    init_journal_db(conn, busy_timeout_ms=1000)
    run_id = "20261018_120000_1234abcd"
    with conn:
        conn.execute(
            "INSERT INTO runs (run_id, started_utc, mode, code_version, config_hash, "
            "registry_hash, status, ended_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?);",
            (run_id, "2026-10-18T12:00:00Z", "paper", "v1.0", "cfg123", "reg123", "running", None),
        )

        prev_hash = GENESIS_HASH
        for i in range(1, 4):
            kind = f"event.type_{i}"
            payload = {"seq_num": i, "data": f"payload_{i}"}
            canon = canonical_json(payload)
            ts = f"2026-10-18T12:00:0{i}Z"
            h = compute_event_hash(prev_hash, run_id, ts, kind, canon)
            conn.execute(
                "INSERT INTO events (seq, run_id, ts_utc, mono_ns, kind, payload_json, "
                "prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?);",
                (i, run_id, ts, i * 1000, kind, canon, prev_hash, h),
            )
            prev_hash = h
    conn.close()
    return run_id


def test_schema_and_triggers_block_events_update(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    _populate_sample_journal(db_path)

    conn = sqlite3.connect(str(db_path))
    with pytest.raises(
        sqlite3.IntegrityError, match="events table is append-only: UPDATE forbidden"
    ):
        conn.execute("UPDATE events SET kind = 'hacked' WHERE seq = 1;")
    conn.close()


def test_schema_and_triggers_block_events_delete(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    _populate_sample_journal(db_path)

    conn = sqlite3.connect(str(db_path))
    with pytest.raises(
        sqlite3.IntegrityError, match="events table is append-only: DELETE forbidden"
    ):
        conn.execute("DELETE FROM events WHERE seq = 1;")
    conn.close()


def test_schema_and_triggers_block_runs_delete(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    run_id = _populate_sample_journal(db_path)

    conn = sqlite3.connect(str(db_path))
    with pytest.raises(sqlite3.IntegrityError, match="runs table is append-only: DELETE forbidden"):
        conn.execute("DELETE FROM runs WHERE run_id = ?;", (run_id,))
    conn.close()


def test_schema_and_triggers_limit_runs_update(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    run_id = _populate_sample_journal(db_path)

    conn = sqlite3.connect(str(db_path))
    # Forbidden to change mode or code_version or started_utc
    with pytest.raises(sqlite3.IntegrityError, match="runs may change only status and ended_utc"):
        conn.execute("UPDATE runs SET mode = 'live' WHERE run_id = ?;", (run_id,))

    # Allowed to change status and ended_utc
    conn.execute(
        "UPDATE runs SET status = 'ok', ended_utc = '2026-10-18T12:05:00Z' WHERE run_id = ?;",
        (run_id,),
    )
    conn.commit()
    cur = conn.cursor()
    cur.execute("SELECT status, ended_utc FROM runs WHERE run_id = ?;", (run_id,))
    row = cur.fetchone()
    assert row[0] == "ok"
    assert row[1] == "2026-10-18T12:05:00Z"
    conn.close()


def test_verify_clean_journal(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    _populate_sample_journal(db_path)

    res = verify_journal(db_path)
    assert res.ok is True
    assert res.checked == 3
    assert res.first_bad_seq is None


def test_verify_detects_edited_payload(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    _populate_sample_journal(db_path)

    # Directly edit sqlite payload bypassing trigger (e.g. drop trigger or disable writable_schema)
    conn = sqlite3.connect(str(db_path))
    conn.execute("DROP TRIGGER trg_events_no_update;")
    conn.execute("UPDATE events SET payload_json = '{\"tampered\": true}' WHERE seq = 2;")
    conn.commit()
    conn.close()

    res = verify_journal(db_path)
    assert res.ok is False
    assert res.checked == 1
    assert res.first_bad_seq == 2
    assert "Hash mismatch" in (res.reason or "")


def test_verify_detects_removed_row(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    _populate_sample_journal(db_path)

    conn = sqlite3.connect(str(db_path))
    conn.execute("DROP TRIGGER trg_events_no_delete;")
    conn.execute("DELETE FROM events WHERE seq = 2;")
    conn.commit()
    conn.close()

    res = verify_journal(db_path)
    assert res.ok is False
    assert res.checked == 1
    assert res.first_bad_seq == 3
    assert "Sequence gap or removed row" in (res.reason or "")


def test_verify_detects_broken_link(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    _populate_sample_journal(db_path)

    conn = sqlite3.connect(str(db_path))
    conn.execute("DROP TRIGGER trg_events_no_update;")
    conn.execute("UPDATE events SET prev_hash = 'badhash123' WHERE seq = 2;")
    conn.commit()
    conn.close()

    res = verify_journal(db_path)
    assert res.ok is False
    assert res.checked == 1
    assert res.first_bad_seq == 2
    assert "Broken hash link" in (res.reason or "")


def test_newer_schema_version_raises_journal_error(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    _populate_sample_journal(db_path)

    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE schema_version SET version = 999;")
    conn.commit()
    conn.close()

    with pytest.raises(JournalError) as exc_info:
        verify_journal(db_path)
    assert "newer than supported" in exc_info.value.what
    assert exc_info.value.fix != ""


def test_corrupt_file_raises_journal_error(tmp_path: Path) -> None:
    db_path = tmp_path / "corrupt.db"
    db_path.write_bytes(b"This is not a sqlite database file at all")

    with pytest.raises(JournalError) as exc_info:
        verify_journal(db_path)
    assert "Corrupt or unreadable" in exc_info.value.what
    assert exc_info.value.fix != ""


def test_missing_file_raises_journal_error(tmp_path: Path) -> None:
    db_path = tmp_path / "nonexistent.db"
    with pytest.raises(JournalError) as exc_info:
        verify_journal(db_path)
    assert "does not exist" in exc_info.value.what


def test_read_apis(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    run_id = _populate_sample_journal(db_path)

    # tail
    t = tail(db_path, 2)
    assert len(t) == 2
    assert [row["seq"] for row in t] == [2, 3]

    # events filtering
    ev_all = events(db_path)
    assert len(ev_all) == 3

    ev_filtered = events(db_path, run_id=run_id, kind="event.type_1")
    assert len(ev_filtered) == 1
    assert ev_filtered[0]["seq"] == 1

    ev_since = events(db_path, since_seq=1, limit=1)
    assert len(ev_since) == 1
    assert ev_since[0]["seq"] == 2

    # stats
    st = stats(db_path)
    assert st["events_count"] == 3
    assert st["first_seq"] == 1
    assert st["last_seq"] == 3
    assert st["runs_count"] == 1
    assert st["unclosed_runs_count"] == 1

    # unclosed_runs
    unc = unclosed_runs(db_path)
    assert len(unc) == 1
    assert unc[0]["run_id"] == run_id


def test_verify_empty_journal(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.db"
    conn = sqlite3.connect(str(db_path))
    init_journal_db(conn, busy_timeout_ms=1000)
    conn.close()

    res = verify_journal(db_path)
    assert res.ok is True
    assert res.checked == 0
    assert res.first_bad_seq is None


def test_verify_detects_tampered_ts_utc(tmp_path: Path) -> None:
    db_path = tmp_path / "ts.db"
    _populate_sample_journal(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute("DROP TRIGGER trg_events_no_update;")
    conn.execute("UPDATE events SET ts_utc = '2020-01-01T00:00:00Z' WHERE seq = 1;")
    conn.commit()
    conn.close()

    res = verify_journal(db_path)
    assert res.ok is False
    assert res.first_bad_seq == 1
    assert "Hash mismatch" in (res.reason or "")


def test_verify_detects_tampered_run_id(tmp_path: Path) -> None:
    db_path = tmp_path / "run_id.db"
    _populate_sample_journal(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = OFF;")
    conn.execute("DROP TRIGGER trg_events_no_update;")
    conn.execute("UPDATE events SET run_id = 'different_run' WHERE seq = 1;")
    conn.commit()
    conn.close()

    res = verify_journal(db_path)
    assert res.ok is False
    assert res.first_bad_seq == 1
    assert "Hash mismatch" in (res.reason or "")


def test_verify_detects_tampered_kind(tmp_path: Path) -> None:
    db_path = tmp_path / "kind.db"
    _populate_sample_journal(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute("DROP TRIGGER trg_events_no_update;")
    conn.execute("UPDATE events SET kind = 'fake.kind' WHERE seq = 2;")
    conn.commit()
    conn.close()

    res = verify_journal(db_path)
    assert res.ok is False
    assert res.first_bad_seq == 2


def test_verify_detects_corrupt_payload_json(tmp_path: Path) -> None:
    db_path = tmp_path / "bad_json.db"
    _populate_sample_journal(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute("DROP TRIGGER trg_events_no_update;")
    conn.execute("UPDATE events SET payload_json = '{invalid' WHERE seq = 1;")
    conn.commit()
    conn.close()

    res = verify_journal(db_path)
    assert res.ok is False
    assert res.first_bad_seq == 1
    assert "Invalid payload JSON" in (res.reason or "")


def test_verify_detects_bad_genesis_prev_hash(tmp_path: Path) -> None:
    db_path = tmp_path / "bad_gen.db"
    _populate_sample_journal(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute("DROP TRIGGER trg_events_no_update;")
    conn.execute("UPDATE events SET prev_hash = '1' WHERE seq = 1;")
    conn.commit()
    conn.close()

    res = verify_journal(db_path)
    assert res.ok is False
    assert res.first_bad_seq == 1
    assert "Broken hash link" in (res.reason or "")


def test_tail_zero_and_large_n(tmp_path: Path) -> None:
    db_path = tmp_path / "tail.db"
    _populate_sample_journal(db_path)

    assert tail(db_path, 0) == []
    assert len(tail(db_path, 100)) == 3


def test_events_filtering_options(tmp_path: Path) -> None:
    db_path = tmp_path / "filter.db"
    _populate_sample_journal(db_path)

    by_kind = events(db_path, kind="event.type_2")
    assert len(by_kind) == 1
    assert by_kind[0]["seq"] == 2

    by_limit = events(db_path, limit=2)
    assert len(by_limit) == 2

    since_2 = events(db_path, since_seq=2)
    assert len(since_2) == 1
    assert since_2[0]["seq"] == 3


def test_stats_empty_journal(tmp_path: Path) -> None:
    db_path = tmp_path / "stats_empty.db"
    conn = sqlite3.connect(str(db_path))
    init_journal_db(conn, busy_timeout_ms=1000)
    conn.close()

    st = stats(db_path)
    assert st["events_count"] == 0
    assert st["first_seq"] is None
    assert st["last_seq"] is None
    assert st["runs_count"] == 0
    assert st["unclosed_runs_count"] == 0


def test_unclosed_runs_empty_when_all_closed(tmp_path: Path) -> None:
    db_path = tmp_path / "all_closed.db"
    run_id = _populate_sample_journal(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE runs SET status = 'ok', ended_utc = '2026-10-18T12:10:00Z' WHERE run_id = ?;",
        (run_id,),
    )
    conn.commit()
    conn.close()

    assert unclosed_runs(db_path) == []


def test_journal_reader_context_manager(tmp_path: Path) -> None:
    from algodesk.journal.store import JournalReader

    db_path = tmp_path / "reader.db"
    _populate_sample_journal(db_path)
    with JournalReader(db_path) as reader:
        assert len(reader.tail(2)) == 2
        assert reader.stats()["events_count"] == 3


def test_schema_triggers_runs_update_same_fields_succeeds(tmp_path: Path) -> None:
    db_path = tmp_path / "same_fields.db"
    run_id = _populate_sample_journal(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE runs SET status = 'error', ended_utc = '2026-10-18T12:05:00Z' WHERE run_id = ?;",
        (run_id,),
    )
    conn.commit()
    conn.close()


def test_schema_triggers_runs_update_started_utc_fails(tmp_path: Path) -> None:
    db_path = tmp_path / "mod_start.db"
    run_id = _populate_sample_journal(db_path)
    conn = sqlite3.connect(str(db_path))
    with pytest.raises(sqlite3.IntegrityError, match="runs may change only status and ended_utc"):
        conn.execute(
            "UPDATE runs SET started_utc = '2026-01-01T00:00:00Z' WHERE run_id = ?;",
            (run_id,),
        )
    conn.close()


def test_schema_triggers_runs_update_config_hash_fails(tmp_path: Path) -> None:
    db_path = tmp_path / "mod_cfg.db"
    run_id = _populate_sample_journal(db_path)
    conn = sqlite3.connect(str(db_path))
    with pytest.raises(sqlite3.IntegrityError, match="runs may change only status and ended_utc"):
        conn.execute("UPDATE runs SET config_hash = 'different_cfg' WHERE run_id = ?;", (run_id,))
    conn.close()


def test_canonical_json_nested_sorts_keys() -> None:
    d = {"z": 1, "a": {"k2": "v2", "k1": "v1"}}
    assert canonical_json(d) == '{"a":{"k1":"v1","k2":"v2"},"z":1}'
