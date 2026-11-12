"""Data accumulation engine for AlgoDesk Next.

Fetches latest complete sessions (1m and 1d) for instrument groups through
the mode-invariant DataService.get path, tracking added rows, missing days,
and days lost beyond source retention windows.
"""

from __future__ import annotations

import zoneinfo
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from algodesk.api.errors import ConfigError, DataError
from algodesk.api.kinds import PluginKind
from algodesk.api.manifest import PluginManifest
from algodesk.api.market import CoverageStatus, InstrumentId, SeriesKey
from algodesk.data.groups import get_group, load_groups
from algodesk.data.layout import dataset_id_for_key
from algodesk.data.manifest import DataManifest
from algodesk.data.planner import GapPlanner


@dataclass
class AccumulationPlanItem:
    """Planned accumulation task for a single instrument and timeframe."""

    instrument: str
    timeframe: str
    target_sessions: list[date]
    stored_sessions: list[date]
    fetchable_sessions: list[date]
    lost_sessions: list[date]
    rows_added: int = 0
    refetched_days: int = 0


@dataclass
class AccumulationReport:
    """Outcome report for a data accumulation run."""

    group: str
    source: str
    days: int
    instruments: list[str]
    plan_items: list[AccumulationPlanItem] = field(default_factory=list)
    rows_added: int = 0
    days_still_missing: int = 0
    days_lost_beyond_window: int = 0
    days_refetched: int = 0
    dry_run: bool = False
    instrument_rows: dict[tuple[str, str], int] = field(default_factory=dict)


def resolve_completed_sessions(
    calendar: Any,
    end_date: date | None = None,
    count: int = 7,
    now_dt: datetime | None = None,
) -> list[date]:
    """Find the latest complete trading sessions."""
    ist_tz = zoneinfo.ZoneInfo("Asia/Kolkata")
    now_ist = now_dt.astimezone(ist_tz) if now_dt else datetime.now(ist_tz)
    today = now_ist.date()

    if end_date is None:
        cur = today - timedelta(days=1) if now_ist.time() < time(15, 30) else today
    else:
        cur = end_date

    sessions: list[date] = []
    cutoff = cur - timedelta(days=120)
    while cur >= cutoff and len(sessions) < count:
        is_trading = False
        if calendar is not None:
            try:
                is_trading = bool(calendar.is_trading_day(cur))
            except Exception:
                is_trading = cur.weekday() < 5
        else:
            is_trading = cur.weekday() < 5

        if is_trading:
            sessions.append(cur)
        cur -= timedelta(days=1)

    sessions.reverse()
    return sessions


def get_source_retention_days(source: str, timeframe: str) -> int:
    """Return max retrievable days for a source and timeframe."""
    src = source.lower()
    if timeframe.lower() == "1m":
        if src == "yfinance":
            return 7
        if src == "upstox":
            return 1800
    return 36500


def _resolve_paths(runtime: Any) -> tuple[Path, Path]:
    """Resolve project root and data directory from Runtime or Path."""
    if isinstance(runtime, Path):
        return runtime, runtime / "data"
    root = Path(runtime.root)
    data_dir = Path(runtime.paths.data) if hasattr(runtime, "paths") else root / "data"
    return root, data_dir


def _resolve_manifest(data_dir: Path, runtime: Any) -> tuple[DataManifest | None, bool]:
    """Resolve manifest from runtime or database file. Returns (manifest, opened_new)."""
    manifest = getattr(runtime, "manifest", None)
    if manifest is not None:
        return manifest, False
    m_path = data_dir / "state" / "data_manifest.db"
    if not m_path.exists() and (data_dir / "manifest.db").exists():
        m_path = data_dir / "manifest.db"
    if m_path.exists():
        return DataManifest(m_path), True
    return None, False


def _resolve_source_manifest(runtime: Any, source: str) -> PluginManifest:
    """Resolve plugin manifest for source with retention settings."""
    from typing import cast

    if hasattr(runtime, "plugins"):
        try:
            return cast(PluginManifest, runtime.plugins.info("broker", source).manifest)
        except Exception:
            try:
                for p in runtime.plugins.list():
                    if getattr(p, "manifest", None) and p.manifest.name == source:
                        return cast(PluginManifest, p.manifest)
            except Exception:
                pass
    return PluginManifest(
        name=source,
        version="1.0.0",
        kind=PluginKind.BROKER,
        api_version=1,
        entry=f"broker:{source}",
        capabilities={"history": True},
        retention={
            "1m": get_source_retention_days(source, "1m"),
            "1d": get_source_retention_days(source, "1d"),
        },
        max_span={"1m": 30, "1d": 3650},
    )


