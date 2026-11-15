"""Day-level coverage index and gap detection using the Calendar Protocol."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from algodesk.api.errors import CalendarCoverageError
from algodesk.api.market import CoverageStatus, DataGap
from algodesk.api.protocols import Calendar
from algodesk.data.calendar_cache import CachedCalendar
from algodesk.data.closures import has_all_samples_evidence
from algodesk.data.manifest import CoverageDayRecord, DataManifest, MarketClosureRecord


def is_trading_session(
    day: date,
    calendar: Any = None,
    manifest: Any = None,
    exchange: str = "NSE",
    closures: Mapping[str, MarketClosureRecord] | None = None,
) -> bool:
    """Check if date is an expected trading session.

    Uses calendar + recorded closures (exchange_file, inferred, special).
    """
    day_str = day.isoformat()
    closure_rec: MarketClosureRecord | None = None
    if closures is not None:
        closure_rec = closures.get(day_str)
    elif manifest is not None and hasattr(manifest, "list_market_closures"):
        matches = manifest.list_market_closures(
            exchange=exchange, start_day=day_str, end_day=day_str
        )
        if matches:
            closure_rec = matches[0]

    closure_kind = closure_rec.kind if closure_rec else None

    # Special session overrides calendar/weekend closure
    if closure_kind == "special":
        return True
    if closure_kind in ("calendar", "exchange_file"):
        return False
    if closure_kind == "inferred":
        if has_all_samples_evidence(closure_rec):
            return False

    if calendar is not None:
        try:
            return bool(calendar.is_trading_day(day))
        except CalendarCoverageError:
            return day.weekday() < 5
        except Exception:
            return day.weekday() < 5

    return day.weekday() < 5


def resolve_trading_sessions(
    end_date: date,
    needed_sessions: int,
    calendar: Any = None,
    manifest: Any = None,
    exchange: str = "NSE",
    closures: Mapping[str, MarketClosureRecord] | None = None,
) -> list[date]:
    """Find the latest N trading sessions on or before end_date."""
    max_search_days = max(120, needed_sessions * 4)
    cutoff = end_date - timedelta(days=max_search_days)

    closures_dict: dict[str, MarketClosureRecord] = {}
    if closures is not None:
        closures_dict = dict(closures)
    elif manifest is not None and hasattr(manifest, "list_market_closures"):
        for mc in manifest.list_market_closures(
            exchange=exchange,
            start_day=cutoff.isoformat(),
            end_day=end_date.isoformat(),
        ):
            closures_dict[mc.day] = mc

    sessions: list[date] = []
    cur = end_date
    while cur >= cutoff and len(sessions) < needed_sessions:
        if is_trading_session(
            cur,
            calendar=calendar,
            manifest=manifest,
            exchange=exchange,
            closures=closures_dict,
        ):
            sessions.append(cur)
        cur -= timedelta(days=1)
    return sessions


class CoverageIndex:
    """Evaluates trading day completeness, settlement margins, and data gaps."""

    __slots__ = ("_calendar", "_manifest", "_settle_delay_m", "_last_closure_counts", "_params")

    def __init__(
        self,
        manifest: DataManifest,
        calendar: Calendar | None = None,
        settle_minutes: int | None = None,
        params: Any = None,
    ) -> None:
        self._manifest = manifest
        self._calendar = CachedCalendar.wrap(calendar)
        self._settle_delay_m = settle_minutes if settle_minutes is not None else 0
        self._params = params
        self._last_closure_counts: dict[str, int] = {
            "closed_calendar": 0,
            "closed_inferred": 0,
            "special_sessions": 0,
        }

    @property
    def calendar(self) -> Calendar | None:
        return self._calendar

    @property
    def settle_minutes(self) -> int:
        return self._settle_delay_m

    @property
    def params(self) -> Any:
        return self._params

    @property
    def last_closure_counts(self) -> dict[str, int]:
        return dict(self._last_closure_counts)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Allow CoverageIndex to be invoked safely as a callable.

        If called with no arguments (e.g. store.coverage_index()), returns self.
        If called with arguments, delegates to evaluate_day_status(*args, **kwargs).
        """
        if not args and not kwargs:
            return self
        return self.evaluate_day_status(*args, **kwargs)

    def is_empty_final(self, rec: CoverageDayRecord, now_dt: datetime | None = None) -> bool:
        """Check whether an existing EMPTY coverage record is final or stale."""
        return is_empty_record_final(
            rec=rec,
            calendar=self._calendar,
            settle_minutes=self._settle_delay_m,
            manifest=self._manifest,
            now_dt=now_dt,
            params=self._params,
        )

    def _resolve_exchange(self, dataset_id: str) -> str:
        u = dataset_id.upper()
        if "BSE" in u:
            return "BSE"
        if "MCX" in u:
            return "MCX"
        return "NSE"

    def evaluate_day_status(
        self,
        dataset_id: str,
        day: date,
        has_bars: bool,
        as_of: datetime | None = None,
    ) -> CoverageStatus:
        """Evaluate status (COMPLETE, PARTIAL, CLOSED, EMPTY) against calendar and settle margin."""
        now_utc = as_of or datetime.now(UTC)
        exch = self._resolve_exchange(dataset_id)
        day_str = day.isoformat()

        # Check market closures in manifest first
        if hasattr(self._manifest, "get_market_closure"):
            closure = self._manifest.get_market_closure(exch, day_str)
            if closure is not None:
                if closure.kind in ("inferred", "calendar"):
                    return CoverageStatus.CLOSED
                if closure.kind == "special":
                    # Special trading session is open
                    pass

        # 1. Check if calendar marks this day closed
        if self._calendar is not None:
            try:
                is_open = self._calendar.is_trading_day(day)
            except Exception:
                is_open = day.weekday() < 5
            if not is_open:
                return CoverageStatus.CLOSED
            try:
                sess_list = self._calendar.sessions(day)
                if not sess_list:
                    return CoverageStatus.CLOSED
                session_end = sess_list[-1].end
            except Exception:
                session_end = datetime(day.year, day.month, day.day, 10, 0, tzinfo=UTC)
        else:
            if day.weekday() >= 5:
                return CoverageStatus.CLOSED
            # Default NSE close is 15:30 IST (10:00 UTC)
            session_end = datetime(day.year, day.month, day.day, 10, 0, tzinfo=UTC)

        settle_deadline = session_end + timedelta(minutes=self._settle_delay_m)

        if not has_bars:
            if now_utc >= settle_deadline:
                return CoverageStatus.EMPTY
            return CoverageStatus.PARTIAL

        if now_utc >= settle_deadline:
            return CoverageStatus.COMPLETE

        return CoverageStatus.PARTIAL

    def is_trading_session(self, day: date, exchange: str = "NSE") -> bool:
        """Check if date is an expected trading session (calendar + closures)."""
        return is_trading_session(
            day=day,
            calendar=self._calendar,
            manifest=self._manifest,
            exchange=exchange,
        )

    def detect_gaps(
        self,
        dataset_id: str,
        start_day: date,
        end_day: date,
    ) -> list[DataGap]:
        """Detect missing or incomplete days across expected trading sessions."""
        cov_records = {
            r.day: r
            for r in self._manifest.get_coverage_days(
                dataset_id,
                start_day=start_day.isoformat(),
                end_day=end_day.isoformat(),
            )
        }

        exch = self._resolve_exchange(dataset_id)
        closures_records: dict[str, MarketClosureRecord] = {}
        if hasattr(self._manifest, "list_market_closures"):
            for mc in self._manifest.list_market_closures(
                exchange=exch,
                start_day=start_day.isoformat(),
                end_day=end_day.isoformat(),
            ):
                closures_records[mc.day] = mc

        gaps: list[DataGap] = []
        cur = start_day
        one_day = timedelta(days=1)

        closed_cal_count = 0
        closed_inf_count = 0
        special_count = 0

        while cur <= end_day:
            day_str = cur.isoformat()
            closure_rec = closures_records.get(day_str)
            closure_kind = closure_rec.kind if closure_rec else None

            is_session = is_trading_session(
                day=cur,
                calendar=self._calendar,
                closures=closures_records,
                exchange=exch,
            )

            if closure_kind == "special":
                special_count += 1
            elif not is_session:
                if closure_kind == "inferred":
                    closed_inf_count += 1
                else:
                    closed_cal_count += 1

            if is_session:
                rec = cov_records.get(day_str)
                if rec is None:
                    gaps.append(
                        DataGap(
                            day=cur,
                            status=CoverageStatus.MISSING,
                            reason="No coverage record in manifest",
                        )
                    )
                elif rec.status == CoverageStatus.EMPTY:
                    final, reason = evaluate_empty_record(
                        rec=rec,
                        calendar=self._calendar,
                        settle_minutes=self._settle_delay_m,
                        manifest=self._manifest,
                        params=self._params,
                    )
                    if final:
                        if "pre-listing" in reason.lower():
                            pass
                        else:
                            gaps.append(
                                DataGap(
                                    day=cur,
                                    status=CoverageStatus.EMPTY,
                                    reason=rec.note or "Fetched and the source returned nothing",
                                )
                            )
                    else:
                        gaps.append(
                            DataGap(
                                day=cur,
                                status=CoverageStatus.MISSING,
                                reason=f"Stale EMPTY record ({reason})",
                            )
                        )
                elif rec.status == "PENDING":
                    gaps.append(
                        DataGap(
                            day=cur,
                            status=CoverageStatus.MISSING,
                            reason=rec.note or "Pending next-day history retry",
                        )
                    )
                elif rec.status not in (CoverageStatus.COMPLETE, CoverageStatus.CLOSED):
                    try:
                        gap_st = CoverageStatus(rec.status)
                    except ValueError:
                        gap_st = CoverageStatus.MISSING
                    gaps.append(
                        DataGap(
                            day=cur,
                            status=gap_st,
                            reason=rec.note or f"Day status is {rec.status}",
                        )
                    )

            cur += one_day

        self._last_closure_counts = {
            "closed_calendar": closed_cal_count,
            "closed_inferred": closed_inf_count,
            "special_sessions": special_count,
        }

        return gaps


