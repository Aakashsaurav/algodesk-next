"""Compare tick-built 1m bars against vendor source 1m bars.

Rebuilds 1m bars from recorded ticks in memory using the mode-invariant BarBuilder
and evaluates bar count, close price differences, and volume differences against
the source without persisting or mutating data.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

from algodesk.api.alias import resolve_alias
from algodesk.api.errors import DataError
from algodesk.api.market import Bar, InstrumentId, SeriesKey, Tick
from algodesk.data.schema import row_to_market_event


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


@dataclass
class CompareReport:
    """Aggregated comparison report across requested symbols."""

    date_val: date
    source: str
    tolerance_pct: float
    results: list[SymbolCompareResult] = field(default_factory=list)


def _load_ticks_for_symbol_date(
    runtime: Any,
    symbol: str,
    target_date: date,
) -> list[Tick]:
    """Load recorded ticks from Parquet files for symbol and date."""
    manifest_db = runtime.paths.data / "state" / "data_manifest.db"
    if not manifest_db.exists() and (runtime.paths.data / "manifest.db").exists():
        manifest_db = runtime.paths.data / "manifest.db"
    if not manifest_db.exists():
        return []

    import pyarrow.parquet as pq

    from algodesk.data.manifest import DataManifest

    m = DataManifest(manifest_db)
    inst_canonical = str(resolve_alias(symbol))
    target_date_str = target_date.isoformat()
    files_to_read: list[Path] = []
    try:
        datasets = {ds.dataset_id: ds for ds in m.list_datasets(kind="ticks")}
        for p in m.get_parts():
            ds = datasets.get(p.dataset_id)
            if ds and (ds.instrument == inst_canonical or ds.instrument == symbol):
                if p.first_ts and p.first_ts[:10] == target_date_str:
                    path = Path(p.path)
                    if not path.is_absolute():
                        path = runtime.paths.data / path
                    if path.is_file():
                        files_to_read.append(path)
    finally:
        m.close()

    inst_id = InstrumentId(inst_canonical)
    ticks: list[Tick] = []
    for f in files_to_read:
        tbl = pq.read_table(f)
        for row in tbl.to_pylist():
            ev = row_to_market_event(inst_id, row)
            if isinstance(ev, Tick) and ev.exch_ts.date() == target_date:
                ticks.append(ev)

    ticks.sort(key=lambda t: t.exch_ts)
    return ticks


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
    return built


def _compare_bar_series(
    built_bars: Sequence[Bar],
    source_bars: Sequence[Bar],
    tolerance_pct: float,
    symbol: str,
) -> SymbolCompareResult:
    """Compute difference metrics between matched tick-built and source bars."""
    src_by_ts = {b.start_ts: b for b in source_bars}
    close_diffs: list[float] = []
    vol_diffs: list[int] = []
    above_tol = 0

    matched = 0
    for b in built_bars:
        if s_bar := src_by_ts.get(b.start_ts):
            matched += 1
            c_diff = abs(b.close - s_bar.close)
            close_diffs.append(c_diff)
            pct = (c_diff / s_bar.close * 100) if s_bar.close > 0 else 0.0
            if pct > tolerance_pct:
                above_tol += 1
            vol_diffs.append(abs(b.volume - s_bar.volume))

    max_c = max(close_diffs) if close_diffs else 0.0
    med_c = statistics.median(close_diffs) if close_diffs else 0.0
    max_v = max(vol_diffs) if vol_diffs else 0
    med_v = float(statistics.median(vol_diffs)) if vol_diffs else 0.0

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
    )


def _get_data_dir(runtime: Any) -> Path:
    """Resolve data directory from runtime or path."""
    if isinstance(runtime, Path):
        return runtime / "data" if (runtime / "data").exists() else runtime
    return Path(runtime.paths.data) if hasattr(runtime, "paths") else Path(runtime.root) / "data"


def _resolve_symbols_for_date(
    runtime: Any,
    target_date: date,
    symbols: Sequence[str] | None,
) -> list[str]:
    """Resolve symbol list for compare from explicit symbols or manifest."""
    if symbols:
        return list(symbols)
    d_dir = _get_data_dir(runtime)
    manifest_db = d_dir / "state" / "data_manifest.db"
    if not manifest_db.exists() and (d_dir / "manifest.db").exists():
        manifest_db = d_dir / "manifest.db"
    if not manifest_db.exists():
        return []
    from algodesk.data.manifest import DataManifest

    m = DataManifest(manifest_db)
    found: list[str] = []
    try:
        datasets = {ds.dataset_id: ds for ds in m.list_datasets(kind="ticks")}
        for p in m.get_parts():
            if (ds := datasets.get(p.dataset_id)) and p.first_ts:
                if p.first_ts[:10] == target_date.isoformat() and ds.instrument not in found:
                    found.append(ds.instrument)
    finally:
        m.close()
    return found


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
) -> SymbolCompareResult:
    """Compare single symbol tick-built bars against source bars."""
    ticks = _load_ticks_for_symbol_date(runtime, sym, date_val)
    built_bars = _build_bars_from_ticks(ticks, builder_factory=builder_factory)

    key = SeriesKey(
        instrument=InstrumentId(sym if ":" in sym else f"NSE:EQ:{sym}"),
        kind="bars",
        timeframe="1m",
        adjustment="raw",
        source=source,
    )
    source_bars: list[Bar] = []
    try:
        d_slice = ds.get(series=key, start=start_dt, end=end_dt, download="raw")
        source_bars = list(d_slice.bars) if d_slice else []
    except Exception:
        pass

    return _compare_bar_series(built_bars, source_bars, tol, sym)


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
    utc = UTC
    start_dt = datetime.combine(date_val, time(0, 0), tzinfo=utc)
    end_dt = datetime.combine(date_val, time(23, 59, 59), tzinfo=utc)
    report = CompareReport(date_val=date_val, source=source, tolerance_pct=tol)

    for sym in sym_list:
        report.results.append(
            _compare_single_symbol(
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
        )
    return report
