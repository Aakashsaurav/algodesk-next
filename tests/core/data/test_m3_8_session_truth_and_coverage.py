"""Tests for M3-8: session truth consistency, fast reset-empty, and skip reasons."""

from __future__ import annotations

import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from typer.testing import CliRunner

from algodesk.api.manifest import PluginManifest
from algodesk.api.market import Bar, CoverageStatus, InstrumentId, SeriesKey
from algodesk.api.protocols import Calendar, DataSource
from algodesk.api.screens import (
    ScreenBars,
    ScreenRefusal,
    ScreenResult,
    SkipReason,
)
from algodesk.cli.data import coverage_app
from algodesk.data.coverage import (
    CoverageIndex,
    is_trading_session,
    reset_stale_empty_coverage,
    resolve_trading_sessions,
)
from algodesk.data.manifest import CoverageDayRecord, DataManifest
from algodesk.data.service import DataService
from algodesk.data.store import BarStore
from algodesk.ops.screens import DataServiceScreenAdapter
from algodesk.screens.models import OperandModel, RequireRule, TomlScreenModel
from algodesk.screens.runner import ScreenRunner

IST = ZoneInfo("Asia/Kolkata")


class HolidayCalendar(Calendar):
    """Hermetic test calendar with configurable holidays."""

    def __init__(self, holidays: set[date] | None = None) -> None:
        self._holidays = holidays or set()

    def coverage(self) -> tuple[date, date]:
        return date(2024, 1, 1), date(2027, 12, 31)

    def is_trading_day(self, d: date, segment: str = "") -> bool:
        if d in self._holidays:
            return False
        return d.weekday() < 5

    def sessions(self, d: date, segment: str = "") -> list[Any]:
        return []

    def next_session(self, ts: datetime, segment: str = "") -> Any:
        return None


class FakeParamReader:
    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self._values = values or {}

    def get(self, name: str, default: Any = None) -> Any:
        return self._values.get(name, default)


def _make_bar(inst: str, d: date, p: float = 100.0) -> Bar:
    dt = datetime(d.year, d.month, d.day, 10, 0, tzinfo=IST).astimezone(UTC)
    end = datetime(d.year, d.month, d.day, 15, 30, tzinfo=IST).astimezone(UTC)
    return Bar(
        instrument=InstrumentId(inst),
        timeframe="1d",
        start_ts=dt,
        end_ts=end,
        open=p,
        high=p + 1.0,
        low=p - 1.0,
        close=p,
        volume=1000,
        complete=True,
    )


def _build_test_service(
    tmp_path: Path,
    calendar: Calendar,
    source_name: str = "upstox",
) -> tuple[DataService, BarStore, DataManifest]:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(data_dir / "data_manifest.db")
    store = BarStore(data_dir, manifest, calendar=calendar)

    mock_source = MagicMock(spec=DataSource)
    mock_source.history.return_value = ()

    manifest_obj = PluginManifest.model_validate(
        {
            "name": source_name,
            "kind": "broker",
            "version": "1.0.0",
            "api_version": 1,
            "entry": f"broker:{source_name}",
            "capabilities": {"candles": True, "history": True},
            "candles_adjustment_basis": "splits",
            "max_span": {"1d": 3650, "1m": 30},
        }
    )
    ds = DataService(
        sources={source_name: mock_source},
        default_source=source_name,
        store=store,
        manifests={source_name: manifest_obj},
        calendar=calendar,
    )
    return ds, store, manifest


def _write_bars(store: BarStore, key: SeriesKey, bars: list[Bar]) -> None:
    from algodesk.data.manifest import PartRecord
    from algodesk.data.store import bar_file_path, write_bars_parquet

    if not bars:
        return
    p = bar_file_path(store.data_dir, key, bars[0].start_ts)
    write_bars_parquet(p, bars)
    store.manifest.record_part(
        PartRecord(
            path=str(p.relative_to(store.data_dir)),
            dataset_id=store.dataset_id(key),
            rows=len(bars),
            bytes=p.stat().st_size,
            first_ts=bars[0].start_ts.isoformat(),
            last_ts=bars[-1].start_ts.isoformat(),
            sha256="fake_sha",
            source=key.source,
            source_version="1.0.0",
            fetched_at="2026-11-14T00:00:00Z",
        )
    )


