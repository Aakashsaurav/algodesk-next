"""Equivalence comparison between paper trading and bar-fidelity backtest."""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

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
    cause: str = "other"


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
    orders_only_in_paper: list[dict[str, Any]] = field(default_factory=list)
    orders_only_in_backtest: list[dict[str, Any]] = field(default_factory=list)
    fills_only_in_paper: list[dict[str, Any]] = field(default_factory=list)
    fills_only_in_backtest: list[dict[str, Any]] = field(default_factory=list)


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


def _parse_event_dt(ts_val: Any) -> datetime | None:
    if not ts_val:
        return None
    try:
        dt = datetime.fromisoformat(str(ts_val))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        else:
            dt = dt.astimezone(UTC)
        return dt.replace(microsecond=0)
    except Exception:
        return None


def _extract_orders_and_fills(
    events: list[dict[str, Any]],
) -> tuple[
    dict[str, dict[str, Any]],
    list[dict[str, Any]],
    dict[str, str],
    list[tuple[str, str, datetime, str]],
    datetime | None,
]:
    orders: dict[str, dict[str, Any]] = {}
    fills: list[dict[str, Any]] = []
    rejects: dict[str, str] = {}
    reject_events: list[tuple[str, str, datetime, str]] = []
    stop_ts: datetime | None = None

    for ev in events:
        k = ev.get("kind")
        p = ev.get("payload_json", ev.get("payload", {}))
        if isinstance(p, str):
            try:
                p = json.loads(p)
            except Exception:
                p = {}

        if k in ("run.end", "session.stop"):
            s_t = _parse_event_dt(p.get("stop_time") or ev.get("ts_utc"))
            if s_t:
                stop_ts = s_t

        elif k == "risk.decision":
            allow = p.get("allow", True)
            if not allow:
                intent_id = str(p.get("intent_id", ""))
                rule = str(p.get("rule", "unknown"))
                if intent_id:
                    rejects[intent_id] = rule
                ts = _parse_event_dt(ev.get("ts_utc") or p.get("ts"))
                inst = str(p.get("instrument") or "")
                side = str(p.get("side", "")).upper()
                if not inst and "_" in intent_id:
                    parts = intent_id.split("_")
                    for part in parts:
                        if ":" in part:
                            inst = part
                            break
                    if "buy" in intent_id.lower():
                        side = "BUY"
                    elif "sell" in intent_id.lower() or "exit" in intent_id.lower():
                        side = "SELL"
                if ts and inst:
                    reject_events.append((inst, side, ts, rule))

        elif k in ("intent", "order.created", "order"):
            cref = p.get("client_ref")
            if cref:
                cref_str = str(cref)
                if cref_str not in orders:
                    spec = p.get("spec", p) if isinstance(p.get("spec"), dict) else p
                    inst = str(spec.get("instrument") or p.get("instrument", ""))
                    side = str(spec.get("side") or p.get("side", "")).upper()
                    qty = spec.get("qty") or p.get("qty", 1)
                    ts = _parse_event_dt(p.get("created_ts") or p.get("ts") or ev.get("ts_utc"))
                    orders[cref_str] = {
                        "client_ref": cref_str,
                        "instrument": inst,
                        "side": side,
                        "qty": qty,
                        "decision_ts": ts,
                        "intent_id": str(p.get("intent_id", "")),
                    }

        elif k == "fill":
            cref = str(p.get("client_ref", ""))
            inst = str(p.get("instrument", ""))
            side = str(p.get("side", "")).upper()
            qty = p.get("qty", 1)
            px = p.get("price", "N/A")
            f_ts = _parse_event_dt(p.get("ts") or ev.get("ts_utc"))
            fills.append(
                {
                    "client_ref": cref,
                    "instrument": inst,
                    "side": side,
                    "qty": qty,
                    "price": px,
                    "fill_ts": f_ts,
                }
            )

    for f in fills:
        dec_ts = orders.get(f["client_ref"], {}).get("decision_ts") or f["fill_ts"]
        f["decision_ts"] = dec_ts

    return orders, fills, rejects, reject_events, stop_ts


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
            cause = "timer close differs" if is_timer else "bar missing in backtest"
            only_in_paper.append(
                {
                    "signal": sig,
                    "bar": b_desc,
                    "source": source,
                    "cause": cause,
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
            cause = "timer close differs" if is_timer else "bar missing in paper"
            only_in_backtest.append(
                {
                    "signal": sig,
                    "bar": b_desc,
                    "source": source,
                    "cause": cause,
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
                        cause="bar content differs",
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
                        cause="timer close differs",
                    )
                )
                break

    for p_idx, item in enumerate(only_in_paper):
        if p_idx in p_matched:
            continue
        sig = item["signal"]
        is_tc = item["source"] == "timer-closed"
        c_name = "timer-closed bar differs" if is_tc else "only in paper"
        cause = item.get("cause", "timer close differs" if is_tc else "bar missing in backtest")
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
                cause=cause,
            )
        )
    for b_idx, item in enumerate(only_in_backtest):
        if b_idx in b_matched:
            continue
        sig = item["signal"]
        is_tc = item["source"] == "timer-closed"
        c_name = "timer-closed bar differs" if is_tc else "only in backtest"
        cause = item.get("cause", "timer close differs" if is_tc else "bar missing in paper")
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
                cause=cause,
            )
        )

    signals_equal = len(only_in_paper) == 0 and len(only_in_backtest) == 0

    # Match orders and fills by (instrument, side, decision_ts_floored)
    p_orders, p_fills, p_rejects, p_reject_events, p_stop = _extract_orders_and_fills(paper_events)
    b_orders, b_fills, b_rejects, b_reject_events, b_stop = _extract_orders_and_fills(bt_events)

    p_sig_set = set(paper_signals)
    b_sig_set = set(bt_signals)

    p_ord_map: dict[tuple[str, str, datetime], list[dict[str, Any]]] = {}
    b_ord_map: dict[tuple[str, str, datetime], list[dict[str, Any]]] = {}
    for o in p_orders.values():
        if o["decision_ts"] is not None:
            p_ord_map.setdefault((o["instrument"], o["side"], o["decision_ts"]), []).append(o)
    for o in b_orders.values():
        if o["decision_ts"] is not None:
            b_ord_map.setdefault((o["instrument"], o["side"], o["decision_ts"]), []).append(o)

    orders_only_in_paper: list[dict[str, Any]] = []
    orders_only_in_backtest: list[dict[str, Any]] = []
    order_cause_map_paper: dict[tuple[str, str, datetime], str] = {}
    order_cause_map_bt: dict[tuple[str, str, datetime], str] = {}

    all_ord_keys = set(p_ord_map.keys()) | set(b_ord_map.keys())
    for key in sorted(all_ord_keys, key=lambda k: (k[2], k[0], k[1])):
        p_list = p_ord_map.get(key, [])
        b_list = b_ord_map.get(key, [])
        diff = len(p_list) - len(b_list)

        if diff > 0:
            rejected_by_rule = False
            for r_inst, r_side, r_ts, _ in b_reject_events:
                if r_inst == key[0] and (not r_side or r_side == key[1]):
                    if abs((r_ts - key[2]).total_seconds()) <= 2:
                        rejected_by_rule = True
                        break
            if not rejected_by_rule:
                for r_id in b_rejects:
                    if key[0] in r_id:
                        rejected_by_rule = True
                        break

            if rejected_by_rule:
                cause = "rejected in backtest (rule)"
            elif b_stop is not None and key[2] >= b_stop:
                cause = "session end"
            elif (key[2], key[0], key[1]) not in b_sig_set and not any(
                s[1] == key[0] and s[2] == key[1] and abs((s[0] - key[2]).total_seconds()) <= 1
                for s in b_sig_set
            ):
                cause = "no signal"
            else:
                cause = "unknown"

            order_cause_map_paper[key] = cause
            for i in range(diff):
                qty = p_list[i].get("qty", 1) if i < len(p_list) else 1
                orders_only_in_paper.append(
                    {
                        "instrument": key[0],
                        "side": key[1],
                        "decision_ts": key[2],
                        "qty": qty,
                        "cause": cause,
                        "formatted": f"{key[2].isoformat()} {key[0]} {key[1]} | qty={qty}",
                    }
                )

        elif diff < 0:
            abs_diff = abs(diff)
            if (key[2], key[0], key[1]) not in p_sig_set and not any(
                s[1] == key[0] and s[2] == key[1] and abs((s[0] - key[2]).total_seconds()) <= 1
                for s in p_sig_set
            ):
                cause = "no signal"
            elif p_stop is not None and key[2] >= p_stop:
                cause = "session end"
            else:
                cause = "unknown"

            order_cause_map_bt[key] = cause
            for i in range(abs_diff):
                qty = b_list[i].get("qty", 1) if i < len(b_list) else 1
                orders_only_in_backtest.append(
                    {
                        "instrument": key[0],
                        "side": key[1],
                        "decision_ts": key[2],
                        "qty": qty,
                        "cause": cause,
                        "formatted": f"{key[2].isoformat()} {key[0]} {key[1]} | qty={qty}",
                    }
                )

    # Match fills
    p_fill_map: dict[tuple[str, str, datetime], list[dict[str, Any]]] = {}
    b_fill_map: dict[tuple[str, str, datetime], list[dict[str, Any]]] = {}
    for f in p_fills:
        if f["decision_ts"] is not None:
            p_fill_map.setdefault((f["instrument"], f["side"], f["decision_ts"]), []).append(f)
    for f in b_fills:
        if f["decision_ts"] is not None:
            b_fill_map.setdefault((f["instrument"], f["side"], f["decision_ts"]), []).append(f)

    fills_only_in_paper: list[dict[str, Any]] = []
    fills_only_in_backtest: list[dict[str, Any]] = []

    all_fill_keys = set(p_fill_map.keys()) | set(b_fill_map.keys())
    for key in sorted(all_fill_keys, key=lambda k: (k[2], k[0], k[1])):
        p_flist = p_fill_map.get(key, [])
        b_flist = b_fill_map.get(key, [])
        fdiff = len(p_flist) - len(b_flist)

        if fdiff > 0:
            if key in order_cause_map_paper:
                cause = order_cause_map_paper[key]
            else:
                cause = "fill model (backtest unfilled or filled differently)"

            for i in range(fdiff):
                f_item = p_flist[i] if i < len(p_flist) else {}
                qty = f_item.get("qty", 1)
                px = f_item.get("price", "N/A")
                fmt_p = f"{key[2].isoformat()} {key[0]} {key[1]} | qty={qty} @ price={px}"
                fills_only_in_paper.append(
                    {
                        "instrument": key[0],
                        "side": key[1],
                        "decision_ts": key[2],
                        "qty": qty,
                        "price": px,
                        "cause": cause,
                        "formatted": fmt_p,
                    }
                )

        elif fdiff < 0:
            abs_fdiff = abs(fdiff)
            if key in order_cause_map_bt:
                cause = order_cause_map_bt[key]
            else:
                cause = "fill model (backtest unfilled or filled differently)"

            for i in range(abs_fdiff):
                f_item = b_flist[i] if i < len(b_flist) else {}
                qty = f_item.get("qty", 1)
                px = f_item.get("price", "N/A")
                fmt_b = f"{key[2].isoformat()} {key[0]} {key[1]} | qty={qty} @ price={px}"
                fills_only_in_backtest.append(
                    {
                        "instrument": key[0],
                        "side": key[1],
                        "decision_ts": key[2],
                        "qty": qty,
                        "price": px,
                        "cause": cause,
                        "formatted": fmt_b,
                    }
                )

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
        orders_only_in_paper=orders_only_in_paper,
        orders_only_in_backtest=orders_only_in_backtest,
        fills_only_in_paper=fills_only_in_paper,
        fills_only_in_backtest=fills_only_in_backtest,
    )


