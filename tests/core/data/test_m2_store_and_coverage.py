"""Tests for CP1: BarStore, CoverageIndex, and pure resampler."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from algodesk.api.market import (
    Bar,
    CoverageStatus,
    InstrumentId,
    SeriesKey,
    Tick,
)
from algodesk.api.protocols import Calendar, Session, TradingSession
from algodesk.data.coverage import CoverageIndex
from algodesk.data.manifest import DataManifest
from algodesk.data.resample import resample
from algodesk.data.store import BarStore
from algodesk.engine.bars import BarBuilder


class FakeCalendar(Calendar):
    """Hermetic test calendar with standard 09:15 - 15:30 IST session."""

    def __init__(self, holidays: set[date] | None = None) -> None:
        self.holidays = holidays or set()
        self.tz = ZoneInfo("Asia/Kolkata")

    @property
    def exchange(self) -> str:
        return "NSE"

    @property
    def timezone(self) -> str:
        return "Asia/Kolkata"

    def is_trading_day(self, d: date, segment: str = "") -> bool:
        if d.weekday() >= 5 or d in self.holidays:
            return False
        return True

    def is_session(self, day: date) -> bool:
        return self.is_trading_day(day)

    def sessions(self, day: date, segment: str = "") -> list[Session]:
        if not self.is_trading_day(day):
            return []
        s_start = datetime(day.year, day.month, day.day, 9, 15, tzinfo=self.tz).astimezone(UTC)
        s_end = datetime(day.year, day.month, day.day, 15, 30, tzinfo=self.tz).astimezone(UTC)
        return [
            Session(
                name="regular",
                start=s_start,
                end=s_end,
                tradable=True,
            )
        ]

    def next_session(self, ts: datetime, segment: str = "") -> TradingSession:
        d = ts.astimezone(self.tz).date() + timedelta(days=1)
        while not self.is_trading_day(d):
            d += timedelta(days=1)
        return self.sessions(d)[0]


class MockJournal:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def append(self, kind: str, payload: dict[str, object], durable: bool = True) -> int:
        self.events.append({"kind": kind, "payload": payload})
        return len(self.events)


def _make_1m_bars(
    instrument: InstrumentId,
    day: date,
    count: int = 5,
    start_minute: int = 15,
) -> list[Bar]:
    ist = ZoneInfo("Asia/Kolkata")
    bars: list[Bar] = []
    base_price = 1000.0
    for i in range(count):
        s_dt = datetime(day.year, day.month, day.day, 9, start_minute + i, tzinfo=ist).astimezone(
            UTC
        )
        e_dt = s_dt + timedelta(minutes=1)
        bars.append(
            Bar(
                instrument=instrument,
                timeframe="1m",
                start_ts=s_dt,
                end_ts=e_dt,
                open=base_price + i,
                high=base_price + i + 2.0,
                low=base_price + i - 1.0,
                close=base_price + i + 1.0,
                volume=100 * (i + 1),
                complete=True,
                synthetic=False,
            )
        )
    return bars


def test_bar_store_commit_1m_atomic_and_idempotent(tmp_path: Path) -> None:
    """1m bars commit atomically and repeated commit gives identical checksum."""
    data_dir = tmp_path / "data"
    manifest = DataManifest(tmp_path / "data_manifest.db")
    journal = MockJournal()
    calendar = FakeCalendar()

    store = BarStore(
        data_dir=data_dir,
        manifest=manifest,
        journal=journal,  # type: ignore[arg-type]
        calendar=calendar,
        settle_minutes=15,
    )

    inst_id = InstrumentId("NSE:EQ:RELIANCE")
    key = SeriesKey(
        instrument=inst_id,
        kind="bars",
        timeframe="1m",
        adjustment="raw",
        source="upstox",
    )
    test_day = date(2026, 10, 26)
    bars = _make_1m_bars(inst_id, test_day, count=10)

    parts1 = store.commit(key, bars)
    assert len(parts1) == 1
    p1 = parts1[0]
    assert p1.rows == 10
    assert "2026-10.parquet" in p1.path

    # Verify manifest parts and coverage
    manifest_parts = manifest.get_parts(dataset_id=p1.dataset_id)
    assert len(manifest_parts) == 1
    assert manifest_parts[0].sha256 == p1.sha256

    # Verify idempotence: same commit again produces identical checksum
    parts2 = store.commit(key, bars)
    assert len(parts2) == 1
    p2 = parts2[0]
    assert p2.sha256 == p1.sha256
    assert p2.rows == 10

    # Read back through store.read
    start_dt = bars[0].start_ts
    end_dt = bars[-1].end_ts
    data_slice = store.read(key, start_dt, end_dt)
    assert len(data_slice.bars) == 10
    assert data_slice.bars[0].open == bars[0].open
    assert len(data_slice.checksums) == 1
    assert data_slice.checksums[0] == p1.sha256
    assert not data_slice.has_gaps


def test_bar_store_commit_1d_daily_parquet(tmp_path: Path) -> None:
    """1d bars commit to single daily.parquet file."""
    data_dir = tmp_path / "data"
    manifest = DataManifest(tmp_path / "data_manifest.db")
    calendar = FakeCalendar()
    store = BarStore(data_dir=data_dir, manifest=manifest, calendar=calendar)

    inst_id = InstrumentId("NSE:EQ:INFY")
    key = SeriesKey(
        instrument=inst_id,
        kind="bars",
        timeframe="1d",
        adjustment="adjusted",
        source="yfinance",
    )
    d1 = datetime(2026, 10, 26, 3, 45, tzinfo=UTC)
    d2 = datetime(2026, 10, 27, 3, 45, tzinfo=UTC)
    bars = [
        Bar(
            instrument=inst_id,
            timeframe="1d",
            start_ts=d1,
            end_ts=d1 + timedelta(hours=6, minutes=15),
            open=1500.0,
            high=1520.0,
            low=1490.0,
            close=1515.0,
            volume=100000,
            complete=True,
        ),
        Bar(
            instrument=inst_id,
            timeframe="1d",
            start_ts=d2,
            end_ts=d2 + timedelta(hours=6, minutes=15),
            open=1515.0,
            high=1530.0,
            low=1510.0,
            close=1525.0,
            volume=120000,
            complete=True,
        ),
    ]

    parts = store.commit(key, bars)
    assert len(parts) == 1
    assert parts[0].path.endswith("daily.parquet")
    assert parts[0].rows == 2


def test_complete_day_preservation_and_refresh(tmp_path: Path) -> None:
    """COMPLETE day is never replaced unless refresh=True, which is journaled."""
    data_dir = tmp_path / "data"
    manifest = DataManifest(tmp_path / "data_manifest.db")
    journal = MockJournal()
    calendar = FakeCalendar()

    store = BarStore(
        data_dir=data_dir,
        manifest=manifest,
        journal=journal,  # type: ignore[arg-type]
        calendar=calendar,
        settle_minutes=0,  # immediate complete after session
    )

    inst_id = InstrumentId("NSE:EQ:TCS")
    key = SeriesKey(
        instrument=inst_id,
        kind="bars",
        timeframe="1m",
        adjustment="raw",
        source="upstox",
    )
    test_day = date(2026, 10, 26)
    original_bars = _make_1m_bars(inst_id, test_day, count=5)

    # Initial commit (marked COMPLETE)
    store.commit(key, original_bars)
    cov = manifest.get_coverage_days(store.dataset_id(key), "2026-10-26", "2026-10-26")
    assert len(cov) == 1
    assert cov[0].status == CoverageStatus.COMPLETE

    # Create mutated bars for the same day
    mutated_bars = [
        Bar(
            instrument=b.instrument,
            timeframe=b.timeframe,
            start_ts=b.start_ts,
            end_ts=b.end_ts,
            open=9999.0,
            high=9999.0,
            low=9999.0,
            close=9999.0,
            volume=9999,
            complete=True,
        )
        for b in original_bars
    ]

    # Without refresh: complete day must NOT be overwritten
    store.commit(key, mutated_bars, refresh=False)
    read_slice = store.read(key, original_bars[0].start_ts, original_bars[-1].end_ts)
    assert read_slice.bars[0].open == original_bars[0].open
    assert read_slice.bars[0].open != 9999.0

    # With refresh: complete day is updated and journal event emitted
    store.commit(key, mutated_bars, refresh=True)
    refreshed_slice = store.read(key, original_bars[0].start_ts, original_bars[-1].end_ts)
    assert refreshed_slice.bars[0].open == 9999.0

    refresh_events = [e for e in journal.events if e["kind"] == "data.refresh"]
    assert len(refresh_events) >= 1
    assert refresh_events[0]["payload"]["instrument"] == inst_id


def test_coverage_index_settle_margin_and_statuses(tmp_path: Path) -> None:
    """CoverageIndex evaluates COMPLETE, PARTIAL, CLOSED, EMPTY, QUARANTINED, UNAVAILABLE."""
    manifest = DataManifest(tmp_path / "data_manifest.db")
    calendar = FakeCalendar(holidays={date(2026, 10, 28)})
    index = CoverageIndex(manifest=manifest, calendar=calendar, settle_minutes=15)

    ds_id = "bars:upstox:1m:NSE:EQ:HDFCBANK:raw"

    # 1. Closed holiday
    st_holiday = index.evaluate_day_status(ds_id, date(2026, 10, 28), has_bars=False)
    assert st_holiday == CoverageStatus.CLOSED
    # 2. Closed weekend (2026-10-25 is Sunday)
    st_weekend = index.evaluate_day_status(ds_id, date(2026, 10, 25), has_bars=False)
    assert st_weekend == CoverageStatus.CLOSED

    # 3. Open session evaluated well after session end + 15m settle
    after_settle = datetime(2026, 10, 26, 16, 0, tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(UTC)
    status_complete = index.evaluate_day_status(
        ds_id,
        date(2026, 10, 26),
        has_bars=True,
        as_of=after_settle,
    )
    assert status_complete == CoverageStatus.COMPLETE

    # 4. Open session evaluated during trading hours (11:00 IST) -> PARTIAL
    during_session = datetime(2026, 10, 26, 11, 0, tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(UTC)
    status_partial = index.evaluate_day_status(
        ds_id,
        date(2026, 10, 26),
        has_bars=True,
        as_of=during_session,
    )
    assert status_partial == CoverageStatus.PARTIAL

    # 5. Open session with 0 bars -> EMPTY
    status_empty = index.evaluate_day_status(
        ds_id,
        date(2026, 10, 26),
        has_bars=False,
        as_of=after_settle,
    )
    assert status_empty == CoverageStatus.EMPTY


def test_data_slice_gaps_reporting(tmp_path: Path) -> None:
    """DataSlice captures missing days as gaps with dataset provenance."""
    data_dir = tmp_path / "data"
    manifest = DataManifest(tmp_path / "data_manifest.db")
    calendar = FakeCalendar()
    store = BarStore(data_dir=data_dir, manifest=manifest, calendar=calendar)

    inst_id = InstrumentId("NSE:EQ:SBIN")
    key = SeriesKey(
        instrument=inst_id,
        kind="bars",
        timeframe="1m",
        adjustment="raw",
        source="upstox",
    )

    # Commit Monday 2026-10-26 and Wednesday 2026-10-28; Tuesday 2026-10-27 is omitted
    bars_mon = _make_1m_bars(inst_id, date(2026, 10, 26), count=5)
    bars_wed = _make_1m_bars(inst_id, date(2026, 10, 28), count=5)

    store.commit(key, bars_mon)
    store.commit(key, bars_wed)

    # Read slice covering Mon to Wed
    start_dt = bars_mon[0].start_ts
    end_dt = bars_wed[-1].end_ts
    res_slice = store.read(key, start_dt, end_dt)

    assert res_slice.has_gaps is True
    gap_days = [g.day for g in res_slice.gaps]
    assert date(2026, 10, 27) in gap_days
    assert len(res_slice.dataset_ids) == 1
    assert len(res_slice.checksums) == 1


def test_pure_resample_1m_to_minutes_and_hours() -> None:
    """resample converts 1m bars to N-minute and hourly intervals aligned to session open."""
    calendar = FakeCalendar()
    inst_id = InstrumentId("NSE:EQ:INFY")
    day = date(2026, 10, 26)
    # Generate 15 1m bars: 09:15 to 09:30 IST
    bars_1m = _make_1m_bars(inst_id, day, count=15, start_minute=15)

    # 1. Resample to 5m: should yield exactly 3 5m bars
    bars_5m = resample(bars_1m, "5m", calendar=calendar)
    assert len(bars_5m) == 3

    # Check first 5m bar (09:15 to 09:20)
    b0 = bars_5m[0]
    first_5 = bars_1m[:5]
    assert b0.open == first_5[0].open
    assert b0.high == max(b.high for b in first_5)
    assert b0.low == min(b.low for b in first_5)
    assert b0.close == first_5[-1].close
    assert b0.volume == sum(b.volume for b in first_5)
    assert b0.timeframe == "5m"

    # 2. Resample to 15m: should yield exactly 1 15m bar
    bars_15m = resample(bars_1m, "15m", calendar=calendar)
    assert len(bars_15m) == 1
    assert bars_15m[0].open == bars_1m[0].open
    assert bars_15m[0].close == bars_1m[-1].close
    assert bars_15m[0].volume == sum(b.volume for b in bars_1m)


def test_pure_resample_1d_to_week_and_month() -> None:
    """resample converts 1d bars to weekly (1w) and monthly (1M) intervals."""
    inst_id = InstrumentId("NSE:EQ:TCS")
    daily_bars: list[Bar] = []
    # 10 days of October 2026
    for day_num in range(1, 11):
        s_dt = datetime(2026, 10, day_num, 3, 45, tzinfo=UTC)
        daily_bars.append(
            Bar(
                instrument=inst_id,
                timeframe="1d",
                start_ts=s_dt,
                end_ts=s_dt + timedelta(hours=6, minutes=15),
                open=3000.0 + day_num,
                high=3020.0 + day_num,
                low=2990.0 + day_num,
                close=3010.0 + day_num,
                volume=1000 * day_num,
                complete=True,
            )
        )

    # Resample to 1M
    bars_month = resample(daily_bars, "1M")
    assert len(bars_month) == 1
    bm = bars_month[0]
    assert bm.timeframe == "1M"
    assert bm.open == daily_bars[0].open
    assert bm.close == daily_bars[-1].close
    assert bm.high == max(b.high for b in daily_bars)
    assert bm.low == min(b.low for b in daily_bars)
    assert bm.volume == sum(b.volume for b in daily_bars)

    # Resample to 1w
    bars_week = resample(daily_bars, "1w")
    assert len(bars_week) >= 1
    for bw in bars_week:
        assert bw.timeframe == "1w"


def test_resample_property_equivalence_with_bar_builder() -> None:
    """Property test: resampled 5m equals bars built by BarBuilder from the same ticks."""
    inst_id = InstrumentId("NSE:EQ:RELIANCE")
    ist = ZoneInfo("Asia/Kolkata")
    base_dt = datetime(2026, 10, 26, 9, 15, tzinfo=ist).astimezone(UTC)

    # Build tick stream: 5 ticks in first 5m (min 0..4), 5 ticks in second 5m (min 5..9),
    # and a boundary tick at min 10 to close both 5m periods in BarBuilder
    ticks: list[Tick] = []
    prices = [100.0, 105.0, 95.0, 102.0, 101.0, 104.0, 98.0, 103.0, 106.0, 99.0, 100.0]
    for i, p in enumerate(prices):
        t_ts = base_dt + timedelta(minutes=i)  # 0 to 10 minutes
        ticks.append(
            Tick(
                instrument=inst_id,
                exch_ts=t_ts,
                recv_ts=t_ts,
                ltp=p,
                ltq=10,
                volume=100 * (i + 1),
            )
        )

    # Run BarBuilder configured for both 1m and 5m
    builder_1m_bars: list[Bar] = []
    builder_5m_bars: list[Bar] = []

    def on_bar_closed(bar: Bar) -> None:
        if bar.timeframe == "1m":
            builder_1m_bars.append(bar)
        elif bar.timeframe == "5m":
            builder_5m_bars.append(bar)

    calendar = FakeCalendar()
    bb = BarBuilder(
        timeframes=("1m", "5m"),
        session_manager=None,
        on_bar_closed=on_bar_closed,
        fill_empty_bars=False,
    )

    for tick in ticks:
        bb.on_tick(tick)

    # Verify BarBuilder produced completed bars
    assert len(builder_1m_bars) >= 10
    assert len(builder_5m_bars) == 2

    # Take the first 10 1m bars corresponding to the two 5m periods
    resampled_5m = resample(builder_1m_bars[:10], "5m", calendar=calendar)

    # Compare completed 5m bars produced by BarBuilder vs resample
    assert len(resampled_5m) == len(builder_5m_bars)
    for b_res, b_build in zip(resampled_5m, builder_5m_bars, strict=True):
        assert b_res.start_ts == b_build.start_ts
        assert b_res.open == pytest.approx(b_build.open)
        assert b_res.high == pytest.approx(b_build.high)
        assert b_res.low == pytest.approx(b_build.low)
        assert b_res.close == pytest.approx(b_build.close)
        assert b_res.volume == b_build.volume
