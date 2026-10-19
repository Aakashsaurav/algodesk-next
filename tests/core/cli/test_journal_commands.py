"""Tests for algodesk journal CLI commands (verify, tail, runs)."""

from __future__ import annotations

import sqlite3
from typing import Any

from typer.testing import CliRunner

from algodesk.cli.app import app
from algodesk.journal.writer import Journal, JournalSettings


def test_journal_verify_absent_db(runner: CliRunner, make_project: Any) -> None:
    """journal verify reports database does not exist when absent."""
    proj = make_project("j_absent")
    res = runner.invoke(app, ["--root", str(proj), "journal", "verify"])
    assert res.exit_code == 0
    assert "Journal database does not exist" in res.output


def test_journal_verify_clean_and_tampered(runner: CliRunner, make_project: Any) -> None:
    """journal verify confirms clean hash chain and fails on tampered records."""
    proj = make_project("j_verify")
    db_path = proj / "data" / "state" / "journal.db"
    db_path.parent.mkdir(parents=True)

    settings = JournalSettings(
        flush_interval_ms=10, max_buffer_events=100, backpressure_timeout_ms=1000
    )
    with Journal(db_path, settings=settings) as j:
        j.start_run("paper", "v1", "c1", "r1")
        j.append("market.tick", {"symbol": "INFY", "price": 1500.0})
        j.end_run("ok")

    # Clean verification
    res_clean = runner.invoke(app, ["--root", str(proj), "journal", "verify"])
    assert res_clean.exit_code == 0
    assert "JOURNAL OK: verified" in res_clean.output

    # Tamper with row payload
    conn = sqlite3.connect(db_path)
    conn.execute("DROP TRIGGER trg_events_no_update;")
    conn.execute("UPDATE events SET payload_json = '{\"tampered\": true}' WHERE seq = 1;")
    conn.commit()
    conn.close()

    # Tampered verification must exit 1
    res_tampered = runner.invoke(app, ["--root", str(proj), "journal", "verify"])
    assert res_tampered.exit_code == 1
    assert "JOURNAL CORRUPT:" in res_tampered.output


def test_journal_tail_and_runs(runner: CliRunner, make_project: Any) -> None:
    """journal tail and journal runs display formatted table output."""
    proj = make_project("j_tail_runs")
    db_path = proj / "data" / "state" / "journal.db"
    db_path.parent.mkdir(parents=True)

    # Empty / absent state
    res_empty_tail = runner.invoke(app, ["--root", str(proj), "journal", "tail"])
    assert "Journal database does not exist" in res_empty_tail.output

    res_empty_runs = runner.invoke(app, ["--root", str(proj), "journal", "runs"])
    assert "Journal database does not exist" in res_empty_runs.output

    # Write events and runs
    settings = JournalSettings(
        flush_interval_ms=10, max_buffer_events=100, backpressure_timeout_ms=1000
    )
    with Journal(db_path, settings=settings) as j:
        run_id = j.start_run("paper", "v1", "c1", "r1")
        j.append("event.one", {"idx": 1})
        j.append("event.two", {"idx": 2})
        j.end_run("ok")

    res_tail = runner.invoke(app, ["--root", str(proj), "journal", "tail", "-n", "2"])
    assert res_tail.exit_code == 0
    assert "event.one" in res_tail.output
    assert "event.two" in res_tail.output

    res_runs = runner.invoke(app, ["--root", str(proj), "journal", "runs"])
    assert res_runs.exit_code == 0
    assert run_id in res_runs.output
    assert "paper" in res_runs.output
