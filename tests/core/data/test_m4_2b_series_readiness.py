"""Unit and integration tests for ADR-084 backtest series resolution and diagnostics."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from algodesk.api.market import (
    Bar,
    CoverageStatus,
    Instrument,
    InstrumentId,
    SeriesKey,
)
from algodesk.data.historical_feed import HistoricalFeed
from algodesk.data.layout import dataset_id_for_key
from algodesk.data.manifest import CoverageDayRecord, DataManifest
from algodesk.data.readiness import (
    check_symbol_readiness,
    resolve_backtest_series,
)
from algodesk.data.service import DataService
from algodesk.data.store import BarStore


class FakeDataSource:
    def __init__(self, name: str = "upstox") -> None:
        self.name = name

    def history(self, key: SeriesKey, start: datetime, end: datetime) -> list[Bar]:
        return []

    def instruments(self) -> list[Instrument]:
        return []


class SimpleCalendar:
    def is_session(self, dt: date | datetime) -> bool:
        d = dt if isinstance(dt, date) and not isinstance(dt, datetime) else dt.date()
        return d.weekday() < 5


@pytest.fixture
def tmp_env(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(data_dir / "manifest.db")
    store = BarStore(data_dir=data_dir, manifest=manifest)
    source = FakeDataSource("upstox")
    service = DataService(sources={"upstox": source}, default_source="upstox", store=store)
    return data_dir, manifest, store, service


def _make_daily_bars(
    instrument: str,
    days: list[date],
    source: str = "upstox",
    open_price: float = 100.0,
) -> list[Bar]:
    inst = InstrumentId(instrument)
    bars = []
    for d in days:
        s_dt = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=UTC)
        e_dt = s_dt + timedelta(days=1)
        bars.append(
            Bar(
                instrument=inst,
                timeframe="1d",
                start_ts=s_dt,
                end_ts=e_dt,
                open=open_price,
                high=open_price + 10.0,
                low=open_price - 5.0,
                close=open_price + 2.0,
                volume=1000.0,
            )
        )
    return bars


def _commit_bars(
    store: BarStore,
    manifest: DataManifest,
    instrument: str,
    days: list[date],
    adjustment: str = "raw",
    source: str = "upstox",
    timeframe: str = "1d",
    open_price: float = 100.0,
) -> None:
    inst = InstrumentId(instrument)
    key = SeriesKey(inst, "bars", timeframe, adjustment, source)
    bars = _make_daily_bars(instrument, days, source=source, open_price=open_price)
    store.commit(key, bars)
    ds_id = dataset_id_for_key(key)
    for d in days:
        manifest.record_coverage_day(
            CoverageDayRecord(
                dataset_id=ds_id,
                day=d.isoformat(),
                status=CoverageStatus.COMPLETE,
                rows=1,
                expected_rows=1,
                part_path=f"bars/{d.isoformat()}.parquet",
                fetched_at=datetime.now(UTC).isoformat(),
            )
        )


def test_resolve_backtest_series_splits_prefers_raw(tmp_env) -> None:
    _, manifest, store, _ = tmp_env
    res = resolve_backtest_series(
        "NSE:EQ:RELIANCE",
        timeframe="1d",
        source="upstox",
        adjust="splits",
        manifest=manifest,
        store=store,
    )
    assert res.adjustment == "raw"
    assert res.adjust == "splits"
    assert res.dataset_id == "bars:upstox:1d:NSE:EQ:RELIANCE:raw"


def test_resolve_backtest_series_none_prefers_raw(tmp_env) -> None:
    _, manifest, store, _ = tmp_env
    res = resolve_backtest_series(
        "NSE:EQ:TCS",
        timeframe="1d",
        source="upstox",
        adjust="none",
        manifest=manifest,
        store=store,
    )
    assert res.adjustment == "raw"
    assert res.adjust == "none"


def test_resolve_backtest_series_total_prefers_adjusted(tmp_env) -> None:
    _, manifest, store, _ = tmp_env
    res = resolve_backtest_series(
        "NSE:EQ:INFY",
        timeframe="1d",
        source="upstox",
        adjust="total",
        manifest=manifest,
        store=store,
    )
    assert res.adjustment == "adjusted"
    assert res.adjust == "total"


def test_resolve_backtest_series_falls_back_to_adjusted_when_raw_missing(tmp_env) -> None:
    _, manifest, store, _ = tmp_env
    days = [date(2025, 1, 6), date(2025, 1, 7)]
    _commit_bars(store, manifest, "NSE:EQ:SBIN", days, adjustment="adjusted")

    res = resolve_backtest_series(
        "NSE:EQ:SBIN",
        timeframe="1d",
        source="upstox",
        adjust="splits",
        manifest=manifest,
        store=store,
    )
    assert res.adjustment == "adjusted"
    assert res.dataset_id == "bars:upstox:1d:NSE:EQ:SBIN:adjusted"


def test_resolve_backtest_series_falls_back_to_raw_when_adjusted_missing(tmp_env) -> None:
    _, manifest, store, _ = tmp_env
    days = [date(2025, 1, 6), date(2025, 1, 7)]
    _commit_bars(store, manifest, "NSE:EQ:HDFCBANK", days, adjustment="raw")

    res = resolve_backtest_series(
        "NSE:EQ:HDFCBANK",
        timeframe="1d",
        source="upstox",
        adjust="total",
        manifest=manifest,
        store=store,
    )
    assert res.adjustment == "raw"
    assert res.dataset_id == "bars:upstox:1d:NSE:EQ:HDFCBANK:raw"


def test_resolve_backtest_series_detects_stale_adjusted_warning(tmp_env) -> None:
    _, manifest, store, _ = tmp_env
    days = [date(2025, 1, 6), date(2025, 1, 7)]
    _commit_bars(store, manifest, "NSE:EQ:RELIANCE", days, adjustment="raw")
    _commit_bars(store, manifest, "NSE:EQ:RELIANCE", days[:1], adjustment="adjusted")

    res = resolve_backtest_series(
        "NSE:EQ:RELIANCE",
        timeframe="1d",
        source="upstox",
        adjust="splits",
        manifest=manifest,
        store=store,
    )
    assert res.adjustment == "raw"
    assert len(res.warnings) == 1
    assert "other series exist, not used by this run" in res.warnings[0]
    assert len(res.cleanup_commands) == 0
    assert "data delete" not in str(res.cleanup_commands)


def test_step2_fixture_healthy_raw_passes_readiness(tmp_env) -> None:
    """Build step 2 fixture with healthy raw and verify readiness passes."""
    _, manifest, store, service = tmp_env
    cal = SimpleCalendar()

    # Session dates: 20 weekdays in Jan 2025
    jan_days = [date(2025, 1, d) for d in range(1, 31) if cal.is_session(date(2025, 1, d))]
    eval_days = jan_days[:15]
    start_d = eval_days[0]
    end_d = eval_days[-1]

    # 1. Complete raw series (15 days)
    _commit_bars(store, manifest, "NSE:EQ:RELIANCE", eval_days, adjustment="raw", source="upstox")

    # 2. Stale adjusted series (first 5 days only)
    _commit_bars(
        store, manifest, "NSE:EQ:RELIANCE", eval_days[:5], adjustment="adjusted", source="upstox"
    )
    # Add coverage records without bars to adjusted partition
    adj_ds_id = dataset_id_for_key(
        SeriesKey(InstrumentId("NSE:EQ:RELIANCE"), "bars", "1d", "adjusted", "upstox")
    )
    for d in eval_days[5:10]:
        manifest.record_coverage_day(
            CoverageDayRecord(
                dataset_id=adj_ds_id,
                day=d.isoformat(),
                status=CoverageStatus.COMPLETE,
                rows=1,
                expected_rows=1,
                part_path=f"bars/{d.isoformat()}.parquet",
                fetched_at=datetime.now(UTC).isoformat(),
            )
        )

    # 3. Alternate source partitions (nse_bhavcopy, yfinance)
    _commit_bars(
        store, manifest, "NSE:EQ:RELIANCE", eval_days[:5], adjustment="raw", source="nse_bhavcopy"
    )
    _commit_bars(
        store, manifest, "NSE:EQ:RELIANCE", eval_days[:5], adjustment="raw", source="yfinance"
    )

    # Check symbol readiness
    sym_rep = check_symbol_readiness(
        symbol="NSE:EQ:RELIANCE",
        data_service=service,
        manifest=manifest,
        calendar=cal,
        expected_sessions=eval_days,
        start=start_d,
        end=end_d,
        timeframe="1d",
        source="upstox",
        warmup_bars_count=0,
        adjust="splits",
    )

    # Healthy raw series MUST pass!
    assert sym_rep.is_ready is True
    assert len(sym_rep.problems) == 0
    assert len(sym_rep.coverage_without_bars) == 0
    assert len(sym_rep.missing_sessions) == 0

    # Stale partitions appear as non-blocking warnings
    assert len(sym_rep.warnings) == 1
    assert "other series exist, not used by this run" in sym_rep.warnings[0]
    assert "data delete" not in str(sym_rep.warning_cleanup_commands)


def test_step2_fixture_real_missing_day_fails_readiness(tmp_env) -> None:
    """When a day is genuinely missing from the resolved raw series, readiness must fail."""
    _, manifest, store, service = tmp_env
    cal = SimpleCalendar()

    days = [date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 8)]
    # Store only day 1 and day 3; day 2 is missing
    _commit_bars(
        store, manifest, "NSE:EQ:RELIANCE", [days[0], days[2]], adjustment="raw", source="upstox"
    )

    sym_rep = check_symbol_readiness(
        symbol="NSE:EQ:RELIANCE",
        data_service=service,
        manifest=manifest,
        calendar=cal,
        expected_sessions=days,
        start=days[0],
        end=days[2],
        timeframe="1d",
        source="upstox",
        warmup_bars_count=0,
        adjust="splits",
    )
    assert sym_rep.is_ready is False
    assert len(sym_rep.missing_sessions) == 1
    assert sym_rep.missing_sessions[0] == days[1]


def test_step2_fixture_adding_raw_rows_updates_readiness(tmp_env) -> None:
    """Adding missing raw rows turns a failing readiness check into a passing one."""
    _, manifest, store, service = tmp_env
    cal = SimpleCalendar()

    days = [date(2025, 1, 6), date(2025, 1, 7)]
    # Initially missing day 2
    _commit_bars(store, manifest, "NSE:EQ:TCS", [days[0]], adjustment="raw", source="upstox")

    rep1 = check_symbol_readiness(
        symbol="NSE:EQ:TCS",
        data_service=service,
        manifest=manifest,
        calendar=cal,
        expected_sessions=days,
        start=days[0],
        end=days[1],
        timeframe="1d",
        source="upstox",
        warmup_bars_count=0,
        adjust="splits",
    )
    assert rep1.is_ready is False
    assert len(rep1.missing_sessions) == 1

    # Now add missing day 2
    _commit_bars(store, manifest, "NSE:EQ:TCS", [days[1]], adjustment="raw", source="upstox")

    rep2 = check_symbol_readiness(
        symbol="NSE:EQ:TCS",
        data_service=service,
        manifest=manifest,
        calendar=cal,
        expected_sessions=days,
        start=days[0],
        end=days[1],
        timeframe="1d",
        source="upstox",
        warmup_bars_count=0,
        adjust="splits",
    )
    assert rep2.is_ready is True
    assert len(rep2.missing_sessions) == 0


def test_historical_feed_reads_resolved_series_matching_readiness(tmp_env) -> None:
    """HistoricalFeed reads the exact same resolved series as readiness."""
    _, manifest, store, service = tmp_env
    cal = SimpleCalendar()

    days = [date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 8)]
    # Store complete raw series (100.0)
    _commit_bars(
        store, manifest, "NSE:EQ:INFY", days, adjustment="raw", source="upstox", open_price=1500.0
    )
    # Store stale adjusted series with different prices (999.0)
    _commit_bars(
        store,
        manifest,
        "NSE:EQ:INFY",
        days[:1],
        adjustment="adjusted",
        source="upstox",
        open_price=999.0,
    )

    feed = HistoricalFeed(
        data_service=service,
        instruments=[InstrumentId("NSE:EQ:INFY")],
        start=days[0],
        end=days[-1],
        timeframe="1d",
        source="upstox",
        warmup_bars_count=0,
        calendar=cal,
        manifest=manifest,
        adjust="splits",
    )
    events = list(feed.iter_with_seq())
    bars = [ev for ev, _ in events if isinstance(ev, Bar)]
    assert len(bars) == 3
    # All bars should come from the resolved raw series (open = 1500.0)
    for b in bars:
        assert b.open == 1500.0
