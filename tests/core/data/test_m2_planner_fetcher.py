"""Tests for M2-2 GapPlanner and parallel rate-limited Fetcher."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from algodesk.api.errors import CalendarCoverageError, DataError
from algodesk.api.manifest import PluginManifest
from algodesk.api.market import (
    Bar,
    CoverageStatus,
    InstrumentId,
    SeriesKey,
)
from algodesk.api.protocols import Calendar, DataSource, TradingSession
from algodesk.api.services import JournalWriter
from algodesk.data.fetcher import Fetcher
from algodesk.data.limiter import RateLimiter
from algodesk.data.manifest import CoverageDayRecord, DataManifest
from algodesk.data.planner import (
    GapPlanner,
    is_older_than_retention,
    resolve_base_timeframe,
)
from algodesk.data.store import BarStore
from algodesk.data.validator import BarValidator

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


class CountingFakeDataSource(DataSource):
    def __init__(self, behavior: str = "ok") -> None:
        self.behavior = behavior
        self.call_count = 0
        self.fetched_ranges: list[tuple[datetime, datetime]] = []

    def history(self, key: SeriesKey, start: datetime, end: datetime) -> Iterable[Bar]:
        self.call_count += 1
        self.fetched_ranges.append((start, end))

        if self.behavior == "fail":
            raise RuntimeError("Upstream API connection refused")
        if self.behavior == "429":
            raise RuntimeError("HTTP 429 Too Many Requests")

        if self.behavior == "corrupt":
            # Non-monotonic bars trigger BarValidator validation failure and quarantine
            b1 = Bar(
                instrument=key.instrument,
                timeframe=key.timeframe,
                start_ts=start + timedelta(hours=2),
                end_ts=start + timedelta(hours=2, minutes=1),
                open=100.0,
                high=105.0,
                low=99.0,
                close=102.0,
                volume=1000,
                complete=True,
            )
            b2 = Bar(
                instrument=key.instrument,
                timeframe=key.timeframe,
                start_ts=start + timedelta(hours=1),
                end_ts=start + timedelta(hours=1, minutes=1),
                open=100.0,
                high=105.0,
                low=99.0,
                close=102.0,
                volume=1000,
                complete=True,
            )
            return [b1, b2]

        bars: list[Bar] = []
        cur = start
        delta = timedelta(minutes=1) if key.timeframe == "1m" else timedelta(days=1)
        while cur <= end:
            b = Bar(
                instrument=key.instrument,
                timeframe=key.timeframe,
                start_ts=cur,
                end_ts=cur + delta,
                open=100.0,
                high=105.0,
                low=99.0,
                close=102.0,
                volume=1000,
                complete=True,
            )
            bars.append(b)
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


# ==============================================================================
# GapPlanner Tests
# ==============================================================================


def test_resolve_base_timeframe() -> None:
    for tf in ("1m", "2m", "5m", "15m", "30m", "60m", "1h", "4h"):
        assert resolve_base_timeframe(tf) == "1m"
    for tf in ("1d", "daily", "1w", "weekly", "1mo", "1y"):
        assert resolve_base_timeframe(tf) == "1d"

    with pytest.raises(DataError):
        resolve_base_timeframe("invalid_tf_xyz")


def test_is_older_than_retention() -> None:
    as_of = date(2026, 10, 31)

    # Integer days
    assert is_older_than_retention(date(2026, 10, 20), 7, as_of) is True
    assert is_older_than_retention(date(2026, 10, 28), 7, as_of) is False

    # ISO string date
    assert is_older_than_retention(date(2021, 12, 31), "2022-01-01", as_of) is True
    assert is_older_than_retention(date(2022, 1, 1), "2022-01-01", as_of) is False


def test_planner_start_after_end_fails_loudly() -> None:
    key = SeriesKey(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        kind="bars",
        timeframe="5m",
        adjustment="adjusted",
        source="upstox",
    )
    t_start = datetime(2026, 10, 25, 10, 0, tzinfo=UTC)
    t_end = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)

    with pytest.raises(CalendarCoverageError):
        GapPlanner.plan(key, t_start, t_end)


def test_planner_identifies_complete_closed_and_missing_days(tmp_path: Path) -> None:
    db_file = tmp_path / "manifest.db"
    manifest = DataManifest(db_file)

    key = SeriesKey(
        instrument=InstrumentId("NSE:EQ:TCS"),
        kind="bars",
        timeframe="15m",
        adjustment="adjusted",
        source="upstox",
    )
    # Range: Monday 2026-10-19 to Sunday 2026-10-25
    t_start = datetime(2026, 10, 19, 0, 0, tzinfo=UTC)
    t_end = datetime(2026, 10, 25, 23, 59, tzinfo=UTC)

    cal = FakeCalendar(holidays={date(2026, 10, 21)})  # Wed is holiday

    # Pre-record Monday 2026-10-19 as COMPLETE
    base_key = SeriesKey(
        instrument=key.instrument,
        kind="bars",
        timeframe="1m",
        adjustment="adjusted",
        source="upstox",
    )
    store = BarStore(data_dir=tmp_path / "bars", manifest=manifest)
    ds_id = store.dataset_id(base_key)
    manifest.record_coverage_days(
        [
            CoverageDayRecord(
                dataset_id=ds_id,
                day="2026-10-19",
                status=CoverageStatus.COMPLETE,
                rows=375,
                expected_rows=375,
                part_path="part1.parquet",
                fetched_at=datetime.now(UTC).isoformat(),
            )
        ]
    )

    plan = GapPlanner.plan(
        key=key,
        start=t_start,
        end=t_end,
        manifest=manifest,
        calendar=cal,
        chunk_days=30,
    )

    assert plan.base_key.timeframe == "1m"
    assert date(2026, 10, 19) in plan.complete_days
    # Holiday Wed 2026-10-21 + weekend Sat 2026-10-24 & Sun 2026-10-25 are closed
    assert date(2026, 10, 21) in plan.closed_days
    assert date(2026, 10, 24) in plan.closed_days
    assert date(2026, 10, 25) in plan.closed_days

    # Missing days: Tue 2026-10-20, Thu 2026-10-22, Fri 2026-10-23
    assert date(2026, 10, 20) in plan.missing_days
    assert date(2026, 10, 22) in plan.missing_days
    assert date(2026, 10, 23) in plan.missing_days
    assert plan.has_work is True


def test_planner_marks_days_older_than_retention_unavailable() -> None:
    key = SeriesKey(
        instrument=InstrumentId("NSE:EQ:INFY"),
        kind="bars",
        timeframe="1m",
        adjustment="raw",
        source="yfinance",
    )
    now_utc = datetime(2026, 10, 31, 10, 0, tzinfo=UTC)
    # Start 20 days ago, end today. yfinance retention for 1m is 7 days.
    t_start = now_utc - timedelta(days=20)
    t_end = now_utc

    source_manifest = PluginManifest(
        name="yfinance",
        kind="broker",
        version="0.1.0",
        api_version=1,
        entry="fake:Plugin",
        capabilities={"history": True},
        retention={"1m": 7, "1d": 36500},
    )

    plan = GapPlanner.plan(
        key=key,
        start=t_start,
        end=t_end,
        source_manifest=source_manifest,
        as_of=now_utc,
    )

    # Days older than 7 days ago must be in unavailable_days
    cutoff = now_utc.date() - timedelta(days=7)
    for d in plan.unavailable_days:
        assert d < cutoff
    # Missing days must be within the 7-day retention window
    for d in plan.missing_days:
        assert d >= cutoff


# ==============================================================================
# Fetcher Tests
# ==============================================================================


def test_fetcher_single_chunk_commits_and_logs(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    manifest = DataManifest(data_dir / "manifest.db")

    limiter = RateLimiter()
    source = CountingFakeDataSource(behavior="ok")
    store = BarStore(data_dir=data_dir, manifest=manifest)
    journal = FakeJournal()

    fetcher = Fetcher(
        store=store,
        limiter=limiter,
        sources={"upstox": source},
        journal=journal,
        max_workers=2,
        retries=2,
    )

    key = SeriesKey(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        kind="bars",
        timeframe="1m",
        adjustment="adjusted",
        source="upstox",
    )
    t_start = datetime(2026, 10, 26, 0, 0, tzinfo=UTC)
    t_end = datetime(2026, 10, 27, 23, 59, tzinfo=UTC)

    plan = GapPlanner.plan(key, t_start, t_end, manifest=manifest)
    assert plan.has_work is True

    results = fetcher.fetch(plan)
    assert len(results) == len(plan.chunks)
    assert all(r.status == "OK" for r in results)
    assert source.call_count == len(plan.chunks)

    # Check fetch_log in manifest
    logs = manifest.get_fetch_logs()
    assert len(logs) >= 1
    assert logs[0].outcome == "OK"
    assert logs[0].rows > 0

    # Check journal
    fetch_events = [e for e in journal.entries if e["kind"] == "data.fetch"]
    assert len(fetch_events) >= 1
    assert fetch_events[0]["payload"]["status"] == "OK"


def test_fetcher_parallel_worker_pool(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    manifest = DataManifest(data_dir / "manifest.db")

    limiter = RateLimiter()
    source = CountingFakeDataSource(behavior="ok")
    store = BarStore(data_dir=data_dir, manifest=manifest)

    fetcher = Fetcher(
        store=store,
        limiter=limiter,
        sources={"upstox": source},
        max_workers=4,
        retries=1,
    )

    key = SeriesKey(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        kind="bars",
        timeframe="1d",
        adjustment="adjusted",
        source="upstox",
    )
    # Multiple chunks with chunk_days=5 across 20 days
    t_start = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    t_end = datetime(2026, 10, 25, 23, 59, tzinfo=UTC)

    plan = GapPlanner.plan(key, t_start, t_end, manifest=manifest, chunk_days=5)
    assert len(plan.chunks) > 1

    results = fetcher.fetch(plan)
    assert len(results) == len(plan.chunks)
    assert all(r.status == "OK" for r in results)


def test_fetcher_validation_failure_quarantines_chunk(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    manifest = DataManifest(data_dir / "manifest.db")

    limiter = RateLimiter()
    source = CountingFakeDataSource(behavior="corrupt")
    store = BarStore(data_dir=data_dir, manifest=manifest)
    journal = FakeJournal()

    fetcher = Fetcher(
        store=store,
        limiter=limiter,
        sources={"upstox": source},
        journal=journal,
        retries=1,
    )

    key = SeriesKey(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        kind="bars",
        timeframe="1m",
        adjustment="adjusted",
        source="upstox",
    )
    t_start = datetime(2026, 10, 26, 0, 0, tzinfo=UTC)
    t_end = datetime(2026, 10, 26, 23, 59, tzinfo=UTC)

    plan = GapPlanner.plan(key, t_start, t_end, manifest=manifest)
    results = fetcher.fetch(plan)

    assert len(results) == 1
    assert results[0].status == "QUARANTINED"

    # Quarantine recorded in manifest
    q_items = manifest.get_quarantine(status="quarantined")
    assert len(q_items) >= 1

    # fetch_log outcome is QUARANTINED
    logs = manifest.get_fetch_logs()
    assert logs[0].outcome == "QUARANTINED"


def test_fetcher_retries_then_empty_with_error(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    manifest = DataManifest(data_dir / "manifest.db")

    limiter = RateLimiter()
    source = CountingFakeDataSource(behavior="fail")
    store = BarStore(data_dir=data_dir, manifest=manifest)

    fetcher = Fetcher(
        store=store,
        limiter=limiter,
        sources={"upstox": source},
        retries=2,
    )

    key = SeriesKey(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        kind="bars",
        timeframe="1m",
        adjustment="adjusted",
        source="upstox",
    )
    t_start = datetime(2026, 10, 26, 0, 0, tzinfo=UTC)
    t_end = datetime(2026, 10, 26, 23, 59, tzinfo=UTC)

    plan = GapPlanner.plan(key, t_start, t_end, manifest=manifest)
    results = fetcher.fetch(plan)

    assert len(results) == 1
    assert results[0].status == "EMPTY"
    # Initial attempt + 2 retries = 3 calls
    assert source.call_count == 3

    # Manifest coverage days recorded as EMPTY
    cov_days = manifest.get_coverage_days(store.dataset_id(plan.base_key))
    assert len(cov_days) >= 1
    assert all(c.status == CoverageStatus.EMPTY for c in cov_days)


def test_interrupted_run_resumes_from_committed_chunks(tmp_path: Path) -> None:
    """Interrupted run resumes: committed chunks are not refetched on subsequent runs."""
    data_dir = tmp_path / "data"
    manifest = DataManifest(data_dir / "manifest.db")

    limiter = RateLimiter()
    source = CountingFakeDataSource(behavior="ok")
    store = BarStore(data_dir=data_dir, manifest=manifest)

    key = SeriesKey(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        kind="bars",
        timeframe="1d",
        adjustment="adjusted",
        source="upstox",
    )
    # Range of 20 days split into 2 chunks of 10 days
    t_start = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    t_end = datetime(2026, 10, 20, 23, 59, tzinfo=UTC)

    plan1 = GapPlanner.plan(key, t_start, t_end, manifest=manifest, chunk_days=10)
    assert len(plan1.chunks) >= 2

    # Simulate fetching only first chunk (e.g. process crashes before chunk 2)
    chunk1 = plan1.chunks[0]
    bars1 = list(source.history(plan1.base_key, chunk1.start, chunk1.end))
    validator = BarValidator(data_dir=data_dir, manifest=manifest)
    store.commit(plan1.base_key, bars1, validator=validator)

    initial_source_calls = source.call_count

    # Second run: re-plan the same date range!
    plan2 = GapPlanner.plan(key, t_start, t_end, manifest=manifest, chunk_days=10)

    # Chunk 1 days should now be COMPLETE and excluded from plan2 chunks!
    for d in chunk1.days:
        assert d in plan2.complete_days
        assert d not in plan2.missing_days

    # Fetch remaining plan2
    fetcher = Fetcher(store=store, limiter=limiter, sources={"upstox": source})
    fetcher.fetch(plan2)

    # Only remaining chunks fetched, no re-fetching of chunk 1
    assert source.call_count > initial_source_calls