def evaluate_empty_record(
    rec: CoverageDayRecord,
    calendar: Calendar | None = None,
    settle_minutes: int | None = None,
    manifest: DataManifest | None = None,
    now_dt: datetime | None = None,
    params: Any = None,
    preloaded_coverage: Mapping[str, CoverageDayRecord] | None = None,
) -> tuple[bool, str]:
    """Evaluate whether an EMPTY coverage record is final or stale/invalid.

    EMPTY is final only when:
    1. The fetch ran after the session ended plus data.settle_minutes.
    2. The response was a successful empty chunk for that exact IST date window.
    3. The source actually serves same-day history (or fetch was on a later date).
    """
    if rec.status == "PENDING":
        return False, "Pending next-day history retry"
    if rec.status != CoverageStatus.EMPTY:
        return True, "Not EMPTY status"
    if not rec.fetched_at:
        return False, "Fetch timestamp missing"

    calendar = CachedCalendar.wrap(calendar)

    try:
        fa = datetime.fromisoformat(rec.fetched_at)
        fa_utc = fa.astimezone(UTC) if fa.tzinfo else fa.replace(tzinfo=UTC)
    except Exception:
        return False, f"Invalid fetched_at timestamp: {rec.fetched_at}"

    # Check same-day history policy for the source
    parts = rec.dataset_id.split(":")
    source = parts[1].lower() if len(parts) >= 2 else ""
    same_day_served = True
    if params is not None:
        try:
            val = params.get(f"source.{source}.same_day_history")
            if val is not None:
                if isinstance(val, bool):
                    same_day_served = val
                elif isinstance(val, str):
                    same_day_served = val.strip().lower() in ("true", "1", "yes")
                else:
                    same_day_served = bool(val)
        except Exception:
            same_day_served = source != "upstox"
    elif source == "upstox":
        same_day_served = False

    day_d = date.fromisoformat(rec.day)
    session_end: datetime | None = None
    if calendar is not None:
        try:
            sess_list = calendar.sessions(day_d)
            if sess_list:
                session_end = sess_list[-1].end.astimezone(UTC)
        except Exception:
            pass

    if session_end is None:
        session_end = datetime(day_d.year, day_d.month, day_d.day, 10, 0, tzinfo=UTC)

    settle_delay_m = settle_minutes if settle_minutes is not None else 0
    settle_deadline = session_end + timedelta(minutes=settle_delay_m)
    if fa_utc < settle_deadline:
        return False, f"Fetched before session end + {settle_delay_m}m settle margin"

    tf = parts[2].lower() if len(parts) >= 3 else ""
    if not same_day_served and tf not in ("1d", "daily"):
        from zoneinfo import ZoneInfo

        fa_ist = fa_utc.astimezone(ZoneInfo("Asia/Kolkata"))
        if day_d == fa_ist.date():
            return False, f"Same-day history not served by source {source}"

    note_low = (rec.note or "").lower()
    if any(
        w in note_low
        for w in (
            "fetch failed",
            "error",
            "failed",
            "retries",
            "errno",
            "exception",
            "traceback",
            "timed out",
            "timeout",
            "unreachable",
            "nodename",
            "object is not callable",
            "has no attribute",
            "crash",
            "typeerror",
            "attributeerror",
            "valueerror",
        )
    ):
        return False, f"Fetch error: {rec.note}"

    if len(parts) >= 3:
        tf = parts[2].lower()
        if source == "yfinance" and tf in ("1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h"):
            cutoff = datetime(2026, 11, 10, 0, 0, tzinfo=UTC)
            if fa_utc < cutoff:
                return False, "Old yfinance intraday date window (pre-M2-11)"

    if manifest is not None:
        if hasattr(manifest, "check_fetch_log_ok"):
            ok, err = manifest.check_fetch_log_ok(rec.fetched_at)
            if not ok:
                return False, f"Fetch log error: {err}"
        elif hasattr(manifest, "_conn"):
            try:
                cur = manifest._conn.execute(
                    "SELECT outcome, error FROM fetch_log WHERE finished = ?", (rec.fetched_at,)
                )
                for row in cur.fetchall():
                    if row["outcome"] != "OK" or (row["error"] and row["error"].strip()):
                        return False, f"Fetch log error: {row['error'] or row['outcome']}"
            except Exception:
                pass

    # ADR-076: EMPTY is final only if (a) the same symbol has bars on a later date (pre-listing)
    # or (b) the empty range is at most registry data.empty_final_max_sessions (default 5).
    allowed_empty_sessions = 5
    if params is not None:
        try:
            val = params.get("data.empty_final_max_sessions")
            if val is not None:
                allowed_empty_sessions = int(val)
        except Exception:
            allowed_empty_sessions = 5

    if preloaded_coverage is not None:
        complete_days = [
            r.day
            for r in preloaded_coverage.values()
            if (r.rows > 0 or r.status == CoverageStatus.COMPLETE)
        ]
        has_earlier = any(d <= rec.day for d in complete_days)
        if not has_earlier:
            has_later = any(d > rec.day for d in complete_days)
            if has_later:
                return True, "Final EMPTY: pre-listing days (symbol has bars on later date)"

        earlier_days = [d for d in complete_days if d < rec.day]
        later_days = [d for d in complete_days if d > rec.day]
        last_bar_day = max(earlier_days) if earlier_days else None
        next_bar_day = min(later_days) if later_days else None

        if last_bar_day and next_bar_day:
            empty_count = sum(
                1
                for r in preloaded_coverage.values()
                if r.status in (CoverageStatus.EMPTY, "PENDING")
                and last_bar_day < r.day < next_bar_day
            )
        elif last_bar_day:
            empty_count = sum(
                1
                for r in preloaded_coverage.values()
                if r.status in (CoverageStatus.EMPTY, "PENDING") and r.day > last_bar_day
            )
        elif next_bar_day:
            empty_count = sum(
                1
                for r in preloaded_coverage.values()
                if r.status in (CoverageStatus.EMPTY, "PENDING") and r.day < next_bar_day
            )
        else:
            empty_count = sum(
                1
                for r in preloaded_coverage.values()
                if r.status in (CoverageStatus.EMPTY, "PENDING")
            )

        if empty_count <= allowed_empty_sessions:
            return (
                True,
                f"Final EMPTY: range length {empty_count} <= allowed {allowed_empty_sessions}",
            )
        return (
            False,
            f"Non-final EMPTY: range length {empty_count} > allowed {allowed_empty_sessions}",
        )

    sym = parts[-2] if len(parts) >= 4 else (parts[1] if len(parts) >= 2 else "")
    if manifest is not None and hasattr(manifest, "_conn"):
        try:
            # Check (a): bars on later date AND no bars on or before rec.day (pre-listing)
            q_earlier = (
                "SELECT 1 FROM coverage_days WHERE dataset_id LIKE ? "
                "AND day <= ? AND (rows > 0 OR status = 'COMPLETE') LIMIT 1"
            )
            has_earlier = (
                manifest._conn.execute(q_earlier, (f"%{sym}%", rec.day)).fetchone() is not None
            )
            if not has_earlier:
                q_p_earlier = (
                    "SELECT 1 FROM parts "
                    "WHERE dataset_id LIKE ? AND first_ts <= ? AND rows > 0 LIMIT 1"
                )
                cur_parts_earlier = manifest._conn.execute(q_p_earlier, (f"%{sym}%", rec.day))
                has_earlier = cur_parts_earlier.fetchone() is not None

            if not has_earlier:
                q_later = (
                    "SELECT 1 FROM coverage_days WHERE dataset_id LIKE ? "
                    "AND day > ? AND (rows > 0 OR status = 'COMPLETE') LIMIT 1"
                )
                cur_later = manifest._conn.execute(q_later, (f"%{sym}%", rec.day))
                if cur_later.fetchone() is not None:
                    return True, "Final EMPTY: pre-listing days (symbol has bars on later date)"

                q_parts = (
                    "SELECT 1 FROM parts "
                    "WHERE dataset_id LIKE ? AND last_ts > ? AND rows > 0 LIMIT 1"
                )
                cur_parts = manifest._conn.execute(q_parts, (f"%{sym}%", rec.day))
                if cur_parts.fetchone() is not None:
                    return True, "Final EMPTY: pre-listing days (symbol has bars on later date)"

            # Check (b): empty range is at most allowed_empty_sessions
            q_last = (
                "SELECT max(day) FROM coverage_days WHERE dataset_id LIKE ? "
                "AND (rows > 0 OR status = 'COMPLETE') AND day < ?"
            )
            cur_last_bar = manifest._conn.execute(q_last, (f"%{sym}%", rec.day))
            last_bar_day = cur_last_bar.fetchone()[0]

            q_next = (
                "SELECT min(day) FROM coverage_days WHERE dataset_id LIKE ? "
                "AND (rows > 0 OR status = 'COMPLETE') AND day > ?"
            )
            cur_next_bar = manifest._conn.execute(q_next, (f"%{sym}%", rec.day))
            next_bar_day = cur_next_bar.fetchone()[0]

            if last_bar_day and next_bar_day:
                q_cnt = (
                    "SELECT count(*) FROM coverage_days WHERE dataset_id = ? "
                    "AND status IN ('EMPTY', 'PENDING') AND day > ? AND day < ?"
                )
                cur_cnt = manifest._conn.execute(
                    q_cnt, (rec.dataset_id, last_bar_day, next_bar_day)
                )
                empty_count = cur_cnt.fetchone()[0] or 0
            elif last_bar_day:
                q_cnt = (
                    "SELECT count(*) FROM coverage_days WHERE dataset_id = ? "
                    "AND status IN ('EMPTY', 'PENDING') AND day > ?"
                )
                cur_cnt = manifest._conn.execute(q_cnt, (rec.dataset_id, last_bar_day))
                empty_count = cur_cnt.fetchone()[0] or 0
            elif next_bar_day:
                q_cnt = (
                    "SELECT count(*) FROM coverage_days WHERE dataset_id = ? "
                    "AND status IN ('EMPTY', 'PENDING') AND day < ?"
                )
                cur_cnt = manifest._conn.execute(q_cnt, (rec.dataset_id, next_bar_day))
                empty_count = cur_cnt.fetchone()[0] or 0
            else:
                q_cnt = (
                    "SELECT count(*) FROM coverage_days WHERE dataset_id = ? "
                    "AND status IN ('EMPTY', 'PENDING')"
                )
                cur_cnt = manifest._conn.execute(q_cnt, (rec.dataset_id,))
                empty_count = cur_cnt.fetchone()[0] or 0

            if empty_count <= allowed_empty_sessions:
                return (
                    True,
                    (
                        f"Final EMPTY: short empty range "
                        f"({empty_count} <= {allowed_empty_sessions} sessions)"
                    ),
                )
            if next_bar_day:
                return (
                    False,
                    f"Empty gap ({empty_count} sessions) exceeds "
                    f"data.empty_final_max_sessions ({allowed_empty_sessions})",
                )
            return (
                False,
                f"All-empty range ({empty_count} sessions) exceeds "
                f"data.empty_final_max_sessions ({allowed_empty_sessions}) without later bars",
            )
        except Exception:
            pass

    return True, "Final EMPTY chunk"


