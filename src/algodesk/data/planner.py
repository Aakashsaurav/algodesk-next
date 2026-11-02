"""Pure gap planner mapping data requests to base series chunks and coverage status."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

from algodesk.api.errors import CalendarCoverageError, DataError
from algodesk.api.manifest import PluginManifest
from algodesk.api.market import CoverageStatus, SeriesKey
from algodesk.api.protocols import Calendar
from algodesk.data.coverage import CoverageIndex
from algodesk.data.layout import dataset_id_for_key
from algodesk.data.manifest import CoverageDayRecord, DataManifest

_DEFAULT_EXCH_TZ = ZoneInfo("Asia/Kolkata")


def resolve_base_timeframe(timeframe: str) -> str:
    """Map any requested timeframe to base storage timeframe ('1m' or '1d')."""
    tf = timeframe.strip().lower()
    if tf in ("1d", "daily", "1w", "weekly", "1m_month", "1mo", "1y"):
        return "1d"
    if tf == "1m":
        return "1m"
    if tf.endswith("m") or tf.endswith("h") or tf.endswith("s"):
        return "1m"
    raise DataError(
        what=f"Cannot map timeframe '{timeframe}' to base series (1m or 1d)",
        where="GapPlanner.resolve_base_timeframe",
        fix="Use standard timeframe interval such as '1m', '5m', '15m', '1h', '1d'",
    )


def is_older_than_retention(
    d: date,
    retention_setting: object,
    as_of: date,
) -> bool:
    """Check if date d is older than source retention limit."""
    if retention_setting is None:
        return False
    if isinstance(retention_setting, int) or (
        isinstance(retention_setting, str) and retention_setting.strip().isdigit()
    ):
        return d < as_of - timedelta(days=int(retention_setting))
    if isinstance(retention_setting, str):
        try:
            return d < date.fromisoformat(retention_setting.strip())
        except ValueError:
            pass
    return False


@dataclass(frozen=True, slots=True)
class FetchChunk:
    """Individual chunk to be fetched from a data source."""

    chunk_id: str
    start_day: date
    end_day: date
    start: datetime
    end: datetime
    days: tuple[date, ...]


@dataclass(frozen=True, slots=True)
class FetchPlan:
    """Deterministic plan describing chunks to fetch and non-missing days."""

    requested_key: SeriesKey
    base_key: SeriesKey
    chunks: tuple[FetchChunk, ...]
    missing_days: tuple[date, ...]
    complete_days: tuple[date, ...]
    closed_days: tuple[date, ...]
    unavailable_days: tuple[date, ...]

    @property
    def has_work(self) -> bool:
        return bool(self.chunks)


class GapPlanner:
    """Pure gap planner evaluating coverage, retention, and chunk boundaries."""

    @classmethod
    def plan(
        cls,
        key: SeriesKey,
        start: datetime,
        end: datetime,
        manifest: DataManifest | None = None,
        coverage_index: CoverageIndex | None = None,
        calendar: Calendar | None = None,
        source_manifest: PluginManifest | None = None,
        chunk_days: int = 30,
        as_of: datetime | None = None,
        exchange_tz: tzinfo | None = None,
    ) -> FetchPlan:
        """Construct pure FetchPlan for key between start and end."""
        if start > end:
            s_iso, e_iso = start.isoformat(), end.isoformat()
            raise CalendarCoverageError(
                what=f"Start timestamp ({s_iso}) must be <= end timestamp ({e_iso})",
                where="GapPlanner.plan",
                fix="Ensure start time precedes end time",
            )

        tz = exchange_tz or _DEFAULT_EXCH_TZ
        now_dt = as_of or datetime.now(UTC)
        as_of_date = now_dt.astimezone(tz).date()

        base_tf = resolve_base_timeframe(key.timeframe)
        base_key = SeriesKey(
            instrument=key.instrument,
            kind="bars",
            timeframe=base_tf,
            adjustment=key.adjustment,
            source=key.source,
        )
        ds_id = dataset_id_for_key(base_key)

        start_day = start.astimezone(tz).date()
        end_day = end.astimezone(tz).date()

        # Resolve retention and max_span for base timeframe
        retention_val: object = None
        span_bound_days = chunk_days

        if source_manifest is not None:
            ret = getattr(source_manifest, "retention", None) or getattr(
                source_manifest, "limits", {}
            ).get("retention", {})
            retention_val = ret.get(base_tf) if isinstance(ret, dict) else None
            msp = getattr(source_manifest, "max_span", None) or getattr(
                source_manifest, "limits", {}
            ).get("max_span", {})
            if isinstance(msp, dict) and (v := msp.get(base_tf)) is not None:
                try:
                    span_bound_days = int(v)
                except Exception:
                    pass

        effective_chunk_limit = max(1, min(span_bound_days, chunk_days))

        # Retrieve existing coverage records
        existing_cov: dict[str, CoverageDayRecord] = {}
        if manifest is not None:
            for r in manifest.get_coverage_days(
                ds_id,
                start_day=start_day.isoformat(),
                end_day=end_day.isoformat(),
            ):
                existing_cov[r.day] = r

        missing: list[date] = []
        complete: list[date] = []
        closed: list[date] = []
        unavailable: list[date] = []

        cur = start_day
        one_day = timedelta(days=1)
        while cur <= end_day:
            day_str = cur.isoformat()

            # 1. Check if closed by calendar
            is_closed = False
            if calendar is not None:
                if not calendar.is_trading_day(cur):
                    is_closed = True
            elif cur.weekday() >= 5:
                is_closed = True

            if is_closed:
                closed.append(cur)
                cur += one_day
                continue

            # 2. Check if older than source retention
            if is_older_than_retention(cur, retention_val, as_of_date):
                unavailable.append(cur)
                cur += one_day
                continue

            # 3. Check existing coverage record
            rec = existing_cov.get(day_str)
            if rec is not None:
                if rec.status == CoverageStatus.COMPLETE:
                    complete.append(cur)
                elif rec.status == CoverageStatus.CLOSED:
                    closed.append(cur)
                elif rec.status == CoverageStatus.UNAVAILABLE:
                    unavailable.append(cur)
                else:
                    missing.append(cur)
            else:
                missing.append(cur)

            cur += one_day

        # Build chunks from contiguous missing days
        chunks: list[FetchChunk] = []
        if missing:
            current_chunk_days: list[date] = []
            chunk_start_day: date = missing[0]

            for d in missing:
                # Check if contiguous with previous or within effective_chunk_limit
                span = (d - chunk_start_day).days + 1
                if span > effective_chunk_limit or (
                    current_chunk_days and (d - current_chunk_days[-1]).days > 7
                ):
                    # Finalize current chunk
                    c_end_day = current_chunk_days[-1]
                    c_start_dt = datetime(
                        chunk_start_day.year,
                        chunk_start_day.month,
                        chunk_start_day.day,
                        0,
                        0,
                        tzinfo=tz,
                    ).astimezone(UTC)
                    c_end_dt = datetime(
                        c_end_day.year,
                        c_end_day.month,
                        c_end_day.day,
                        23,
                        59,
                        59,
                        999999,
                        tzinfo=tz,
                    ).astimezone(UTC)
                    c_id = (
                        f"{base_key.instrument}:{base_key.source}:{base_key.timeframe}:"
                        f"{chunk_start_day.isoformat()}_{c_end_day.isoformat()}"
                    )
                    chunks.append(
                        FetchChunk(
                            chunk_id=c_id,
                            start_day=chunk_start_day,
                            end_day=c_end_day,
                            start=c_start_dt,
                            end=c_end_dt,
                            days=tuple(current_chunk_days),
                        )
                    )
                    chunk_start_day = d
                    current_chunk_days = [d]
                else:
                    current_chunk_days.append(d)

            if current_chunk_days:
                c_end_day = current_chunk_days[-1]
                c_start_dt = datetime(
                    chunk_start_day.year,
                    chunk_start_day.month,
                    chunk_start_day.day,
                    0,
                    0,
                    tzinfo=tz,
                ).astimezone(UTC)
                c_end_dt = datetime(
                    c_end_day.year,
                    c_end_day.month,
                    c_end_day.day,
                    23,
                    59,
                    59,
                    999999,
                    tzinfo=tz,
                ).astimezone(UTC)
                c_id = (
                    f"{base_key.instrument}:{base_key.source}:{base_key.timeframe}:"
                    f"{chunk_start_day.isoformat()}_{c_end_day.isoformat()}"
                )
                chunks.append(
                    FetchChunk(
                        chunk_id=c_id,
                        start_day=chunk_start_day,
                        end_day=c_end_day,
                        start=c_start_dt,
                        end=c_end_dt,
                        days=tuple(current_chunk_days),
                    )
                )

        return FetchPlan(
            requested_key=key,
            base_key=base_key,
            chunks=tuple(chunks),
            missing_days=tuple(missing),
            complete_days=tuple(complete),
            closed_days=tuple(closed),
            unavailable_days=tuple(unavailable),
        )
