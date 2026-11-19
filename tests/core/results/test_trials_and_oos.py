"""Tests for Step 11: Studies, trials, trial numbering, out-of-sample splits."""

from __future__ import annotations

import json
from pathlib import Path

from algodesk.results.index import ResultsIndex
from algodesk.results.writer import ResultsWriter


def test_study_trial_counting_in_index(tmp_path: Path) -> None:
    """The index counts backtest runs per study and reports 'trial n of study S'."""
    results_dir = tmp_path / "results"
    db_path = tmp_path / "data" / "state" / "results_index.db"
    index = ResultsIndex(db_path)

    study_name = "grid_search_vol"
    assert index.count_study_trials(study_name) == 0

    # First run in study
    run1 = results_dir / "bt_trial_1"
    run1.mkdir(parents=True)
    (run1 / "metrics.json").write_text(json.dumps({"mode": "backtest", "study": study_name}))
    (run1 / "manifest.json").write_text(json.dumps({"mode": "backtest", "study": study_name}))
    (run1 / "SUMMARY.md").write_text("# Summary\nVerdict: PASS\n")
    index.upsert_run(run1)

    assert index.count_study_trials(study_name) == 1

    # Second run in study
    run2 = results_dir / "bt_trial_2"
    run2.mkdir(parents=True)
    (run2 / "metrics.json").write_text(json.dumps({"mode": "backtest", "study": study_name}))
    (run2 / "manifest.json").write_text(json.dumps({"mode": "backtest", "study": study_name}))
    (run2 / "SUMMARY.md").write_text("# Summary\nVerdict: PASS\n")
    index.upsert_run(run2)

    assert index.count_study_trials(study_name) == 2


def test_summary_reports_trial_n_of_study_s(tmp_path: Path) -> None:
    """SUMMARY.md explicitly prints 'trial n of study S' in assumptions line."""
    writer = ResultsWriter(run_id="bt_trial_test", results_dir=tmp_path / "results")

    out_path = writer.write(
        summary_info={"mode": "backtest", "run_name": "bt_run", "study": "momentum_10", "trial": 5},
        metrics={"trades": 40, "study": "momentum_10", "trial": 5},
        trades=[],
        equity_points=[],
        effective_config_text="",
        registry_snapshot={"hash": "core", "params": {}},
        manifest_info={"mode": "backtest", "code_version": "0.1.0"},
    )

    summary_text = (out_path / "SUMMARY.md").read_text(encoding="utf-8")
    assert "trial 5 of study momentum_10" in summary_text

    metrics_data = json.loads((out_path / "metrics.json").read_text(encoding="utf-8"))
    assert metrics_data["study"] == "momentum_10"
    assert metrics_data["trial"] == 5


def test_oos_from_partitions_in_sample_and_out_of_sample_metrics(tmp_path: Path) -> None:
    """When oos_from is set, metrics are partitioned into in_sample and out_of_sample."""
    writer = ResultsWriter(run_id="bt_oos_test", results_dir=tmp_path / "results")

    # Equity points spanning before and after 2025-06-01
    equity_points = [
        {
            "ts": "2025-01-01T10:00:00+00:00",
            "ts_ist": "2025-01-01 15:30:00 IST",
            "equity": 100000.0,
        },
        {
            "ts": "2025-03-01T10:00:00+00:00",
            "ts_ist": "2025-03-01 15:30:00 IST",
            "equity": 105000.0,
        },
        {
            "ts": "2025-05-30T10:00:00+00:00",
            "ts_ist": "2025-05-30 15:30:00 IST",
            "equity": 110000.0,
        },
        # OOS starts 2025-06-01
        {
            "ts": "2025-06-01T10:00:00+00:00",
            "ts_ist": "2025-06-01 15:30:00 IST",
            "equity": 112000.0,
        },
        {
            "ts": "2025-08-01T10:00:00+00:00",
            "ts_ist": "2025-08-01 15:30:00 IST",
            "equity": 118000.0,
        },
    ]

    trades = [
        # In-sample trade
        {
            "trade_id": "T1",
            "fill_id": "F1",
            "client_ref": "O1",
            "instrument": "NSE:EQ:RELIANCE",
            "side": "BUY",
            "qty": 10,
            "price": 2000.0,
            "charges": 10.0,
            "ts": "2025-01-10T04:00:00+00:00",
            "ts_ist": "2025-01-10 09:30:00 IST",
        },
        {
            "trade_id": "T2",
            "fill_id": "F2",
            "client_ref": "O2",
            "instrument": "NSE:EQ:RELIANCE",
            "side": "SELL",
            "qty": 10,
            "price": 2200.0,
            "charges": 10.0,
            "ts": "2025-02-10T04:00:00+00:00",
            "ts_ist": "2025-02-10 09:30:00 IST",
        },
        # Out-of-sample trade
        {
            "trade_id": "T3",
            "fill_id": "F3",
            "client_ref": "O3",
            "instrument": "NSE:EQ:TCS",
            "side": "BUY",
            "qty": 5,
            "price": 3000.0,
            "charges": 15.0,
            "ts": "2025-06-15T04:00:00+00:00",
            "ts_ist": "2025-06-15 09:30:00 IST",
        },
        {
            "trade_id": "T4",
            "fill_id": "F4",
            "client_ref": "O4",
            "instrument": "NSE:EQ:TCS",
            "side": "SELL",
            "qty": 5,
            "price": 3200.0,
            "charges": 15.0,
            "ts": "2025-07-15T04:00:00+00:00",
            "ts_ist": "2025-07-15 09:30:00 IST",
        },
    ]

    out_path = writer.write(
        summary_info={
            "mode": "backtest",
            "run_name": "bt_oos_run",
            "oos_from": "2025-06-01",
        },
        metrics={"trades": 4},
        trades=trades,
        equity_points=equity_points,
        effective_config_text="",
        registry_snapshot={"hash": "core", "params": {}},
        manifest_info={"mode": "backtest", "code_version": "0.1.0"},
    )

    metrics_data = json.loads((out_path / "metrics.json").read_text(encoding="utf-8"))
    assert metrics_data["oos_from"] == "2025-06-01"
    assert "in_sample" in metrics_data
    assert "out_of_sample" in metrics_data

    is_m = metrics_data["in_sample"]
    oos_m = metrics_data["out_of_sample"]

    assert is_m["trades"] == 2
    assert oos_m["trades"] == 2
    assert is_m["closed_round_trips"] == 1
    assert oos_m["closed_round_trips"] == 1


def test_summary_leads_with_out_of_sample_period_when_oos_configured(tmp_path: Path) -> None:
    """When oos_from is set, SUMMARY.md leads with the out-of-sample period."""
    writer = ResultsWriter(run_id="bt_lead_oos", results_dir=tmp_path / "results")

    out_path = writer.write(
        summary_info={
            "mode": "backtest",
            "run_name": "bt_lead_oos",
            "oos_from": "2025-06-01",
        },
        metrics={
            "trades": 40,
            "out_of_sample": {
                "net_pnl": 5200.0,
                "win_rate": 65.0,
                "sharpe": 1.85,
            },
            "oos_from": "2025-06-01",
        },
        trades=[],
        equity_points=[],
        effective_config_text="",
        registry_snapshot={"hash": "core", "params": {}},
        manifest_info={"mode": "backtest", "code_version": "0.1.0"},
    )

    summary_text = (out_path / "SUMMARY.md").read_text(encoding="utf-8")
    assert (
        "Out-of-sample (from 2025-06-01): Net P&L: ₹5200.00, win rate: 65.0%, Sharpe: 1.85."
        in summary_text
    )