def is_empty_record_final(
    rec: CoverageDayRecord,
    calendar: Calendar | None = None,
    settle_minutes: int | None = None,
    manifest: DataManifest | None = None,
    now_dt: datetime | None = None,
    params: Any = None,
    preloaded_coverage: Mapping[str, CoverageDayRecord] | None = None,
) -> bool:
    """Return True if the EMPTY record is validly final, False if stale/invalid."""
    is_final, _ = evaluate_empty_record(
        rec=rec,
        calendar=calendar,
        settle_minutes=settle_minutes,
        manifest=manifest,
        now_dt=now_dt,
        params=params,
        preloaded_coverage=preloaded_coverage,
    )
    return is_final


def reset_stale_empty_coverage(
    manifest: DataManifest,
    calendar: Calendar | None = None,
    settle_minutes: int | None = None,
    source: str | None = None,
    timeframe: str | None = None,
    dry_run: bool = False,
    journal: Any = None,
    params: Any = None,
    progress_callback: Any = None,
) -> list[tuple[CoverageDayRecord, str]]:
    """Re-evaluate existing EMPTY coverage records and reset non-final ones to MISSING.

    Uses bulk reads (one query per table for the selection) and tracks progress per symbol.
    """
    if not hasattr(manifest, "_conn"):
        return []

    # 1. coverage_days: All EMPTY or PENDING records matching criteria
    cur = manifest._conn.execute(
        "SELECT dataset_id, day, status, rows, expected_rows, part_path, fetched_at, note "
        "FROM coverage_days WHERE status IN ('EMPTY', 'PENDING') ORDER BY dataset_id, day"
    )
    all_ep_rows = cur.fetchall()

    filtered_records: list[CoverageDayRecord] = []
    for r in all_ep_rows:
        rec = CoverageDayRecord(**dict(r))
        parts = rec.dataset_id.split(":")
        r_source = parts[1].lower() if len(parts) >= 2 else ""
        r_tf = parts[2].lower() if len(parts) >= 3 else ""

        if source and r_source != source.strip().lower():
            continue
        if timeframe and r_tf != timeframe.strip().lower():
            continue
        filtered_records.append(rec)

    if not filtered_records:
        return []

    # 2. coverage_days: summary of COMPLETE / rows > 0 per dataset
    cur_bars = manifest._conn.execute(
        "SELECT dataset_id, min(day) as min_day, max(day) as max_day, sum(rows) as total_bars "
        "FROM coverage_days "
        "WHERE status = 'COMPLETE' OR rows > 0 "
        "GROUP BY dataset_id"
    )
    symbol_total_bars: dict[str, int] = {}
    symbol_min_bar_day: dict[str, str] = {}
    symbol_max_bar_day: dict[str, str] = {}
    for r in cur_bars.fetchall():
        ds = r["dataset_id"]
        p = ds.split(":")
        s = p[-2] if len(p) >= 4 else (p[1] if len(p) >= 2 else ds)
        t_bars = r["total_bars"] or 0
        symbol_total_bars[s] = symbol_total_bars.get(s, 0) + t_bars
        m_day = r["max_day"] or ""
        if s not in symbol_max_bar_day or m_day > symbol_max_bar_day[s]:
            symbol_max_bar_day[s] = m_day
        min_d = r["min_day"] or ""
        if s not in symbol_min_bar_day or (min_d and min_d < symbol_min_bar_day[s]):
            symbol_min_bar_day[s] = min_d

    # 3. coverage_days: All EMPTY/PENDING days per dataset
    cur_days = manifest._conn.execute(
        "SELECT dataset_id, day FROM coverage_days WHERE status IN ('EMPTY', 'PENDING')"
    )
    dataset_ep_days: dict[str, list[str]] = {}
    for r in cur_days.fetchall():
        dataset_ep_days.setdefault(r["dataset_id"], []).append(r["day"])

    # 4. parts: Min first_ts and Max last_ts per dataset from parts
    q_parts_range = (
        "SELECT dataset_id, min(first_ts) as min_ts, max(last_ts) as max_ts "
        "FROM parts WHERE rows > 0 GROUP BY dataset_id"
    )
    cur_parts = manifest._conn.execute(q_parts_range)
    symbol_min_part_ts: dict[str, str] = {}
    symbol_max_part_ts: dict[str, str] = {}
    for r in cur_parts.fetchall():
        ds = r["dataset_id"]
        p = ds.split(":")
        s = p[-2] if len(p) >= 4 else (p[1] if len(p) >= 2 else ds)
        m_ts = r["max_ts"] or ""
        if s not in symbol_max_part_ts or m_ts > symbol_max_part_ts[s]:
            symbol_max_part_ts[s] = m_ts
        min_ts = r["min_ts"] or ""
        if s not in symbol_min_part_ts or (min_ts and min_ts < symbol_min_part_ts[s]):
            symbol_min_part_ts[s] = min_ts

    # 5. fetch_log: Non-OK entries
    cur_fl = manifest._conn.execute(
        "SELECT finished, outcome, error FROM fetch_log "
        "WHERE outcome != 'OK' OR (error IS NOT NULL AND trim(error) != '')"
    )
    bad_fetch_logs: dict[str, str] = {
        r["finished"]: r["error"] or r["outcome"] or "fetch error" for r in cur_fl.fetchall()
    }

    # Parameters
    allowed_empty_sessions = 5
    if params is not None:
        try:
            val = params.get("data.empty_final_max_sessions")
            if val is not None:
                allowed_empty_sessions = int(val)
        except Exception:
            pass

    progress_every = 25
    if params is not None:
        try:
            val = params.get("cli.progress_every")
            if val is not None:
                progress_every = int(val)
        except Exception:
            pass

    cal_wrapped = CachedCalendar.wrap(calendar)
    s_margin = settle_minutes if settle_minutes is not None else 0

    # Collect unique symbols
    symbols_order: list[str] = []
    seen_symbols: set[str] = set()
    for rec in filtered_records:
        p = rec.dataset_id.split(":")
        s = p[-2] if len(p) >= 4 else (p[1] if len(p) >= 2 else rec.dataset_id)
        if s not in seen_symbols:
            seen_symbols.add(s)
            symbols_order.append(s)

    total_symbols = len(symbols_order)
    processed_symbols: set[str] = set()

    to_reset: list[tuple[CoverageDayRecord, str]] = []
    for rec in filtered_records:
        p = rec.dataset_id.split(":")
        sym = p[-2] if len(p) >= 4 else (p[1] if len(p) >= 2 else rec.dataset_id)

        if sym not in processed_symbols:
            processed_symbols.add(sym)
            cur_count = len(processed_symbols)
            if cur_count % progress_every == 0 or cur_count == total_symbols:
                if progress_callback:
                    try:
                        progress_callback(cur_count, total_symbols)
                    except Exception:
                        pass
                else:
                    try:
                        import sys

                        print(
                            f"[coverage reset-empty] {cur_count}/{total_symbols} symbols evaluated",
                            file=sys.stderr,
                            flush=True,
                        )
                    except (BrokenPipeError, OSError):
                        pass

        if rec.status == "PENDING":
            to_reset.append((rec, "Pending next-day history retry"))
            continue

        if not rec.fetched_at:
            to_reset.append((rec, "Fetch timestamp missing"))
            continue

        # 1. Error / crash in note
        note_low = (rec.note or "").lower()
        if any(
            w in note_low
            for w in (
                "fetch failed",
                "error",
                "failed",
                "retries",
                "errno",
                "exception",
                "traceback",
                "timed out",
                "timeout",
                "unreachable",
                "nodename",
                "object is not callable",
                "has no attribute",
                "crash",
                "typeerror",
                "attributeerror",
                "keyerror",
                "connection reset",
                "broken pipe",
            )
        ):
            to_reset.append((rec, f"Non-final EMPTY: fetch error / crash in note ({rec.note})"))
            continue

        # 2. Check fetch_log
        if rec.fetched_at in bad_fetch_logs:
            to_reset.append(
                (rec, f"Non-final EMPTY: fetch failed ({bad_fetch_logs[rec.fetched_at]})")
            )
            continue

        # Settle delay check
        if cal_wrapped is not None and s_margin > 0:
            try:
                fa = datetime.fromisoformat(rec.fetched_at)
                fa_utc = fa.astimezone(UTC) if fa.tzinfo else fa.replace(tzinfo=UTC)
                r_day = date.fromisoformat(rec.day)
                sess = list(cal_wrapped.sessions(r_day))
                if sess:
                    settle_dt = sess[-1].close + timedelta(minutes=s_margin)
                    if fa_utc < settle_dt:
                        to_reset.append(
                            (rec, f"Fetched before settle margin ended ({settle_dt.isoformat()})")
                        )
                        continue
            except Exception:
                pass

        # 3. Instrument has zero stored bars over whole range
        if symbol_total_bars.get(sym, 0) == 0:
            to_reset.append((rec, "Instrument has zero stored bars over the whole range"))
            continue

        # 4. Check if symbol has bars on later date (pre-listing)
        min_d = symbol_min_bar_day.get(sym)
        min_part = symbol_min_part_ts.get(sym)
        earliest_bar_day = min(
            [d for d in (min_d, min_part[:10] if min_part else None) if d],
            default=None,
        )
        if earliest_bar_day and rec.day < earliest_bar_day:
            # Final pre-listing
            continue

        # 5. Check empty range count
        max_d = symbol_max_bar_day.get(sym)
        max_part = symbol_max_part_ts.get(sym)
        latest_bar_day = max(
            [d for d in (max_d, max_part[:10] if max_part else None) if d],
            default=None,
        )

        ep_list = dataset_ep_days.get(rec.dataset_id, [])
        if latest_bar_day and rec.day > latest_bar_day:
            empty_count = len([d for d in ep_list if d > latest_bar_day])
            if empty_count > allowed_empty_sessions:
                to_reset.append(
                    (
                        rec,
                        f"All-empty range ({empty_count} sessions) exceeds "
                        f"data.empty_final_max_sessions "
                        f"({allowed_empty_sessions}) without later bars",
                    )
                )
                continue

    if not dry_run and to_reset:
        updated: list[CoverageDayRecord] = [
            CoverageDayRecord(
                dataset_id=rec.dataset_id,
                day=rec.day,
                status=CoverageStatus.MISSING,
                rows=0,
                expected_rows=rec.expected_rows,
                part_path="",
                fetched_at=rec.fetched_at,
                note=f"Reset from stale EMPTY: {reason}",
            )
            for rec, reason in to_reset
        ]
        manifest.record_coverage_days(updated)

        by_symbol: dict[str, int] = {}
        for rec, _ in to_reset:
            s_parts = rec.dataset_id.split(":")
            sym_name = s_parts[-2] if len(s_parts) >= 4 else rec.dataset_id
            by_symbol[sym_name] = by_symbol.get(sym_name, 0) + 1

        if journal is not None:
            journal.append(
                kind="data.coverage_reset_empty",
                payload={
                    "source": source or "all",
                    "timeframe": timeframe or "all",
                    "reset_count": len(to_reset),
                    "symbol_count": len(by_symbol),
                    "symbols": by_symbol,
                    "records": [
                        {
                            "dataset_id": rec.dataset_id,
                            "day": rec.day,
                            "reason": reason,
                            "fetched_at": rec.fetched_at,
                        }
                        for rec, reason in to_reset
                    ],
                },
                durable=True,
            )

    return to_reset