def _resolve_instruments_for_group(group: str, groups_dir: Path) -> list[str]:
    """Resolve members list for group name or raise ConfigError."""
    instruments = get_group(group, groups_dir)
    if instruments:
        return instruments
    all_groups = load_groups(groups_dir)
    if group in all_groups:
        return all_groups[group]
    known = ", ".join(all_groups.keys()) or "none"
    raise ConfigError(
        what=f"Instrument group '{group}' not found or empty",
        where="data.accumulate",
        fix=f"Specify valid group from config/groups (available: {known})",
    )


def _plan_item_for_series(
    inst: str,
    tf: str,
    source: str,
    sessions: list[date],
    start_dt: datetime,
    end_dt: datetime,
    manifest: DataManifest | None,
    calendar: Any,
    src_manifest: PluginManifest,
    now_utc: datetime,
    ist_tz: zoneinfo.ZoneInfo,
) -> AccumulationPlanItem:
    """Evaluate single series using GapPlanner and build AccumulationPlanItem."""
    key = SeriesKey(
        instrument=InstrumentId(inst),
        kind="bars",
        timeframe=tf,
        adjustment="raw",
        source=source,
    )
    plan = GapPlanner.plan(
        key=key,
        start=start_dt,
        end=end_dt,
        manifest=manifest,
        calendar=calendar,
        source_manifest=src_manifest,
        as_of=now_utc,
        exchange_tz=ist_tz,
        refresh=False,
    )
    missing_set = set(plan.missing_days)
    unavailable_set = set(plan.unavailable_days)
    fetchable = [d for d in sessions if d in missing_set]
    lost = [d for d in sessions if d in unavailable_set]
    stored = [d for d in sessions if d not in missing_set and d not in unavailable_set]

    refetched_cnt = 0
    if manifest is not None and fetchable:
        ds_id = dataset_id_for_key(key)
        try:
            cov = {
                r.day: r
                for r in manifest.get_coverage_days(
                    ds_id,
                    start_day=min(sessions).isoformat(),
                    end_day=max(sessions).isoformat(),
                )
            }
            refetched_cnt = len([d for d in fetchable if d.isoformat() in cov])
        except Exception:
            refetched_cnt = 0

    return AccumulationPlanItem(
        instrument=inst,
        timeframe=tf,
        target_sessions=sessions,
        stored_sessions=stored,
        fetchable_sessions=fetchable,
        lost_sessions=lost,
        refetched_days=refetched_cnt,
    )


def plan_accumulation(
    group: str,
    source: str,
    days: int,
    runtime: Any,
    calendar: Any = None,
    now_dt: datetime | None = None,
) -> tuple[list[str], list[AccumulationPlanItem]]:
    """Build accumulation plan for group instruments across 1m and 1d using GapPlanner."""
    root, data_dir = _resolve_paths(runtime)
    instruments = _resolve_instruments_for_group(group, root / "config" / "groups")
    sessions = resolve_completed_sessions(calendar, count=days, now_dt=now_dt)
    if not sessions:
        raise DataError(
            what=f"No completed trading sessions found in last {days} days",
            where="data.accumulate",
            fix="Check calendar coverage and system date",
        )

    ist_tz = zoneinfo.ZoneInfo("Asia/Kolkata")
    now_utc = now_dt.astimezone(UTC) if now_dt else datetime.now(UTC)
    start_dt = datetime.combine(min(sessions), time(0, 0), tzinfo=ist_tz).astimezone(UTC)
    end_dt = datetime.combine(max(sessions), time(23, 59, 59), tzinfo=ist_tz).astimezone(UTC)

    manifest, opened = _resolve_manifest(data_dir, runtime)
    src_manifest = _resolve_source_manifest(runtime, source)
    items: list[AccumulationPlanItem] = []
    try:
        for inst in instruments:
            for tf in ("1m", "1d"):
                items.append(
                    _plan_item_for_series(
                        inst,
                        tf,
                        source,
                        sessions,
                        start_dt,
                        end_dt,
                        manifest,
                        calendar,
                        src_manifest,
                        now_utc,
                        ist_tz,
                    )
                )
    finally:
        if opened and manifest is not None:
            manifest.close()

    return instruments, items


def _fetch_item_bars(
    item: AccumulationPlanItem,
    source: str,
    ds: Any,
) -> tuple[int, bool]:
    """Fetch bars for a single plan item, returning (rows_added, success)."""
    if not item.fetchable_sessions:
        return 0, True
    start_date = min(item.fetchable_sessions)
    end_date = max(item.fetchable_sessions)
    utc = UTC
    start_dt = datetime.combine(start_date, time(0, 0), tzinfo=utc)
    end_dt = datetime.combine(end_date, time(23, 59, 59), tzinfo=utc)

    key = SeriesKey(
        instrument=InstrumentId(item.instrument),
        kind="bars",
        timeframe=item.timeframe,
        adjustment="raw",
        source=source,
    )
    try:
        d_slice = ds.get(series=key, start=start_dt, end=end_dt, download="raw", fill_only=True)
        return (len(d_slice.bars) if d_slice else 0), True
    except Exception:
        return 0, False


