"""Tests for M4-5d: Trials, trial numbering, out-of-sample split, and summary leads."""

from __future__ import annotations

import json
from pathlib import Path

from algodesk.config.loader import load_run
from algodesk.results.index import ResultsIndex
from algodesk.results.writer import ResultsWriter


def test_config_study_and_oos_from_loading() -> None:
    """Run configurations for rotation_1d_bt and sma_1d_bt define study and oos_from."""
    proj_root = Path(__file__).resolve().parents[3]
    rot_cfg = load_run(proj_root, "rotation_1d_bt")
    assert rot_cfg.backtest is not None
    assert rot_cfg.backtest.study == "rotation_nifty50"
    assert str(rot_cfg.backtest.oos_from) == "2025-07-01"

    sma_cfg = load_run(proj_root, "sma_1d_bt")
    assert sma_cfg.backtest is not None
    assert sma_cfg.backtest.study == "sma_cross_demo5"
    assert str(sma_cfg.backtest.oos_from) == "2025-07-01"


def test_trial_counter_increments_per_run_and_not_per_results_show(tmp_path: Path) -> None:
    """Study trial count increments per backtest run and NOT per results show / query."""
    results_dir = tmp_path / "results"
    db_path = tmp_path / "data" / "state" / "results_index.db"
    index = ResultsIndex(db_path)
    study = "rotation_nifty50"

    assert index.count_study_trials(study) == 0

    # Simulate run 1
    run1 = results_dir / "run_1"
    run1.mkdir(parents=True)
    (run1 / "metrics.json").write_text(
        json.dumps({"mode": "backtest", "study": study, "trial_number": 1})
    )
    (run1 / "manifest.json").write_text(
        json.dumps({"mode": "backtest", "study": study, "trial_number": 1})
    )
    (run1 / "SUMMARY.md").write_text("# Summary\nVerdict: PASS\n")
    index.upsert_run(run1)

    assert index.count_study_trials(study) == 1

    # Simulate running 'results show' multiple times (read-only inspection)
    # Neither read-only reads nor calling upsert_run on existing run increments the count
    index.upsert_run(run1)
    assert index.count_study_trials(study) == 1

    # Simulate run 2
    run2 = results_dir / "run_2"
    run2.mkdir(parents=True)
    (run2 / "metrics.json").write_text(
        json.dumps({"mode": "backtest", "study": study, "trial_number": 2})
    )
    (run2 / "manifest.json").write_text(
        json.dumps({"mode": "backtest", "study": study, "trial_number": 2})
    )
    (run2 / "SUMMARY.md").write_text("# Summary\nVerdict: PASS\n")
    index.upsert_run(run2)

    assert index.count_study_trials(study) == 2


