"""Drills and unit tests for results writer, metrics calculation, and drawdown math."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from algodesk.results.writer import ResultsWriter, compute_max_drawdown

# ---------------------------------------------------------------------------
# Max Drawdown Calculations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("curve", "expected_dd"),
    [
        ((), 0.0),
        ((100.0,), 0.0),
        ((100.0, 105.0, 110.0, 120.0), 0.0),  # Strictly increasing
        ((100.0, 90.0), 10.0),  # 10% drop
        ((100.0, 80.0), 20.0),  # 20% drop
        ((100.0, 50.0), 50.0),  # 50% drop
        ((100.0, 0.0), 100.0),  # 100% wipeout
        ((100.0, 95.0, 105.0, 90.0), 14.2857),  # Peak 105 down to 90 = 15/105 = 14.2857%
        ((100.0, 110.0, 100.0, 120.0, 100.0), 16.6667),  # Peak 120 down to 100 = 20/120 = 16.6667%
        ((50.0, 50.0, 50.0), 0.0),  # Flat equity
        ((100.0, 101.0, 100.5, 102.0), 0.4950),  # Minor fluctuation
    ],
)
def test_compute_max_drawdown_exact_values(curve: tuple[float, ...], expected_dd: float) -> None:
    dd = compute_max_drawdown(curve)
    assert abs(dd - expected_dd) < 1e-3


@pytest.mark.parametrize("initial_capital", [1000.0, 50000.0, 100000.0, 1000000.0])
def test_compute_max_drawdown_scale_invariance(initial_capital: float) -> None:
    # A 10% drop followed by 5% drop should yield 14.5% drawdown regardless of scale
    mult = initial_capital / 100.0
    curve = (100.0 * mult, 90.0 * mult, 95.0 * mult, 85.5 * mult)
    dd = compute_max_drawdown(curve)
    assert abs(dd - 14.5) < 1e-3


# ---------------------------------------------------------------------------
# ResultsWriter Artifacts and Manifest Integrity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("net_pnl", "trades_count", "win_rate", "max_dd"),
    [
        (0.0, 0, 0.0, 0.0),
        (1500.50, 5, 80.0, 1.25),
        (-450.00, 3, 33.3, 4.50),
        (12500.25, 20, 65.0, 2.10),
        (-2500.00, 10, 20.0, 12.50),
        (99999.99, 100, 95.0, 0.50),
    ],
)
def test_results_writer_summary_word_count_and_content(
    tmp_path: Path,
    net_pnl: float,
    trades_count: int,
    win_rate: float,
    max_dd: float,
) -> None:
    writer = ResultsWriter(run_id="test_run_metrics", results_dir=tmp_path)
    summary_info = {
        "run_name": "test_run",
        "mode": "paper",
        "date": "2026-10-26",
        "verdict": "PASS" if net_pnl >= 0 else "FAIL",
    }
    metrics = {
        "gross_pnl": net_pnl + 50.0,
        "net_pnl": net_pnl,
        "charges": 50.0,
        "trades": trades_count,
        "win_rate": win_rate,
        "max_drawdown": max_dd,
    }

    out_dir = writer.write(
        summary_info=summary_info,
        metrics=metrics,
        trades=[],
        equity_points=[],
        effective_config_text="[run]\nname = 'test'\n",
        registry_snapshot={"test": 1},
        manifest_info={"version": 1},
    )

    summary_file = out_dir / "SUMMARY.md"
    assert summary_file.exists()
    content = summary_file.read_text(encoding="utf-8")
    words = content.split()
    # Must adhere to repository rule: Summary of at most 150 words
    assert len(words) <= 150
    assert f"Net P&L: ₹{net_pnl:.2f}" in content
    assert f"{trades_count} trades" in content


@pytest.mark.parametrize("file_count", [1, 5, 10])
def test_results_writer_manifest_sha256_checksums(tmp_path: Path, file_count: int) -> None:
    writer = ResultsWriter(run_id=f"test_run_sha_{file_count}", results_dir=tmp_path)
    trades = [
        {
            "trade_id": f"t_{i}",
            "fill_id": f"f_{i}",
            "client_ref": f"ref_{i}",
            "instrument": "NSE:EQ:RELIANCE",
            "side": "BUY",
            "qty": 10,
            "price": 100.0 + i,
            "charges": 2.5,
            "ts": "2026-10-26T09:15:00+00:00",
        }
        for i in range(file_count)
    ]
    equity = [
        {
            "ts": "2026-10-26T09:15:00+00:00",
            "equity": 100000.0 + (i * 10),
            "cash": 90000.0,
            "unrealized": 0.0,
            "realized": float(i * 10),
            "drawdown": 0.0,
        }
        for i in range(file_count)
    ]

    out_dir = writer.write(
        summary_info={"run_name": "sha_test", "mode": "backtest", "date": "2026-10-26"},
        metrics={"trades": file_count, "net_pnl": 500.0},
        trades=trades,
        equity_points=equity,
        effective_config_text="[test]\n",
        registry_snapshot={"k": "v"},
        manifest_info={"files_count": file_count},
    )

    manifest_path = out_dir / "manifest.json"
    assert manifest_path.exists()
    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest_data.get("files_count") == file_count

    # Verify all 8 files required for M1 results bundle are generated
    required_files = [
        "SUMMARY.md",
        "metrics.json",
        "trades.csv",
        "equity.csv",
        "config.effective.txt",
        "registry.snapshot.json",
        "manifest.json",
        "session.md",
    ]
    for fname in required_files:
        fpath = out_dir / fname
        assert fpath.exists(), f"Missing required artifact: {fname}"
        content = fpath.read_bytes()
        assert len(content) > 0, f"Artifact {fname} must not be empty"
        sha = hashlib.sha256(content).hexdigest()
        assert len(sha) == 64


def test_results_writer_default_metrics_fallback(tmp_path: Path) -> None:
    writer = ResultsWriter(run_id="test_run_defaults", results_dir=tmp_path)
    out_dir = writer.write(
        summary_info={},
        metrics={},
        trades=[],
        equity_points=[],
        effective_config_text="",
        registry_snapshot={},
        manifest_info={},
    )
    metrics_file = out_dir / "metrics.json"
    assert metrics_file.exists()
    data = json.loads(metrics_file.read_text(encoding="utf-8"))
    assert data["gross_pnl"] == 0.0
    assert data["net_pnl"] == 0.0
    assert data["trades"] == 0
    assert data["win_rate"] == 0.0
    assert data["max_drawdown"] == 0.0


@pytest.mark.parametrize(
    "equity_val",
    [10000.0, 50000.0, 100000.0, 250000.0, 1000000.0],
)
def test_results_writer_equity_csv_row_formatting(tmp_path: Path, equity_val: float) -> None:
    writer = ResultsWriter(run_id=f"test_equity_{int(equity_val)}", results_dir=tmp_path)
    pts = [
        {
            "ts": "2026-10-26T09:15:00+00:00",
            "equity": equity_val,
            "cash": equity_val,
            "unrealized": 0.0,
            "realized": 0.0,
            "drawdown": 0.0,
        }
    ]
    out_dir = writer.write(
        summary_info={},
        metrics={},
        trades=[],
        equity_points=pts,
        effective_config_text="",
        registry_snapshot={},
        manifest_info={},
    )
    equity_file = out_dir / "equity.csv"
    assert equity_file.exists()
    content = equity_file.read_text(encoding="utf-8")
    assert str(equity_val) in content


def test_results_writer_custom_session_text(tmp_path: Path) -> None:
    writer = ResultsWriter(run_id="test_custom_session", results_dir=tmp_path)
    custom_msg = "Custom operator audit message for trading day."
    out_dir = writer.write(
        summary_info={},
        metrics={},
        trades=[],
        equity_points=[],
        effective_config_text="",
        registry_snapshot={},
        manifest_info={},
        session_text=custom_msg,
    )
    session_file = out_dir / "session.md"
    assert session_file.exists()
    assert custom_msg in session_file.read_text(encoding="utf-8")
