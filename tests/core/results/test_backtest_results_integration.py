"""Integration and drill tests for M4-3 results store, metrics, comparison, and CLI."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from typer.testing import CliRunner

from algodesk.cli.app import app
from algodesk.results.compare import compare_backtest_manifests
from algodesk.results.metrics import (
    compute_cagr,
    compute_drawdown_metrics,
    compute_sortino,
    compute_volatility,
)
from algodesk.results.writer import ResultsWriter


def test_round_trip_duration_and_holding_dates(tmp_path: Path) -> None:
    """Round trips across distinct sessions correctly identify entry_date and exit_date."""
    writer = ResultsWriter(run_id="rt_dates_run", results_dir=tmp_path / "results")

    trades = [
        {
            "trade_id": "T1",
            "fill_id": "F1",
            "client_ref": "O1",
            "instrument": "NSE:EQ:INFY",
            "side": "BUY",
            "qty": 100,
            "price": 1500.0,
            "charges": 10.0,
            "ts": "2025-01-02T04:00:00+00:00",
            "ts_ist": "2025-01-02 09:30:00 IST",
        },
        {
            "trade_id": "T2",
            "fill_id": "F2",
            "client_ref": "O2",
            "instrument": "NSE:EQ:INFY",
            "side": "SELL",
            "qty": 50,
            "price": 1550.0,
            "charges": 5.0,
            "ts": "2025-01-05T04:00:00+00:00",
            "ts_ist": "2025-01-05 09:30:00 IST",
        },
        {
            "trade_id": "T3",
            "fill_id": "F3",
            "client_ref": "O3",
            "instrument": "NSE:EQ:INFY",
            "side": "SELL",
            "qty": 50,
            "price": 1600.0,
            "charges": 5.0,
            "ts": "2025-01-10T04:00:00+00:00",
            "ts_ist": "2025-01-10 09:30:00 IST",
        },
    ]

    out_path = writer.write(
        summary_info={"mode": "backtest", "run_name": "rt_test"},
        metrics={"trades": 3},
        trades=trades,
        equity_points=[],
        effective_config_text="",
        registry_snapshot={"hash": "core", "params": {}},
        manifest_info={"mode": "backtest", "code_version": "0.1.0"},
    )

    with open(out_path / "round_trips.csv", encoding="utf-8") as f:
        rts = list(csv.DictReader(f))

    assert len(rts) == 2
    assert rts[0]["entry_date"] == "2025-01-02"
    assert rts[0]["exit_date"] == "2025-01-05"
    assert int(rts[0]["qty"]) == 50
    assert float(rts[0]["net_pnl"]) == 2490.0  # 50 * 50 - 10 = 2490

    assert rts[1]["entry_date"] == "2025-01-02"
    assert rts[1]["exit_date"] == "2025-01-10"
    assert int(rts[1]["qty"]) == 50
    assert float(rts[1]["net_pnl"]) == 4990.0  # 50 * 100 - 10 = 4990


def test_daily_equity_mark_filtering_intra_session(tmp_path: Path) -> None:
    """equity_daily.csv takes only the final snapshot of each trading date."""
    writer = ResultsWriter(run_id="multi_snap_run", results_dir=tmp_path / "results")

    # 3 snapshots on 2025-01-02, 2 snapshots on 2025-01-03
    equity_points = [
        {
            "ts": "2025-01-02T04:00:00+00:00",
            "ts_ist": "2025-01-02 09:30:00 IST",
            "equity": 100000.0,
        },
        {
            "ts": "2025-01-02T06:00:00+00:00",
            "ts_ist": "2025-01-02 11:30:00 IST",
            "equity": 101000.0,
        },
        {
            "ts": "2025-01-02T10:00:00+00:00",
            "ts_ist": "2025-01-02 15:30:00 IST",
            "equity": 102000.0,
        },  # final mark for day 1
        {
            "ts": "2025-01-03T04:00:00+00:00",
            "ts_ist": "2025-01-03 09:30:00 IST",
            "equity": 101500.0,
        },
        {
            "ts": "2025-01-03T10:00:00+00:00",
            "ts_ist": "2025-01-03 15:30:00 IST",
            "equity": 103000.0,
        },  # final mark for day 2
    ]

    out_path = writer.write(
        summary_info={"mode": "backtest", "run_name": "multi_snap"},
        metrics={"trades": 0},
        trades=[],
        equity_points=equity_points,
        effective_config_text="",
        registry_snapshot={"hash": "core", "params": {}},
        manifest_info={"mode": "backtest", "code_version": "0.1.0"},
    )

    with open(out_path / "equity_daily.csv", encoding="utf-8") as f:
        daily_rows = list(csv.DictReader(f))

    assert len(daily_rows) == 2
    assert daily_rows[0]["date"] == "2025-01-02"
    assert float(daily_rows[0]["equity"]) == 102000.0

    assert daily_rows[1]["date"] == "2025-01-03"
    assert float(daily_rows[1]["equity"]) == 103000.0


def test_drawdown_peak_recovery_calculation() -> None:
    # 100 -> 120 -> 110 -> 105 -> 120 (recovered)
    # peak 120 at idx 1, recovered at idx 4
    # duration = 4 - 1 = 3
    equities = [100.0, 120.0, 110.0, 105.0, 120.0]
    max_dd, duration = compute_drawdown_metrics(equities)
    # dd = (120 - 105) / 120 = 15 / 120 = 12.5%
    assert max_dd == 12.5
    assert duration == 3


def test_drawdown_unrecovered_at_end() -> None:
    # 100 -> 150 -> 140 -> 130 -> 120 (never recovered)
    # peak 150 at idx 1, series ends at idx 4 without recovery
    # duration = 4 - 1 = 3
    equities = [100.0, 150.0, 140.0, 130.0, 120.0]
    max_dd, duration = compute_drawdown_metrics(equities)
    # dd = (150 - 120) / 150 = 30 / 150 = 20.0%
    assert max_dd == 20.0
    assert duration == 3


def test_cagr_zero_or_negative_inputs() -> None:
    assert compute_cagr(0.0, 100.0, 365) == 0.0
    assert compute_cagr(100.0, 0.0, 365) == 0.0
    assert compute_cagr(-50.0, 100.0, 365) == 0.0
    assert compute_cagr(100.0, 100.0, 0) == 0.0


def test_volatility_under_two_points() -> None:
    assert compute_volatility([]) == 0.0
    assert compute_volatility([0.05]) == 0.0


def test_sharpe_and_sortino_zero_deviation() -> None:
    # Zero downside deviation should yield None without division by zero
    returns = [0.02, 0.03, 0.04]  # all positive, rf=0
    assert compute_sortino(returns, risk_free_rate_pct=0.0) is None


def test_compare_backtest_different_fee_schedule(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    dir_a = results_dir / "fee_a"
    dir_b = results_dir / "fee_b"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)

    man_a = {"result_hash": "h_a", "fee_schedule_hash": "fee_v1", "dataset_checksums": ["chk"]}
    man_b = {"result_hash": "h_b", "fee_schedule_hash": "fee_v2", "dataset_checksums": ["chk"]}
    (dir_a / "manifest.json").write_text(json.dumps(man_a))
    (dir_b / "manifest.json").write_text(json.dumps(man_b))

    h_equal, diff = compare_backtest_manifests(dir_a, dir_b)
    assert h_equal is False
    assert diff == "fee schedule: fee_v1 != fee_v2"


def test_compare_backtest_different_parameters(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    dir_a = results_dir / "param_a"
    dir_b = results_dir / "param_b"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)

    man_a = {"result_hash": "h_a", "strategy_parameters": {"fast": 10}, "fee_schedule_hash": "fee"}
    man_b = {"result_hash": "h_b", "strategy_parameters": {"fast": 20}, "fee_schedule_hash": "fee"}
    (dir_a / "manifest.json").write_text(json.dumps(man_a))
    (dir_b / "manifest.json").write_text(json.dumps(man_b))

    h_equal, diff = compare_backtest_manifests(dir_a, dir_b)
    assert h_equal is False
    assert "parameter: fast (10 != 20)" in str(diff)


def test_compare_backtest_different_code_version(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    dir_a = results_dir / "cv_a"
    dir_b = results_dir / "cv_b"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)

    man_a = {"result_hash": "h_a", "code_version": "0.1.0"}
    man_b = {"result_hash": "h_b", "code_version": "0.2.0"}
    (dir_a / "manifest.json").write_text(json.dumps(man_a))
    (dir_b / "manifest.json").write_text(json.dumps(man_b))

    h_equal, diff = compare_backtest_manifests(dir_a, dir_b)
    assert h_equal is False
    assert diff == "code version: 0.1.0 != 0.2.0"


def test_results_delete_unknown_run_raises(tmp_project: Path) -> None:
    """Attempting to delete a non-existent run raises ConfigError."""
    runner = CliRunner()
    res = runner.invoke(app, ["results", "delete", "non_existent_run_id", "--reason", "test"])
    assert res.exit_code != 0
    assert "not found" in res.stdout


def test_results_show_unknown_run_raises(tmp_project: Path) -> None:
    """Attempting to show a non-existent run raises ConfigError."""
    runner = CliRunner()
    res = runner.invoke(app, ["results", "show", "non_existent_run_id"])
    assert res.exit_code != 0
    assert "not found" in res.stdout
