"""Equivalence comparison between paper trading and bar-fidelity backtest."""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
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
    bar_info: str = ""
    bar_source: str = ""


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
    only_in_paper: list[dict[str, Any]] = field(default_factory=list)
    only_in_backtest: list[dict[str, Any]] = field(default_factory=list)


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
    bars: list[Any] | None = None,
) -> EquivalenceResult:
    """Compare journal events from a paper session and its equivalence backtest."""
    paper_signals: list[tuple[datetime, str, str]] = []
    paper_order_refs: set[str] = set()
    paper_fills_count = 0
    paper_timer_closes_set: set[tuple[str, datetime]] = set()
    paper_exit_timers: list[datetime] = []

    # Map bars by (instrument, end_ts_floored)
    bars_map: dict[tuple[str, datetime], Any] = {}
    if bars:
        for b in bars:
            b_end = b.end_ts if b.end_ts.tzinfo else b.end_ts.replace(tzinfo=UTC)
            b_sec = b_end.astimezone(UTC).replace(microsecond=0)
            bars_map[(str(b.instrument), b_sec)] = b

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
        elif k in ("order.created", "intent", "order"):
            cref = p.get("client_ref")
            if cref:
                paper_order_refs.add(str(cref))
            else:
                paper_order_refs.add(f"order_{len(paper_order_refs)}")
        elif k == "fill":
            paper_fills_count += 1
        elif k == "bar.timer_close":
            inst_str = str(p.get("instrument", ""))
            end_ts_val = p.get("end_ts")
            if inst_str and end_ts_val:
                try:
                    tc_dt = datetime.fromisoformat(end_ts_val)
                    if tc_dt.tzinfo is None:
                        tc_dt = tc_dt.replace(tzinfo=UTC)
                    else:
                        tc_dt = tc_dt.astimezone(UTC)
                    paper_timer_closes_set.add((inst_str, tc_dt.replace(microsecond=0)))
                except Exception:
                    pass
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
    bt_order_refs: set[str] = set()
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
        elif k in ("order.created", "intent", "order"):
            cref = p.get("client_ref")
            if cref:
                bt_order_refs.add(str(cref))
            else:
                bt_order_refs.add(f"bt_order_{len(bt_order_refs)}")
        elif k == "fill":
            bt_fills_count += 1

    paper_counts = Counter(paper_signals)
    bt_counts = Counter(bt_signals)

    only_in_paper_counts = paper_counts - bt_counts
    only_in_bt_counts = bt_counts - paper_counts

    only_in_paper: list[dict[str, Any]] = []
    for sig, count in sorted(
        only_in_paper_counts.items(), key=lambda x: (x[0][0], x[0][1], x[0][2])
    ):
        for _ in range(count):
            sig_ts, inst, side = sig
            bar = bars_map.get((inst, sig_ts))
            is_timer = (inst, sig_ts) in paper_timer_closes_set
            source = "timer-closed" if is_timer else ("tick-built" if bar else "timer-exit")
            b_desc = (
                f"O={bar.open} H={bar.high} L={bar.low} C={bar.close} V={bar.volume}"
                if bar
                else "N/A"
            )
            only_in_paper.append(
                {
                    "signal": sig,
                    "bar": b_desc,
                    "source": source,
                    "formatted": f"{sig_ts.isoformat()} {inst} {side} | {b_desc} | {source}",
                }
            )

    only_in_backtest: list[dict[str, Any]] = []
    for sig, count in sorted(only_in_bt_counts.items(), key=lambda x: (x[0][0], x[0][1], x[0][2])):
        for _ in range(count):
            sig_ts, inst, side = sig
            bar = bars_map.get((inst, sig_ts))
            is_timer = (inst, sig_ts) in paper_timer_closes_set
            source = "timer-closed" if is_timer else ("tick-built" if bar else "timer-exit")
            b_desc = (
                f"O={bar.open} H={bar.high} L={bar.low} C={bar.close} V={bar.volume}"
                if bar
                else "N/A"
            )
            only_in_backtest.append(
                {
                    "signal": sig,
                    "bar": b_desc,
                    "source": source,
                    "formatted": f"{sig_ts.isoformat()} {inst} {side} | {b_desc} | {source}",
                }
            )

    diffs: list[SignalDiff] = []
    p_matched: set[int] = set()
    b_matched: set[int] = set()

    for p_idx, p_item in enumerate(only_in_paper):
        p_sig = p_item["signal"]
        p_ts, p_inst, p_side = p_sig
        for b_idx, b_item in enumerate(only_in_backtest):
            if b_idx in b_matched:
                continue
            b_sig = b_item["signal"]
            b_ts, b_inst, b_side = b_sig
            if p_inst == b_inst and p_ts == b_ts:
                p_matched.add(p_idx)
                b_matched.add(b_idx)
                diffs.append(
                    SignalDiff(
                        index=len(diffs),
                        classification="bar content differs",
                        paper_signal=p_sig,
                        backtest_signal=b_sig,
                        description=(
                            f"Signal at {p_ts.isoformat()} {p_inst}: paper {p_side} "
                            f"vs backtest {b_side} [{p_item['bar']} ({p_item['source']})]"
                        ),
                        bar_info=p_item["bar"],
                        bar_source=p_item["source"],
                    )
                )
                break
            elif (
                p_inst == b_inst
                and abs((p_ts - b_ts).total_seconds()) <= 120
                and (p_ts in paper_exit_timers or b_ts in paper_exit_timers)
            ):
                p_matched.add(p_idx)
                b_matched.add(b_idx)
                diffs.append(
                    SignalDiff(
                        index=len(diffs),
                        classification="timer exit time differs",
                        paper_signal=p_sig,
                        backtest_signal=b_sig,
                        description=(
                            f"Timer exit differs: paper {p_ts.isoformat()} "
                            f"vs backtest {b_ts.isoformat()}"
                        ),
                        bar_info=p_item["bar"],
                        bar_source=p_item["source"],
                    )
                )
                break

    for p_idx, item in enumerate(only_in_paper):
        if p_idx in p_matched:
            continue
        sig = item["signal"]
        is_tc = item["source"] == "timer-closed"
        c_name = "timer-closed bar differs" if is_tc else "only in paper"
        desc = (
            f"Signal only in paper: {sig[0].isoformat()} {sig[1]} {sig[2]} "
            f"[{item['bar']} ({item['source']})]"
        )
        diffs.append(
            SignalDiff(
                index=len(diffs),
                classification=c_name,
                paper_signal=sig,
                backtest_signal=None,
                description=desc,
                bar_info=item["bar"],
                bar_source=item["source"],
            )
        )
    for b_idx, item in enumerate(only_in_backtest):
        if b_idx in b_matched:
            continue
        sig = item["signal"]
        is_tc = item["source"] == "timer-closed"
        c_name = "timer-closed bar differs" if is_tc else "only in backtest"
        desc = (
            f"Signal only in backtest: {sig[0].isoformat()} {sig[1]} {sig[2]} "
            f"[{item['bar']} ({item['source']})]"
        )
        diffs.append(
            SignalDiff(
                index=len(diffs),
                classification=c_name,
                paper_signal=None,
                backtest_signal=sig,
                description=desc,
                bar_info=item["bar"],
                bar_source=item["source"],
            )
        )

    signals_equal = len(only_in_paper) == 0 and len(only_in_backtest) == 0

    return EquivalenceResult(
        signals_equal=signals_equal,
        paper_signals_count=len(paper_signals),
        backtest_signals_count=len(bt_signals),
        paper_orders_count=len(paper_order_refs),
        backtest_orders_count=len(bt_order_refs),
        paper_fills_count=paper_fills_count,
        backtest_fills_count=bt_fills_count,
        differences=diffs,
        backtest_path=bt_res_path,
        only_in_paper=only_in_paper,
        only_in_backtest=only_in_backtest,
    )
