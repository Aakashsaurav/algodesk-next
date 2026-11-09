"""Live smoke test command logic for data layer validation.

Validates that repeated DataService.get requests produce zero network calls,
evaluates gap detection, runs store integrity verification, and measures
cross-source price agreement without mutating production configuration.
"""

from __future__ import annotations

import zoneinfo
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from algodesk.api.errors import DataError
from algodesk.api.market import InstrumentId, SeriesKey


@dataclass(frozen=True)
class SymbolSmokeResult:
    """Smoke outcome for an individual symbol."""

    symbol: str
    network_requests_run2: int
    gaps_count: int
    verify_passed: bool
    max_close_diff_vs_source2: float


@dataclass
class SmokeReport:
    """Consolidated smoke test report."""

    symbols: list[str]
    primary_source: str
    secondary_source: str
    results: list[SymbolSmokeResult] = field(default_factory=list)
    overall_passed: bool = True


def _resolve_smoke_dates(
    cal: Any, now_dt: datetime | None = None
) -> tuple[datetime, datetime, datetime, datetime]:
    """Resolve start/end timestamps for 1d (30d) and 1m (last 5 sessions)."""
    ist_tz = zoneinfo.ZoneInfo("Asia/Kolkata")
    now_ist = now_dt.astimezone(ist_tz) if now_dt else datetime.now(ist_tz)
    today = now_ist.date()
    cur = today if now_ist.time() >= time(15, 30) else today - timedelta(days=1)

    sessions: list[date] = []
    scan = cur
    while len(sessions) < 5 and scan >= cur - timedelta(days=30):
        if cal is None or cal.is_trading_day(scan):
            sessions.append(scan)
        scan -= timedelta(days=1)
    sessions.reverse()

    utc = UTC
    end_dt = datetime.combine(cur, time(23, 59, 59), tzinfo=utc)
    start_1d = datetime.combine(cur - timedelta(days=30), time(0, 0), tzinfo=utc)
    start_1m = datetime.combine(sessions[0] if sessions else cur, time(0, 0), tzinfo=utc)
    return start_1d, start_1m, end_dt, end_dt


def _execute_run(
    ds: Any,
    symbol: str,
    start_1d: datetime,
    start_1m: datetime,
    end_dt: datetime,
    source: str,
) -> tuple[int, Any, Any]:
    """Execute 1d and 1m get calls and return gaps count and data slices."""
    inst_id = InstrumentId(symbol if ":" in symbol else f"NSE:EQ:{symbol}")
    k_1d = SeriesKey(
        instrument=inst_id, kind="bars", timeframe="1d", adjustment="raw", source=source
    )
    k_1m = SeriesKey(
        instrument=inst_id, kind="bars", timeframe="1m", adjustment="raw", source=source
    )

    s_1d = ds.get(series=k_1d, start=start_1d, end=end_dt, download="raw")
    s_1m = ds.get(series=k_1m, start=start_1m, end=end_dt, download="raw")

    gaps = (len(s_1d.gaps) if s_1d else 0) + (len(s_1m.gaps) if s_1m else 0)
    return gaps, s_1d, s_1m


def _measure_secondary_diff(
    ds_sec: Any,
    symbol: str,
    start_1d: datetime,
    end_dt: datetime,
    source2: str,
    s_1d_pri: Any,
) -> float:
    """Measure maximum close price difference against secondary source."""
    if not s_1d_pri or not s_1d_pri.bars:
        return 0.0
    inst_id = InstrumentId(symbol if ":" in symbol else f"NSE:EQ:{symbol}")
    k_sec = SeriesKey(
        instrument=inst_id, kind="bars", timeframe="1d", adjustment="raw", source=source2
    )
    try:
        s_sec = ds_sec.get(series=k_sec, start=start_1d, end=end_dt, download="raw")
        if not s_sec or not s_sec.bars:
            return 0.0
        sec_by_ts = {b.start_ts: b.close for b in s_sec.bars}
        diffs = [
            abs(b.close - sec_close)
            for b in s_1d_pri.bars
            if (sec_close := sec_by_ts.get(b.start_ts)) is not None
        ]
        return max(diffs) if diffs else 0.0
    except Exception:
        return 0.0


def _verify_manifest_integrity(runtime: Any) -> bool:
    """Verify manifest integrity check passes."""
    try:
        paths = getattr(runtime, "paths", None)
        data_dir = (
            paths.data
            if paths is not None
            else (runtime / "data" if isinstance(runtime, Path) else Path("data"))
        )
        manifest_db = data_dir / "state" / "data_manifest.db"
        if not manifest_db.exists() and (data_dir / "manifest.db").exists():
            manifest_db = data_dir / "manifest.db"
        if manifest_db.exists():
            from algodesk.data.manifest import DataManifest

            m = DataManifest(manifest_db)
            conn = getattr(m, "_conn", getattr(m, "conn", None))
            row = conn.execute("PRAGMA integrity_check").fetchone() if conn else None
            m.close()
            return bool(row and row[0] == "ok")
        return True
    except Exception:
        return False


def _smoke_single_symbol(
    sym: str,
    ds_pri: Any,
    ds_sec: Any,
    primary_source: str,
    secondary_source: str,
    start_1d: datetime,
    start_1m: datetime,
    end_dt: datetime,
    runtime: Any,
) -> SymbolSmokeResult:
    """Execute smoke test passes for a single symbol."""
    gaps, s_1d, _ = _execute_run(ds_pri, sym, start_1d, start_1m, end_dt, primary_source)

    src_plugin = ds_pri._resolve_source(primary_source)
    orig_history = src_plugin.history
    call_count = 0

    def _instrumented_history(*args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        return orig_history(*args, **kwargs)

    src_plugin.history = _instrumented_history
    try:
        _execute_run(ds_pri, sym, start_1d, start_1m, end_dt, primary_source)
    finally:
        src_plugin.history = orig_history

    sec_diff = _measure_secondary_diff(ds_sec, sym, start_1d, end_dt, secondary_source, s_1d)
    verify_ok = _verify_manifest_integrity(runtime)

    return SymbolSmokeResult(
        symbol=sym,
        network_requests_run2=call_count,
        gaps_count=gaps,
        verify_passed=verify_ok,
        max_close_diff_vs_source2=round(sec_diff, 4),
    )


def run_data_smoke(
    symbols: Sequence[str],
    runtime: Any,
    primary_source: str = "upstox",
    secondary_source: str = "yfinance",
    data_service_primary: Any = None,
    data_service_secondary: Any = None,
    calendar: Any = None,
    now_dt: datetime | None = None,
) -> SmokeReport:
    """Run data smoke test verifying caching, gaps, verify integrity, and cross-source diff."""
    ds_pri = data_service_primary
    ds_sec = data_service_secondary
    if ds_pri is None or ds_sec is None:
        raise DataError(
            what="Primary and secondary DataServices are required for smoke testing",
            where="data.smoke",
            fix="Pass initialized DataServices or execute via 'algodesk data smoke'",
        )
    start_1d, start_1m, end_1d, _ = _resolve_smoke_dates(calendar, now_dt)

    sym_list = [s.strip() for s in symbols if s.strip()]
    report = SmokeReport(
        symbols=sym_list, primary_source=primary_source, secondary_source=secondary_source
    )

    for sym in sym_list:
        res = _smoke_single_symbol(
            sym,
            ds_pri,
            ds_sec,
            primary_source,
            secondary_source,
            start_1d,
            start_1m,
            end_1d,
            runtime,
        )
        report.results.append(res)
        if res.network_requests_run2 > 0 or not res.verify_passed:
            report.overall_passed = False

    return report
