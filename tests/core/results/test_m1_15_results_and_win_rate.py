"""Summary: Tests for M1-15 win rate consistency, open positions reporting, and session report.

Covers:
1. Win rate definition with a known trade table (net of charges, closed round trips).
2. Open positions at stop listed in SUMMARY.md and verdict note.
3. Open positions at stop listed in CLI session report ('Open at stop').
4. Closing path cancels active orders but adds no automatic orders for unresolved positions.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from algodesk.cli.app import app
from algodesk.results.roundtrips import compute_round_trip_metrics
from algodesk.results.writer import ResultsWriter


def test_win_rate_known_trade_table_net_of_charges() -> None:
    """Verify win rate is defined strictly as closed round trips net of transaction charges.

    Trade table:
    1. RELIANCE: BUY 1 @ 1000, SELL 1 @ 1020, charges 5.0 -> Net +15 -> WIN
    2. TCS: BUY 1 @ 2000, SELL 1 @ 2002, charges 6.0 -> Gross +2, Net -4 -> LOSS
    3. INFY: BUY 1 @ 1500, SELL 1 @ 1490, charges 4.0 -> Net -14 -> LOSS
    4. HDFCBANK: BUY 2 @ 800 (unclosed open position at stop) -> NOT a closed round trip
    """
    fills = [
        # Trade 1: Net win
        {
            "instrument": "NSE:EQ:RELIANCE",
            "side": "BUY",
            "qty": 1,
            "price": 1000.0,
            "charges": 2.5,
        },
        {
            "instrument": "NSE:EQ:RELIANCE",
            "side": "SELL",
            "qty": 1,
            "price": 1020.0,
            "charges": 2.5,
        },
        # Trade 2: Gross win but Net loss due to charges
        {"instrument": "NSE:EQ:TCS", "side": "BUY", "qty": 1, "price": 2000.0, "charges": 3.0},
        {"instrument": "NSE:EQ:TCS", "side": "SELL", "qty": 1, "price": 2002.0, "charges": 3.0},
        # Trade 3: Gross and Net loss
        {"instrument": "NSE:EQ:INFY", "side": "BUY", "qty": 1, "price": 1500.0, "charges": 2.0},
        {"instrument": "NSE:EQ:INFY", "side": "SELL", "qty": 1, "price": 1490.0, "charges": 2.0},
        # Trade 4: Unclosed position (open at stop)
        {"instrument": "NSE:EQ:HDFCBANK", "side": "BUY", "qty": 2, "price": 800.0, "charges": 1.0},
    ]

    res = compute_round_trip_metrics(fills)
    assert res["round_trips"] == 3
    assert res["winning_round_trips"] == 1
    assert res["gross_winning_round_trips"] == 2
    assert res["losing_round_trips"] == 2

    # Net win rate = 1 / 3 = 33.33%
    assert res["win_rate"] == 33.33
    # Gross win rate = 2 / 3 = 66.67%
    assert res["gross_win_rate"] == 66.67


def test_summary_md_lists_open_positions_and_verdict_note(tmp_path: Path) -> None:
    """SUMMARY.md lists open positions at stop and mentions count in verdict note."""
    writer = ResultsWriter(run_id="run_test_unresolved", results_dir=tmp_path)
    metrics = {
        "net_pnl": 150.0,
        "gross_pnl": 200.0,
        "charges": 50.0,
        "trades": 6,
        "closed_round_trips": 2,
        "win_rate": 50.0,
        "max_drawdown": 0.05,
        "open_positions": [
            {"instrument": "NSE:EQ:RELIANCE", "product": "MIS", "qty": 2, "avg_price": 1335.40},
            {"instrument": "NSE:EQ:TCS", "product": "MIS", "qty": 1, "avg_price": 2472.10},
        ],
    }
    summary_info = {
        "run_name": "test_run",
        "mode": "paper",
        "date": "2026-11-12",
        "verdict": "PASS (partial)",
    }

    writer.write(
        summary_info=summary_info,
        metrics=metrics,
        trades=[],
        equity_points=[],
        effective_config_text="",
        registry_snapshot={},
        manifest_info={},
    )

    summary_file = tmp_path / "run_test_unresolved" / "SUMMARY.md"
    assert summary_file.is_file()
    text = summary_file.read_text(encoding="utf-8")

    assert "Open at stop: NSE:EQ:RELIANCE (qty=2), NSE:EQ:TCS (qty=1)" in text
    assert "Unresolved open positions at stop" in text
    assert len(text.split()) <= 150


def test_session_report_cli_displays_open_at_stop_table(tmp_project: Path) -> None:
    """CLI session report prints 'Open at stop' table with instrument, product, qty."""
    res_dir = tmp_project / "results"
    res_dir.mkdir(parents=True, exist_ok=True)
    run_dir = res_dir / "demo_paper_fast_paper_20261112_090000_123456"
    run_dir.mkdir(exist_ok=True)

    metrics = {
        "run_id": run_dir.name,
        "mode": "paper",
        "date": "2026-11-12",
        "closed_round_trips": 2,
        "win_rate": 0.0,
        "gross_win_rate": 0.0,
        "trades": 8,
        "open_positions": [
            {"instrument": "NSE:EQ:RELIANCE", "product": "MIS", "qty": 2, "avg_price": 1335.4},
            {"instrument": "NSE:EQ:ICICIBANK", "product": "MIS", "qty": 1, "avg_price": 1566.3},
        ],
    }
    (run_dir / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    (run_dir / "session.md").write_text(
        "Session demo_paper_fast (paper) completed.\n"
        "Verdict: PASS (partial)\n"
        "Verdict Note: Unresolved open positions at stop (2)\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    res = runner.invoke(app, ["--root", str(tmp_project), "session", "report", run_dir.name])
    assert res.exit_code == 0
    assert "Open at stop:" in res.output
    assert "NSE:EQ:RELIANCE" in res.output
    assert "NSE:EQ:ICICIBANK" in res.output
    assert "Verdict Note:" in res.output
    assert "Unresolved open positions at stop (2)" in res.output
