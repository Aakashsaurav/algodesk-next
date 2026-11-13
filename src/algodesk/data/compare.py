"""Compare tick-built 1m bars against vendor source 1m bars.

Rebuilds 1m bars from recorded ticks in memory using the mode-invariant BarBuilder
and evaluates bar count, close price differences, and volume differences against
the source without persisting or mutating data.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from algodesk.api.alias import resolve_alias
from algodesk.api.errors import DataError
from algodesk.api.market import Bar, InstrumentId, SeriesKey, Tick
from algodesk.data.accumulate import get_source_retention_days
from algodesk.data.schema import row_to_market_event

_EXCH_TZ = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class SymbolCompareResult:
    """Comparison metrics for a single symbol between tick-built and source bars."""

    symbol: str
    built_count: int
    source_count: int
    matched_count: int
    max_close_diff: float
    median_close_diff: float
    count_above_tolerance: int
    max_volume_diff: int
    median_volume_diff: float
    built_first_ts: datetime | None = None
    built_last_ts: datetime | None = None
    source_first_ts: datetime | None = None
    source_last_ts: datetime | None = None
    built_only_count: int = 0
    source_only_count: int = 0
    first_bar_vol_excluded: bool = False
    src_missing_explanation: str | None = None


@dataclass
class CompareReport:
    """Aggregated comparison report across requested symbols."""

    date_val: date
    source: str
    tolerance_pct: float
    results: list[SymbolCompareResult] = field(default_factory=list)
    explanation_lines: list[str] = field(default_factory=list)


def _get_data_dir(runtime: Any) -> Path:
    """Resolve data directory from runtime or path."""
    if isinstance(runtime, Path):
        return runtime / "data" if (runtime / "data").exists() else runtime
    return Path(runtime.paths.data) if hasattr(runtime, "paths") else Path(runtime.root) / "data"


def _resolve_canonical_id(runtime: Any, symbol: str) -> InstrumentId:
    """Resolve symbol or alias to canonical InstrumentId."""
    c = resolve_alias(symbol)
    if ":" in str(c):
        return InstrumentId(str(c))
    d_dir = _get_data_dir(runtime)
    manifest_db = d_dir / "state" / "data_manifest.db"
    if not manifest_db.exists() and (d_dir / "manifest.db").exists():
        manifest_db = d_dir / "manifest.db"
    if manifest_db.exists():
        from algodesk.data.manifest import DataManifest

        m = DataManifest(manifest_db)
        try:
            m_res = m.resolve_alias(c)
            if ":" in str(m_res):
                return InstrumentId(str(m_res))
            from algodesk.data.catalog import resolve_selector

            matched = resolve_selector(c, m)
            if matched:
                return InstrumentId(matched[0])
        except Exception:
            pass
        finally:
            m.close()
    return InstrumentId(f"NSE:EQ:{c}")


def _find_tick_files_for_symbol_date(
    runtime: Any,
    inst_id: InstrumentId,
    target_date: date,
) -> list[Path]:
    """Find all parquet files containing ticks for given instrument and IST date."""
    from algodesk.data.layout import instrument_partition_path

    d_dir = _get_data_dir(runtime)
    part = instrument_partition_path(inst_id)
    sym = inst_id.split(":")[-1] if ":" in inst_id else inst_id

    dates_to_check = [
        target_date.isoformat(),
        (target_date - timedelta(days=1)).isoformat(),
    ]
    files: list[Path] = []
    seen: set[str] = set()

    def _add_if_exists(p: Path) -> None:
        if p.is_file():
            canon = str(p.resolve())
            if canon not in seen:
                seen.add(canon)
                files.append(p)

    for base in [d_dir / "ticks", d_dir / "recorded" / "ticks"]:
        for d_str in dates_to_check:
            p_dir = base / part / f"date={d_str}"
            if p_dir.is_dir():
                for f in sorted(p_dir.glob("*.parquet")):
                    _add_if_exists(f)
            bare_dir = base / sym / f"date={d_str}"
            if bare_dir.is_dir():
                for f in sorted(bare_dir.glob("*.parquet")):
                    _add_if_exists(f)

    manifest_db = d_dir / "state" / "data_manifest.db"
    if not manifest_db.exists() and (d_dir / "manifest.db").exists():
        manifest_db = d_dir / "manifest.db"
    if manifest_db.exists():
        from algodesk.data.manifest import DataManifest

        m = DataManifest(manifest_db)
        try:
            for cand in (str(inst_id), sym):
                for f_rec in m.get_files(instrument=cand, kind="ticks"):
                    if f_rec.date in dates_to_check:
                        p = Path(f_rec.path)
                        if not p.is_absolute():
                            p = d_dir / p
                        _add_if_exists(p)
        finally:
            m.close()

    return files


def _load_ticks_for_symbol_date(
    runtime: Any,
    symbol: str,
    target_date: date,
) -> tuple[InstrumentId, list[Tick]]:
    """Load recorded ticks from Parquet files for symbol and IST date."""
    inst_id = _resolve_canonical_id(runtime, symbol)
    files = _find_tick_files_for_symbol_date(runtime, inst_id, target_date)
    if not files:
        return inst_id, []

    import pyarrow.parquet as pq

    ticks: list[Tick] = []
    for f in files:
        try:
            tbl = pq.read_table(f)
            for row in tbl.to_pylist():
                ev = row_to_market_event(inst_id, row)
                if isinstance(ev, Tick) and ev.exch_ts.astimezone(_EXCH_TZ).date() == target_date:
                    ticks.append(ev)
        except Exception:
            continue

    ticks.sort(key=lambda t: t.exch_ts)
    return inst_id, ticks


def _build_bars_from_ticks(ticks: Sequence[Tick], builder_factory: Any = None) -> list[Bar]:
    """Rebuild 1m bars in memory using BarBuilder."""
    built: list[Bar] = []
    if builder_factory is not None:
        bb = builder_factory(built.append)
    else:
        raise DataError(
            what="BarBuilder factory is required for bar reconstruction",
            where="data.compare",
            fix="Pass builder_factory or execute via 'algodesk data compare'",
        )
    for t in ticks:
        bb.on_tick(t)
    if hasattr(bb, "_active_bars") and hasattr(bb, "close_bar_timer"):
        for k, act in list(bb._active_bars.items()):
            bb.close_bar_timer(k[0], k[1], act.end_ts)
    return built


def _compare_bar_series(
    built_bars: Sequence[Bar],
    source_bars: Sequence[Bar],
    tolerance_pct: float,
    symbol: str,
    mid_session_start: bool | None = None,
) -> SymbolCompareResult:
    """Compute difference metrics between matched tick-built and source bars."""
    src_by_ts = {b.start_ts: b for b in source_bars}
    built_by_ts = {b.start_ts: b for b in built_bars}
    close_diffs: list[float] = []
    vol_diffs: list[int] = []
    above_tol = 0

    is_mid = mid_session_start
    if is_mid is None:
        is_mid = bool(
            source_bars and built_bars and built_bars[0].start_ts > source_bars[0].start_ts
        )

    vol_excluded = False
    matched = 0
    for i, b in enumerate(built_bars):
        if s_bar := src_by_ts.get(b.start_ts):
            matched += 1
            c_diff = abs(b.close - s_bar.close)
            close_diffs.append(c_diff)
            pct = (c_diff / s_bar.close * 100) if s_bar.close > 0 else 0.0
            if pct > tolerance_pct:
                above_tol += 1
            if is_mid and i == 0:
                vol_excluded = True
            else:
                vol_diffs.append(abs(b.volume - s_bar.volume))

    max_c = max(close_diffs) if close_diffs else 0.0
    med_c = statistics.median(close_diffs) if close_diffs else 0.0
    max_v = max(vol_diffs) if vol_diffs else 0
    med_v = float(statistics.median(vol_diffs)) if vol_diffs else 0.0

    built_first = built_bars[0].start_ts if built_bars else None
    built_last = built_bars[-1].start_ts if built_bars else None
    source_first = source_bars[0].start_ts if source_bars else None
    source_last = source_bars[-1].start_ts if source_bars else None
    built_only = len(set(built_by_ts) - set(src_by_ts))
    source_only = len(set(src_by_ts) - set(built_by_ts))

    src_missing_msg = None
    if built_only > 0:
        src_missing_msg = (
            f"SRC MISSING: {symbol} has {built_only} built bar(s) unmatched because "
            f"vendor source has no bars at those timestamps"
        )

    return SymbolCompareResult(
        symbol=symbol,
        built_count=len(built_bars),
        source_count=len(source_bars),
        matched_count=matched,
        max_close_diff=round(max_c, 4),
        median_close_diff=round(med_c, 4),
        count_above_tolerance=above_tol,
        max_volume_diff=max_v,
        median_volume_diff=round(med_v, 2),
        built_first_ts=built_first,
        built_last_ts=built_last,
        source_first_ts=source_first,
        source_last_ts=source_last,
        built_only_count=built_only,
        source_only_count=source_only,
        first_bar_vol_excluded=vol_excluded,
        src_missing_explanation=src_missing_msg,
    )


def _derive_instrument_from_tick_path(pdir: Path) -> str:
    """Extract canonical instrument string from partitioned tick directory."""
    exch, seg, sym = "", "", ""
    for p in pdir.parts:
        if p.startswith("exchange="):
            exch = p.split("=")[1]
        elif p.startswith("segment="):
            seg = p.split("=")[1]
        elif p.startswith("symbol="):
            sym = p.split("=")[1]
    if exch and seg and sym:
        return f"{exch}:{seg}:{sym}".upper()
    return ""


def _resolve_symbols_for_date(
    runtime: Any,
    target_date: date,
    symbols: Sequence[str] | None,
) -> list[str]:
    """Resolve symbol list for compare from explicit symbols or manifest/layout."""
    if symbols:
        return list(symbols)
    d_dir = _get_data_dir(runtime)
    found: list[str] = []

    manifest_db = d_dir / "state" / "data_manifest.db"
    if not manifest_db.exists() and (d_dir / "manifest.db").exists():
        manifest_db = d_dir / "manifest.db"
    if manifest_db.exists():
        from algodesk.data.manifest import DataManifest

        m = DataManifest(manifest_db)
        try:
            d_str = target_date.isoformat()
            for f in m.get_files(date_str=d_str, kind="ticks"):
                if f.instrument and f.instrument not in found:
                    found.append(f.instrument)
        finally:
            m.close()

    for base in [d_dir / "ticks", d_dir / "recorded" / "ticks"]:
        if base.is_dir():
            for p in base.glob(f"**/date={target_date.isoformat()}/*.parquet"):
                inst = _derive_instrument_from_tick_path(p.parent)
                if inst and inst not in found:
                    found.append(inst)

    return sorted(found)


def _check_source_availability(source: str, target_date: date) -> str | None:
    """Return refusal reason string if target date cannot be served by source."""
    today = datetime.now(_EXCH_TZ).date()
    if target_date > today:
        return f"date {target_date.isoformat()} is in the future"
    if target_date == today:
        src = source.lower()
        if src == "upstox":
            return "same-day candles are not served by the history endpoint"
        if src == "nse_bhavcopy":
            return "bhavcopy is not available for current session until after EOD publish"
    ret_days = get_source_retention_days(source, "1m")
    if target_date < (today - timedelta(days=ret_days)):
        return f"date beyond the source's retention ({ret_days}d for {source} 1m)"
    return None


def _fetch_source_bars(
    ds: Any,
    key: SeriesKey,
    start_dt: datetime,
    end_dt: datetime,
    date_val: date,
) -> list[Bar]:
    """Fetch 1m bars from DataService for IST session date."""
    try:
        d_slice = ds.get(series=key, start=start_dt, end=end_dt, download="raw")
        bars = (
            [b for b in d_slice.bars if b.start_ts.astimezone(_EXCH_TZ).date() == date_val]
            if d_slice
            else []
        )
        if not bars:
            d_slice = ds.get(series=key, start=start_dt, end=end_dt, download="raw", refresh=True)
            bars = (
                [b for b in d_slice.bars if b.start_ts.astimezone(_EXCH_TZ).date() == date_val]
                if d_slice
                else []
            )
        return bars
    except Exception:
        return []


def _compare_single_symbol(
    sym: str,
    date_val: date,
    start_dt: datetime,
    end_dt: datetime,
    source: str,
    ds: Any,
    tol: float,
    runtime: Any,
    builder_factory: Any = None,
) -> tuple[SymbolCompareResult | None, str | None, str | None]:
    """Compare single symbol tick-built bars against source bars.

    Returns (result, built_refusal_reason, source_refusal_reason).
    """
    inst_id, ticks = _load_ticks_for_symbol_date(runtime, sym, date_val)
    built_bars = _build_bars_from_ticks(ticks, builder_factory=builder_factory) if ticks else []

    key = SeriesKey(
        instrument=inst_id,
        kind="bars",
        timeframe="1m",
        adjustment="raw",
        source=source,
    )
    source_bars = _fetch_source_bars(ds, key, start_dt, end_dt, date_val)

    built_refusal = None
    if not built_bars:
        built_refusal = "no ticks recorded" if not ticks else "no bars built from ticks"

    source_refusal = None
    if not source_bars:
        source_refusal = _check_source_availability(source, date_val) or "source returned nothing"

    if built_refusal or source_refusal:
        return None, built_refusal, source_refusal

    return _compare_bar_series(built_bars, source_bars, tol, sym), None, None


def _resolve_tolerance(runtime: Any, tolerance_pct: float | None) -> float:
    if tolerance_pct is not None:
        return tolerance_pct
    p = getattr(runtime, "params", None)
    raw_tol = p.get("data.compare_close_tolerance_pct") if p else None
    return float(raw_tol) if raw_tol is not None else 0.1


def compare_ticks_with_source(
    date_val: date,
    symbols: Sequence[str] | None,
    source: str,
    runtime: Any,
    tolerance_pct: float | None = None,
    data_service: Any = None,
    builder_factory: Any = None,
) -> CompareReport:
    """Compare in-memory tick-built 1m bars against vendor source 1m bars."""
    tol = _resolve_tolerance(runtime, tolerance_pct)
    sym_list = _resolve_symbols_for_date(runtime, date_val, symbols)
    if not sym_list:
        raise DataError(
            what=f"No recorded ticks found on {date_val.isoformat()}",
            where="data.compare",
            fix="Specify --symbols or record live/synthetic ticks for this date",
        )

    ds = data_service
    if ds is None:
        raise DataError(
            what="DataService is required for compare",
            where="data.compare",
            fix="Pass an active DataService or execute via 'algodesk data compare'",
        )

    start_dt = datetime.combine(date_val, time(0, 0), tzinfo=_EXCH_TZ).astimezone(UTC)
    end_dt = datetime.combine(date_val, time(23, 59, 59), tzinfo=_EXCH_TZ).astimezone(UTC)
    report = CompareReport(date_val=date_val, source=source, tolerance_pct=tol)

    refusals: list[tuple[str, str | None, str | None]] = []
    for sym in sym_list:
        res, b_err, s_err = _compare_single_symbol(
            sym,
            date_val,
            start_dt,
            end_dt,
            source,
            ds,
            tol,
            runtime,
            builder_factory=builder_factory,
        )
        if b_err or s_err:
            refusals.append((sym, b_err, s_err))
        elif res is not None:
            report.results.append(res)

    if refusals:
        lines: list[str] = []
        for sym, b_err, s_err in refusals:
            parts: list[str] = []
            if b_err:
                parts.append(f"built side: {b_err}")
            if s_err:
                parts.append(f"source side: {s_err}")
            lines.append(f"  - {sym}: {'; '.join(parts)}")
        err_msg = (
            f"Refusing comparison on {date_val.isoformat()} for source '{source}':\n"
            + "\n".join(lines)
        )
        raise DataError(
            what=err_msg,
            where="data.compare",
            fix="Ensure ticks are recorded for the date and source retains 1m data for this date",
        )

    for r in report.results:
        if r.src_missing_explanation and r.src_missing_explanation not in report.explanation_lines:
            report.explanation_lines.append(r.src_missing_explanation)

    return report
