"""Market closure inference, verification, and disagreement analysis."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any

from algodesk.api.errors import CalendarCoverageError
from algodesk.api.market import InstrumentId, SeriesKey
from algodesk.api.protocols import Calendar
from algodesk.data.manifest import DataManifest, MarketClosureRecord
from algodesk.data.service import DataService


def infer_market_closures(
    manifest: DataManifest,
    data_service: DataService,
    exchange: str,
    start_d: date,
    end_d: date,
    sample_symbols: Sequence[str],
    min_symbols: int = 1,
    calendar: Calendar | None = None,
) -> list[MarketClosureRecord]:
    """Infer market closures and special sessions across a date range using liquid symbols.

    - Calendar-open day is marked CLOSED (inferred) only when every sample symbol returns no bar.
    - Weekend or holiday day with bars from at least min_symbols is recorded as a special session.
    - A single symbol with no bar is never a closure.
    """
    exch = exchange.upper()
    tz = data_service._exchange_tz

    # Fetch 1d bars for all sample symbols across [start_d, end_d]
    # Represent start_d at 00:00 and end_d at 23:59:59
    start_dt = datetime(start_d.year, start_d.month, start_d.day, 0, 0, 0, tzinfo=tz)
    end_dt = datetime(end_d.year, end_d.month, end_d.day, 23, 59, 59, tzinfo=tz)

    bars_by_day_and_sym: dict[date, dict[str, list[Any]]] = defaultdict(lambda: defaultdict(list))

    for sym in sample_symbols:
        try:
            s_key = SeriesKey(
                instrument=InstrumentId(sym),
                kind="bars",
                timeframe="1d",
                adjustment="raw",
                source=getattr(data_service, "_default_source", "yfinance"),
            )
            d_slice = data_service.get(
                s_key,
                start=start_dt,
                end=end_dt,
                download="raw",
            )
            for bar in d_slice.bars:
                bar_ts = getattr(bar, "start_ts", getattr(bar, "start", None))
                if bar_ts is not None:
                    bar_d = bar_ts.astimezone(tz).date()
                    if start_d <= bar_d <= end_d:
                        bars_by_day_and_sym[bar_d][sym].append(bar)
        except Exception:
            # If fetching fails for a symbol, treat as 0 bars for that symbol
            pass

    recorded: list[MarketClosureRecord] = []
    cur = start_d
    one_day = timedelta(days=1)

    while cur <= end_d:
        day_str = cur.isoformat()
        is_cal_open = True
        if calendar is not None:
            try:
                is_cal_open = calendar.is_trading_day(cur)
            except CalendarCoverageError:
                is_cal_open = cur.weekday() < 5
        else:
            is_cal_open = cur.weekday() < 5

        day_sym_map = bars_by_day_and_sym.get(cur, {})
        symbols_with_bars = [s for s in sample_symbols if len(day_sym_map.get(s, [])) > 0]
        symbols_missing_bars = [s for s in sample_symbols if len(day_sym_map.get(s, [])) == 0]

        if is_cal_open:
            # Calendar expects day to be open: if all sample symbols have NO bar -> inferred closure
            if len(symbols_with_bars) == 0 and len(sample_symbols) > 0:
                evidence = json.dumps(
                    {
                        "type": "inferred_closure",
                        "sample_count": len(sample_symbols),
                        "missing_symbols": symbols_missing_bars,
                    }
                )
                manifest.record_market_closure(
                    exchange=exch,
                    day=day_str,
                    kind="inferred",
                    evidence=evidence,
                )
                rec = manifest.get_market_closure(exch, day_str)
                if rec:
                    recorded.append(rec)
        else:
            # Calendar expects day to be closed (weekend or holiday)
            # If bars exist from at least min_symbols -> special session
            if len(symbols_with_bars) >= min_symbols:
                evidence = json.dumps(
                    {
                        "type": "special_session",
                        "reporting_count": len(symbols_with_bars),
                        "reporting_symbols": symbols_with_bars,
                    }
                )
                manifest.record_market_closure(
                    exchange=exch,
                    day=day_str,
                    kind="special",
                    evidence=evidence,
                )
                rec = manifest.get_market_closure(exch, day_str)
                if rec:
                    recorded.append(rec)

        cur += one_day

    return recorded


def check_market_closure_disagreements(
    manifest: DataManifest,
    calendar: Calendar | None,
    exchange: str,
    start_d: date | None = None,
    end_d: date | None = None,
) -> list[dict[str, Any]]:
    """List disagreements between calendar rules and recorded closures / data evidence.

    Disagreements:
    1. Calendar open but inferred closed (missing holiday in calendar).
    2. Calendar closed but bars exist or special session recorded
       (wrong holiday or special session).
    """
    exch = exchange.upper()
    start_str = start_d.isoformat() if start_d else None
    end_str = end_d.isoformat() if end_d else None

    closures = manifest.list_market_closures(
        exchange=exch,
        start_day=start_str,
        end_day=end_str,
    )

    disagreements: list[dict[str, Any]] = []

    for c in closures:
        c_day = date.fromisoformat(c.day)
        is_cal_open = True
        cal_status_str = "open"
        if calendar is not None:
            try:
                is_cal_open = calendar.is_trading_day(c_day)
                cal_status_str = "open" if is_cal_open else "closed"
            except CalendarCoverageError:
                is_cal_open = c_day.weekday() < 5
                cal_status_str = "open" if is_cal_open else "closed (weekend)"
        else:
            is_cal_open = c_day.weekday() < 5
            cal_status_str = "open" if is_cal_open else "closed (weekend)"

        if c.kind == "inferred":
            # Inferred closed
            if is_cal_open:
                disagreements.append(
                    {
                        "exchange": exch,
                        "day": c.day,
                        "calendar": cal_status_str,
                        "data_evidence": "CLOSED (inferred)",
                        "disagreement_type": "missing holiday (calendar open but inferred closed)",
                        "evidence": c.evidence,
                    }
                )
        elif c.kind == "special":
            # Special session (open on weekend / holiday)
            if not is_cal_open:
                disagreements.append(
                    {
                        "exchange": exch,
                        "day": c.day,
                        "calendar": cal_status_str,
                        "data_evidence": "OPEN (special session)",
                        "disagreement_type": "special session (calendar closed but bars exist)",
                        "evidence": c.evidence,
                    }
                )

    # Check if calendar declares a holiday but coverage_days has COMPLETE bars > 0
    if calendar is not None and start_d and end_d:
        cur = start_d
        one_day = timedelta(days=1)
        special_closure_days = {c.day for c in closures if c.kind == "special"}
        while cur <= end_d:
            cur_str = cur.isoformat()
            if cur_str not in special_closure_days:
                try:
                    cal_open = calendar.is_trading_day(cur)
                except CalendarCoverageError:
                    cal_open = cur.weekday() < 5
                if not cal_open:
                    # Check if any dataset has COMPLETE coverage with rows > 0 for this exchange
                    day_rows = manifest._conn.execute(
                        "SELECT dataset_id FROM coverage_days "
                        "WHERE day = ? AND status = 'COMPLETE' AND rows > 0",
                        (cur_str,),
                    ).fetchall()
                    has_bars = any(exch in r[0].upper() for r in day_rows)
                    if has_bars:
                        disagreements.append(
                            {
                                "exchange": exch,
                                "day": cur_str,
                                "calendar": "closed",
                                "data_evidence": "OPEN (bars found in dataset)",
                                "disagreement_type": (
                                    "wrong holiday or special session "
                                    "(calendar closed but bars exist)"
                                ),
                                "evidence": "",
                            }
                        )
            cur += one_day

    return disagreements