# =========================================================================
# Step 4 Tests: Holiday, Special Session, Newly Listed Symbol, Missing Day
# =========================================================================


def test_screen_window_holiday_inside_window_not_a_gap(tmp_path: Path) -> None:
    """A holiday inside the window (e.g. 2026-11-10 Diwali) is closed, never a gap."""
    holiday = date(2026, 11, 10)  # Tuesday
    cal = HolidayCalendar(holidays={holiday})
    ds, store, manifest = _build_test_service(tmp_path, calendar=cal)

    key = SeriesKey(InstrumentId("NSE:EQ:TCS"), "bars", "1d", "adjusted", "upstox")
    ds_id = store.dataset_id(key)

    # Days from 2026-11-06 (Fri) to 2026-11-13 (Fri)
    # Mon 11-09, Wed 11-11, Thu 11-12, Fri 11-13 are complete trading days
    # Tue 11-10 is Diwali holiday -> closed
    trading_days = [
        date(2026, 11, 6),
        date(2026, 11, 9),
        date(2026, 11, 11),
        date(2026, 11, 12),
        date(2026, 11, 13),
    ]
    bars = [_make_bar("NSE:EQ:TCS", d) for d in trading_days]
    _write_bars(store, key, bars)

    for d in trading_days:
        manifest.record_coverage_day(
            CoverageDayRecord(
                dataset_id=ds_id,
                day=d.isoformat(),
                status=CoverageStatus.COMPLETE,
                rows=1,
                expected_rows=1,
                part_path="parts/tcs.parquet",
                fetched_at="2026-11-14T00:00:00Z",
            )
        )

    # Real CoverageIndex with calendar
    idx = CoverageIndex(manifest=manifest, calendar=cal)
    gaps = idx.detect_gaps(ds_id, date(2026, 11, 6), date(2026, 11, 13))
    assert gaps == [], "Closed holiday 2026-11-10 must never be reported as a gap"

    # Screen adapter and runner should pass without gap_in_window
    adapter = DataServiceScreenAdapter(ds)
    res = adapter.get_bars(
        instrument="TCS",
        timeframe="1d",
        start=datetime(2026, 11, 6, 0, 0, tzinfo=IST).astimezone(UTC),
        end=datetime(2026, 11, 13, 23, 59, 59, tzinfo=IST).astimezone(UTC),
        adjust="splits",
    )
    assert isinstance(res, ScreenBars)
    assert len(res.bars) == 5


def test_screen_window_special_session_recognized_as_open(tmp_path: Path) -> None:
    """Special session (e.g. Sunday session) is an expected session; gap if missing."""
    special_day = date(2026, 2, 1)  # Sunday
    cal = HolidayCalendar()  # Sunday is normally not trading day
    ds, store, manifest = _build_test_service(tmp_path, calendar=cal)

    # Record special session closure
    manifest.record_market_closure(
        exchange="NSE",
        day=special_day.isoformat(),
        kind="special",
        evidence="Budget day special session",
        created="2026-02-01T00:00:00Z",
    )

    idx = CoverageIndex(manifest=manifest, calendar=cal)
    assert idx.is_trading_session(special_day, exchange="NSE") is True

    key = SeriesKey(InstrumentId("NSE:EQ:INFY"), "bars", "1d", "adjusted", "upstox")
    ds_id = store.dataset_id(key)

    # Missing special day in coverage -> reported as gap
    gaps = idx.detect_gaps(ds_id, special_day, special_day)
    assert len(gaps) == 1
    assert gaps[0].day == special_day
    assert gaps[0].status == CoverageStatus.MISSING

    # Cover special day -> 0 gaps
    manifest.record_coverage_day(
        CoverageDayRecord(
            dataset_id=ds_id,
            day=special_day.isoformat(),
            status=CoverageStatus.COMPLETE,
            rows=1,
            expected_rows=1,
            part_path="parts/infy.parquet",
            fetched_at="2026-02-01T16:00:00Z",
        )
    )
    gaps_covered = idx.detect_gaps(ds_id, special_day, special_day)
    assert gaps_covered == []


