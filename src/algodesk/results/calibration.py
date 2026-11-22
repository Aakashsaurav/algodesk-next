"""Slippage calibration metrics and results writer."""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from algodesk.results.equivalence import load_journal_events

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
    adverse_pairs_count: int = 0
    unpaired_paper_fills: int = 0
    unpaired_backtest_fills: int = 0
    refused: bool = False
    refusal_reason: str = ""


def load_trades_csv(path: Path) -> list[dict[str, Any]]:
    """Load trades from trades.csv in a run results directory."""
    trades_file = path / "trades.csv"
    if not trades_file.is_file():
        return []
    with open(trades_file, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader)


def _parse_ts(val: Any) -> datetime | None:
    if not val:
        return None
    try:
        dt = datetime.fromisoformat(str(val))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        return None


def _build_decision_map(events: list[dict[str, Any]]) -> dict[str, datetime]:
    """Map client_ref -> decision timestamp floored to second from journal events."""
    dec_map: dict[str, datetime] = {}
    for ev in events:
        k = ev.get("kind")
        p = ev.get("payload_json", ev.get("payload", {}))
        if isinstance(p, str):
            try:
                p = json.loads(p)
            except Exception:
                p = {}
        if k in ("intent", "order.created"):
            cref = p.get("client_ref")
            ts_str = ev.get("ts_utc") or p.get("ts")
            if cref and ts_str:
                dt = _parse_ts(ts_str)
                if dt:
                    dec_map[str(cref)] = dt.replace(microsecond=0)
    return dec_map


def compute_calibration_from_trades(
    session_id: str,
    sess_dir: Path,
    bt_dir: Path,
    paper_trades: list[dict[str, Any]],
    bt_trades: list[dict[str, Any]],
    paper_events: list[dict[str, Any]] | None = None,
    bt_events: list[dict[str, Any]] | None = None,
) -> CalibrationResult:
    """Calculate slippage calibration metrics pairing fills by order identity."""
    cal_file = bt_dir / "calibration.json"

    if paper_events is None:
        paper_events = load_journal_events(sess_dir)
    if bt_events is None:
        bt_events = load_journal_events(bt_dir)

    p_dec_map = _build_decision_map(paper_events)
    b_dec_map = _build_decision_map(bt_events)

    # Key = (instrument, side, decision_ts_floored)
    paper_by_key: dict[tuple[str, str, datetime], list[dict[str, Any]]] = {}
    for tr in paper_trades:
        inst = str(tr.get("instrument", ""))
        side = str(tr.get("side", "")).upper()
        cref = str(tr.get("client_ref", ""))
        dec_ts = p_dec_map.get(cref)
        if dec_ts is None:
            dt = _parse_ts(tr.get("ts"))
            dec_ts = dt.replace(microsecond=0) if dt else datetime(1970, 1, 1, tzinfo=UTC)
        key = (inst, side, dec_ts)
        paper_by_key.setdefault(key, []).append(tr)

    bt_by_key: dict[tuple[str, str, datetime], list[dict[str, Any]]] = {}
    for tr in bt_trades:
        inst = str(tr.get("instrument", ""))
        side = str(tr.get("side", "")).upper()
        cref = str(tr.get("client_ref", ""))
        dec_ts = b_dec_map.get(cref)
        if dec_ts is None:
            dt = _parse_ts(tr.get("ts"))
            dec_ts = dt.replace(microsecond=0) if dt else datetime(1970, 1, 1, tzinfo=UTC)
        key = (inst, side, dec_ts)
        bt_by_key.setdefault(key, []).append(tr)

    paired_fills: list[tuple[dict[str, Any], dict[str, Any]]] = []
    unpaired_paper: list[dict[str, Any]] = []
    unpaired_bt: list[dict[str, Any]] = []

    all_keys = set(paper_by_key.keys()) | set(bt_by_key.keys())
    for key in sorted(all_keys, key=lambda k: (k[2], k[0], k[1])):
        p_list = list(paper_by_key.get(key, []))
        b_list = list(bt_by_key.get(key, []))
        common_len = min(len(p_list), len(b_list))
        for i in range(common_len):
            paired_fills.append((p_list[i], b_list[i]))
        if len(p_list) > common_len:
            unpaired_paper.extend(p_list[common_len:])
        if len(b_list) > common_len:
            unpaired_bt.extend(b_list[common_len:])

    n_pairs = len(paired_fills)
    if n_pairs < 10:
        refusal_msg = (
            f"Fewer than 10 fill pairs found ({n_pairs} pairs); "
            "calibration requires at least 10 pairs."
        )
        cal_data = {
            "session_id": session_id,
            "fills_compared": n_pairs,
            "unpaired_paper_fills": len(unpaired_paper),
            "unpaired_backtest_fills": len(unpaired_bt),
            "refused": True,
            "refusal_reason": refusal_msg,
            "advisory": True,
        }
        cal_file.write_text(json.dumps(cal_data, indent=2), encoding="utf-8")
        (sess_dir / "calibration.json").write_text(json.dumps(cal_data, indent=2), encoding="utf-8")
        return CalibrationResult(
            session_id=session_id,
            fills_compared=n_pairs,
            mean_adverse_bps=0.0,
            median_adverse_bps=0.0,
            p95_adverse_bps=0.0,
            mean_pnl_diff=0.0,
            median_pnl_diff=0.0,
            suggested_slippage_bps=5.0,
            calibration_file=cal_file,
            unpaired_paper_fills=len(unpaired_paper),
            unpaired_backtest_fills=len(unpaired_bt),
            refused=True,
            refusal_reason=refusal_msg,
        )

    # Signed slippage: positive = worse for the trader (adverse)
    all_signed_slippage: list[float] = []
    adverse_bps_list: list[float] = []
    pnl_diff_list: list[float] = []

    for p_tr, b_tr in paired_fills:
        p_price = float(p_tr.get("price", 0.0))
        b_price = float(b_tr.get("price", 0.0))
        side = str(p_tr.get("side", "")).upper()

        if b_price > 0:
            if "BUY" in side:
                slip = (p_price - b_price) / b_price * 10000.0
            else:
                slip = (b_price - p_price) / b_price * 10000.0
            all_signed_slippage.append(round(slip, 2))
            if slip > 0:
                adverse_bps_list.append(round(slip, 2))

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

    suggested = round(median_bps, 1) if median_bps > 0 else 5.0

    cal_data = {
        "session_id": session_id,
        "fills_compared": n_pairs,
        "adverse_pairs_count": len(adverse_bps_list),
        "unpaired_paper_fills": len(unpaired_paper),
        "unpaired_backtest_fills": len(unpaired_bt),
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
        "advisory": True,
        "refused": False,
    }

    cal_file.write_text(json.dumps(cal_data, indent=2), encoding="utf-8")
    (sess_dir / "calibration.json").write_text(json.dumps(cal_data, indent=2), encoding="utf-8")

    return CalibrationResult(
        session_id=session_id,
        fills_compared=n_pairs,
        mean_adverse_bps=round(mean_bps, 2),
        median_adverse_bps=round(median_bps, 2),
        p95_adverse_bps=round(p95_bps, 2),
        mean_pnl_diff=round(mean_pnl, 2),
        median_pnl_diff=round(median_pnl, 2),
        suggested_slippage_bps=suggested,
        calibration_file=cal_file,
        adverse_pairs_count=len(adverse_bps_list),
        unpaired_paper_fills=len(unpaired_paper),
        unpaired_backtest_fills=len(unpaired_bt),
        refused=False,
    )
