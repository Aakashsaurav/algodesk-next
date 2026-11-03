"""Tests for DataService.get with gap planning, rate limiting, caching, and resampling."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from algodesk.api.errors import CalendarCoverageError, NotSupported
from algodesk.api.manifest import PluginManifest
from algodesk.api.market import Bar, InstrumentId, SeriesKey
from algodesk.api.protocols import Calendar, DataSource, TradingSession
from algodesk.api.services import JournalWriter
from algodesk.data.limiter import RateLimiter
from algodesk.data.manifest import DataManifest
from algodesk.data.resample import resample
from algodesk.data.service import DataService
from algodesk.data.store import BarStore
from algodesk.results.writer import ResultsWriter

_IST = ZoneInfo("Asia/Kolkata")


class FakeCalendar(Calendar):
    def __init__(self, holidays: set[date] | None = None) -> None:
        self.holidays = holidays or set()

    def is_trading_day(self, d: date, segment: str = "") -> bool:
        if d in self.holidays:
            return False
        return d.weekday() < 5

    def sessions(self, d: date, segment: str = "") -> list[TradingSession]:
        if not self.is_trading_day(d):
            return []
        s_start = datetime(d.year, d.month, d.day, 9, 15, tzinfo=_IST).astimezone(UTC)
        s_end = datetime(d.year, d.month, d.day, 15, 30, tzinfo=_IST).astimezone(UTC)
        return [TradingSession(name=f"sess_{d.isoformat()}", start=s_start, end=s_end)]

    def next_session(self, ts: datetime, segment: str = "") -> TradingSession:
        return self.sessions(ts.date())[0]


class CountingFakeBroker(DataSource):
    def __init__(self, name: str = "fake") -> None:
        self.name = name
        self.call_count = 0
        self.calls: list[tuple[SeriesKey, datetime, datetime]] = []

    def history(self, key: SeriesKey, start: datetime, end: datetime) -> Iterable[Bar]:
        self.call_count += 1
        self.calls.append((key, start, end))

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
            price += 0.5
            cur += delta
        return bars

    def instruments(self) -> Iterable[Any]:
        return []

    def stream(self, instruments: Any) -> Any:
        raise NotImplementedError


class FakeJournal(JournalWriter):
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def append(self, kind: str, payload: dict[str, Any]) -> None:
        self.entries.append({"kind": kind, "payload": payload})


@pytest.fixture
def test_setup(tmp_path: Path) -> dict[str, Any]:
    data_dir = tmp_path / "data"
    manifest = DataManifest(data_dir / "manifest.db")
    store = BarStore(data_dir=data_dir, manifest=manifest)
    limiter = RateLimiter()
    yfinance_broker = CountingFakeBroker("yfinance")
    upstox_broker = CountingFakeBroker("upstox")
    journal = FakeJournal()
    calendar = FakeCalendar()

    yf_manifest = PluginManifest(
        name="yfinance",
        kind="broker",
        version="0.1.0",
        api_version=1,
        entry="fake:YF",
        capabilities={"history": True},
        retention={"1m": 30, "1d": 36500},
        max_span={"1m": 7, "1d": 3650},
    )
    upstox_manifest = PluginManifest(
        name="upstox",
        kind="broker",
        version="0.1.0",
        api_version=1,
        entry="fake:Upstox",
        capabilities={"history": True},
        retention={"1m": "2022-01-01", "1d": "2000-01-01"},
        max_span={"1m": 30, "1d": 3650},
        candles_adjusted="unknown",
    )

    ds = DataService(
        sources={"yfinance": yfinance_broker, "upstox": upstox_broker},
        default_source="yfinance",
        store=store,
        limiter=limiter,
        manifests={"yfinance": yf_manifest, "upstox": upstox_manifest},
        calendar=calendar,
        journal=journal,
    )

    return {
        "ds": ds,
        "store": store,
        "manifest": manifest,
        "yf_broker": yfinance_broker,
        "upstox_broker": upstox_broker,
        "journal": journal,
        "data_dir": data_dir,
    }


def test_adjust_other_than_none_raises_notsupported(test_setup: dict[str, Any]) -> None:
    ds: DataService = test_setup["ds"]
    key = SeriesKey(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        kind="bars",
        timeframe="1m",
        adjustment="adjusted",
        source="yfinance",
    )
    now = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)

    with pytest.raises(NotSupported) as exc:
        ds.get(key, now, now + timedelta(hours=1), adjust="dividends")
    assert "Dynamic adjustment" in exc.value.what


def test_calendar_coverage_error_on_invalid_dates(test_setup: dict[str, Any]) -> None:
    ds: DataService = test_setup["ds"]
    key = SeriesKey(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        kind="bars",
        timeframe="1m",
        adjustment="adjusted",
        source="yfinance",
    )
    now = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)

    with pytest.raises(CalendarCoverageError):
        ds.get(key, now, now - timedelta(hours=1))


def test_second_identical_get_makes_zero_network_calls(test_setup: dict[str, Any]) -> None:
    ds: DataService = test_setup["ds"]
    broker: CountingFakeBroker = test_setup["yf_broker"]

    key = SeriesKey(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        kind="bars",
        timeframe="1m",
        adjustment="adjusted",
        source="yfinance",
    )
    # Session: Monday 2026-10-26
    start = datetime(2026, 10, 26, 4, 0, tzinfo=UTC)
    end = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)

    # First get -> fetches from broker
    slice1 = ds.get(key, start, end)
    assert len(slice1.bars) > 0
    first_calls = broker.call_count
    assert first_calls > 0

    # Second identical get -> served completely from BarStore cache!
    slice2 = ds.get(key, start, end)
    assert len(slice2.bars) == len(slice1.bars)
    assert broker.call_count == first_calls  # Zero new network calls!


def test_extend_by_one_session_fetches_one_session(test_setup: dict[str, Any]) -> None:
    ds: DataService = test_setup["ds"]
    broker: CountingFakeBroker = test_setup["yf_broker"]

    key = SeriesKey(
        instrument=InstrumentId("NSE:EQ:TCS"),
        kind="bars",
        timeframe="1m",
        adjustment="adjusted",
        source="yfinance",
    )
    # Session 1: Monday 2026-10-26
    s1_start = datetime(2026, 10, 26, 4, 0, tzinfo=UTC)
    s1_end = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)
    ds.get(key, s1_start, s1_end)
    calls_after_s1 = broker.call_count

    # Session 2: Tuesday 2026-10-27
    s2_end = datetime(2026, 10, 27, 10, 0, tzinfo=UTC)
    # Query spanning both sessions [s1_start, s2_end]
    slice_both = ds.get(key, s1_start, s2_end)
    assert len(slice_both.bars) > 0

    # Broker was only called for Session 2!
    assert broker.call_count == calls_after_s1 + 1


def test_5m_request_equals_resampled_1m(test_setup: dict[str, Any]) -> None:
    ds: DataService = test_setup["ds"]

    key_1m = SeriesKey(
        instrument=InstrumentId("NSE:EQ:INFY"),
        kind="bars",
        timeframe="1m",
        adjustment="adjusted",
        source="yfinance",
    )
    key_5m = SeriesKey(
        instrument=InstrumentId("NSE:EQ:INFY"),
        kind="bars",
        timeframe="5m",
        adjustment="adjusted",
        source="yfinance",
    )
    start = datetime(2026, 10, 26, 4, 0, tzinfo=UTC)
    end = datetime(2026, 10, 26, 6, 0, tzinfo=UTC)

    slice_1m = ds.get(key_1m, start, end)
    slice_5m = ds.get(key_5m, start, end)

    # 5m bars should match direct resample of 1m bars
    resampled_direct = tuple(resample(slice_1m.bars, "5m"))
    assert len(slice_5m.bars) == len(resampled_direct)
    for b_svc, b_direct in zip(slice_5m.bars, resampled_direct, strict=False):
        assert b_svc.start_ts == b_direct.start_ts
        assert b_svc.open == pytest.approx(b_direct.open)
        assert b_svc.high == pytest.approx(b_direct.high)
        assert b_svc.low == pytest.approx(b_direct.low)
        assert b_svc.close == pytest.approx(b_direct.close)
        assert b_svc.volume == b_direct.volume


def test_fill_only_fetches_without_returning_bars(test_setup: dict[str, Any]) -> None:
    ds: DataService = test_setup["ds"]
    broker: CountingFakeBroker = test_setup["yf_broker"]

    key = SeriesKey(
        instrument=InstrumentId("NSE:EQ:HDFCBANK"),
        kind="bars",
        timeframe="1m",
        adjustment="adjusted",
        source="yfinance",
    )
    start = datetime(2026, 10, 26, 4, 0, tzinfo=UTC)
    end = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)

    slice_filled = ds.get(key, start, end, fill_only=True)
    assert len(slice_filled.bars) == 0
    assert broker.call_count == 1

    # Now read without fill_only -> zero network calls
    slice_read = ds.get(key, start, end, fill_only=False)
    assert len(slice_read.bars) > 0
    assert broker.call_count == 1


def test_upstox_provenance_contains_adjustment_warning(test_setup: dict[str, Any]) -> None:
    ds: DataService = test_setup["ds"]
    key = SeriesKey(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        kind="bars",
        timeframe="1d",
        adjustment="adjusted",
        source="upstox",
    )
    start = datetime(2026, 10, 20, 0, 0, tzinfo=UTC)
    end = datetime(2026, 10, 25, 23, 59, tzinfo=UTC)

    data_slice = ds.get(key, start, end)
    prov = data_slice.provenance_info
    assert "warnings" in prov
    assert any("Upstox candle adjustment unknown" in w for w in prov["warnings"])


def test_yfinance_separate_series_adjusted_and_raw(test_setup: dict[str, Any]) -> None:
    ds: DataService = test_setup["ds"]
    broker: CountingFakeBroker = test_setup["yf_broker"]

    inst = InstrumentId("NSE:EQ:SBIN")
    start = datetime(2026, 10, 26, 4, 0, tzinfo=UTC)
    end = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)

    # Fetch adjusted
    slice_adj = ds.get(inst, start, end, download="adjusted")
    assert slice_adj.adjustment == "adjusted"
    assert broker.calls[-1][0].adjustment == "adjusted"

    # Fetch raw
    slice_raw = ds.get(inst, start, end, download="raw")
    assert slice_raw.adjustment == "raw"
    assert broker.calls[-1][0].adjustment == "raw"

    # Distinct datasets stored
    assert slice_adj.dataset_ids != slice_raw.dataset_ids


def test_results_manifest_lists_datasets_and_checksums(tmp_path: Path) -> None:
    """FR-DAT-7: Results manifest records all datasets and checksums accessed."""
    run_dir = tmp_path / "results"
    writer = ResultsWriter(run_id="test_run_prov", results_dir=run_dir)

    manifest_info = {
        "code_version": "0.1.0",
        "datasets": ["NSE:EQ:RELIANCE:1m:adjusted", "NSE:EQ:TCS:1m:adjusted"],
        "checksums": ["a" * 64, "b" * 64],
        "data_files": [],
        "seed": 42,
    }

    writer.write(
        summary_info={"run_name": "test_run_prov", "mode": "paper"},
        metrics={"net_pnl": 100.0},
        trades=[],
        equity_points=[],
        effective_config_text="",
        registry_snapshot={},
        manifest_info=manifest_info,
    )

    manifest_file = run_dir / "test_run_prov" / "manifest.json"
    assert manifest_file.exists()
    saved = json.loads(manifest_file.read_text())
    assert "datasets" in saved
    assert "NSE:EQ:RELIANCE:1m:adjusted" in saved["datasets"]
    assert "checksums" in saved
    assert len(saved["checksums"]) == 2
