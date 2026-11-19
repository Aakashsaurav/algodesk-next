"""Data readiness verification for backtest execution conforming to FR-BKT-2 and ADR-081."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from algodesk.api.market import InstrumentId, SeriesKey
from algodesk.data.aliases import resolve_alias
from algodesk.data.coverage import is_empty_record_final, is_trading_session
from algodesk.data.layout import dataset_id_for_key
from algodesk.data.manifest import DataManifest
from algodesk.data.service import DataService

_DEFAULT_EXCH_TZ = ZoneInfo("Asia/Kolkata")


@dataclass
class ResolvedSeries:
    """Resolved data series parameters for backtest execution and readiness check."""

    instrument: InstrumentId
    timeframe: str
    source: str
    adjustment: str
    adjust: str
    dataset_id: str
    warnings: list[str] = field(default_factory=list)
    cleanup_commands: list[str] = field(default_factory=list)


def resolve_backtest_series(
    instrument: InstrumentId | str,
    timeframe: str = "1d",
    source: str = "upstox",
    adjust: str = "splits",
    manifest: Any = None,
    store: Any = None,
) -> ResolvedSeries:
    """Resolve single authoritative series for backtest execution and readiness (ADR-084)."""
    raw_str = str(instrument).strip().upper()
    if manifest is not None and isinstance(manifest, DataManifest):
        try:
            res_alias = resolve_alias(raw_str, manifest=manifest)
            canonical_inst = InstrumentId(str(res_alias).upper())
        except Exception:
            canonical_inst = InstrumentId(raw_str)
    else:
        try:
            res_alias = resolve_alias(raw_str)
            canonical_inst = InstrumentId(str(res_alias).upper())
        except Exception:
            canonical_inst = InstrumentId(raw_str)
    inst_str = str(canonical_inst)
    adj_mode = adjust if adjust in ("none", "splits", "total") else "splits"
    pref_download = "raw" if adj_mode in ("splits", "none") else "adjusted"
    fb_download = "adjusted" if pref_download == "raw" else "raw"

    pref_key = SeriesKey(canonical_inst, "bars", timeframe, pref_download, source)
    pref_ds_id = dataset_id_for_key(pref_key)
    fb_key = SeriesKey(canonical_inst, "bars", timeframe, fb_download, source)
    fb_ds_id = dataset_id_for_key(fb_key)

    legacy_id = f"{inst_str}_{timeframe}_{source}"
    pref_exists = _series_partition_exists(
        pref_ds_id, pref_key, manifest, store, legacy_id=legacy_id
    )
    fb_exists = _series_partition_exists(fb_ds_id, fb_key, manifest, store)

    chosen_download = pref_download if (pref_exists or not fb_exists) else fb_download
    chosen_ds_id = pref_ds_id if chosen_download == pref_download else fb_ds_id

    warnings, cleanup_cmds = _collect_partition_warnings(
        inst_str,
        canonical_inst,
        timeframe,
        chosen_ds_id,
        chosen_download,
        pref_download,
        fb_ds_id,
        fb_download,
        fb_exists,
        source,
        manifest,
    )
    return ResolvedSeries(
        instrument=canonical_inst,
        timeframe=timeframe,
        source=source,
        adjustment=chosen_download,
        adjust=adj_mode,
        dataset_id=chosen_ds_id,
        warnings=warnings,
        cleanup_commands=cleanup_cmds,
    )


def _series_partition_exists(
    ds_id: str,
    key: SeriesKey,
    manifest: Any,
    store: Any,
    legacy_id: str | None = None,
) -> bool:
    """Check if partition exists in manifest or store directory."""
    if manifest is not None:
        try:
            if hasattr(manifest, "get_parts"):
                if manifest.get_parts(dataset_id=ds_id):
                    return True
                if legacy_id and manifest.get_parts(dataset_id=legacy_id):
                    return True
        except Exception:
            pass
        try:
            if hasattr(manifest, "get_coverage_days"):
                cov = manifest.get_coverage_days(ds_id)
                if not cov and legacy_id and key.adjustment == "raw":
                    cov = manifest.get_coverage_days(legacy_id)
                valid_statuses = (
                    "COMPLETE",
                    "CoverageStatus.COMPLETE",
                    "PENDING",
                    "CoverageStatus.PENDING",
                    "EMPTY",
                    "CoverageStatus.EMPTY",
                )
                if any(
                    str(r.status) in valid_statuses
                    or getattr(r.status, "name", "") in ("COMPLETE", "PENDING", "EMPTY")
                    for r in cov
                ):
                    return True
        except Exception:
            pass
    if store is not None and hasattr(store, "data_dir"):
        from algodesk.data.layout import bar_file_path

        try:
            if bar_file_path(store.data_dir, key, datetime.now(UTC)).exists():
                return True
        except Exception:
            pass
    return False


def _collect_partition_warnings(
    inst_str: str,
    canonical_inst: InstrumentId,
    timeframe: str,
    chosen_ds_id: str,
    chosen_download: str,
    pref_download: str,
    fb_ds_id: str,
    fb_download: str,
    fb_exists: bool,
    source: str,
    manifest: Any,
) -> tuple[list[str], list[str]]:
    """Collect non-blocking warnings and cleanup commands for alternate partitions."""
    warnings: list[str] = []
    cleanup_cmds: list[str] = []
    if manifest is not None:
        try:
            for ds in manifest.list_datasets():
                if (
                    ds.instrument == inst_str
                    and ds.timeframe == timeframe
                    and ds.dataset_id != chosen_ds_id
                ):
                    warnings.append(
                        f"Alternate partition found: dataset={ds.dataset_id} "
                        f"(source={ds.source}, adjustment={getattr(ds, 'adjustment', 'unknown')})"
                    )
                    cleanup_cmds.append(
                        f"algodesk data delete {canonical_inst} "
                        f"--reason 'cleanup alternate partition {ds.dataset_id}'"
                    )
        except Exception:
            pass
    if chosen_download == pref_download and fb_exists:
        fb_warn = (
            f"Alternate partition found: dataset={fb_ds_id} "
            f"(source={source}, adjustment={fb_download})"
        )
        if fb_warn not in warnings:
            warnings.append(fb_warn)
            cleanup_cmds.append(
                f"algodesk data delete {canonical_inst} "
                f"--reason 'cleanup alternate partition {fb_ds_id}'"
            )
    return warnings, cleanup_cmds


@dataclass
class SymbolReadiness:
    """Readiness status and detailed diagnosis for a single symbol."""

    symbol: str
    instrument_id: str
    first_stored_day: date | None = None
    last_stored_day: date | None = None
    missing_sessions: list[date] = field(default_factory=list)
    pending_empty_days: list[date] = field(default_factory=list)
    quarantined_days: list[str] = field(default_factory=list)
    coverage_without_bars: list[date] = field(default_factory=list)
    insufficient_warmup: bool = False
    warmup_bars_found: int = 0
    warmup_bars_needed: int = 0
    problems: list[str] = field(default_factory=list)
    fix_commands: list[str] = field(default_factory=list)
    resolved_series: str = ""
    warnings: list[str] = field(default_factory=list)
    warning_cleanup_commands: list[str] = field(default_factory=list)

    @property
    def is_ready(self) -> bool:
        return len(self.problems) == 0

    @property
    def primary_skip_reason(self) -> str | None:
        if not self.problems:
            return None
        if self.coverage_without_bars:
            return "coverage_without_bars"
        if self.quarantined_days:
            return "quarantined"
        if self.pending_empty_days:
            return "pending_empty"
        if self.insufficient_warmup:
            return "insufficient_warmup"
        if self.missing_sessions:
            return "missing_sessions"
        return "data_problem"


@dataclass
class ReadinessReport:
    """Aggregated readiness check report across an evaluated universe."""

    version: int = 1
    start: date = field(default_factory=date.today)
    end: date = field(default_factory=date.today)
    group: str | None = None
    as_of: str = ""
    universe_hash: str = ""
    members: list[str] = field(default_factory=list)
    with_data: list[str] = field(default_factory=list)
    coverage_pct: float = 0.0
    missing_pct: float = 0.0
    skipped: list[dict[str, str]] = field(default_factory=list)
    problems: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    symbol_reports: dict[str, SymbolReadiness] = field(default_factory=dict)
    remediation_commands: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize readiness report conforming to readiness.json schema."""
        return {
            "version": self.version,
            "range": {
                "start": self.start.isoformat(),
                "end": self.end.isoformat(),
            },
            "universe": {
                "group": self.group,
                "as_of": self.as_of,
                "hash": self.universe_hash,
                "members": self.members,
                "with_data": self.with_data,
                "coverage_pct": self.coverage_pct,
                "skipped": self.skipped,
            },
            "missing_pct": self.missing_pct,
            "problems": self.problems,
            "warnings": self.warnings,
        }