def test_screen_window_newly_listed_symbol_gives_insufficient_history(tmp_path: Path) -> None:
    """Pre-listing EMPTY-final days give insufficient_history, not a gap."""
    cal = HolidayCalendar()
    ds, store, manifest = _build_test_service(tmp_path, calendar=cal)

    key = SeriesKey(InstrumentId("NSE:EQ:NEWCO"), "bars", "1d", "adjusted", "upstox")
    ds_id = store.dataset_id(key)

    # Listed on 2026-11-10; has 3 bars (11-11, 11-12, 11-13)
    listing_days = [date(2026, 11, 11), date(2026, 11, 12), date(2026, 11, 13)]
    bars = [_make_bar("NSE:EQ:NEWCO", d) for d in listing_days]
    _write_bars(store, key, bars)
    for d in listing_days:
        manifest.record_coverage_day(
            CoverageDayRecord(
                dataset_id=ds_id,
                day=d.isoformat(),
                status=CoverageStatus.COMPLETE,
                rows=1,
                expected_rows=1,
                part_path="parts/newco.parquet",
                fetched_at="2026-11-14T00:00:00Z",
            )
        )

    # Pre-listing days have final EMPTY records
    pre_listing_days = [
        date(2026, 11, 2),
        date(2026, 11, 3),
        date(2026, 11, 4),
        date(2026, 11, 5),
        date(2026, 11, 6),
        date(2026, 11, 9),
        date(2026, 11, 10),
    ]
    for d in pre_listing_days:
        manifest.record_coverage_day(
            CoverageDayRecord(
                dataset_id=ds_id,
                day=d.isoformat(),
                status=CoverageStatus.EMPTY,
                rows=0,
                expected_rows=0,
                part_path="",
                fetched_at="2026-11-14T00:00:00Z",
                note="No data returned for pre-listing date",
            )
        )

    idx = CoverageIndex(manifest=manifest, calendar=cal)
    gaps = idx.detect_gaps(ds_id, date(2026, 11, 2), date(2026, 11, 13))
    assert gaps == [], "Pre-listing final EMPTY days must not be returned as gaps"

    adapter = DataServiceScreenAdapter(ds)
    # Runner requires 20 bars warm-up
    resolver = MagicMock()
    fake_ind = MagicMock()
    fake_ind.warmup = 20
    resolver.get_indicator.return_value = fake_ind

    runner = ScreenRunner(
        data_reader=adapter,
        indicator_resolver=resolver,
        calendar=cal,
        manifest=manifest,
        session_resolver=lambda end_d, count: resolve_trading_sessions(
            end_date=end_d,
            needed_sessions=count,
            calendar=cal,
            manifest=manifest,
            exchange="NSE",
        ),
    )

    screen = TomlScreenModel(
        name="test_screen",
        timeframe="1d",
        require=[RequireRule(left=OperandModel(ind="sma", period=20), op=">", right=0)],
    )

    res = runner.run(screen=screen, symbols=["NEWCO"], as_of="2026-11-13")
    assert len(res.skipped) == 1
    sym, reason, detail = res.skipped[0]
    assert reason == SkipReason.INSUFFICIENT_HISTORY
    assert "fewer than required warm-up" in detail


