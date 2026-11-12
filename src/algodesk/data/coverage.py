"""Day-level coverage index and gap detection using the Calendar Protocol."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from algodesk.api.market import CoverageStatus, DataGap
from algodesk.api.protocols import Calendar
from algodesk.data.closures import has_all_samples_evidence
from algodesk.data.manifest import CoverageDayRecord, DataManifest, MarketClosureRecord


class CoverageIndex:
    """Evaluates trading day completeness, settlement margins, and data gaps."""

    __slots__ = ("_calendar", "_manifest", "_settle_delay_m", "_last_closure_counts")

    def __init__(
        self,
        manifest: DataManifest,
        calendar: Calendar | None = None,
        settle_minutes: int | None = None,
    ) -> None:
        self._manifest = manifest
        self._calendar = calendar
        self._settle_delay_m = settle_minutes if settle_minutes is not None else 0
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

            is_closed = False
            if closure_kind == "special":
                special_count += 1
                is_closed = False
            elif closure_kind == "inferred":
                if has_all_samples_evidence(closure_rec):
                    closed_inf_count += 1
                    is_closed = True
                else:
                    is_closed = False
            elif closure_kind in ("calendar", "exchange_file"):
                closed_cal_count += 1
                is_closed = True
            else:
                if self._calendar is not None:
                    try:
                        if not self._calendar.is_trading_day(cur):
                            is_closed = True
                            closed_cal_count += 1
                    except Exception:
                        if cur.weekday() >= 5:
                            is_closed = True
                            closed_cal_count += 1
                elif cur.weekday() >= 5:
                    is_closed = True
                    closed_cal_count += 1

            if not is_closed:
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
                    )
                    if final:
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
                elif rec.status not in (CoverageStatus.COMPLETE, CoverageStatus.CLOSED):
                    gaps.append(
                        DataGap(
                            day=cur,
                            status=CoverageStatus(rec.status),
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
) -> tuple[bool, str]:
    """Evaluate whether an EMPTY coverage record is final or stale/invalid.

    EMPTY is final only when:
    1. The fetch ran after the session ended plus data.settle_minutes.
    2. The response was a successful empty chunk for that exact IST date window.
    """
    if rec.status != CoverageStatus.EMPTY:
        return True, "Not EMPTY status"
    if not rec.fetched_at:
        return False, "Fetch timestamp missing"

    try:
        fa = datetime.fromisoformat(rec.fetched_at)
        fa_utc = fa.astimezone(UTC) if fa.tzinfo else fa.replace(tzinfo=UTC)
    except Exception:
        return False, f"Invalid fetched_at timestamp: {rec.fetched_at}"

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

    note_low = (rec.note or "").lower()
    if any(
        w in note_low
        for w in (
            "object is not callable",
            "traceback",
            "exception",
            "crash",
            "typeerror",
            "attributeerror",
            "valueerror",
        )
    ):
        return False, f"Fetch error: {rec.note}"

    parts = rec.dataset_id.split(":")
    if len(parts) >= 3:
        source = parts[1].lower()
        tf = parts[2].lower()
        if source == "yfinance" and tf in ("1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h"):
            cutoff = datetime(2026, 11, 10, 0, 0, tzinfo=UTC)
            if fa_utc < cutoff:
                return False, "Old yfinance intraday date window (pre-M2-11)"

    if manifest is not None and hasattr(manifest, "_conn"):
        try:
            cur = manifest._conn.execute(
                "SELECT outcome, error FROM fetch_log WHERE finished = ?", (rec.fetched_at,)
            )
            for row in cur.fetchall():
                if row["outcome"] != "OK" or (row["error"] and row["error"].strip()):
                    return False, f"Fetch log error: {row['error'] or row['outcome']}"
        except Exception:
            pass

    return True, "Final EMPTY chunk"


def is_empty_record_final(
    rec: CoverageDayRecord,
    calendar: Calendar | None = None,
    settle_minutes: int | None = None,
    manifest: DataManifest | None = None,
    now_dt: datetime | None = None,
) -> bool:
    """Return True if the EMPTY record is validly final, False if stale/invalid."""
    is_final, _ = evaluate_empty_record(
        rec=rec,
        calendar=calendar,
        settle_minutes=settle_minutes,
        manifest=manifest,
        now_dt=now_dt,
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
) -> list[tuple[CoverageDayRecord, str]]:
    """Re-evaluate existing EMPTY coverage records and reset non-final ones to MISSING."""
    if not hasattr(manifest, "_conn"):
        return []

    cur = manifest._conn.execute(
        "SELECT * FROM coverage_days WHERE status = 'EMPTY' ORDER BY dataset_id, day"
    )
    rows = cur.fetchall()

    to_reset: list[tuple[CoverageDayRecord, str]] = []
    for r in rows:
        rec = CoverageDayRecord(**dict(r))
        parts = rec.dataset_id.split(":")
        r_source = parts[1].lower() if len(parts) >= 2 else ""
        r_tf = parts[2].lower() if len(parts) >= 3 else ""

        if source and r_source != source.strip().lower():
            continue
        if timeframe and r_tf != timeframe.strip().lower():
            continue

        is_final, reason = evaluate_empty_record(
            rec=rec,
            calendar=calendar,
            settle_minutes=settle_minutes,
            manifest=manifest,
        )
        if not is_final:
            to_reset.append((rec, reason))

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

        if journal is not None:
            journal.append(
                kind="data.coverage_reset_empty",
                payload={
                    "source": source or "all",
                    "timeframe": timeframe or "all",
                    "reset_count": len(to_reset),
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
