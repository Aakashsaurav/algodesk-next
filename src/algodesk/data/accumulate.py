"""Data accumulation engine for AlgoDesk Next.

Fetches latest complete sessions (1m and 1d) for instrument groups through
the mode-invariant DataService.get path, tracking added rows, missing days,
and days lost beyond source retention windows.
"""

from __future__ import annotations

import zoneinfo
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from algodesk.api.errors import ConfigError, DataError
from algodesk.api.kinds import PluginKind
from algodesk.api.manifest import PluginManifest
from algodesk.api.market import CoverageStatus, InstrumentId, SeriesKey
from algodesk.data.calendar_cache import CachedCalendar
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
    waiting_sessions: list[date] = field(default_factory=list)
    request_count: int = 0


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
    waiting_items: list[AccumulationPlanItem] = field(default_factory=list)
    request_count: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)


def _check_same_day_history(source: str, params: Any = None) -> bool:
    if params is not None:
        try:
            val = params.get(f"source.{source}.same_day_history")
            if val is not None:
                if isinstance(val, bool):
                    return val
                if isinstance(val, str):
                    return val.strip().lower() in ("true", "1", "yes")
                return bool(val)
        except Exception:
            pass
    if source.lower() == "upstox":
        return False
    return True


def resolve_completed_sessions(
    calendar: Any,
    end_date: date | None = None,
    count: int = 7,
    now_dt: datetime | None = None,
    source: str | None = None,
    params: Any = None,
    same_day_history: bool | None = None,
    calendar_days: int | None = None,
) -> list[date]:
    """Find the latest complete trading sessions."""
    ist_tz = zoneinfo.ZoneInfo("Asia/Kolkata")
    now_ist = now_dt.astimezone(ist_tz) if now_dt else datetime.now(ist_tz)
    today = now_ist.date()

    if same_day_history is None and source is not None:
        same_day_history = _check_same_day_history(source, params)
    if same_day_history is None:
        same_day_history = True

    if end_date is None:
        if not same_day_history:
            # Treats a session as completed only from the next IST date
            cur = today - timedelta(days=1)
        else:
            cur = today - timedelta(days=1) if now_ist.time() < time(15, 30) else today
    else:
        cur = end_date
        if not same_day_history and cur >= today:
            cur = today - timedelta(days=1)

    sessions: list[date] = []
    if calendar_days is not None:
        start_cutoff = today - timedelta(days=calendar_days)
        while cur >= start_cutoff:
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
    else:
        max_lookback = max(120, count * 3)
        cutoff = cur - timedelta(days=max_lookback)
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
    refresh: bool = False,
    preloaded_cov: dict[str, Any] | None = None,
    preloaded_closures: dict[str, Any] | None = None,
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
        refresh=refresh,
        preloaded_coverage=preloaded_cov,
        preloaded_closures=preloaded_closures,
    )
    missing_set = set(plan.missing_days)
    unavailable_set = set(plan.unavailable_days)
    fetchable = [d for d in sessions if d in missing_set]
    lost = [d for d in sessions if d in unavailable_set]
    stored = [d for d in sessions if d not in missing_set and d not in unavailable_set]

    refetched_cnt = 0
    if fetchable:
        if preloaded_cov is not None:
            refetched_cnt = len([d for d in fetchable if d.isoformat() in preloaded_cov])
        elif manifest is not None:
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
    refresh: bool = False,
    timeframe: str | None = None,
    calendar_days: bool = False,
) -> tuple[list[str], list[AccumulationPlanItem]]:
    """Build accumulation plan for group instruments using GapPlanner."""
    calendar = CachedCalendar.wrap(calendar)
    root, data_dir = _resolve_paths(runtime)
    instruments = _resolve_instruments_for_group(group, root / "config" / "groups")
    same_day = _check_same_day_history(source, getattr(runtime, "params", None))

    is_cal_mode = calendar_days or (timeframe == "1d" and days >= 30) or days > 30
    if is_cal_mode:
        sessions = resolve_completed_sessions(
            calendar,
            now_dt=now_dt,
            source=source,
            params=getattr(runtime, "params", None),
            same_day_history=same_day,
            calendar_days=days,
        )
    else:
        sessions = resolve_completed_sessions(
            calendar,
            count=days,
            now_dt=now_dt,
            source=source,
            params=getattr(runtime, "params", None),
            same_day_history=same_day,
        )

    if not sessions:
        raise DataError(
            what=f"No completed trading sessions found in last {days} days",
            where="data.accumulate",
            fix="Check calendar coverage and system date",
        )

    ist_tz = zoneinfo.ZoneInfo("Asia/Kolkata")
    now_ist = now_dt.astimezone(ist_tz) if now_dt else datetime.now(ist_tz)
    today = now_ist.date()

    is_today_trading = False
    if calendar is not None:
        try:
            is_today_trading = bool(calendar.is_trading_day(today))
        except Exception:
            is_today_trading = today.weekday() < 5
    else:
        is_today_trading = today.weekday() < 5

    today_waiting = (not same_day) and is_today_trading and (now_ist.time() >= time(15, 30))

    now_utc = now_dt.astimezone(UTC) if now_dt else datetime.now(UTC)
    start_dt = datetime.combine(min(sessions), time(0, 0), tzinfo=ist_tz).astimezone(UTC)
    end_dt = datetime.combine(max(sessions), time(23, 59, 59), tzinfo=ist_tz).astimezone(UTC)

    manifest, opened = _resolve_manifest(data_dir, runtime)
    src_manifest = _resolve_source_manifest(runtime, source)
    items: list[AccumulationPlanItem] = []
    target_tfs = [timeframe] if timeframe else ["1m", "1d"]
    try:
        all_ds_ids = [
            dataset_id_for_key(SeriesKey(InstrumentId(inst), "bars", tf, "raw", source))
            for inst in instruments
            for tf in target_tfs
        ]
        start_day_iso = min(sessions).isoformat()
        end_day_iso = max(sessions).isoformat()
        bulk_cov = (
            manifest.get_coverage_days_bulk(
                all_ds_ids, start_day=start_day_iso, end_day=end_day_iso
            )
            if manifest is not None and hasattr(manifest, "get_coverage_days_bulk")
            else {}
        )
        closures = (
            manifest.list_market_closures(start_day=start_day_iso, end_day=end_day_iso)
            if manifest is not None and hasattr(manifest, "list_market_closures")
            else []
        )
        closures_map = {mc.day: mc for mc in closures}

        progress_every = 25
        if hasattr(runtime, "params") and runtime.params:
            try:
                progress_every = int(runtime.params.get("cli.progress_every", "25"))
            except Exception:
                progress_every = 25
        import time as _t

        t_plan0 = _t.perf_counter()

        for idx, inst in enumerate(instruments, 1):
            for tf in target_tfs:
                ds_id = dataset_id_for_key(SeriesKey(InstrumentId(inst), "bars", tf, "raw", source))
                inst_cov = {r.day: r for r in bulk_cov.get(ds_id, [])} if bulk_cov else None
                it = _plan_item_for_series(
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
                    refresh=refresh,
                    preloaded_cov=inst_cov,
                    preloaded_closures=closures_map,
                )
                if today_waiting:
                    it.waiting_sessions = [today]

                if it.fetchable_sessions:
                    chunk_span = src_manifest.max_span.get(tf, 30 if tf == "1m" else 3650)
                    span_days = (max(it.fetchable_sessions) - min(it.fetchable_sessions)).days + 1
                    it.request_count = max(1, (span_days + chunk_span - 1) // chunk_span)
                else:
                    it.request_count = 0

                items.append(it)
            is_prog = (
                progress_every > 0
                and len(instruments) > progress_every
                and (idx % progress_every == 0 or idx == len(instruments))
            )
            if is_prog:
                elapsed = _t.perf_counter() - t_plan0
                print(
                    f"[accumulate:plan] {idx}/{len(instruments)} symbols planned ({elapsed:.1f}s)",
                    flush=True,
                )
    finally:
        if opened and manifest is not None:
            manifest.close()

    return instruments, items


def _fetch_item_bars(
    item: AccumulationPlanItem,
    source: str,
    ds: Any,
    refresh: bool = False,
) -> tuple[int, bool, str | None]:
    """Fetch bars for a single plan item, returning (rows_added, success, error_msg)."""
    if not item.fetchable_sessions:
        return 0, True, None
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
        d_slice = None
        try:
            d_slice = ds.get(
                series=key,
                start=start_dt,
                end=end_dt,
                download="raw",
                adjust="none",
                fill_only=True,
                refresh=refresh,
            )
        except TypeError:
            try:
                d_slice = ds.get(
                    series=key,
                    start=start_dt,
                    end=end_dt,
                    download="raw",
                    fill_only=True,
                )
            except TypeError:
                d_slice = ds.get(
                    series=key,
                    start=start_dt,
                    end=end_dt,
                    download="raw",
                    adjust="none",
                    fill_only=False,
                    refresh=refresh,
                )
        added = 0
        if d_slice is not None:
            prov = getattr(d_slice, "provenance", None)
            if prov is None and hasattr(d_slice, "provenance_info"):
                prov = d_slice.provenance_info
            if isinstance(prov, Mapping):
                val = prov.get("rows_added")
                if isinstance(val, (int, float)):
                    added = int(val)
            if added == 0 and hasattr(ds, "_last_fetch_rows"):
                val = ds._last_fetch_rows
                if isinstance(val, (int, float)):
                    added = int(val)
            if added == 0:
                bars = getattr(d_slice, "bars", None)
                if isinstance(bars, (list, tuple)):
                    added = len(bars)
        return added, True, None
    except Exception as exc:
        return 0, False, str(exc)


def _run_fetches(
    plan_items: Sequence[AccumulationPlanItem],
    source: str,
    ds: Any,
    refresh: bool = False,
) -> tuple[int, int, list[dict[str, Any]]]:
    rows_added, missing_after = 0, 0
    failures: list[dict[str, Any]] = []
    for it in plan_items:
        added, ok, err = _fetch_item_bars(it, source, ds, refresh=refresh)
        it.rows_added = added
        rows_added += added
        if not ok:
            missing_after += len(it.fetchable_sessions)
            failures.append(
                {
                    "instrument": it.instrument,
                    "timeframe": it.timeframe,
                    "error": err or "Fetch failed",
                }
            )
    return rows_added, missing_after, failures


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

    cal_wrapped = CachedCalendar.wrap(calendar)
    cov_idx = CoverageIndex(manifest=manifest, calendar=cal_wrapped)
    try:
        all_checks: list[tuple[str, str]] = []
        for it in plan_items:
            if not it.fetchable_sessions:
                continue
            key = SeriesKey(InstrumentId(it.instrument), "bars", it.timeframe, "raw", source)
            ds_id = dataset_id_for_key(key)
            for d in it.fetchable_sessions:
                all_checks.append((ds_id, d.isoformat()))
        if not all_checks:
            return True

        unique_ds_ids = list(set(ds for ds, _ in all_checks))
        min_day = min(d for _, d in all_checks)
        max_day = max(d for _, d in all_checks)
        bulk_cov = (
            manifest.get_coverage_days_bulk(unique_ds_ids, start_day=min_day, end_day=max_day)
            if hasattr(manifest, "get_coverage_days_bulk")
            else {}
        )

        for ds_id, d_iso in all_checks:
            recs = [r for r in bulk_cov.get(ds_id, []) if r.day == d_iso]
            if not recs and not bulk_cov:
                recs = manifest.get_coverage_days(ds_id, start_day=d_iso, end_day=d_iso)
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
    refresh: bool = False,
    timeframe: str | None = None,
    calendar_days: bool = False,
) -> AccumulationReport:
    """Execute data accumulation for group, fetching missing bars and journaling."""
    calendar = CachedCalendar.wrap(calendar)
    instruments, plan_items = plan_accumulation(
        group=group,
        source=source,
        days=days,
        runtime=runtime,
        calendar=calendar,
        now_dt=now_dt,
        refresh=refresh,
        timeframe=timeframe,
        calendar_days=calendar_days,
    )
    total_lost = sum(len(it.lost_sessions) for it in plan_items)
    waiting_items = [it for it in plan_items if getattr(it, "waiting_sessions", None)]
    total_requests = sum(it.request_count for it in plan_items)
    report = AccumulationReport(
        group=group,
        source=source,
        days=days,
        instruments=instruments,
        plan_items=plan_items,
        days_lost_beyond_window=total_lost,
        dry_run=dry_run,
        waiting_items=waiting_items,
        request_count=total_requests,
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
    failures: list[dict[str, Any]] = []

    progress_every = 25
    if hasattr(runtime, "params") and runtime.params:
        try:
            progress_every = int(runtime.params.get("cli.progress_every", "25"))
        except Exception:
            progress_every = 25
    import time as _t

    t_exec0 = _t.perf_counter()

    for idx, it in enumerate(plan_items, 1):
        added, ok, err = _fetch_item_bars(it, source, data_service, refresh=refresh)
        it.rows_added = added
        instrument_rows[(it.instrument, it.timeframe)] = added
        rows_added += added
        if not ok:
            missing_after += len(it.fetchable_sessions)
            failures.append(
                {
                    "instrument": it.instrument,
                    "timeframe": it.timeframe,
                    "error": err or "Fetch failed",
                }
            )
        else:
            total_refetched += it.refetched_days
        is_prog = (
            progress_every > 0
            and len(plan_items) > progress_every
            and (idx % progress_every == 0 or idx == len(plan_items))
        )
        if is_prog:
            elapsed = _t.perf_counter() - t_exec0
            print(
                f"[accumulate:fetch] {idx}/{len(plan_items)} symbols processed ({elapsed:.1f}s)",
                flush=True,
            )

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
    report.failures = failures
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
