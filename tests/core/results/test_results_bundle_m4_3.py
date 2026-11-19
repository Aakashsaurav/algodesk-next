"""Tests for Step 6 & 7: Results bundle artifacts.

Covers equity_daily, round_trips, orders, metrics.json, and readiness.json.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from algodesk.results.writer import ResultsWriter


def test_results_bundle_writes_equity_daily_csv(tmp_path: Path) -> None:
    """In backtest mode, writer creates equity_daily.csv with one row per session close."""
    run_dir = tmp_path / "results"
    writer = ResultsWriter(run_id="test_bt_bundle", results_dir=run_dir)

    equity_points = [
        {
            "ts": "2025-01-01T10:00:00+00:00",
            "ts_ist": "2025-01-01 15:30:00 IST",
            "equity": 500000.0,
            "cash": 500000.0,
            "unrealized": 0.0,
            "realized": 0.0,
            "drawdown": 0.0,
        },
        {
            "ts": "2025-01-02T10:00:00+00:00",
            "ts_ist": "2025-01-02 15:30:00 IST",
            "equity": 505000.0,
            "cash": 400000.0,
            "unrealized": 5000.0,
            "realized": 0.0,
            "drawdown": 0.0,
        },
        {
            "ts": "2025-01-03T10:00:00+00:00",
            "ts_ist": "2025-01-03 15:30:00 IST",
            "equity": 498000.0,
            "cash": 498000.0,
            "unrealized": 0.0,
            "realized": -2000.0,
            "drawdown": 1.3861,
        },
    ]

    out_path = writer.write(
        summary_info={"mode": "backtest", "run_name": "test_bt"},
        metrics={"net_pnl": -2000.0, "trades": 2},
        trades=[],
        equity_points=equity_points,
        effective_config_text="strategy = 'sma_cross'\n",
        registry_snapshot={"hash": "core", "params": {}},
        manifest_info={"mode": "backtest", "code_version": "0.1.0"},
    )

    eq_daily_file = out_path / "equity_daily.csv"
    assert eq_daily_file.is_file()

    with open(eq_daily_file, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 3
    assert rows[0]["date"] == "2025-01-01"
    assert float(rows[0]["equity"]) == 500000.0
    assert float(rows[0]["daily_return"]) == 0.0

    assert rows[1]["date"] == "2025-01-02"
    assert float(rows[1]["equity"]) == 505000.0
    assert float(rows[1]["daily_return"]) == 0.01  # (505000 - 500000) / 500000 = 0.01

    assert rows[2]["date"] == "2025-01-03"
    assert float(rows[2]["equity"]) == 498000.0


def test_results_bundle_writes_round_trips_csv(tmp_path: Path) -> None:
    """In backtest mode, writer produces round_trips.csv with entry/exit IST dates,
    qty, prices, net P&L.
    """
    run_dir = tmp_path / "results"
    writer = ResultsWriter(run_id="test_bt_rt", results_dir=run_dir)

    trades = [
        {
            "trade_id": "T1",
            "fill_id": "F1",
            "client_ref": "ORD-1",
            "instrument": "NSE:EQ:RELIANCE",
            "side": "BUY",
            "qty": 10,
            "price": 2500.0,
            "charges": 20.0,
            "ts": "2025-01-05T04:00:00+00:00",
            "ts_ist": "2025-01-05 09:30:00 IST",
        },
        {
            "trade_id": "T2",
            "fill_id": "F2",
            "client_ref": "ORD-2",
            "instrument": "NSE:EQ:RELIANCE",
            "side": "SELL",
            "qty": 10,
            "price": 2600.0,
            "charges": 25.0,
            "ts": "2025-01-10T09:30:00+00:00",
            "ts_ist": "2025-01-10 15:00:00 IST",
        },
    ]

    out_path = writer.write(
        summary_info={"mode": "backtest", "run_name": "test_bt"},
        metrics={"net_pnl": 955.0, "trades": 2},
        trades=trades,
        equity_points=[],
        effective_config_text="",
        registry_snapshot={"hash": "core", "params": {}},
        manifest_info={"mode": "backtest", "code_version": "0.1.0"},
    )

    rt_file = out_path / "round_trips.csv"
    assert rt_file.is_file()

    with open(rt_file, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 1
    rt = rows[0]
    assert rt["round_trip_id"] == "RT-1"
    assert rt["instrument"] == "NSE:EQ:RELIANCE"
    assert rt["entry_date"] == "2025-01-05"
    assert rt["exit_date"] == "2025-01-10"
    assert rt["open_side"] == "BUY"
    assert int(rt["qty"]) == 10
    assert float(rt["open_price"]) == 2500.0
    assert float(rt["close_price"]) == 2600.0
    assert float(rt["gross_pnl"]) == 1000.0
    assert float(rt["charges"]) == 45.0
    assert float(rt["net_pnl"]) == 955.0


def test_results_bundle_writes_orders_csv(tmp_path: Path) -> None:
    """orders.csv is serialized with full order metadata."""
    run_dir = tmp_path / "results"
    writer = ResultsWriter(run_id="test_bt_orders", results_dir=run_dir)

    orders = [
        {
            "client_ref": "ORD-101",
            "venue_ref": "V-101",
            "instrument": "NSE:EQ:INFY",
            "side": "BUY",
            "order_type": "LIMIT",
            "qty": 50,
            "price": 1800.5,
            "trigger_price": "",
            "product": "CNC",
            "state": "FILLED",
            "filled_qty": 50,
            "avg_price": 1800.5,
            "created_ts": "2025-01-08T04:00:00+00:00",
            "created_ts_ist": "2025-01-08 09:30:00 IST",
        }
    ]

    out_path = writer.write(
        summary_info={"mode": "backtest", "run_name": "test_bt"},
        metrics={"net_pnl": 0.0, "trades": 1},
        trades=[],
        equity_points=[],
        effective_config_text="",
        registry_snapshot={"hash": "core", "params": {}},
        manifest_info={"mode": "backtest", "code_version": "0.1.0"},
        orders=orders,
    )

    ord_file = out_path / "orders.csv"
    assert ord_file.is_file()

    with open(ord_file, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 1
    assert rows[0]["client_ref"] == "ORD-101"
    assert rows[0]["instrument"] == "NSE:EQ:INFY"
    assert rows[0]["state"] == "FILLED"
    assert int(rows[0]["qty"]) == 50


def test_results_bundle_metrics_version_and_enrichment(tmp_path: Path) -> None:
    """metrics.json contains metrics_version = 1 and all statistical fields."""
    run_dir = tmp_path / "results"
    writer = ResultsWriter(run_id="test_metrics_ver", results_dir=run_dir)

    out_path = writer.write(
        summary_info={"mode": "backtest", "run_name": "test_bt"},
        metrics={"net_pnl": 1200.0, "trades": 5},
        trades=[],
        equity_points=[
            {
                "ts": "2025-01-01T10:00:00+00:00",
                "ts_ist": "2025-01-01 15:30:00 IST",
                "equity": 100000.0,
            },
            {
                "ts": "2025-01-02T10:00:00+00:00",
                "ts_ist": "2025-01-02 15:30:00 IST",
                "equity": 101200.0,
            },
        ],
        effective_config_text="",
        registry_snapshot={"hash": "core", "params": {}},
        manifest_info={"mode": "backtest", "code_version": "0.1.0"},
    )

    m_file = out_path / "metrics.json"
    assert m_file.is_file()
    data = json.loads(m_file.read_text(encoding="utf-8"))

    assert data["metrics_version"] == 1
    assert "cagr" in data
    assert "volatility" in data
    assert "sharpe" in data
    assert "sortino" in data
    assert "longest_drawdown_days" in data
    assert "expectancy" in data
    assert "profit_factor" in data
    assert "exposure" in data
    assert "turnover" in data
    assert "annualized_turnover" in data
    assert "total_return" in data


def test_results_bundle_reads_readiness_json(tmp_path: Path) -> None:
    """Writer reads readiness.json if present in the results folder."""
    run_dir = tmp_path / "results" / "test_readiness_run"
    run_dir.mkdir(parents=True)
    r_file = run_dir / "readiness.json"
    r_file.write_text(
        json.dumps(
            {
                "version": 1,
                "range": {"start": "2024-01-01", "end": "2025-01-01"},
                "universe": {
                    "hash": "abc12345def67890",
                    "coverage_pct": 98.5,
                    "members": ["RELIANCE", "TCS"],
                },
            }
        ),
        encoding="utf-8",
    )

    writer = ResultsWriter(run_id="test_readiness_run", results_dir=tmp_path / "results")
    out_path = writer.write(
        summary_info={"mode": "backtest", "run_name": "test_bt"},
        metrics={"net_pnl": 0.0, "trades": 35},
        trades=[],
        equity_points=[],
        effective_config_text="",
        registry_snapshot={"hash": "core", "params": {}},
        manifest_info={"mode": "backtest", "code_version": "0.1.0"},
    )

    man = json.loads((out_path / "manifest.json").read_text())
    assert man["universe_hash"] == "abc12345def67890"

    summary_text = (out_path / "SUMMARY.md").read_text()
    assert "universe coverage (98%)" in summary_text
    assert "2024-01-01 to 2025-01-01" in summary_text


def test_trades_csv_preserves_schema(tmp_path: Path) -> None:
    """trades.csv maintains exactly its established schema."""
    run_dir = tmp_path / "results"
    writer = ResultsWriter(run_id="test_trades_schema", results_dir=run_dir)

    trades = [
        {
            "trade_id": "T1",
            "fill_id": "F1",
            "client_ref": "REF1",
            "instrument": "NSE:EQ:SBIN",
            "side": "BUY",
            "qty": 100,
            "price": 800.0,
            "charges": 15.0,
            "ts": "2025-01-01T04:00:00+00:00",
            "ts_ist": "2025-01-01 09:30:00 IST",
        }
    ]

    out_path = writer.write(
        summary_info={"mode": "backtest", "run_name": "test_bt"},
        metrics={"net_pnl": 0.0, "trades": 1},
        trades=trades,
        equity_points=[],
        effective_config_text="",
        registry_snapshot={"hash": "core", "params": {}},
        manifest_info={"mode": "backtest", "code_version": "0.1.0"},
    )

    expected_cols = [
        "trade_id",
        "fill_id",
        "client_ref",
        "instrument",
        "side",
        "qty",
        "price",
        "charges",
        "ts",
        "ts_ist",
    ]
    with open(out_path / "trades.csv", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        assert header == expected_cols
