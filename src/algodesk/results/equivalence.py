"""Equivalence comparison between paper trading and bar-fidelity backtest."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from algodesk.results.compare import _load_events_for_run

logger = logging.getLogger("algodesk.results.equivalence")


def load_journal_events(run_dir: Path) -> list[dict[str, Any]]:
    """Load journal events from events.jsonl or sqlite journal."""
    events_jsonl = run_dir / "events.jsonl"
    if events_jsonl.is_file():
        events: list[dict[str, Any]] = []
        for line in events_jsonl.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    events.append(json.loads(line))
                except Exception:
                    pass
        if events:
            return events
    root = run_dir.parent.parent
    return _load_events_for_run(root, run_dir.name, run_dir)


@dataclass(frozen=True)
class SignalDiff:
    """Classified difference between paper and backtest signal."""

    index: int
    classification: str  # 'bar content differs' | 'timer-closed bar differs' | etc.
    paper_signal: tuple[datetime, str, str] | None
    backtest_signal: tuple[datetime, str, str] | None
    description: str


@dataclass(frozen=True)
class EquivalenceResult:
    """Outcome of backtest equivalence comparison."""

    signals_equal: bool
    paper_signals_count: int
    backtest_signals_count: int
    paper_orders_count: int
    backtest_orders_count: int
    paper_fills_count: int
    backtest_fills_count: int
    differences: list[SignalDiff]
    backtest_path: Path


def _parse_signal_tuple(p: dict[str, Any]) -> tuple[datetime, str, str] | None:
    ts_val = p.get("ts")
    if not ts_val:
        return None
    try:
        dt = datetime.fromisoformat(str(ts_val))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        else:
            dt = dt.astimezone(UTC)
        floored_ts = dt.replace(microsecond=0)
        inst = str(p.get("instrument", ""))
        side = str(p.get("side", "")).upper()
        return (floored_ts, inst, side)
    except Exception:
        return None


def compare_equivalence_events(
    paper_events: list[dict[str, Any]],
    bt_events: list[dict[str, Any]],
    bt_res_path: Path,
) -> EquivalenceResult:
    """Compare journal events from a paper session and its equivalence backtest."""
    paper_signals: list[tuple[datetime, str, str]] = []
    paper_orders_count = 0
    paper_fills_count = 0
    paper_timer_closes: list[dict[str, Any]] = []
    paper_exit_timers: list[datetime] = []

    for ev in paper_events:
        k = ev.get("kind")
        p = ev.get("payload_json", ev.get("payload", {}))
        if isinstance(p, str):
            try:
                p = json.loads(p)
            except Exception:
                p = {}

        if k == "signal":
            sig = _parse_signal_tuple(p)
            if sig:
                paper_signals.append(sig)
        elif k in ("order", "order.created", "intent"):
            paper_orders_count += 1
        elif k == "fill":
            paper_fills_count += 1
        elif k == "bar.timer_close":
            paper_timer_closes.append(p)
        elif k == "timer.strategy":
            t_str = p.get("ts")
            if t_str:
                try:
                    paper_exit_timers.append(
                        datetime.fromisoformat(t_str).astimezone(UTC).replace(microsecond=0)
                    )
                except Exception:
                    pass

    bt_signals: list[tuple[datetime, str, str]] = []
    bt_orders_count = 0
    bt_fills_count = 0

    for ev in bt_events:
        k = ev.get("kind")
        p = ev.get("payload_json", ev.get("payload", {}))
        if isinstance(p, str):
            try:
                p = json.loads(p)
            except Exception:
                p = {}

        if k == "signal":
            sig = _parse_signal_tuple(p)
            if sig:
                bt_signals.append(sig)
        elif k in ("order", "order.created", "intent"):
            bt_orders_count += 1
        elif k == "fill":
            bt_fills_count += 1

    diffs: list[SignalDiff] = []
    max_len = max(len(paper_signals), len(bt_signals))

    for idx in range(max_len):
        p_sig = paper_signals[idx] if idx < len(paper_signals) else None
        b_sig = bt_signals[idx] if idx < len(bt_signals) else None

        if p_sig != b_sig:
            classification = "other"
            sig_ts = p_sig[0] if p_sig else (b_sig[0] if b_sig else None)

            if p_sig and b_sig and p_sig[0] == b_sig[0]:
                classification = "bar content differs"
                desc = (
                    f"Signal at {p_sig[0].isoformat()} differed in instrument/side "
                    f"({p_sig} vs {b_sig})"
                )
            elif sig_ts and any(
                abs((sig_ts - et).total_seconds()) <= 300 for et in paper_exit_timers
            ):
                classification = "timer exit time differs"
                desc = f"Signal at index {idx} coincides with strategy exit timer"
            elif sig_ts and any(
                abs(
                    (
                        sig_ts - datetime.fromisoformat(tc.get("end_ts", "")).astimezone(UTC)
                    ).total_seconds()
                )
                <= 60
                for tc in paper_timer_closes
                if tc.get("end_ts")
            ):
                classification = "timer-closed bar differs"
                desc = f"Signal at index {idx} coincides with timer-closed bar"
            else:
                if p_sig is not None and b_sig is not None:
                    classification = "bar content differs"
                else:
                    classification = "other"
                desc = f"Mismatch at index {idx}: paper={p_sig}, backtest={b_sig}"

            diffs.append(
                SignalDiff(
                    index=idx,
                    classification=classification,
                    paper_signal=p_sig,
                    backtest_signal=b_sig,
                    description=desc,
                )
            )

    return EquivalenceResult(
        signals_equal=len(diffs) == 0,
        paper_signals_count=len(paper_signals),
        backtest_signals_count=len(bt_signals),
        paper_orders_count=paper_orders_count,
        backtest_orders_count=bt_orders_count,
        paper_fills_count=paper_fills_count,
        backtest_fills_count=bt_fills_count,
        differences=diffs,
        backtest_path=bt_res_path,
    )
