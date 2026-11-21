"""Slippage calibration metrics and results writer."""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger("algodesk.results.calibration")


@dataclass(frozen=True)
class CalibrationResult:
    """Outcome of slippage calibration."""

    session_id: str
    fills_compared: int
    mean_adverse_bps: float
    median_adverse_bps: float
    p95_adverse_bps: float
    mean_pnl_diff: float
    median_pnl_diff: float
    suggested_slippage_bps: float
    calibration_file: Path


def load_trades_csv(path: Path) -> list[dict[str, Any]]:
    """Load trades from trades.csv in a run results directory."""
    trades_file = path / "trades.csv"
    if not trades_file.is_file():
        return []
    with open(trades_file, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader)


def compute_calibration_from_trades(
    session_id: str,
    sess_dir: Path,
    bt_dir: Path,
    paper_trades: list[dict[str, Any]],
    bt_trades: list[dict[str, Any]],
) -> CalibrationResult:
    """Calculate slippage calibration metrics and write calibration.json."""
    adverse_bps_list: list[float] = []
    pnl_diff_list: list[float] = []

    # Match fills by index
    min_len = min(len(paper_trades), len(bt_trades))
    for i in range(min_len):
        p_tr = paper_trades[i]
        b_tr = bt_trades[i]

        p_price = float(p_tr.get("price", 0.0))
        b_price = float(b_tr.get("price", 0.0))
        side = str(p_tr.get("side", "")).upper()

        if b_price > 0:
            if "BUY" in side:
                diff = max(0.0, (p_price - b_price) / b_price * 10000.0)
            else:
                diff = max(0.0, (b_price - p_price) / b_price * 10000.0)
            adverse_bps_list.append(round(diff, 2))

        p_net = float(p_tr.get("net_pnl", 0.0))
        b_net = float(b_tr.get("net_pnl", 0.0))
        pnl_diff_list.append(round(abs(p_net - b_net), 2))

    if adverse_bps_list:
        mean_bps = float(np.mean(adverse_bps_list))
        median_bps = float(np.median(adverse_bps_list))
        p95_bps = float(np.percentile(adverse_bps_list, 95))
    else:
        mean_bps = 0.0
        median_bps = 0.0
        p95_bps = 0.0

    if pnl_diff_list:
        mean_pnl = float(np.mean(pnl_diff_list))
        median_pnl = float(np.median(pnl_diff_list))
    else:
        mean_pnl = 0.0
        median_pnl = 0.0

    # Suggested slippage: median or p95, defaulting to current registry default if 0
    if median_bps > 0:
        suggested = round(median_bps, 1)
    elif p95_bps > 0:
        suggested = round(p95_bps, 1)
    else:
        suggested = 5.0

    cal_data = {
        "session_id": session_id,
        "fills_compared": len(adverse_bps_list),
        "adverse_slippage_bps": {
            "mean": round(mean_bps, 2),
            "median": round(median_bps, 2),
            "p95": round(p95_bps, 2),
        },
        "net_pnl_diff_per_trade": {
            "mean": round(mean_pnl, 2),
            "median": round(median_pnl, 2),
        },
        "suggested_slippage_bps": suggested,
    }

    # Write calibration.json in the backtest results folder and session dir
    cal_file = bt_dir / "calibration.json"
    cal_file.write_text(json.dumps(cal_data, indent=2), encoding="utf-8")
    (sess_dir / "calibration.json").write_text(json.dumps(cal_data, indent=2), encoding="utf-8")

    return CalibrationResult(
        session_id=session_id,
        fills_compared=len(adverse_bps_list),
        mean_adverse_bps=round(mean_bps, 2),
        median_adverse_bps=round(median_bps, 2),
        p95_adverse_bps=round(p95_bps, 2),
        mean_pnl_diff=round(mean_pnl, 2),
        median_pnl_diff=round(median_pnl, 2),
        suggested_slippage_bps=suggested,
        calibration_file=cal_file,
    )