@dataclass(frozen=True)
class CoverageRepairItem:
    """Record of a coverage day repair action re-derived from parquet bars."""

    dataset_id: str
    day: str
    old_status: str
    new_status: str
    old_rows: int
    new_rows: int
    part_path: str
    note: str


def repair_coverage(
    manifest: DataManifest,
    data_dir: Path,
    calendar: Calendar | None = None,
    source: str | None = None,
    timeframe: str | None = None,
    dry_run: bool = False,
    journal: Any = None,
    now_dt: datetime | None = None,
) -> list[CoverageRepairItem]:
    """Re-derive coverage_days from stored parquet files.

    If bars are present in stored parquet and the session is complete,
    ensures status is COMPLETE with accurate row count, journaling any changes.
    """
    from zoneinfo import ZoneInfo

    import pyarrow.parquet as pq

    exch_tz = ZoneInfo("Asia/Kolkata")
    now_ist = (now_dt or datetime.now(UTC)).astimezone(exch_tz)
    today = now_ist.date()

    datasets = manifest.list_datasets(kind="bars")
    repaired_items: list[CoverageRepairItem] = []
    to_update_records: list[CoverageDayRecord] = []

    for ds in datasets:
        parts = ds.dataset_id.split(":")
        r_source = parts[1].lower() if len(parts) >= 2 else ""
        r_tf = parts[2].lower() if len(parts) >= 3 else ""

        if source and r_source != source.strip().lower():
            continue
        if timeframe and r_tf != timeframe.strip().lower():
            continue

        existing_cov = {r.day: r for r in manifest.get_coverage_days(ds.dataset_id)}
        ds_parts = manifest.get_parts(ds.dataset_id)
        day_counts: dict[str, int] = {}
        day_paths: dict[str, str] = {}

        for p in ds_parts or ():
            p_full = data_dir / p.path if not Path(p.path).is_absolute() else Path(p.path)
            if not p_full.exists():
                continue
            try:
                tbl = pq.read_table(p_full, columns=["start_ts"])
                if "start_ts" not in tbl.column_names or tbl.num_rows == 0:
                    continue
                for ts_val in tbl.column("start_ts").to_pylist():
                    if isinstance(ts_val, datetime):
                        ts_ist = (
                            ts_val.astimezone(exch_tz)
                            if ts_val.tzinfo
                            else ts_val.replace(tzinfo=UTC).astimezone(exch_tz)
                        )
                        d_str = ts_ist.strftime("%Y-%m-%d")
                        day_counts[d_str] = day_counts.get(d_str, 0) + 1
                        day_paths[d_str] = p.path
            except Exception:
                continue

        for d_str, count in sorted(day_counts.items()):
            if count <= 0:
                continue
            d_date = date.fromisoformat(d_str)

            is_complete_session = False
            if d_date < today:
                is_complete_session = True
            elif d_date == today:
                if calendar is not None:
                    try:
                        s_list = calendar.sessions(d_date)
                        if (
                            s_list
                            and hasattr(s_list[-1], "end")
                            and isinstance(s_list[-1].end, datetime)
                        ):
                            sess_end = s_list[-1].end.astimezone(exch_tz)
                            if now_ist >= sess_end:
                                is_complete_session = True
                    except Exception:
                        pass
                if not is_complete_session and now_ist.time() >= time(15, 30):
                    is_complete_session = True

            if not is_complete_session:
                continue

            old_rec = existing_cov.get(d_str)
            old_st = old_rec.status if old_rec else "MISSING"
            old_rows = old_rec.rows if old_rec else 0
            old_path = old_rec.part_path if old_rec else ""

            if str(old_st) == CoverageStatus.CLOSED or str(old_st) == "CLOSED":
                is_special = False
                if calendar is not None and hasattr(calendar, "is_special"):
                    try:
                        is_special = bool(calendar.is_special(d_date))
                    except Exception:
                        is_special = False
                has_disagreement = False
                if hasattr(manifest, "get_market_closure"):
                    exch = (
                        ds.dataset_id.split(":")[3] if len(ds.dataset_id.split(":")) > 3 else "NSE"
                    )
                    cl = manifest.get_market_closure(exch, d_str)
                    if cl is not None and cl.kind == "inferred":
                        has_disagreement = True
                if not is_special and not has_disagreement:
                    continue

            if old_st != CoverageStatus.COMPLETE or old_rows != count:
                rep_item = CoverageRepairItem(
                    dataset_id=ds.dataset_id,
                    day=d_str,
                    old_status=str(old_st),
                    new_status=CoverageStatus.COMPLETE,
                    old_rows=old_rows,
                    new_rows=count,
                    part_path=day_paths.get(d_str, old_path),
                    note="Re-derived from stored parquet bars",
                )
                repaired_items.append(rep_item)
                to_update_records.append(
                    CoverageDayRecord(
                        dataset_id=ds.dataset_id,
                        day=d_str,
                        status=CoverageStatus.COMPLETE,
                        rows=count,
                        expected_rows=count,
                        part_path=day_paths.get(d_str, old_path),
                        fetched_at=(
                            old_rec.fetched_at
                            if old_rec and old_rec.fetched_at
                            else now_ist.astimezone(UTC).isoformat()
                        ),
                        note="",
                    )
                )

        # Also reset COMPLETE days that have NO stored bar in parquet to MISSING
        for cov_day, old_rec in sorted(existing_cov.items()):
            if str(old_rec.status) == CoverageStatus.COMPLETE or str(old_rec.status) == "COMPLETE":
                if cov_day not in day_counts or day_counts[cov_day] <= 0:
                    rep_item = CoverageRepairItem(
                        dataset_id=ds.dataset_id,
                        day=cov_day,
                        old_status=str(old_rec.status),
                        new_status=CoverageStatus.MISSING,
                        old_rows=old_rec.rows,
                        new_rows=0,
                        part_path="",
                        note="Reset to MISSING: no stored bars in parquet",
                    )
                    repaired_items.append(rep_item)
                    to_update_records.append(
                        CoverageDayRecord(
                            dataset_id=ds.dataset_id,
                            day=cov_day,
                            status=CoverageStatus.MISSING,
                            rows=0,
                            expected_rows=old_rec.expected_rows,
                            part_path="",
                            fetched_at=now_ist.astimezone(UTC).isoformat(),
                            note="Reset to MISSING: no stored bars in parquet",
                        )
                    )

    if not dry_run and to_update_records:
        manifest.record_coverage_days(to_update_records)
        by_symbol: dict[str, int] = {}
        for item in repaired_items:
            s_parts = item.dataset_id.split(":")
            sym_name = s_parts[-2] if len(s_parts) >= 4 else item.dataset_id
            by_symbol[sym_name] = by_symbol.get(sym_name, 0) + 1

        if journal is not None:
            journal.append(
                kind="data.coverage.repair",
                payload={
                    "source": source or "all",
                    "timeframe": timeframe or "all",
                    "repair_count": len(repaired_items),
                    "symbol_counts": by_symbol,
                    "symbols": by_symbol,
                    "items": [asdict(item) for item in repaired_items],
                },
                durable=True,
            )

    return repaired_items


