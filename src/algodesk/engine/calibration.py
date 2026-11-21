"""Slippage calibration execution: runs equivalence backtest and computes calibration."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from algodesk.api.errors import ConfigError
from algodesk.engine.equivalence import run_backtest_equivalence
from algodesk.results.calibration import (
    CalibrationResult,
    compute_calibration_from_trades,
    load_trades_csv,
)

logger = logging.getLogger("algodesk.engine.calibration")


def run_slippage_calibration(rt: Any, session_id: str) -> CalibrationResult:
    """Compare bar-fidelity fills with tick-fidelity (paper latency) fills."""
    sess_dir = rt.paths.results / session_id
    if not sess_dir.is_dir() and Path(session_id).is_dir():
        sess_dir = Path(session_id)
    if not sess_dir.is_dir():
        raise ConfigError(
            what=f"Recorded session '{session_id}' not found in results directory",
            where=f"results/{session_id}",
            fix="Provide a valid paper session ID",
        )

    # 1. Run bar-fidelity backtest
    eq_res = run_backtest_equivalence(rt, session_id)
    bt_dir = eq_res.backtest_path

    # 2. Load trades from both runs
    paper_trades = load_trades_csv(sess_dir)
    bt_trades = load_trades_csv(bt_dir)

    # 3. Compute metrics and write calibration.json
    return compute_calibration_from_trades(
        session_id=session_id,
        sess_dir=sess_dir,
        bt_dir=bt_dir,
        paper_trades=paper_trades,
        bt_trades=bt_trades,
    )