def check_symbol_readiness(
    symbol: str,
    data_service: DataService,
    manifest: DataManifest,
    calendar: Any,
    expected_sessions: list[date],
    start: date,
    end: date,
    timeframe: str = "1d",
    source: str = "upstox",
    warmup_bars_count: int = 50,
    group: str | None = None,
    params: Any = None,
    adjust: str = "splits",
) -> SymbolReadiness:
    """Inspect readiness for a single symbol using local cache only (no network)."""
    canonical_inst = resolve_alias(symbol, manifest=manifest)
    inst_str = str(canonical_inst)

    resolved = resolve_backtest_series(
        canonical_inst,
        timeframe=timeframe,
        source=source,
        adjust=adjust,
        manifest=manifest,
        store=getattr(data_service, "store", None),
    )

    report = SymbolReadiness(
        symbol=symbol,
        instrument_id=inst_str,
        resolved_series=resolved.dataset_id,
        warnings=list(resolved.warnings),
        warning_cleanup_commands=list(resolved.cleanup_commands),
    )
    series_key = f"{inst_str}:{timeframe}"

    exch_tz = (
        getattr(data_service, "_exchange_tz", None)
        or getattr(getattr(data_service, "store", None), "exchange_tz", None)
        or getattr(getattr(data_service, "store", None), "_exchange_tz", None)
        or getattr(calendar, "tz", None)
        or getattr(calendar, "_tz", None)
        or _DEFAULT_EXCH_TZ
    )
    if callable(exch_tz):
        exch_tz = exch_tz()

    start_dt = datetime.combine(start, time(0, 0), tzinfo=exch_tz).astimezone(UTC)
    end_dt = datetime.combine(end, time(23, 59, 59, 999999), tzinfo=exch_tz).astimezone(UTC)

    def _bar_date(b: Any) -> date:
        ts: datetime = b.start_ts
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        res_date: date = ts.astimezone(exch_tz).date()
        return res_date

    # 1. Stored bars inspection (cache-only)
    stored_bars = []
    try:
        data_slice = data_service.get(
            series_key,
            start=start_dt,
            end=end_dt,
            source=resolved.source,
            download=resolved.adjustment,
            cache_only=True,
            adjust=resolved.adjust,
        )
        stored_bars = [b for b in data_slice.bars if start <= _bar_date(b) <= end]
    except Exception:
        stored_bars = []

    stored_days_set = {_bar_date(b) for b in stored_bars}
    if stored_bars:
        report.first_stored_day = min(stored_days_set)
        report.last_stored_day = max(stored_days_set)

    # 2. Coverage records inspection
    dataset_id = resolved.dataset_id
    cov_recs = manifest.get_coverage_days(
        dataset_id,
        start_day=start.isoformat(),
        end_day=end.isoformat(),
    )
    if not cov_recs:
        alt_id = f"{inst_str}_{timeframe}_{resolved.source}"
        cov_recs = manifest.get_coverage_days(
            alt_id,
            start_day=start.isoformat(),
            end_day=end.isoformat(),
        )
    if not cov_recs:
        alt_adj = "raw" if resolved.adjustment == "adjusted" else "adjusted"
        alt_ds_id = dataset_id_for_key(
            SeriesKey(canonical_inst, "bars", timeframe, alt_adj, resolved.source)
        )
        cov_recs = manifest.get_coverage_days(
            alt_ds_id,
            start_day=start.isoformat(),
            end_day=end.isoformat(),
        )
    cov_map = {r.day: r for r in cov_recs}

    # 3. Check coverage_without_bars (COMPLETE in coverage but no bars stored)
    for c_day_str, r in sorted(cov_map.items()):
        st = str(r.status)
        if st in ("COMPLETE", "CoverageStatus.COMPLETE") or (
            hasattr(r.status, "name") and r.status.name == "COMPLETE"
        ):
            d = date.fromisoformat(c_day_str)
            if d not in stored_days_set:
                report.coverage_without_bars.append(d)

    # 4. Check PENDING or non-final EMPTY days
    for c_day_str, r in sorted(cov_map.items()):
        st = str(r.status)
        d = date.fromisoformat(c_day_str)
        if st in ("PENDING", "CoverageStatus.PENDING") or (
            hasattr(r.status, "name") and r.status.name == "PENDING"
        ):
            report.pending_empty_days.append(d)
        elif st in ("EMPTY", "CoverageStatus.EMPTY") or (
            hasattr(r.status, "name") and r.status.name == "EMPTY"
        ):
            if not is_empty_record_final(r, calendar=calendar, manifest=manifest, params=params):
                report.pending_empty_days.append(d)

    # 5. Check quarantined records
    q_records = manifest.get_quarantine(status="quarantined")
    for q in q_records:
        if symbol in q.chunk or inst_str in q.chunk or symbol in q.id:
            report.quarantined_days.append(f"{q.id} ({q.reason})")

    # 6. Missing sessions
    for sess in expected_sessions:
        if sess not in stored_days_set:
            rec = cov_map.get(sess.isoformat())
            if rec:
                st = str(rec.status)
                if st in ("EMPTY", "CoverageStatus.EMPTY") or (
                    hasattr(rec.status, "name") and rec.status.name == "EMPTY"
                ):
                    if is_empty_record_final(
                        rec, calendar=calendar, manifest=manifest, params=params
                    ):
                        continue
            report.missing_sessions.append(sess)

    # 7. Warm-up bars inspection
    if warmup_bars_count > 0:
        mult = 3 if timeframe == "1d" else 1
        w_days = max(10, warmup_bars_count * mult)
        w_start_dt = start_dt - timedelta(days=w_days)
        w_bars = []
        try:
            w_slice = data_service.get(
                series_key,
                start=w_start_dt,
                end=start_dt,
                source=resolved.source,
                download=resolved.adjustment,
                cache_only=True,
                adjust=resolved.adjust,
            )
            w_bars = [b for b in w_slice.bars if b.end_ts <= start_dt]
        except Exception:
            w_bars = []
        report.warmup_bars_found = len(w_bars)
        report.warmup_bars_needed = warmup_bars_count
        if len(w_bars) < warmup_bars_count:
            report.insufficient_warmup = True

    # 8. Compile problems and exact fix commands
    today = date.today()
    earliest_missing_date: date | None = None
    if report.missing_sessions:
        earliest_missing_date = min(report.missing_sessions)
        report.problems.append(
            f"Missing {len(report.missing_sessions)} sessions between {start} and {end} "
            f"(earliest: {earliest_missing_date.isoformat()})"
        )

    if report.pending_empty_days:
        p_earliest = min(report.pending_empty_days)
        if earliest_missing_date is None or p_earliest < earliest_missing_date:
            earliest_missing_date = p_earliest
        report.problems.append(
            f"Found {len(report.pending_empty_days)} PENDING or non-final EMPTY days"
        )

    if report.coverage_without_bars:
        c_earliest = min(report.coverage_without_bars)
        if earliest_missing_date is None or c_earliest < earliest_missing_date:
            earliest_missing_date = c_earliest
        report.problems.append(
            f"Found {len(report.coverage_without_bars)} COMPLETE coverage days without stored bars"
        )

    if report.quarantined_days:
        report.problems.append(f"Quarantined data: {'; '.join(report.quarantined_days)}")

    if report.insufficient_warmup:
        w_start_date = start - timedelta(days=max(10, warmup_bars_count * 3))
        if earliest_missing_date is None or w_start_date < earliest_missing_date:
            earliest_missing_date = w_start_date
        report.problems.append(
            f"Insufficient history for warm-up: found {report.warmup_bars_found} bars, "
            f"need {report.warmup_bars_needed}"
        )

    if report.problems:
        ref_date = earliest_missing_date if earliest_missing_date is not None else start
        days_n = max(7, (today - ref_date).days + 1)
        target = f"--group {group}" if group else f"--symbols {symbol}"
        cmd = (
            f"algodesk data accumulate {target} --source {source} "
            f"--timeframe {timeframe} --days {days_n}"
        )
        report.fix_commands.append(cmd)

    return report