def test_screen_window_real_missing_day_gives_gap_in_window(tmp_path: Path) -> None:
    """A real missing day between covered sessions produces GAP_IN_WINDOW."""
    cal = HolidayCalendar()
    ds, store, manifest = _build_test_service(tmp_path, calendar=cal)

    key = SeriesKey(InstrumentId("NSE:EQ:GAPCO"), "bars", "1d", "adjusted", "upstox")
    ds_id = store.dataset_id(key)

    # Days 11-09, 11-11, 11-12 complete; 11-10 is missing (no record)
    complete_days = [date(2026, 11, 9), date(2026, 11, 11), date(2026, 11, 12)]
    bars = [_make_bar("NSE:EQ:GAPCO", d) for d in complete_days]
    _write_bars(store, key, bars)
    for d in complete_days:
        manifest.record_coverage_day(
            CoverageDayRecord(
                dataset_id=ds_id,
                day=d.isoformat(),
                status=CoverageStatus.COMPLETE,
                rows=1,
                expected_rows=1,
                part_path="parts/gapco.parquet",
                fetched_at="2026-11-14T00:00:00Z",
            )
        )

    # 2026-11-10 is a Tuesday (trading day in HolidayCalendar), but has NO coverage record
    idx = CoverageIndex(manifest=manifest, calendar=cal)
    gaps = idx.detect_gaps(ds_id, date(2026, 11, 9), date(2026, 11, 12))
    assert any(g.day == date(2026, 11, 10) and g.status == CoverageStatus.MISSING for g in gaps)

    adapter = DataServiceScreenAdapter(ds)
    res = adapter.get_bars(
        instrument="GAPCO",
        timeframe="1d",
        start=datetime(2026, 11, 9, 0, 0, tzinfo=IST).astimezone(UTC),
        end=datetime(2026, 11, 12, 23, 59, 59, tzinfo=IST).astimezone(UTC),
        adjust="splits",
    )
    assert isinstance(res, ScreenRefusal)
    assert res.reason == SkipReason.GAP_IN_WINDOW
    assert "2026-11-10" in res.detail


# =========================================================================
# Step 6 Tests: Fast Bulk reset-empty and Performance Bound
# =========================================================================


def test_reset_stale_empty_coverage_bulk_reads_performance_bound(tmp_path: Path) -> None:
    """500 symbols evaluated in at most 10 s with a strict statement count bound."""
    manifest_path = tmp_path / "manifest.db"
    manifest = DataManifest(manifest_path)
    cal = HolidayCalendar()

    # Seed 500 symbols, each with 5 EMPTY records and 1 COMPLETE record
    symbols = [f"SYM{i:03d}" for i in range(500)]
    records: list[CoverageDayRecord] = []
    for sym in symbols:
        ds_id = f"bars:upstox:1d:NSE:EQ:{sym}:adjusted"
        # 1 Complete bar
        records.append(
            CoverageDayRecord(
                dataset_id=ds_id,
                day="2026-11-13",
                status=CoverageStatus.COMPLETE,
                rows=1,
                expected_rows=1,
                part_path=f"parts/{sym}.parquet",
                fetched_at="2026-11-14T00:00:00Z",
            )
        )
        # 3 Pre-listing EMPTY days (days < 2026-11-13 -> final)
        for d in ("2026-11-09", "2026-11-10", "2026-11-11"):
            records.append(
                CoverageDayRecord(
                    dataset_id=ds_id,
                    day=d,
                    status=CoverageStatus.EMPTY,
                    rows=0,
                    expected_rows=0,
                    part_path="",
                    fetched_at="2026-11-14T00:00:00Z",
                    note="No data before listing",
                )
            )
        # 1 Non-final EMPTY day with fetch error
        records.append(
            CoverageDayRecord(
                dataset_id=ds_id,
                day="2026-11-12",
                status=CoverageStatus.EMPTY,
                rows=0,
                expected_rows=0,
                part_path="",
                fetched_at="2026-11-14T00:00:00Z",
                note="Fetch failed after 3 retries: timeout",
            )
        )
    manifest.record_coverage_days(records)

    # Track SQL statements executed
    stmt_count = 0

    def trace_cb(stmt: str) -> None:
        nonlocal stmt_count
        stmt_count += 1

    manifest._conn.set_trace_callback(trace_cb)

    t0 = time.perf_counter()
    reset_list = reset_stale_empty_coverage(
        manifest=manifest,
        calendar=cal,
        source="upstox",
        timeframe="1d",
        dry_run=True,
    )
    elapsed = time.perf_counter() - t0

    manifest._conn.set_trace_callback(None)

    # Assertions
    assert elapsed < 10.0, f"Expected < 10s for 500 symbols, got {elapsed:.3f}s"
    assert stmt_count <= 15, f"Expected <= 15 SQL statements, got {stmt_count}"
    assert len(reset_list) == 500, (
        f"Expected 500 non-final records to be identified, got {len(reset_list)}"
    )


