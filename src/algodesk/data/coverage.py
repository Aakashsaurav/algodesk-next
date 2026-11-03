"""Day-level coverage index and gap detection using the Calendar Protocol."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from algodesk.api.market import CoverageStatus, DataGap
from algodesk.api.protocols import Calendar
from algodesk.data.manifest import DataManifest


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
        closures_map: dict[str, str] = {}
        if hasattr(self._manifest, "list_market_closures"):
            for mc in self._manifest.list_market_closures(
                exchange=exch,
                start_day=start_day.isoformat(),
                end_day=end_day.isoformat(),
            ):
                closures_map[mc.day] = mc.kind

        gaps: list[DataGap] = []
        cur = start_day
        one_day = timedelta(days=1)

        closed_cal_count = 0
        closed_inf_count = 0
        special_count = 0

        while cur <= end_day:
            day_str = cur.isoformat()
            closure_kind = closures_map.get(day_str)

            is_closed = False
            if closure_kind == "special":
                special_count += 1
                is_closed = False
            elif closure_kind == "inferred":
                closed_inf_count += 1
                is_closed = True
            elif closure_kind == "calendar":
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
                    gaps.append(
                        DataGap(
                            day=cur,
                            status=CoverageStatus.EMPTY,
                            reason=rec.note or "Fetched and the source returned nothing",
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