def explain_equivalence(
    rt: Any,
    session_id: str,
    instrument: str,
    hh_mm: str,
) -> str:
    """Print phone-width diagnostic report for a specific instrument and minute (UTC).

    Prints recorded ticks in that minute and adjacent minutes (before/after),
    the paper bar and backtest bar, whether each was timer-closed, and the signal
    each produced. Output is strictly <= 25 lines.
    """
    import glob

    import pyarrow.parquet as pq

    parts = hh_mm.strip().split(":")
    if len(parts) != 2:
        return f"Error: invalid time format '{hh_mm}', expected HH:MM"
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return f"Error: invalid time format '{hh_mm}', expected integers HH:MM"

    sess_dir = rt.paths.results / session_id
    if not sess_dir.is_dir() and Path(session_id).is_dir():
        sess_dir = Path(session_id)
    if not sess_dir.is_dir():
        return f"Error: session directory '{session_id}' not found"

    m_path = sess_dir / "manifest.json"
    if not m_path.is_file():
        return f"Error: manifest.json not found in '{sess_dir}'"
    m_data = json.loads(m_path.read_text(encoding="utf-8"))
    first_tick = m_data.get("first_tick_utc") or m_data.get("first_tick_recv_utc")
    if not first_tick:
        return "Error: first_tick_utc not found in manifest"
    sess_date = datetime.fromisoformat(first_tick).date()

    target_start = datetime(
        sess_date.year, sess_date.month, sess_date.day, hour, minute, tzinfo=UTC
    )
    target_end = target_start + timedelta(minutes=1)
    m_prev = target_start - timedelta(minutes=1)
    win_end = target_start + timedelta(minutes=2)

    sym = instrument.split(":")[-1]
    matches = glob.glob(
        f"{rt.paths.data}/ticks/**/symbol={sym}/**/date={sess_date}/*.parquet",
        recursive=True,
    )
    if not matches:
        matches = glob.glob(f"{sess_dir}/**/symbol={sym}/**/*.parquet", recursive=True)

    t_prev_len, t_prev_ltp = 0, "N/A"
    t_next_len, t_next_ltp = 0, "N/A"
    t_curr: list[dict[str, Any]] = []

    if matches:
        try:
            df = pq.read_table(matches[0]).to_pandas()
            if df["exch_ts"].dt.tz is None:
                df["exch_ts"] = df["exch_ts"].dt.tz_localize("UTC")
            else:
                df["exch_ts"] = df["exch_ts"].dt.tz_convert("UTC")

            sub_prev = df[(df["exch_ts"] >= m_prev) & (df["exch_ts"] < target_start)]
            t_prev_len = len(sub_prev)
            if t_prev_len > 0:
                t_prev_ltp = str(sub_prev["ltp"].iloc[-1])

            sub_curr = df[(df["exch_ts"] >= target_start) & (df["exch_ts"] < target_end)]
            t_curr = sub_curr.to_dict("records")

            sub_next = df[(df["exch_ts"] >= target_end) & (df["exch_ts"] < win_end)]
            t_next_len = len(sub_next)
            if t_next_len > 0:
                t_next_ltp = str(sub_next["ltp"].iloc[0])
        except Exception:
            pass

    p_events = load_journal_events(sess_dir)
    bt_dir = rt.paths.results / f"{session_id}_bt_eq"
    b_events = load_journal_events(bt_dir) if bt_dir.is_dir() else []

    p_tc = any(
        e.get("kind") == "bar.timer_close"
        and json.loads(e.get("payload_json", "{}")).get("instrument") == instrument
        and json.loads(e.get("payload_json", "{}")).get("end_ts") == target_end.isoformat()
        for e in p_events
    )

    def _find_sig(ev_list: list[dict[str, Any]]) -> dict[str, Any] | None:
        for e in ev_list:
            if e.get("kind") == "signal":
                raw_p = e.get("payload_json")
                if isinstance(raw_p, str):
                    p = json.loads(raw_p)
                else:
                    p = e.get("payload", {})
                if isinstance(p, dict) and p.get("instrument") == instrument:
                    t_str = p.get("ts") or e.get("ts_utc")
                    if t_str:
                        dt = datetime.fromisoformat(str(t_str)).astimezone(UTC)
                        if target_start <= dt < target_end + timedelta(seconds=2):
                            return cast(dict[str, Any], p)
        return None

    p_sig = _find_sig(p_events)
    b_sig = _find_sig(b_events)

    feed_bars = []
    try:
        from algodesk.data.tick_bar_feed import TickBarFeed

        feed = TickBarFeed(sess_dir, data_dir=rt.paths.data, timeframe="1m")
        feed_bars = feed.bars
    except Exception:
        pass

    curr_bar = next(
        (b for b in feed_bars if str(b.instrument) == instrument and b.start_ts == target_start),
        None,
    )

    lines: list[str] = []
    lines.append(f"=== Equivalence Explain: {instrument} at {hour:02d}:{minute:02d} UTC ===")
    lines.append(f"Session: {sess_dir.name}")
    lines.append("Ticks:")
    lines.append(f"  {m_prev.strftime('%H:%M')} (before): {t_prev_len} ticks, ltp={t_prev_ltp}")
    if len(t_curr) == 0:
        lines.append(f"  {target_start.strftime('%H:%M')} (target): 0 ticks (flat/gap minute)")
    elif len(t_curr) <= 6:
        lines.append(f"  {target_start.strftime('%H:%M')} (target, {len(t_curr)} ticks):")
        for r in t_curr:
            t_s = r["exch_ts"].strftime("%H:%M:%S")
            lines.append(
                f"    {t_s} ltp={r['ltp']} vol={r.get('volume', 0)} seq={r.get('seq', 'N/A')}"
            )
    else:
        first_r, last_r = t_curr[0], t_curr[-1]
        t_seq = f"seq={first_r.get('seq')}..{last_r.get('seq')}"
        lines.append(
            f"  {target_start.strftime('%H:%M')} (target): {len(t_curr)} ticks, "
            f"ltp={first_r['ltp']}..{last_r['ltp']}, {t_seq}"
        )
    lines.append(f"  {target_end.strftime('%H:%M')} (after):  {t_next_len} ticks, ltp={t_next_ltp}")

    lines.append("Bars:")
    if curr_bar:
        b_str = (
            f"O={curr_bar.open} H={curr_bar.high} "
            f"L={curr_bar.low} C={curr_bar.close} V={curr_bar.volume}"
        )
        lines.append(f"  Paper bar:    {b_str}")
        lines.append(f"  Backtest bar: {b_str}")
    else:
        lines.append("  Paper bar:    None")
        lines.append("  Backtest bar: None")
    lines.append(
        f"  Timer closed: paper={'Yes' if p_tc else 'No'}, backtest={'Yes' if p_tc else 'No'}"
    )

    lines.append("Signals:")
    p_sig_str = f"{p_sig['side']} ({p_sig.get('intent_id', '')})" if p_sig else "None"
    b_sig_str = f"{b_sig['side']} ({b_sig.get('intent_id', '')})" if b_sig else "None"
    lines.append(f"  Paper:    {p_sig_str}")
    lines.append(f"  Backtest: {b_sig_str}")

    p_s = p_sig["side"] if p_sig else None
    b_s = b_sig["side"] if b_sig else None
    if p_s == b_s:
        cause = "none (signals match)"
    elif p_tc and (not p_sig or not b_sig):
        cause = "timer close differs"
    elif curr_bar and curr_bar.volume == 0:
        cause = "timer close differs (flat bar)"
    else:
        cause = "bar content differs"
    lines.append(f"Diagnosis: {cause}")

    return "\n".join(lines[:25])