@dataclass
class PruneReport:
    """Report of pruned coverage, dataset, and fetch log records."""

    pruned_datasets: list[str]
    deleted_coverage_rows: int
    deleted_fetch_log_rows: int
    dry_run: bool


def prune_unmappable_coverage(
    manifest: DataManifest,
    dry_run: bool = True,
    journal: Any = None,
) -> PruneReport:
    """Delete coverage and dataset rows that have no stored files and an unmappable instrument.

    Identifies orphan datasets (such as test runs with NONEXISTENT_SYMBOL) where no parquet
    files or parts exist on disk, and purges them from coverage_days, datasets, and fetch_log.
    """
    conn = manifest._conn
    cursor = conn.cursor()

    # Query all datasets and distinct coverage dataset IDs
    cursor.execute("SELECT dataset_id, instrument FROM datasets")
    ds_map = {row[0]: row[1] for row in cursor.fetchall()}

    cursor.execute("SELECT DISTINCT dataset_id FROM coverage_days")
    all_dataset_ids = sorted(list(set(ds_map.keys()) | {row[0] for row in cursor.fetchall()}))

    target_datasets: list[str] = []
    target_instruments: set[str] = set()

    for ds_id in all_dataset_ids:
        inst_str = ds_map.get(ds_id, "")
        if not inst_str:
            # Extract instrument from dataset_id convention: kind:source:tf:EXCH:SEG:SYM:adj
            parts = ds_id.split(":")
            if len(parts) >= 6:
                inst_str = f"{parts[3]}:{parts[4]}:{parts[5]}"
            elif len(parts) >= 4:
                inst_str = parts[3]

        # Check if stored parts or files exist
        cursor.execute("SELECT count(*) FROM parts WHERE dataset_id = ?", (ds_id,))
        p_count = cursor.fetchone()[0]
        cursor.execute("SELECT count(*) FROM files WHERE instrument = ?", (inst_str,))
        f_count = cursor.fetchone()[0]
        if p_count > 0 or f_count > 0:
            continue

        # Check if there are COMPLETE coverage rows
        cursor.execute(
            "SELECT count(*) FROM coverage_days WHERE dataset_id = ? AND status = 'COMPLETE'",
            (ds_id,),
        )
        c_count = cursor.fetchone()[0]
        if c_count > 0:
            continue

        # Check if instrument is unmappable:
        # Notes in coverage_days mentioning unknown/cannot map, or NONEXISTENT in identifier
        upper_inst = str(inst_str).upper()
        upper_id = str(ds_id).upper()
        is_unmappable = (
            "NONEXISTENT" in upper_inst or "NONEXISTENT" in upper_id or "DUMMY" in upper_inst
        )
        if not is_unmappable:
            cursor.execute(
                "SELECT count(*) FROM coverage_days WHERE dataset_id = ? AND ("
                "note LIKE '%Cannot map unknown symbol%' OR note LIKE '%unknown symbol%')",
                (ds_id,),
            )
            note_match = cursor.fetchone()[0]
            if note_match > 0:
                is_unmappable = True

        if is_unmappable:
            target_datasets.append(ds_id)
            if inst_str:
                target_instruments.add(inst_str)

    # Count affected rows
    cov_rows_count = 0
    fetch_log_count = 0
    for ds_id in target_datasets:
        cursor.execute("SELECT count(*) FROM coverage_days WHERE dataset_id = ?", (ds_id,))
        cov_rows_count += cursor.fetchone()[0]

    for inst_str in target_instruments:
        cursor.execute("SELECT count(*) FROM fetch_log WHERE chunk LIKE ?", (f"{inst_str}%",))
        fetch_log_count += cursor.fetchone()[0]

    if not dry_run and target_datasets:
        for ds_id in target_datasets:
            cursor.execute("DELETE FROM coverage_days WHERE dataset_id = ?", (ds_id,))
            cursor.execute("DELETE FROM datasets WHERE dataset_id = ?", (ds_id,))

        for inst_str in target_instruments:
            cursor.execute("DELETE FROM fetch_log WHERE chunk LIKE ?", (f"{inst_str}%",))

        conn.commit()

        if journal is not None:
            try:
                journal.append(
                    kind="data.coverage.prune",
                    payload={
                        "pruned_datasets": target_datasets,
                        "deleted_coverage_rows": cov_rows_count,
                        "deleted_fetch_log_rows": fetch_log_count,
                    },
                    durable=True,
                )
            except Exception:
                pass

    return PruneReport(
        pruned_datasets=target_datasets,
        deleted_coverage_rows=cov_rows_count,
        deleted_fetch_log_rows=fetch_log_count,
        dry_run=dry_run,
    )