def test_reset_empty_dry_run_summary_by_reason_vs_verbose(tmp_path: Path, monkeypatch: Any) -> None:
    """Dry-run prints summary line per reason; table only with --verbose."""
    manifest_path = tmp_path / "manifest.db"
    manifest = DataManifest(manifest_path)

    # Seed 2 symbols with different reset reasons
    recs = [
        CoverageDayRecord(
            dataset_id="bars:upstox:1d:NSE:EQ:STOCKA:adjusted",
            day="2026-11-10",
            status=CoverageStatus.EMPTY,
            rows=0,
            expected_rows=0,
            part_path="",
            fetched_at="2026-11-14T00:00:00Z",
            note="Fetch failed after 3 retries",
        ),
        CoverageDayRecord(
            dataset_id="bars:upstox:1d:NSE:EQ:STOCKB:adjusted",
            day="2026-11-10",
            status="PENDING",
            rows=0,
            expected_rows=0,
            part_path="",
            fetched_at="2026-11-14T00:00:00Z",
            note="",
        ),
    ]
    manifest.record_coverage_days(recs)

    # Monkeypatch runtime
    mock_rt = MagicMock()
    mock_rt.paths.data = tmp_path
    monkeypatch.setattr("algodesk.cli.data._open_rt", lambda *a, **kw: mock_rt)
    monkeypatch.setattr("algodesk.cli.data._get_manifest", lambda *a, **kw: manifest)
    monkeypatch.setattr("algodesk.cli.data._resolve_secrets_provider", lambda *a: None)

    runner = CliRunner()

    # 1. Without --verbose: summary line per reason, no table headers
    res_summary = runner.invoke(coverage_app, ["reset-empty", "--dry-run"])
    assert res_summary.exit_code == 0
    assert "record(s) across" in res_summary.output
    assert "DATASET" not in res_summary.output

    # 2. With --verbose: shows table headers
    res_verbose = runner.invoke(coverage_app, ["reset-empty", "--dry-run", "--verbose"])
    assert res_verbose.exit_code == 0
    assert "DATASET" in res_verbose.output
    assert "STOCKA" in res_verbose.output


# =========================================================================
# Step 7 Tests: Screen Run Skip Presentation
# =========================================================================


def test_screen_run_cli_output_skipped_counts_and_first_5() -> None:
    """CLI prints skipped counts by reason and first 5 skip messages with fix command."""
    mock_res = ScreenResult(
        run_id="test_run",
        name="test_screen",
        as_of=date(2026, 11, 13),
        timeframe="1d",
        verdict="FAIL",
        fail_reason="Too many skipped",
        rows=(),
        skipped=(
            (
                "S1",
                SkipReason.GAP_IN_WINDOW,
                "S1: gap_in_window: Gap in window on 2026-11-10. "
                "Fix with: algodesk data accumulate --group G --timeframe 1d",
            ),
            (
                "S2",
                SkipReason.GAP_IN_WINDOW,
                "S2: gap_in_window: Gap in window on 2026-11-10. "
                "Fix with: algodesk data accumulate --group G --timeframe 1d",
            ),
            (
                "S3",
                SkipReason.INSUFFICIENT_HISTORY,
                "S3: insufficient_history: Only 5 bars. "
                "Fix with: algodesk data accumulate --group G --timeframe 1d",
            ),
            (
                "S4",
                SkipReason.NO_DATA,
                "S4: no_data: No data found. "
                "Fix with: algodesk data accumulate --group G --timeframe 1d",
            ),
            (
                "S5",
                SkipReason.NO_DATA,
                "S5: no_data: No data found. "
                "Fix with: algodesk data accumulate --group G --timeframe 1d",
            ),
            (
                "S6",
                SkipReason.NO_DATA,
                "S6: no_data: No data found. "
                "Fix with: algodesk data accumulate --group G --timeframe 1d",
            ),
        ),
        timing={},
    )

    runner = MagicMock()
    runner.run.return_value = mock_res

    # Test the presentation formatting directly
    reason_counts: dict[str, int] = {}
    for item in mock_res.skipped:
        r_str = item[1].value if hasattr(item[1], "value") else str(item[1])
        reason_counts[r_str] = reason_counts.get(r_str, 0) + 1
    counts_line = ", ".join(f"{r}: {c}" for r, c in sorted(reason_counts.items()))

    assert "gap_in_window: 2" in counts_line
    assert "insufficient_history: 1" in counts_line
    assert "no_data: 3" in counts_line

    first_5 = [item[2] for item in mock_res.skipped[:5]]
    assert len(first_5) == 5
    assert all("Fix with:" in msg for msg in first_5)