def check_backtest_readiness(
    symbols: list[str],
    start: date,
    end: date,
    data_service: DataService,
    manifest: DataManifest,
    calendar: Any,
    timeframe: str = "1d",
    source: str = "upstox",
    warmup_bars_count: int = 50,
    group: str | None = None,
    as_of: str = "",
    universe_hash: str = "",
    params: Any = None,
    adjust: str = "splits",
) -> ReadinessReport:
    """Execute complete readiness check across all symbols in the universe."""
    # Resolve expected trading sessions between start and end
    expected_sessions: list[date] = []
    curr = start
    while curr <= end:
        if is_trading_session(curr, calendar=calendar, manifest=manifest):
            expected_sessions.append(curr)
        curr += timedelta(days=1)

    report = ReadinessReport(
        start=start,
        end=end,
        group=group,
        as_of=as_of or start.isoformat(),
        universe_hash=universe_hash,
        members=list(symbols),
    )

    remed_commands_set: set[str] = set()

    for sym in symbols:
        sym_rep = check_symbol_readiness(
            symbol=sym,
            data_service=data_service,
            manifest=manifest,
            calendar=calendar,
            expected_sessions=expected_sessions,
            start=start,
            end=end,
            timeframe=timeframe,
            source=source,
            warmup_bars_count=warmup_bars_count,
            group=group,
            params=params,
            adjust=adjust,
        )
        report.symbol_reports[sym] = sym_rep

        for warn in sym_rep.warnings:
            fix = sym_rep.warning_cleanup_commands[0] if sym_rep.warning_cleanup_commands else ""
            report.warnings.append({"symbol": sym, "warning": warn, "fix": fix})

        if sym_rep.is_ready:
            report.with_data.append(sym)
        else:
            reason = sym_rep.primary_skip_reason or "unknown"
            report.skipped.append({"symbol": sym, "reason": reason})
            for prob in sym_rep.problems:
                fix_cmd = sym_rep.fix_commands[0] if sym_rep.fix_commands else ""
                report.problems.append(
                    {
                        "symbol": sym,
                        "problem": prob,
                        "fix": fix_cmd,
                    }
                )
            for cmd in sym_rep.fix_commands:
                remed_commands_set.add(cmd)

    total = len(symbols)
    ready_count = len(report.with_data)
    skipped_count = len(report.skipped)

    report.coverage_pct = round((ready_count / total * 100.0), 2) if total > 0 else 0.0
    report.missing_pct = round((skipped_count / total * 100.0), 2) if total > 0 else 0.0
    report.remediation_commands = sorted(list(remed_commands_set))

    return report
