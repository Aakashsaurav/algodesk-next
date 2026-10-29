"""Hardening tests for ResultsWriter, metrics calculations, and SUMMARY.md constraints."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from algodesk.results.writer import (
    ResultsWriter,
    compute_max_drawdown,
)


def count_words(text: str) -> int:
    return len(text.split())


@pytest.mark.parametrize(
    ("text", "expected_count"),
    [
        ("", 0),
        ("hello world", 2),
        ("one   two   three", 3),
        ("AlgoDesk Next backtest session 'demo_paper' executed on 2026-10-26.", 8),
        ("\n\nWord1\nWord2\tWord3\n", 3),
        ("word " * 149, 149),
        ("word " * 150, 150),
        ("word " * 151, 151),
    ],
)
def test_count_words_utility(text: str, expected_count: int) -> None:
    assert count_words(text) == expected_count


@pytest.mark.parametrize(
    ("curve", "expected_dd"),
    [
        ([], 0.0),
        ([100.0], 0.0),
        ([100.0, 105.0, 110.0], 0.0),
        ([100.0, 90.0, 95.0], 10.0),
        ([100.0, 80.0, 120.0, 60.0], 50.0),
        ([1000.0, 500.0], 50.0),
        ([100.0, 100.0, 100.0], 0.0),
        ([50.0, 25.0, 10.0], 80.0),
        ([200.0, 180.0, 190.0, 170.0], 15.0),
        ([10.0, 20.0, 15.0, 30.0, 24.0], 25.0),
    ],
)
def test_compute_max_drawdown(curve: list[float], expected_dd: float) -> None:
    dd = compute_max_drawdown(curve)
    assert abs(dd - expected_dd) < 1e-4


@pytest.mark.parametrize(
    ("gross", "charges", "trades_count", "wins", "verdict"),
    [
        (0.0, 0.0, 0, 0, "PASS"),
        (1500.50, 42.10, 10, 7, "PASS"),
        (-2500.00, 115.00, 25, 8, "FAIL"),
        (50000.00, 1200.00, 100, 65, "PASS"),
        (-500.00, 15.00, 1, 0, "FAIL"),
        (100.00, 10.00, 2, 1, "PASS"),
        (0.00, 50.00, 5, 2, "FAIL"),
        (1000.00, 0.00, 4, 4, "PASS"),
        (-10000.00, 500.00, 50, 10, "FAIL"),
        (250.00, 25.00, 3, 2, "PASS"),
        (750.25, 45.75, 12, 8, "PASS"),
        (-80.00, 12.00, 4, 1, "FAIL"),
        (0.00, 0.00, 1, 0, "PASS"),
        (3300.00, 120.00, 15, 10, "PASS"),
        (-1200.00, 60.00, 8, 3, "FAIL"),
    ],
)
def test_summary_markdown_word_count_strictly_under_150(
    tmp_path: Path,
    gross: float,
    charges: float,
    trades_count: int,
    wins: int,
    verdict: str,
) -> None:
    run_id = f"test_run_{trades_count}_{wins}"
    writer = ResultsWriter(run_id, results_dir=tmp_path)

    net = round(gross - charges, 2)
    win_rate = round((wins / trades_count * 100.0) if trades_count else 0.0, 2)

    metrics = {
        "gross_pnl": gross,
        "net_pnl": net,
        "charges": charges,
        "trades": trades_count,
        "win_rate": win_rate,
        "max_drawdown": 5.25,
        "exposure_time": 12.5,
        "rejects_by_rule": {"max_pos": 1},
    }

    summary_info = {
        "run_name": "test_strategy_session",
        "mode": "paper",
        "date": "2026-10-26",
        "verdict": verdict,
    }

    res_dir = writer.write(
        summary_info=summary_info,
        metrics=metrics,
        trades=[],
        equity_points=[{"ts": "2026-10-26T09:15:00Z", "equity": 100000.0}],
        effective_config_text="[run]\nname = 'test'\n",
        registry_snapshot={"test": 1},
        manifest_info={"code_version": "0.1.0"},
    )

    summary_file = res_dir / "SUMMARY.md"
    assert summary_file.is_file()
    text = summary_file.read_text()
    words = count_words(text)
    assert words <= 150, f"SUMMARY.md word count {words} exceeds 150 words!"
    assert "## Summary" in text
    assert verdict in text


def test_results_bundle_completeness(tmp_path: Path) -> None:
    writer = ResultsWriter("bundle_check_run", results_dir=tmp_path)
    trades = [
        {
            "trade_id": "T1",
            "fill_id": "F1",
            "client_ref": "R1",
            "instrument": "NSE:EQ:RELIANCE",
            "side": "BUY",
            "qty": 10,
            "price": 2500.0,
            "charges": 15.5,
            "ts": "2026-10-26T09:20:00Z",
        },
        {
            "trade_id": "T2",
            "fill_id": "F2",
            "client_ref": "R2",
            "instrument": "NSE:EQ:RELIANCE",
            "side": "SELL",
            "qty": 10,
            "price": 2520.0,
            "charges": 16.0,
            "ts": "2026-10-26T09:45:00Z",
        },
    ]

    curve = [
        {"ts": "2026-10-26T09:15:00Z", "equity": 100000.0},
        {"ts": "2026-10-26T09:20:00Z", "equity": 99984.5},
        {"ts": "2026-10-26T09:45:00Z", "equity": 100168.5},
    ]

    metrics = {
        "gross_pnl": 200.0,
        "net_pnl": 168.5,
        "charges": 31.5,
        "trades": 2,
        "win_rate": 100.0,
        "max_drawdown": 0.02,
        "exposure_time": 25.0,
        "rejects_by_rule": {},
    }

    res_dir = writer.write(
        summary_info={
            "run_name": "bundle_run",
            "mode": "paper",
            "date": "2026-10-26",
            "verdict": "PASS",
        },
        metrics=metrics,
        trades=trades,
        equity_points=curve,
        effective_config_text="test config",
        registry_snapshot={"param": "val"},
        manifest_info={"seed": 7, "code_version": "0.1.0"},
    )

    expected_files = [
        "SUMMARY.md",
        "metrics.json",
        "trades.csv",
        "equity.csv",
        "config.effective.txt",
        "registry.snapshot.json",
        "manifest.json",
        "session.md",
    ]

    for fname in expected_files:
        p = res_dir / fname
        assert p.is_file(), f"Missing expected file {fname} in results bundle"
        assert p.stat().st_size > 0, f"File {fname} is empty"

    # Verify CSV headers
    trades_content = (res_dir / "trades.csv").read_text()
    assert "trade_id,fill_id,client_ref,instrument,side,qty,price,charges,ts" in trades_content
    assert "NSE:EQ:RELIANCE" in trades_content

    equity_content = (res_dir / "equity.csv").read_text()
    assert "ts,equity" in equity_content
    assert "100168.5" in equity_content

    # Verify JSON content
    metrics_read = json.loads((res_dir / "metrics.json").read_text())
    assert metrics_read["gross_pnl"] == 200.0
    assert metrics_read["net_pnl"] == 168.5
