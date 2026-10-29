"""Tests for ResultsWriter and compute_max_drawdown."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from algodesk.results.writer import ResultsWriter, compute_max_drawdown


def test_compute_max_drawdown_scenarios() -> None:
    assert compute_max_drawdown([]) == 0.0
    assert compute_max_drawdown([100.0, 105.0, 110.0]) == 0.0
    # Peak 100 -> drop to 80 is 20% drawdown
    assert compute_max_drawdown([100.0, 80.0, 90.0]) == 20.0
    # Peak 100 -> 80 -> 120 -> 90 (peak 120 -> 90 is 25% drawdown)
    assert compute_max_drawdown([100.0, 80.0, 120.0, 90.0]) == 25.0


def test_results_writer_writes_all_artifacts(tmp_path: Path) -> None:
    writer = ResultsWriter(run_id="run_test_001", results_dir=tmp_path)
    summary_info = {
        "run_name": "demo_paper",
        "mode": "paper",
        "date": "2026-10-26",
        "verdict": "PASS",
    }
    metrics = {
        "gross_pnl": 1500.0,
        "net_pnl": 1250.0,
        "charges": 250.0,
        "trades": 4,
        "win_rate": 75.0,
        "max_drawdown": 2.5,
    }
    trades = [
        {
            "trade_id": "t-1",
            "fill_id": "f-1",
            "client_ref": "c-1",
            "instrument": "NSE:EQ:INFY",
            "side": "BUY",
            "qty": 10,
            "price": "1500.00",
            "charges": "5.00",
            "ts": "2026-10-26T09:15:00Z",
        }
    ]
    equity_points = [
        {"ts": "2026-10-26T09:15:00Z", "equity": 1000000.0, "cash": 985000.0, "drawdown": 0.0}
    ]
    manifest = {"files": {"trades.csv": "abc123hash"}}

    out_dir = writer.write(
        summary_info=summary_info,
        metrics=metrics,
        trades=trades,
        equity_points=equity_points,
        effective_config_text="[run]\nname = 'demo_paper'",
        registry_snapshot={"params": {}},
        manifest_info=manifest,
    )

    assert out_dir.exists()
    assert (out_dir / "SUMMARY.md").exists()
    assert (out_dir / "metrics.json").exists()
    assert (out_dir / "trades.csv").exists()
    assert (out_dir / "equity.csv").exists()
    assert (out_dir / "config.effective.txt").exists()
    assert (out_dir / "registry.snapshot.json").exists()
    assert (out_dir / "manifest.json").exists()
    assert (out_dir / "session.md").exists()


def test_results_writer_summary_word_count(tmp_path: Path) -> None:
    writer = ResultsWriter(run_id="run_test_words", results_dir=tmp_path)
    writer.write(
        summary_info={"run_name": "test", "mode": "paper"},
        metrics={"net_pnl": 100.0, "trades": 2},
        trades=[],
        equity_points=[],
        effective_config_text="",
        registry_snapshot={},
        manifest_info={},
    )
    summary_text = (tmp_path / "run_test_words" / "SUMMARY.md").read_text(encoding="utf-8")
    words = summary_text.split()
    assert len(words) <= 150
    assert "## Summary" in summary_text


def test_results_writer_csv_and_json_content(tmp_path: Path) -> None:
    writer = ResultsWriter(run_id="run_test_data", results_dir=tmp_path)
    metrics_in = {"net_pnl": 500.0, "trades": 1}
    trades_in = [{"trade_id": "t-1", "instrument": "NSE:EQ:INFY", "qty": 10, "price": "150.0"}]
    writer.write(
        summary_info={},
        metrics=metrics_in,
        trades=trades_in,
        equity_points=[{"ts": "2026-10-26T09:15:00Z", "equity": 1000.0}],
        effective_config_text="",
        registry_snapshot={},
        manifest_info={},
    )
    run_dir = tmp_path / "run_test_data"
    with open(run_dir / "trades.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["instrument"] == "NSE:EQ:INFY"

    data = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
    assert data["net_pnl"] == 500.0
    assert data["trades"] == 1