def _run_fetches(
    plan_items: Sequence[AccumulationPlanItem], source: str, ds: Any
) -> tuple[int, int]:
    rows_added, missing_after = 0, 0
    for it in plan_items:
        added, ok = _fetch_item_bars(it, source, ds)
        rows_added += added
        if not ok:
            missing_after += len(it.fetchable_sessions)
    return rows_added, missing_after


def _are_all_fetchable_days_final_empty(
    plan_items: Sequence[AccumulationPlanItem],
    source: str,
    runtime: Any,
    calendar: Any = None,
    now_dt: datetime | None = None,
) -> bool:
    """Check if all fetchable days that added 0 rows are recorded as final EMPTY."""
    _, data_dir = _resolve_paths(runtime)
    manifest, opened = _resolve_manifest(data_dir, runtime)
    if manifest is None:
        return False

    from algodesk.data.coverage import CoverageIndex

    cov_idx = CoverageIndex(manifest=manifest, calendar=calendar)
    try:
        for it in plan_items:
            if not it.fetchable_sessions:
                continue
            key = SeriesKey(InstrumentId(it.instrument), "bars", it.timeframe, "raw", source)
            ds_id = dataset_id_for_key(key)
            for d in it.fetchable_sessions:
                recs = manifest.get_coverage_days(
                    ds_id, start_day=d.isoformat(), end_day=d.isoformat()
                )
                if not recs or recs[0].status != CoverageStatus.EMPTY:
                    return False
                if not cov_idx.is_empty_final(recs[0], now_dt=now_dt):
                    return False
        return True
    finally:
        if opened:
            manifest.close()


def execute_accumulation(
    group: str,
    source: str,
    days: int,
    runtime: Any,
    dry_run: bool = False,
    data_service: Any = None,
    calendar: Any = None,
    now_dt: datetime | None = None,
) -> AccumulationReport:
    """Execute data accumulation for group, fetching missing bars and journaling."""
    instruments, plan_items = plan_accumulation(
        group=group,
        source=source,
        days=days,
        runtime=runtime,
        calendar=calendar,
        now_dt=now_dt,
    )
    total_lost = sum(len(it.lost_sessions) for it in plan_items)
    report = AccumulationReport(
        group=group,
        source=source,
        days=days,
        instruments=instruments,
        plan_items=plan_items,
        days_lost_beyond_window=total_lost,
        dry_run=dry_run,
    )
    total_fetchable = sum(len(it.fetchable_sessions) for it in plan_items)
    if dry_run or total_fetchable == 0:
        report.days_still_missing = total_fetchable if dry_run else 0
        report.days_refetched = sum(it.refetched_days for it in plan_items)
        if not dry_run and total_lost > 0:
            _log_accumulation_event(runtime, group, source, days, 0, 0, total_lost, 0, instruments)
        return report

    if data_service is None:
        raise DataError(
            what="DataService is required to execute accumulation",
            where="data.accumulate",
            fix="Pass an active DataService or execute via 'algodesk data accumulate'",
        )

    rows_added, missing_after = 0, 0
    total_refetched = 0
    instrument_rows: dict[tuple[str, str], int] = {}
    for it in plan_items:
        added, ok = _fetch_item_bars(it, source, data_service)
        it.rows_added = added
        instrument_rows[(it.instrument, it.timeframe)] = added
        rows_added += added
        if not ok:
            missing_after += len(it.fetchable_sessions)
        else:
            total_refetched += it.refetched_days

    if total_fetchable > 0 and rows_added == 0 and missing_after == 0:
        if not _are_all_fetchable_days_final_empty(plan_items, source, runtime, calendar, now_dt):
            raise DataError(
                what=(
                    f"Accumulation planned {total_fetchable} session-timeframe(s) to fetch "
                    f"for group '{group}' from '{source}', but zero rows were added with no error"
                ),
                where="data.accumulate",
                fix=(
                    "Check source coverage, verify date window parameters, "
                    "or run 'algodesk data coverage reset-empty' if stale EMPTY records exist"
                ),
            )

    report.rows_added = rows_added
    report.days_still_missing = missing_after
    report.days_refetched = total_refetched
    report.instrument_rows = instrument_rows
    _log_accumulation_event(
        runtime,
        group,
        source,
        days,
        rows_added,
        missing_after,
        total_lost,
        total_refetched,
        instruments,
    )
    return report


def _log_accumulation_event(
    runtime: Any,
    group: str,
    source: str,
    days: int,
    rows: int,
    missing: int,
    lost: int,
    refetched: int,
    instruments: list[str],
) -> None:
    """Record durable data.accumulate event in journal if available."""
    if hasattr(runtime, "journal") and runtime.journal is not None:
        runtime.journal.append(
            "data.accumulate",
            {
                "group": group,
                "source": source,
                "days": days,
                "rows_added": rows,
                "days_missing": missing,
                "days_lost": lost,
                "days_refetched": refetched,
                "instruments": instruments,
            },
            durable=True,
        )
