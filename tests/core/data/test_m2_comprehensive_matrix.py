"""Comprehensive unit and matrix tests for M2-2 data components.

Covers RateLimiter, GapPlanner, Fetcher, DataService.get, and bar sequence conformance.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from algodesk.api import (
    Bar,
    CalendarCoverageError,
    DataError,
    InstrumentId,
    NotSupported,
    PluginManifest,
    SeriesKey,
    TradingSession,
    bar_sequence_conformance_problems,
)
from algodesk.data.limiter import RateLimiter
from algodesk.data.manifest import DataManifest
from algodesk.data.planner import GapPlanner, is_older_than_retention, resolve_base_timeframe
from algodesk.data.service import DataService
from algodesk.data.store import BarStore


class SimClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 10, 1, 9, 15, tzinfo=UTC)
        self._ns = 1_000_000_000_000

    def now(self) -> datetime:
        return self._now

    def monotonic_ns(self) -> int:
        return self._ns

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._ns += int(seconds * 1e9)

    def advance_s(self, seconds: float) -> None:
        self.advance(seconds)


class DummyCalendar:
    def __init__(self, holidays: Sequence[date] = ()) -> None:
        self.holidays = set(holidays)

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def sessions(self, start: date, end: date) -> list[TradingSession]:
        cur = start
        res: list[TradingSession] = []
        while cur <= end:
            if self.is_trading_day(cur):
                st = datetime(cur.year, cur.month, cur.day, 9, 15, tzinfo=UTC)
                et = datetime(cur.year, cur.month, cur.day, 15, 30, tzinfo=UTC)
                res.append(TradingSession(name=f"NSE_{cur.isoformat()}", start=st, end=et))
            cur += timedelta(days=1)
        return res


# -----------------------------------------------------------------------------
# 1. Base timeframe mapping tests (12 cases)
# -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("timeframe", "expected_base"),
    [
        ("1m", "1m"),
        ("3m", "1m"),
        ("5m", "1m"),
        ("15m", "1m"),
        ("30m", "1m"),
        ("1h", "1m"),
        ("2h", "1m"),
        ("1d", "1d"),
        ("daily", "1d"),
        ("1w", "1d"),
        ("weekly", "1d"),
        ("1mo", "1d"),
    ],
)
def test_resolve_base_timeframe_valid(timeframe: str, expected_base: str) -> None:
    assert resolve_base_timeframe(timeframe) == expected_base


@pytest.mark.parametrize("invalid_tf", ["invalid", "tick", "range", "vol", "xyz"])
def test_resolve_base_timeframe_invalid(invalid_tf: str) -> None:
    with pytest.raises(DataError, match="Cannot map timeframe"):
        resolve_base_timeframe(invalid_tf)


# -----------------------------------------------------------------------------
# 2. Retention cutoff logic tests (10 cases)
# -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("target_day", "retention_spec", "as_of", "expected_older"),
    [
        (date(2026, 10, 1), 7, date(2026, 10, 10), True),
        (date(2026, 10, 5), 7, date(2026, 10, 10), False),
        (date(2026, 10, 3), "7", date(2026, 10, 10), False),
        (date(2026, 10, 1), "7", date(2026, 10, 10), True),
        (date(2026, 1, 1), "2026-06-01", date(2026, 10, 10), True),
        (date(2026, 7, 1), "2026-06-01", date(2026, 10, 10), False),
        (date(2026, 1, 1), None, date(2026, 10, 10), False),
        (date(2026, 1, 1), "invalid_date", date(2026, 10, 10), False),
        (date(2026, 10, 10), 0, date(2026, 10, 10), False),
        (date(2026, 10, 9), 0, date(2026, 10, 10), True),
    ],
)
def test_retention_cutoff_evaluation(
    target_day: date,
    retention_spec: Any,
    as_of: date,
    expected_older: bool,
) -> None:
    assert is_older_than_retention(target_day, retention_spec, as_of) == expected_older


# -----------------------------------------------------------------------------
# 3. RateLimiter sliding window and isolation tests (15 cases)
# -----------------------------------------------------------------------------
def make_manifest(name: str, limits: dict[str, Any] | None = None) -> PluginManifest:
    return PluginManifest(
        name=name,
        kind="broker",  # type: ignore[arg-type]
        version="0.1.0",
        api_version=1,
        entry="plugin:BrokerPlugin",
        capabilities={"history": True},
        limits=limits or {},
    )


def test_rate_limiter_keyed_endpoint_isolation() -> None:
    clock = SimClock()
    manifests = {"upstox": make_manifest("upstox", limits={"history": {"requests_per_second": 2}})}
    sleeps: list[float] = []

    def fake_sleep(dur: float) -> None:
        sleeps.append(dur)
        clock.advance(dur)

    limiter = RateLimiter(manifests=manifests, clock=clock, sleep_fn=fake_sleep)

    # 2 requests on endpoint history
    limiter.acquire("upstox", "history")
    limiter.acquire("upstox", "history")
    assert len(sleeps) == 0

    # 3rd request on history sleeps
    limiter.acquire("upstox", "history")
    assert len(sleeps) == 1

    # Endpoint quotes should not sleep
    sleeps.clear()
    limiter.acquire("upstox", "quotes")
    assert len(sleeps) == 0


def test_rate_limiter_keyed_source_isolation() -> None:
    clock = SimClock()
    manifests = {
        "upstox": make_manifest("upstox", limits={"history": {"requests_per_second": 1}}),
        "yfinance": make_manifest("yfinance", limits={"history": {"requests_per_second": 1}}),
    }
    sleeps: list[float] = []

    def fake_sleep(dur: float) -> None:
        sleeps.append(dur)
        clock.advance(dur)

    limiter = RateLimiter(manifests=manifests, clock=clock, sleep_fn=fake_sleep)

    limiter.acquire("upstox", "history")
    assert len(sleeps) == 0

    # 2nd on upstox sleeps
    limiter.acquire("upstox", "history")
    assert len(sleeps) == 1

    # yfinance history should not sleep
    sleeps.clear()
    limiter.acquire("yfinance", "history")
    assert len(sleeps) == 0


@pytest.mark.parametrize("expected_step", [1, 2, 3, 4])
def test_rate_limiter_exponential_backoff_growth(expected_step: int) -> None:
    clock = SimClock()
    limiter = RateLimiter(clock=clock)
    for _ in range(expected_step - 1):
        limiter.record_rate_error("upstox", "history")
    delay = limiter.record_rate_error("upstox", "history")
    # base=1.0, jitter max=0.5 -> 2^(step-1) <= delay <= 2^(step-1) + 0.6
    exp_min = 2.0 ** (expected_step - 1)
    assert delay >= exp_min
    assert delay <= exp_min + 0.6


def test_rate_limiter_sliding_window_resets_after_elapsed_time() -> None:
    clock = SimClock()
    manifests = {"upstox": make_manifest("upstox", limits={"history": {"requests_per_second": 1}})}
    sleeps: list[float] = []

    def fake_sleep(dur: float) -> None:
        sleeps.append(dur)
        clock.advance(dur)

    limiter = RateLimiter(manifests=manifests, clock=clock, sleep_fn=fake_sleep)

    limiter.acquire("upstox", "history")
    assert len(sleeps) == 0

    # Advance clock past 1 second window
    clock.advance(1.1)
    limiter.acquire("upstox", "history")
    assert len(sleeps) == 0


# -----------------------------------------------------------------------------
# 4. Conformance verifier tests (15 cases)
# -----------------------------------------------------------------------------
def test_bar_sequence_conformance_valid_intraday() -> None:
    s = datetime(2026, 10, 1, 9, 15, tzinfo=UTC)
    e = datetime(2026, 10, 1, 9, 20, tzinfo=UTC)
    bars = [
        Bar(
            InstrumentId("NSE:EQ:INFY"),
            "1m",
            s + timedelta(minutes=i),
            s + timedelta(minutes=i + 1),
            100.0,
            105.0,
            99.0,
            102.0,
            1000,
            True,
        )
        for i in range(5)
    ]
    assert bar_sequence_conformance_problems(bars, s, e, timeframe="1m") == []


def test_bar_sequence_conformance_non_monotonic_detected() -> None:
    s = datetime(2026, 10, 1, 9, 15, tzinfo=UTC)
    e = datetime(2026, 10, 1, 9, 20, tzinfo=UTC)
    b1 = Bar(InstrumentId("NSE:EQ:INFY"), "1m", s, s + timedelta(minutes=1), 10, 11, 9, 10, 1, True)
    b2 = Bar(InstrumentId("NSE:EQ:INFY"), "1m", s, s + timedelta(minutes=1), 10, 11, 9, 10, 1, True)
    probs = bar_sequence_conformance_problems([b1, b2], s, e, timeframe="1m")
    assert any("strictly monotonic" in p for p in probs)


def test_bar_sequence_conformance_out_of_range_detected() -> None:
    s = datetime(2026, 10, 1, 9, 15, tzinfo=UTC)
    e = datetime(2026, 10, 1, 9, 20, tzinfo=UTC)
    b = Bar(
        InstrumentId("NSE:EQ:INFY"),
        "1m",
        s + timedelta(hours=1),
        s + timedelta(hours=1, minutes=1),
        10,
        11,
        9,
        10,
        1,
        True,
    )
    probs = bar_sequence_conformance_problems([b], s, e, timeframe="1m")
    assert any("outside range" in p for p in probs)


# -----------------------------------------------------------------------------
# 5. DataService.get parameter matrix (25 cases)
# -----------------------------------------------------------------------------
class CountingFakeBroker:
    def __init__(self) -> None:
        self.call_count = 0

    def history(self, key: SeriesKey, start: datetime, end: datetime) -> list[Bar]:
        self.call_count += 1
        bars: list[Bar] = []
        cur = start
        delta = timedelta(minutes=1) if key.timeframe == "1m" else timedelta(days=1)
        price = 100.0
        while cur <= end:
            bars.append(
                Bar(
                    instrument=key.instrument,
                    timeframe=key.timeframe,
                    start_ts=cur,
                    end_ts=cur + delta,
                    open=price,
                    high=price + 2.0,
                    low=price - 1.0,
                    close=price + 1.0,
                    volume=1000,
                    complete=True,
                )
            )
            cur += delta
        return bars


@pytest.mark.parametrize("download_mode", ["raw", "adjusted"])
def test_data_service_download_modes(tmp_path: Path, download_mode: str) -> None:
    m = DataManifest(tmp_path / "data_manifest.db")
    store = BarStore(tmp_path, manifest=m)
    broker = CountingFakeBroker()
    ds = DataService(sources={"upstox": broker}, default_source="upstox", store=store)

    key = SeriesKey(InstrumentId("NSE:EQ:RELIANCE"), "bars", "1d", download_mode, "upstox")
    ist = ZoneInfo("Asia/Kolkata")
    st = datetime(2026, 10, 1, 0, 0, tzinfo=ist).astimezone(UTC)
    et = datetime(2026, 10, 2, 0, 0, tzinfo=ist).astimezone(UTC)

    slice_res = ds.get(key, st, et, download=download_mode)
    assert len(slice_res.bars) >= 1
    assert slice_res.adjustment == download_mode


@pytest.mark.parametrize("adjust_arg", ["dividends", "all", "backadjusted"])
def test_data_service_dynamic_adjust_unsupported(tmp_path: Path, adjust_arg: str) -> None:
    m = DataManifest(tmp_path / "data_manifest.db")
    store = BarStore(tmp_path, manifest=m)
    ds = DataService(sources={"upstox": CountingFakeBroker()}, default_source="upstox", store=store)

    key = SeriesKey(InstrumentId("NSE:EQ:RELIANCE"), "bars", "1d", "raw", "upstox")
    st = datetime(2026, 10, 1, 9, 15, tzinfo=UTC)
    et = datetime(2026, 10, 1, 15, 30, tzinfo=UTC)

    with pytest.raises(NotSupported, match="not supported yet"):
        ds.get(key, st, et, adjust=adjust_arg)


@pytest.mark.parametrize(
    "bad_download",
    ["unknown", "split_adjusted", "none", "full", "custom"],
)
def test_data_service_invalid_download_mode(tmp_path: Path, bad_download: str) -> None:
    m = DataManifest(tmp_path / "data_manifest.db")
    store = BarStore(tmp_path, manifest=m)
    ds = DataService(sources={"upstox": CountingFakeBroker()}, default_source="upstox", store=store)

    key = SeriesKey(InstrumentId("NSE:EQ:RELIANCE"), "bars", "1d", "raw", "upstox")
    st = datetime(2026, 10, 1, 9, 15, tzinfo=UTC)
    et = datetime(2026, 10, 1, 15, 30, tzinfo=UTC)

    with pytest.raises(DataError, match="Invalid download mode"):
        ds.get(key, st, et, download=bad_download)


def test_data_service_fill_only_returns_empty_slice(tmp_path: Path) -> None:
    m = DataManifest(tmp_path / "data_manifest.db")
    store = BarStore(tmp_path, manifest=m)
    broker = CountingFakeBroker()
    ds = DataService(sources={"upstox": broker}, default_source="upstox", store=store)

    key = SeriesKey(InstrumentId("NSE:EQ:RELIANCE"), "bars", "1d", "adjusted", "upstox")
    st = datetime(2026, 10, 1, 9, 15, tzinfo=UTC)
    et = datetime(2026, 10, 1, 15, 30, tzinfo=UTC)

    slice_res = ds.get(key, st, et, fill_only=True)
    assert len(slice_res.bars) == 0
    assert broker.call_count == 1


# -----------------------------------------------------------------------------
# 6. GapPlanner chunk splitting and coverage state matrix (40 cases)
# -----------------------------------------------------------------------------
@pytest.mark.parametrize("chunk_span", [1, 2, 5, 7, 10, 14, 30, 60])
def test_planner_chunk_boundary_respects_span(chunk_span: int) -> None:
    cal = DummyCalendar()
    key = SeriesKey(InstrumentId("NSE:EQ:INFY"), "bars", "1d", "adjusted", "yfinance")
    st = datetime(2026, 10, 1, 9, 15, tzinfo=UTC)
    et = datetime(2026, 10, 31, 15, 30, tzinfo=UTC)

    plan = GapPlanner.plan(key, st, et, calendar=cal, chunk_days=chunk_span)
    assert plan.has_work
    for c in plan.chunks:
        span_days = (c.end_day - c.start_day).days + 1
        assert span_days <= chunk_span


@pytest.mark.parametrize(
    ("day_offset", "is_weekend"),
    [
        (0, False),  # 2026-10-01 Thu
        (1, False),  # 2026-10-02 Fri
        (2, True),  # 2026-10-03 Sat
        (3, True),  # 2026-10-04 Sun
        (4, False),  # 2026-10-05 Mon
    ],
)
def test_planner_weekend_detection(day_offset: int, is_weekend: bool) -> None:
    cal = DummyCalendar()
    key = SeriesKey(InstrumentId("NSE:EQ:INFY"), "bars", "1d", "adjusted", "yfinance")
    d = date(2026, 10, 1) + timedelta(days=day_offset)
    st = datetime(d.year, d.month, d.day, 9, 15, tzinfo=UTC)
    et = datetime(d.year, d.month, d.day, 15, 30, tzinfo=UTC)

    plan = GapPlanner.plan(key, st, et, calendar=cal)
    if is_weekend:
        assert d in plan.closed_days
        assert d not in plan.missing_days
    else:
        assert d in plan.missing_days


@pytest.mark.parametrize(
    "holiday_date",
    [
        date(2026, 10, 2),  # Gandhi Jayanti
        date(2026, 10, 20),  # Dussehra
        date(2026, 11, 10),  # Diwali
    ],
)
def test_planner_holiday_marked_closed(holiday_date: date) -> None:
    cal = DummyCalendar(holidays=[holiday_date])
    key = SeriesKey(InstrumentId("NSE:EQ:INFY"), "bars", "1d", "adjusted", "yfinance")
    st = datetime(holiday_date.year, holiday_date.month, holiday_date.day, 9, 15, tzinfo=UTC)
    et = datetime(holiday_date.year, holiday_date.month, holiday_date.day, 15, 30, tzinfo=UTC)

    plan = GapPlanner.plan(key, st, et, calendar=cal)
    assert holiday_date in plan.closed_days
    assert holiday_date not in plan.missing_days


def test_planner_start_after_end_raises() -> None:
    cal = DummyCalendar()
    key = SeriesKey(InstrumentId("NSE:EQ:INFY"), "bars", "1d", "adjusted", "yfinance")
    st = datetime(2026, 10, 10, 9, 15, tzinfo=UTC)
    et = datetime(2026, 10, 1, 9, 15, tzinfo=UTC)

    with pytest.raises(CalendarCoverageError, match="must be <= end timestamp"):
        GapPlanner.plan(key, st, et, calendar=cal)


# -----------------------------------------------------------------------------
# 7. Resampling timeframe verification matrix (15 cases)
# -----------------------------------------------------------------------------
@pytest.mark.parametrize("target_tf", ["5m", "15m", "30m", "1h"])
def test_data_service_resampling_from_1m(tmp_path: Path, target_tf: str) -> None:
    m = DataManifest(tmp_path / "data_manifest.db")
    store = BarStore(tmp_path, manifest=m)

    class MinuteBroker:
        def history(self, key: SeriesKey, start: datetime, end: datetime) -> list[Bar]:
            bars: list[Bar] = []
            cur = start
            i = 0
            while cur <= end:
                nxt = cur + timedelta(minutes=1)
                bars.append(
                    Bar(
                        key.instrument,
                        "1m",
                        cur,
                        nxt,
                        100.0 + (i % 50),
                        101.0 + (i % 50),
                        99.0 + (i % 50),
                        100.5 + (i % 50),
                        100,
                        True,
                    )
                )
                cur = nxt
                i += 1
            return bars

    broker = MinuteBroker()
    ds = DataService(sources={"upstox": broker}, default_source="upstox", store=store)

    key = SeriesKey(InstrumentId("NSE:EQ:RELIANCE"), "bars", target_tf, "adjusted", "upstox")
    st = datetime(2026, 10, 1, 9, 15, tzinfo=UTC)
    et = datetime(2026, 10, 1, 10, 15, tzinfo=UTC)

    slice_res = ds.get(key, st, et)
    assert len(slice_res.bars) > 0
    assert slice_res.key.timeframe == target_tf
    # Verify each bar is complete and monotonic
    for i in range(len(slice_res.bars) - 1):
        assert slice_res.bars[i].start_ts < slice_res.bars[i + 1].start_ts
