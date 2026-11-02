"""CLI command verification tests for M1-9 IST presentation."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from algodesk.api.market import Bar, InstrumentId
from algodesk.cli.app import app
from algodesk.journal.writer import Journal, JournalSettings


def test_cli_journal_tail_displays_column_ts_ist(tmp_project: Path) -> None:
    """algodesk journal tail outputs TS_IST column header and timestamps with IST suffix."""
    runner = CliRunner()
    db_path = tmp_project / "data" / "state" / "journal.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    settings = JournalSettings(
        flush_interval_ms=10, max_buffer_events=100, backpressure_timeout_ms=1000
    )
    with Journal(db_path, settings=settings) as j:
        j.start_run("paper", "v1", "c1", "r1")
        j.append("test.event", {"data": 123})
        j.end_run("ok")

    res = runner.invoke(app, ["--root", str(tmp_project), "journal", "tail"])
    assert res.exit_code == 0
    assert "TS_IST" in res.output
    assert "TS_UTC" not in res.output
    assert "IST" in res.output
    assert "+00:00" not in res.output


def test_cli_journal_runs_displays_column_started_ist(tmp_project: Path) -> None:
    """algodesk journal runs outputs STARTED_IST and ENDED_IST column headers."""
    runner = CliRunner()
    db_path = tmp_project / "data" / "state" / "journal.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    settings = JournalSettings(
        flush_interval_ms=10, max_buffer_events=100, backpressure_timeout_ms=1000
    )
    with Journal(db_path, settings=settings) as j:
        j.start_run("paper", "v1", "c1", "r1")
        j.end_run("ok")

    res = runner.invoke(app, ["--root", str(tmp_project), "journal", "runs"])
    assert res.exit_code == 0
    assert "STARTED_IST" in res.output
    assert "STARTED_UTC" not in res.output
    assert "IST" in res.output


def test_cli_results_list_includes_start_ist_column(tmp_project: Path) -> None:
    """algodesk results list outputs START_IST column displaying IST start time."""
    runner = CliRunner()
    results_dir = tmp_project / "results"
    run_dir = results_dir / "demo_paper_test_20261102_055142_abc"
    run_dir.mkdir(parents=True, exist_ok=True)

    metrics = {
        "date": "2026-11-02",
        "mode": "paper",
        "first_tick_ist": "2026-11-02 11:21:42 IST",
        "trades": 0,
        "net_pnl": 0.0,
    }
    (run_dir / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    (run_dir / "SUMMARY.md").write_text("# Run Summary\nVerdict: PASS\n", encoding="utf-8")

    res = runner.invoke(app, ["--root", str(tmp_project), "results", "list"])
    assert res.exit_code == 0
    assert "START_IST" in res.output
    assert "2026-11-02 11:21:42 IST" in res.output


def test_cli_spike_history_daily_candles_display_ist_date(tmp_project: Path) -> None:
    """Spike history daily candles render trading dates in IST, never +00:00."""
    runner = CliRunner()
    candle_1830z = Bar(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        timeframe="1d",
        start_ts=datetime(2026, 10, 25, 18, 30, 0, tzinfo=UTC),
        end_ts=datetime(2026, 10, 26, 18, 30, 0, tzinfo=UTC),
        open=2500.0,
        high=2550.0,
        low=2490.0,
        close=2540.0,
        volume=100000,
    )

    class MockBroker:
        manifest = MagicMock(name="upstox")

        def history(self, *args: Any, **kwargs: Any) -> Any:
            return [candle_1830z]

    with patch("algodesk.registry.PluginRegistry.create", return_value=MockBroker()):
        res = runner.invoke(
            app,
            [
                "--root",
                str(tmp_project),
                "spike",
                "upstox-history",
                "--symbol",
                "RELIANCE",
            ],
        )
        assert res.exit_code == 0
        assert "2026-10-26" in res.output
        assert "+00:00" not in res.output


def test_cli_data_gaps_header_displays_day_ist(tmp_project: Path) -> None:
    """algodesk data gaps table header displays DAY (IST)."""
    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "data",
            "gaps",
            "NSE:EQ:INFY",
            "--from",
            "2026-10-20",
            "--to",
            "2026-10-25",
        ],
    )
    assert res.exit_code == 0
    assert "DAY (IST)" in res.output or "No gaps found" in res.output