def test_metrics_and_manifest_json_have_study_and_trial_number(tmp_path: Path) -> None:
    """ResultsWriter persists study and trial_number in both metrics.json and manifest.json."""
    writer = ResultsWriter(run_id="bt_study_test", results_dir=tmp_path / "results")

    out_path = writer.write(
        summary_info={
            "mode": "backtest",
            "run_name": "bt_study_test",
            "study": "rotation_nifty50",
            "trial": 3,
            "trial_number": 3,
        },
        metrics={"trades": 10},
        trades=[],
        equity_points=[],
        effective_config_text="",
        registry_snapshot={"hash": "core", "params": {}},
        manifest_info={
            "mode": "backtest",
            "code_version": "0.1.0",
            "study": "rotation_nifty50",
            "trial_number": 3,
        },
    )

    metrics_data = json.loads((out_path / "metrics.json").read_text(encoding="utf-8"))
    assert metrics_data["study"] == "rotation_nifty50"
    assert metrics_data["trial_number"] == 3
    assert metrics_data["trial"] == 3

    manifest_data = json.loads((out_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest_data["study"] == "rotation_nifty50"
    assert manifest_data["trial_number"] == 3


def test_oos_split_partitions_equity_and_trades_correctly(tmp_path: Path) -> None:
    """When oos_from is configured, in_sample and out_of_sample metrics are partitioned by date."""
    writer = ResultsWriter(run_id="bt_oos_split", results_dir=tmp_path / "results")

    equity_points = [
        {
            "ts": "2025-01-02T10:00:00+00:00",
            "ts_ist": "2025-01-02 15:30:00 IST",
            "equity": 1000000.0,
        },
        {
            "ts": "2025-06-30T10:00:00+00:00",
            "ts_ist": "2025-06-30 15:30:00 IST",
            "equity": 1050000.0,
        },
        # OOS begins 2025-07-01
        {
            "ts": "2025-07-01T10:00:00+00:00",
            "ts_ist": "2025-07-01 15:30:00 IST",
            "equity": 1055000.0,
        },
        {
            "ts": "2025-11-13T10:00:00+00:00",
            "ts_ist": "2025-11-13 15:30:00 IST",
            "equity": 1100000.0,
        },
    ]

    trades = [
        # In-sample trade pair
        {
            "trade_id": "T1",
            "fill_id": "F1",
            "instrument": "NSE:EQ:INFY",
            "side": "BUY",
            "qty": 10,
            "price": 1500.0,
            "charges": 5.0,
            "ts_ist": "2025-02-01 09:15:00 IST",
        },
        {
            "trade_id": "T2",
            "fill_id": "F2",
            "instrument": "NSE:EQ:INFY",
            "side": "SELL",
            "qty": 10,
            "price": 1600.0,
            "charges": 5.0,
            "ts_ist": "2025-03-01 09:15:00 IST",
        },
        # Out-of-sample trade pair
        {
            "trade_id": "T3",
            "fill_id": "F3",
            "instrument": "NSE:EQ:TCS",
            "side": "BUY",
            "qty": 5,
            "price": 3200.0,
            "charges": 10.0,
            "ts_ist": "2025-08-01 09:15:00 IST",
        },
        {
            "trade_id": "T4",
            "fill_id": "F4",
            "instrument": "NSE:EQ:TCS",
            "side": "SELL",
            "qty": 5,
            "price": 3400.0,
            "charges": 10.0,
            "ts_ist": "2025-09-01 09:15:00 IST",
        },
    ]

    out_path = writer.write(
        summary_info={
            "mode": "backtest",
            "run_name": "bt_oos_split",
            "oos_from": "2025-07-01",
        },
        metrics={"trades": 4},
        trades=trades,
        equity_points=equity_points,
        effective_config_text="",
        registry_snapshot={"hash": "core", "params": {}},
        manifest_info={"mode": "backtest", "code_version": "0.1.0"},
    )

    m = json.loads((out_path / "metrics.json").read_text(encoding="utf-8"))
    assert m["oos_from"] == "2025-07-01"
    assert "in_sample" in m
    assert "out_of_sample" in m

    is_m = m["in_sample"]
    oos_m = m["out_of_sample"]

    assert is_m["trades"] == 2
    assert oos_m["trades"] == 2
    assert is_m["closed_round_trips"] == 1
    assert oos_m["closed_round_trips"] == 1
    assert is_m["net_pnl"] == 50000.0
    assert oos_m["net_pnl"] == 45000.0


def test_oos_window_with_no_trades(tmp_path: Path) -> None:
    """When the out-of-sample window contains zero trades, sub-metrics compute cleanly."""
    writer = ResultsWriter(run_id="bt_oos_no_trades", results_dir=tmp_path / "results")

    equity_points = [
        {
            "ts": "2025-01-02T10:00:00+00:00",
            "ts_ist": "2025-01-02 15:30:00 IST",
            "equity": 1000000.0,
        },
        {
            "ts": "2025-06-30T10:00:00+00:00",
            "ts_ist": "2025-06-30 15:30:00 IST",
            "equity": 1050000.0,
        },
        {
            "ts": "2025-07-01T10:00:00+00:00",
            "ts_ist": "2025-07-01 15:30:00 IST",
            "equity": 1050000.0,
        },
        {
            "ts": "2025-11-13T10:00:00+00:00",
            "ts_ist": "2025-11-13 15:30:00 IST",
            "equity": 1050000.0,
        },
    ]

    # All trades are in-sample
    trades = [
        {
            "trade_id": "T1",
            "fill_id": "F1",
            "instrument": "NSE:EQ:INFY",
            "side": "BUY",
            "qty": 10,
            "price": 1500.0,
            "charges": 5.0,
            "ts_ist": "2025-02-01 09:15:00 IST",
        },
    ]

    out_path = writer.write(
        summary_info={
            "mode": "backtest",
            "run_name": "bt_oos_no_trades",
            "oos_from": "2025-07-01",
        },
        metrics={"trades": 1},
        trades=trades,
        equity_points=equity_points,
        effective_config_text="",
        registry_snapshot={"hash": "core", "params": {}},
        manifest_info={"mode": "backtest", "code_version": "0.1.0"},
    )

    m = json.loads((out_path / "metrics.json").read_text(encoding="utf-8"))
    oos_m = m["out_of_sample"]
    assert oos_m["trades"] == 0
    assert oos_m["closed_round_trips"] == 0
    assert oos_m["win_rate"] == 0.0
    assert oos_m["net_pnl"] == 0.0
    assert oos_m["max_drawdown"] == 0.0


def test_is_window_with_no_trades(tmp_path: Path) -> None:
    """When the in-sample window contains zero trades, sub-metrics compute cleanly."""
    writer = ResultsWriter(run_id="bt_is_no_trades", results_dir=tmp_path / "results")

    equity_points = [
        {
            "ts": "2025-01-02T10:00:00+00:00",
            "ts_ist": "2025-01-02 15:30:00 IST",
            "equity": 1000000.0,
        },
        {
            "ts": "2025-07-01T10:00:00+00:00",
            "ts_ist": "2025-07-01 15:30:00 IST",
            "equity": 1000000.0,
        },
        {
            "ts": "2025-11-13T10:00:00+00:00",
            "ts_ist": "2025-11-13 15:30:00 IST",
            "equity": 1020000.0,
        },
    ]

    # Trade only in out-of-sample
    trades = [
        {
            "trade_id": "T1",
            "fill_id": "F1",
            "instrument": "NSE:EQ:TCS",
            "side": "BUY",
            "qty": 5,
            "price": 3200.0,
            "charges": 10.0,
            "ts_ist": "2025-08-01 09:15:00 IST",
        },
    ]

    out_path = writer.write(
        summary_info={
            "mode": "backtest",
            "run_name": "bt_is_no_trades",
            "oos_from": "2025-07-01",
        },
        metrics={"trades": 1},
        trades=trades,
        equity_points=equity_points,
        effective_config_text="",
        registry_snapshot={"hash": "core", "params": {}},
        manifest_info={"mode": "backtest", "code_version": "0.1.0"},
    )

    m = json.loads((out_path / "metrics.json").read_text(encoding="utf-8"))
    is_m = m["in_sample"]
    assert is_m["trades"] == 0
    assert is_m["closed_round_trips"] == 0
    assert is_m["win_rate"] == 0.0


def test_summary_leads_with_out_of_sample_figures(tmp_path: Path) -> None:
    """SUMMARY.md leads with out-of-sample figures: net P&L, max drawdown, trades."""
    writer = ResultsWriter(run_id="bt_lead_oos_figures", results_dir=tmp_path / "results")

    out_path = writer.write(
        summary_info={
            "mode": "backtest",
            "run_name": "rotation_1d_bt",
            "study": "rotation_nifty50",
            "trial_number": 1,
            "oos_from": "2025-07-01",
        },
        metrics={
            "trades": 50,
            "net_pnl": 450000.0,
            "out_of_sample": {
                "net_pnl": 125000.50,
                "max_drawdown": 4.25,
                "trades": 18,
            },
            "oos_from": "2025-07-01",
        },
        trades=[],
        equity_points=[],
        effective_config_text="strategy = screen_rotation\n",
        registry_snapshot={"hash": "core", "params": {}},
        manifest_info={"mode": "backtest", "code_version": "0.1.0"},
    )

    summary_text = (out_path / "SUMMARY.md").read_text(encoding="utf-8")
    assert (
        "Out-of-sample (from 2025-07-01): Net P&L: ₹125000.50, max DD: 4.25%, trades: 18."
        in summary_text
    )
    assert "trial 1 of study rotation_nifty50" in summary_text


def test_summary_assumptions_has_trial_n_of_study_s(tmp_path: Path) -> None:
    """SUMMARY.md assumptions line explicitly contains 'trial n of study S'."""
    writer = ResultsWriter(run_id="bt_assumptions_trial", results_dir=tmp_path / "results")

    out_path = writer.write(
        summary_info={
            "mode": "backtest",
            "run_name": "sma_1d_bt",
            "study": "sma_cross_demo5",
            "trial_number": 4,
        },
        metrics={"trades": 20},
        trades=[],
        equity_points=[],
        effective_config_text="",
        registry_snapshot={"hash": "core", "params": {}},
        manifest_info={"mode": "backtest", "code_version": "0.1.0"},
    )

    summary_text = (out_path / "SUMMARY.md").read_text(encoding="utf-8")
    assert "trial 4 of study sma_cross_demo5" in summary_text


def test_summary_strictly_under_150_words(tmp_path: Path) -> None:
    """SUMMARY.md body strictly conforms to the <= 150 words requirement."""
    writer = ResultsWriter(run_id="bt_word_count_check", results_dir=tmp_path / "results")

    out_path = writer.write(
        summary_info={
            "mode": "backtest",
            "run_name": "rotation_1d_bt",
            "study": "rotation_nifty50",
            "trial_number": 2,
            "oos_from": "2025-07-01",
            "screen_name": "trend_rank_test",
        },
        metrics={
            "trades": 120,
            "net_pnl": 350000.0,
            "gross_pnl": 380000.0,
            "charges": 30000.0,
            "win_rate": 58.5,
            "max_drawdown": 8.2,
            "closed_round_trips": 60,
            "rejects_by_rule": {"daily_loss_limit_pct": 2},
            "out_of_sample": {
                "net_pnl": 95000.0,
                "max_drawdown": 3.8,
                "trades": 35,
            },
            "oos_from": "2025-07-01",
        },
        trades=[],
        equity_points=[],
        effective_config_text="strategy = screen_rotation\n",
        registry_snapshot={"hash": "core", "params": {}},
        manifest_info={"mode": "backtest", "code_version": "0.1.0"},
    )

    summary_content = (out_path / "SUMMARY.md").read_text(encoding="utf-8")
    # Extract body under ## Summary
    body_lines = summary_content.split("## Summary\n")[-1].strip().split()
    word_count = len(body_lines)
    assert word_count <= 150, f"Summary exceeded 150 words: {word_count} words"


def test_results_index_list_runs_filtered_by_study(tmp_path: Path) -> None:
    """ResultsIndex.list_runs accurately filters by study name."""
    db_path = tmp_path / "data" / "state" / "results_index.db"
    index = ResultsIndex(db_path)

    run_a = tmp_path / "results" / "run_study_a"
    run_a.mkdir(parents=True)
    (run_a / "metrics.json").write_text(
        json.dumps({"mode": "backtest", "study": "study_alpha", "trial_number": 1})
    )
    (run_a / "manifest.json").write_text(json.dumps({"mode": "backtest", "study": "study_alpha"}))
    (run_a / "SUMMARY.md").write_text("# Summary\nVerdict: PASS\n")
    index.upsert_run(run_a)

    run_b = tmp_path / "results" / "run_study_b"
    run_b.mkdir(parents=True)
    (run_b / "metrics.json").write_text(
        json.dumps({"mode": "backtest", "study": "study_beta", "trial_number": 1})
    )
    (run_b / "manifest.json").write_text(json.dumps({"mode": "backtest", "study": "study_beta"}))
    (run_b / "SUMMARY.md").write_text("# Summary\nVerdict: PASS\n")
    index.upsert_run(run_b)

    alpha_runs = index.list_runs(study="study_alpha")
    assert len(alpha_runs) == 1
    assert alpha_runs[0]["study"] == "study_alpha"

    beta_runs = index.list_runs(study="study_beta")
    assert len(beta_runs) == 1
    assert beta_runs[0]["study"] == "study_beta"


def test_backtest_assumptions_oos_none_when_unconfigured(tmp_path: Path) -> None:
    """When oos_from is not configured, assumptions report 'out-of-sample (none)'."""
    writer = ResultsWriter(run_id="bt_oos_none", results_dir=tmp_path / "results")

    out_path = writer.write(
        summary_info={
            "mode": "backtest",
            "run_name": "bt_oos_none",
            "study": "study_gamma",
            "trial_number": 1,
        },
        metrics={"trades": 10},
        trades=[],
        equity_points=[],
        effective_config_text="",
        registry_snapshot={"hash": "core", "params": {}},
        manifest_info={"mode": "backtest", "code_version": "0.1.0"},
    )

    summary_text = (out_path / "SUMMARY.md").read_text(encoding="utf-8")
    assert "out-of-sample (none)" in summary_text
    assert "trial 1 of study study_gamma" in summary_text
