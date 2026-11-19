"""Tests for Step 7: SUMMARY.md word budget, min_trades threshold, and assumptions line."""

from __future__ import annotations

import json
from pathlib import Path

from algodesk.results.writer import ResultsWriter


def test_summary_md_stays_at_most_150_words(tmp_path: Path) -> None:
    """SUMMARY.md body strictly adheres to the 150-word maximum limit."""
    writer = ResultsWriter(run_id="test_word_limit", results_dir=tmp_path / "results")

    # Generate a rich summary with all clauses
    metrics = {
        "net_pnl": 125430.75,
        "gross_pnl": 130200.0,
        "charges": 4769.25,
        "trades": 85,
        "win_rate": 62.35,
        "max_drawdown": 4.12,
        "closed_round_trips": 42,
        "open_positions": [
            {"instrument": "NSE:EQ:RELIANCE", "qty": 10},
            {"instrument": "NSE:EQ:TCS", "qty": 5},
        ],
        "out_of_sample": {"net_pnl": 34500.0, "win_rate": 58.0, "sharpe": 1.45},
        "oos_from": "2025-06-01",
        "study": "grid_search",
        "trial": 3,
    }

    out_path = writer.write(
        summary_info={"mode": "backtest", "run_name": "sma_cross_opt", "verdict": "PASS"},
        metrics=metrics,
        trades=[],
        equity_points=[],
        effective_config_text="",
        registry_snapshot={"hash": "core", "params": {}},
        manifest_info={"mode": "backtest", "code_version": "0.1.0"},
    )

    summary_text = (out_path / "SUMMARY.md").read_text(encoding="utf-8")
    lines = [
        line.strip()
        for line in summary_text.splitlines()
        if line.strip() and not line.startswith("#")
    ]
    body = " ".join(lines)
    word_count = len(body.split())
    assert word_count <= 150, f"SUMMARY.md word count {word_count} exceeds limit of 150 words!"


def test_summary_md_warns_when_trades_below_min_trades(tmp_path: Path) -> None:
    """When trade count is below report.min_trades (default 30), first sentence
    notes lack of statistical meaning.
    """
    writer = ResultsWriter(run_id="test_low_trades", results_dir=tmp_path / "results")

    out_path = writer.write(
        summary_info={"mode": "backtest", "run_name": "sma_test"},
        metrics={"net_pnl": 500.0, "trades": 12},  # 12 < 30
        trades=[],
        equity_points=[],
        effective_config_text="",
        registry_snapshot={"hash": "core", "params": {"report.min_trades": 30}},
        manifest_info={"mode": "backtest", "code_version": "0.1.0"},
    )

    summary_text = (out_path / "SUMMARY.md").read_text(encoding="utf-8")
    lines = [
        line.strip()
        for line in summary_text.splitlines()
        if line.strip() and not line.startswith("#")
    ]
    body = " ".join(lines)
    assert body.startswith("Statistics are not statistically meaningful due to low trade count.")


def test_summary_md_no_warning_when_trades_meet_min_trades(tmp_path: Path) -> None:
    """When trade count is at or above report.min_trades, the low-trade warning is omitted."""
    writer = ResultsWriter(run_id="test_ample_trades", results_dir=tmp_path / "results")

    out_path = writer.write(
        summary_info={"mode": "backtest", "run_name": "sma_test"},
        metrics={"net_pnl": 500.0, "trades": 35},  # 35 >= 30
        trades=[],
        equity_points=[],
        effective_config_text="",
        registry_snapshot={"hash": "core", "params": {"report.min_trades": 30}},
        manifest_info={"mode": "backtest", "code_version": "0.1.0"},
    )

    summary_text = (out_path / "SUMMARY.md").read_text(encoding="utf-8")
    assert "Statistics are not statistically meaningful" not in summary_text


def test_summary_md_assumptions_line_components(tmp_path: Path) -> None:
    """The assumptions line documents fill model, price basis, dividends, fee schedule,
    range, coverage, trial, OOS, and survivorship.
    """
    run_dir = tmp_path / "results" / "test_assumptions"
    run_dir.mkdir(parents=True)
    (run_dir / "readiness.json").write_text(
        json.dumps(
            {
                "range": {"start": "2024-01-01", "end": "2025-01-01"},
                "universe": {"coverage_pct": 100.0, "hash": "h123"},
            }
        )
    )

    writer = ResultsWriter(run_id="test_assumptions", results_dir=tmp_path / "results")
    out_path = writer.write(
        summary_info={"mode": "backtest", "run_name": "test_run"},
        metrics={"trades": 45, "trial": 2, "study": "trend_opt"},
        trades=[],
        equity_points=[],
        effective_config_text="",
        registry_snapshot={"hash": "core", "params": {}},
        manifest_info={"mode": "backtest", "code_version": "0.1.0"},
    )

    summary_text = (out_path / "SUMMARY.md").read_text(encoding="utf-8")
    assert "Assumptions:" in summary_text
    assert "fill model (bar synth with slippage)" in summary_text
    assert "price basis (splits)" in summary_text
    assert "dividends not credited" in summary_text
    assert "fee schedule (zerodha_eq)" in summary_text
    assert "data range (2024-01-01 to 2025-01-01)" in summary_text
    assert "universe coverage (100%)" in summary_text
    assert "trial 2 of study trend_opt" in summary_text
    assert "survivorship note" in summary_text