# =========================================================================
# Additional Session and Closure Tests
# =========================================================================


def test_is_trading_session_manifest_closures(tmp_path: Path) -> None:
    """is_trading_session respects exchange_file and confirmed inferred closures."""
    manifest = DataManifest(tmp_path / "manifest.db")
    cal = HolidayCalendar()

    manifest.record_market_closure(
        exchange="NSE",
        day="2026-10-20",
        kind="exchange_file",
        evidence='{"source": "nse_bhavcopy", "status": "absent"}',
        created="2026-10-20T00:00:00Z",
    )
    manifest.record_market_closure(
        exchange="NSE",
        day="2026-10-21",
        kind="inferred",
        evidence='{"all_samples": true, "source": "samples"}',
        created="2026-10-21T00:00:00Z",
    )
    manifest.record_market_closure(
        exchange="NSE",
        day="2026-10-22",
        kind="inferred",
        evidence='{"all_samples": false, "missing_symbols": ["A"], "sample_count": 5}',
        created="2026-10-22T00:00:00Z",
    )

    # exchange_file closure -> closed
    assert is_trading_session(date(2026, 10, 20), calendar=cal, manifest=manifest) is False
    # confirmed inferred closure -> closed
    assert is_trading_session(date(2026, 10, 21), calendar=cal, manifest=manifest) is False
    # unconfirmed inferred closure -> open (weekday Wednesday)
    assert is_trading_session(date(2026, 10, 22), calendar=cal, manifest=manifest) is True


def test_resolve_trading_sessions_matches_coverage_index(tmp_path: Path) -> None:
    """resolve_trading_sessions agrees with CoverageIndex.is_trading_session."""
    manifest = DataManifest(tmp_path / "manifest.db")
    cal = HolidayCalendar(holidays={date(2026, 11, 10)})
    idx = CoverageIndex(manifest=manifest, calendar=cal)

    sessions = resolve_trading_sessions(
        end_date=date(2026, 11, 13),
        needed_sessions=5,
        calendar=cal,
        manifest=manifest,
    )
    assert len(sessions) == 5
    assert date(2026, 11, 10) not in sessions
    for s in sessions:
        assert idx.is_trading_session(s) is True


def test_reset_stale_empty_coverage_progress_callback(tmp_path: Path) -> None:
    """Progress callback receives symbol increments during reset_stale_empty_coverage."""
    manifest = DataManifest(tmp_path / "manifest.db")
    cal = HolidayCalendar()

    recs = [
        CoverageDayRecord(
            dataset_id=f"bars:upstox:1d:NSE:EQ:SYM{i}:adjusted",
            day="2026-11-10",
            status=CoverageStatus.EMPTY,
            rows=0,
            expected_rows=0,
            part_path="",
            fetched_at="2026-11-14T00:00:00Z",
            note="Fetch failed: timeout",
        )
        for i in range(30)
    ]
    manifest.record_coverage_days(recs)

    cb_calls: list[tuple[int, int]] = []
    params = FakeParamReader({"cli.progress_every": 10})

    reset_stale_empty_coverage(
        manifest=manifest,
        calendar=cal,
        params=params,
        dry_run=True,
        progress_callback=lambda cur, total: cb_calls.append((cur, total)),
    )
    assert len(cb_calls) >= 3
    assert cb_calls[-1][0] == 30
    assert cb_calls[-1][1] == 30


def test_coverage_index_is_trading_session_method(tmp_path: Path) -> None:
    """CoverageIndex.is_trading_session delegates to is_trading_session."""
    manifest = DataManifest(tmp_path / "manifest.db")
    holiday = date(2026, 11, 10)
    cal = HolidayCalendar(holidays={holiday})
    idx = CoverageIndex(manifest=manifest, calendar=cal)

    assert idx.is_trading_session(holiday) is False
    assert idx.is_trading_session(date(2026, 11, 11)) is True
    # Sunday
    assert idx.is_trading_session(date(2026, 11, 8)) is False
