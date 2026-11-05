"""Market closure inference, verification, and disagreement analysis."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from algodesk.api.errors import CalendarCoverageError, DataError
from algodesk.api.market import CoverageStatus, InstrumentId, SeriesKey
from algodesk.api.protocols import Calendar
from algodesk.data.manifest import DataManifest, MarketClosureRecord

if TYPE_CHECKING:
    from algodesk.data.service import DataService


def has_all_samples_evidence(rec: MarketClosureRecord | None) -> bool:
    """Return True if inferred closure has verified evidence from all sample symbols."""
    if rec is None or not rec.evidence:
        return False
    try:
        ev = json.loads(rec.evidence)
        if isinstance(ev, dict):
            if ev.get("all_samples") is True:
                return True
            missing = ev.get("missing_symbols", [])
            sample_count = ev.get("sample_count", 0)
            if sample_count > 0 and len(missing) >= sample_count:
                return True
            if missing and not sample_count:
                return True
            return False
    except Exception:
        pass
    ev_str = str(rec.evidence).lower()
    if "missing" in ev_str or "all" in ev_str:
        return True
    return False


def infer_market_closures(
    manifest: DataManifest,
    data_service: DataService,
    exchange: str,
    start_d: date,
    end_d: date,
    sample_symbols: Sequence[str],
    min_symbols: int = 1,
    calendar: Calendar | None = None,
    dry_run: bool = False,
    closure_max_pct: float | None = None,
) -> list[MarketClosureRecord]:
    """Infer market closures and special sessions across a date range using liquid symbols.

    - Calendar-open day is marked CLOSED (inferred) only when every sample symbol
      counts as 'no bar'.
    - A sample symbol counts as 'no bar' on a day only when that day's coverage is EMPTY
      from a fetch whose fetch_log outcome is ok and whose chunk returned at least one bar.
    - MISSING, failed or unfetched days never count.
    - infer fetches sample series first and aborts with a clear error if any sample chunk failed.
    - Safety cap closure_max_pct: if inferred closures exceed that share of expected sessions,
      write nothing and raise DataError.
    - Weekend or holiday day with bars from >= min_symbols is recorded as special session.
    - dry_run returns candidate records without writing to manifest.
    """
    effective_share = 8.0 if closure_max_pct is None else closure_max_pct
    exch = exchange.upper()
    tz = data_service._exchange_tz

    # UTC datetime range for fetching
    start_dt = datetime(start_d.year, start_d.month, start_d.day, 0, 0, 0, tzinfo=UTC)
    end_dt = datetime(end_d.year, end_d.month, end_d.day, 23, 59, 59, 999999, tzinfo=UTC)

    sym_bars_days: dict[str, set[date]] = defaultdict(set)
    valid_fetch_chunk_days: dict[str, set[date]] = defaultdict(set)
    cov_by_sym_day: dict[str, dict[str, str]] = defaultdict(dict)

    default_src = getattr(data_service, "_default_source", "yfinance")
    if not isinstance(default_src, str):
        default_src = "yfinance"

    # 1. Fetch sample series first and abort with a clear error if any chunk failed
    for raw_sym in sample_symbols:
        clean_sym = raw_sym if ":" in raw_sym else f"{exch}:EQ:{raw_sym}"
        s_key = SeriesKey(
            instrument=InstrumentId(clean_sym),
            kind="bars",
            timeframe="1d",
            adjustment="raw",
            source=default_src,
        )
        try:
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
                        sym_bars_days[raw_sym].add(bar_d)
        except Exception as exc:
            raise DataError(
                what=f"Sample series fetch failed for {raw_sym}: {exc}",
                where="infer_market_closures",
                fix="Ensure data source is accessible and retry",
            ) from exc

        # Check fetch_log for any failed chunk belonging to this symbol
        fetch_logs = manifest._conn.execute(
            "SELECT chunk, outcome, rows, error FROM fetch_log WHERE chunk LIKE ?",
            (f"{clean_sym}:{default_src}:1d:%",),
        ).fetchall()

        for r in fetch_logs:
            chunk_id, outcome, rows, err = r["chunk"], r["outcome"], r["rows"], r["error"]
            try:
                date_part = chunk_id.rsplit(":", 1)[1]
                c_start_s, c_end_s = date_part.split("_")
                c_start_d = date.fromisoformat(c_start_s)
                c_end_d = date.fromisoformat(c_end_s)
            except Exception:
                continue

            if c_end_d < start_d or c_start_d > end_d:
                continue

            outcome_u = outcome.upper() if outcome else ""
            if outcome_u not in ("OK", "EMPTY"):
                raise DataError(
                    what=(
                        f"Sample series fetch chunk failed for {raw_sym}: chunk '{chunk_id}' "
                        f"outcome '{outcome}' error: {err}"
                    ),
                    where="infer_market_closures",
                    fix="Check data source status and resolve quarantined or failed chunks",
                )
            if outcome_u == "EMPTY" and err:
                raise DataError(
                    what=(
                        f"Sample series fetch chunk failed for {raw_sym}: "
                        f"chunk '{chunk_id}' failed: {err}"
                    ),
                    where="infer_market_closures",
                    fix="Check data source status and retry",
                )

            if outcome_u == "OK" and rows > 0:
                c_cur = max(c_start_d, start_d)
                c_last = min(c_end_d, end_d)
                while c_cur <= c_last:
                    valid_fetch_chunk_days[raw_sym].add(c_cur)
                    c_cur += timedelta(days=1)

        ds_id = f"bars:{default_src}:1d:{clean_sym}:raw"
        cov_recs = manifest.get_coverage_days(
            ds_id,
            start_day=start_d.isoformat(),
            end_day=end_d.isoformat(),
        )
        for cr in cov_recs:
            cov_by_sym_day[raw_sym][cr.day] = cr.status

    inferred_candidates: list[tuple[date, str]] = []
    special_candidates: list[tuple[date, str]] = []
    expected_sessions = 0
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

        symbols_with_bars = [s for s in sample_symbols if cur in sym_bars_days[s]]
        symbols_no_bar: list[str] = []
        for s in sample_symbols:
            has_cov = bool(cov_by_sym_day[s])
            if has_cov:
                if (
                    cov_by_sym_day[s].get(day_str) == CoverageStatus.EMPTY
                    and cur in valid_fetch_chunk_days[s]
                ):
                    symbols_no_bar.append(s)
            else:
                if cur not in sym_bars_days[s]:
                    symbols_no_bar.append(s)

        if is_cal_open:
            expected_sessions += 1
            if len(symbols_with_bars) == 0 and len(symbols_no_bar) == len(sample_symbols):
                evidence = json.dumps(
                    {
                        "type": "inferred_closure",
                        "sample_count": len(sample_symbols),
                        "missing_symbols": sorted(symbols_no_bar),
                        "all_samples": True,
                    }
                )
                inferred_candidates.append((cur, evidence))
        else:
            if len(symbols_with_bars) >= min_symbols:
                evidence = json.dumps(
                    {
                        "type": "special_session",
                        "reporting_count": len(symbols_with_bars),
                        "reporting_symbols": sorted(symbols_with_bars),
                    }
                )
                special_candidates.append((cur, evidence))

        cur += one_day

    if effective_share is not None and expected_sessions >= 10:
        inferred_pct = (len(inferred_candidates) / expected_sessions) * 100.0
        if inferred_pct > effective_share:
            cap_msg = (
                f"Inferred closures ({len(inferred_candidates)} / {expected_sessions}, "
                f"{inferred_pct:.1f}%) exceed safety cap "
                f"data.closure_max_pct ({effective_share:.1f}%)"
            )
            raise DataError(
                what=cap_msg,
                where="infer_market_closures",
                fix=(
                    "Verify sample data feeds, increase cap via data.closure_max_pct parameter, "
                    "or clear erroneous closures"
                ),
            )

    now_iso = datetime.now(UTC).isoformat()
    recorded: list[MarketClosureRecord] = []

    if dry_run:
        for c_day, ev in inferred_candidates:
            recorded.append(
                MarketClosureRecord(
                    exchange=exch,
                    day=c_day.isoformat(),
                    kind="inferred",
                    evidence=ev,
                    created=now_iso,
                )
            )
        for c_day, ev in special_candidates:
            recorded.append(
                MarketClosureRecord(
                    exchange=exch,
                    day=c_day.isoformat(),
                    kind="special",
                    evidence=ev,
                    created=now_iso,
                )
            )
        return recorded

    for c_day, ev in inferred_candidates:
        c_iso = c_day.isoformat()
        existing = manifest.get_market_closure(exch, c_iso)
        if existing and existing.kind in ("exchange_file", "calendar"):
            recorded.append(existing)
            continue
        manifest.record_market_closure(
            exchange=exch,
            day=c_iso,
            kind="inferred",
            evidence=ev,
        )
        rec = manifest.get_market_closure(exch, c_iso)
        if rec:
            recorded.append(rec)

    for c_day, ev in special_candidates:
        manifest.record_market_closure(
            exchange=exch,
            day=c_day.isoformat(),
            kind="special",
            evidence=ev,
        )
        rec = manifest.get_market_closure(exch, c_day.isoformat())
        if rec:
            recorded.append(rec)

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
