"""Tests for M2-15: Coverage protection, auth errors, repair, and compare."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from algodesk.api.errors import AuthError, DataError, RateLimitError
from algodesk.api.manifest import PluginManifest
from algodesk.api.market import Bar, CoverageStatus, InstrumentId, SeriesKey
from algodesk.api.protocols import Calendar, DataSource, TradingSession
from algodesk.cli.app import app
from algodesk.data.accumulate import execute_accumulation, plan_accumulation
from algodesk.data.compare import compare_ticks_with_source
from algodesk.data.coverage import repair_coverage
from algodesk.data.fetcher import Fetcher
from algodesk.data.limiter import RateLimiter
from algodesk.data.manifest import CoverageDayRecord, DataManifest
from algodesk.data.planner import FetchChunk, FetchPlan
from algodesk.data.store import BarStore

_IST = ZoneInfo("Asia/Kolkata")


class FakeCalendar(Calendar):
    """Calendar recognizing weekdays as trading sessions from 09:15 to 15:30 IST."""

    def is_trading_day(self, d: date, segment: str = "") -> bool:
        return d.weekday() < 5

    def sessions(self, d: date, segment: str = "") -> list[TradingSession]:
        s_start = datetime(d.year, d.month, d.day, 9, 15, tzinfo=_IST).astimezone(UTC)
        s_end = datetime(d.year, d.month, d.day, 15, 30, tzinfo=_IST).astimezone(UTC)
        return [TradingSession(name=f"sess_{d.isoformat()}", start=s_start, end=s_end)]

    def next_session(self, ts: datetime, segment: str = "") -> TradingSession:
        return self.sessions(ts.date())[0]


class ConfigurableBroker(DataSource):
    """Broker source configurable to return bars, return empty, or raise errors."""

    def __init__(
        self,
        name: str = "upstox",
        bars: list[Bar] | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self.name = name
        self.bars = bars or []
        self.raise_exc = raise_exc
        self.history_calls: list[dict[str, Any]] = []
        self.manifest = PluginManifest(
            name=name,
            version="1.0.0",
            kind="broker",
            api_version=1,
            entry=f"broker:{name}",
            capabilities={"history": True},
            retention={"1m": 1800 if name == "upstox" else 7, "1d": 36500},
            max_span={"1m": 30, "1d": 3650},
        )

    def history(self, key: SeriesKey, start: datetime, end: datetime) -> Iterable[Bar]:
        self.history_calls.append({"key": key, "start": start, "end": end})
        if self.raise_exc is not None:
            raise self.raise_exc
        return [b for b in self.bars if start <= b.start_ts <= end]

    def instruments(self) -> Iterable[Any]:
        return []

    def stream(self, instruments: Any) -> Any:
        raise NotImplementedError


def _make_bar(
    instrument: str,
    ts: datetime,
    tf: str = "1m",
    open_p: float = 100.0,
    vol: int = 500,
) -> Bar:
    delta = timedelta(days=1) if tf == "1d" else timedelta(minutes=1)
    return Bar(
        instrument=InstrumentId(instrument),
        timeframe=tf,
        start_ts=ts,
        end_ts=ts + delta,
        open=open_p,
        high=open_p + 1.0,
        low=open_p - 1.0,
        close=open_p,
        volume=vol,
        complete=True,
    )


# ---------------------------------------------------------------------------
# Step 6: Protection against overwriting COMPLETE days, auth errors, --refresh
# ---------------------------------------------------------------------------


def test_refetch_zero_rows_never_changes_complete_day_to_empty(tmp_project: Any) -> None:
    """A refetch returning zero rows leaves existing COMPLETE and row count intact."""
    data_dir = tmp_project / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(m_path)
    cal = FakeCalendar()
    store = BarStore(data_dir, manifest, calendar=cal)

    key = SeriesKey(InstrumentId("NSE:EQ:TCS"), "bars", "1m", "raw", "upstox")
    d1 = date(2026, 11, 11)
    ts = datetime(2026, 11, 11, 4, 0, tzinfo=UTC)
    bar = _make_bar("NSE:EQ:TCS", ts, tf="1m")
    # Commit initial bar -> COMPLETE with 1 row
    store.commit(key, [bar])
    ds_id = store.dataset_id(key)
    recs = manifest.get_coverage_days(ds_id, start_day="2026-11-11", end_day="2026-11-11")
    assert len(recs) == 1
    assert recs[0].status == CoverageStatus.COMPLETE
    assert recs[0].rows == 1

    # Empty broker returning 0 rows
    empty_broker = ConfigurableBroker(name="upstox", bars=[])
    limiter = RateLimiter()
    fetcher = Fetcher(store=store, limiter=limiter, sources={"upstox": empty_broker})

    chunk = FetchChunk(
        chunk_id="test_chunk",
        start_day=d1,
        end_day=d1,
        start=datetime(2026, 11, 11, 0, 0, tzinfo=UTC),
        end=datetime(2026, 11, 11, 23, 59, 59, tzinfo=UTC),
        days=(d1,),
    )
    plan = FetchPlan(
        requested_key=key,
        base_key=key,
        chunks=(chunk,),
        missing_days=(d1,),
        complete_days=(),
        closed_days=(),
        unavailable_days=(),
    )
    results = fetcher.fetch(plan)
    assert len(results) == 1

    # Verify coverage record was NOT turned into EMPTY
    recs_after = manifest.get_coverage_days(ds_id, start_day="2026-11-11", end_day="2026-11-11")
    assert len(recs_after) == 1
    assert recs_after[0].status == CoverageStatus.COMPLETE
    assert recs_after[0].rows == 1


def test_refetch_failure_never_changes_stored_bars_to_empty(tmp_project: Any) -> None:
    """A refetch that fails with RateLimitError/timeout never overwrites stored COMPLETE day."""
    data_dir = tmp_project / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(m_path)
    cal = FakeCalendar()
    store = BarStore(data_dir, manifest, calendar=cal)

    key = SeriesKey(InstrumentId("NSE:EQ:TCS"), "bars", "1m", "raw", "upstox")
    d1 = date(2026, 11, 11)
    bar = _make_bar("NSE:EQ:TCS", datetime(2026, 11, 11, 4, 0, tzinfo=UTC), tf="1m")
    store.commit(key, [bar])
    ds_id = store.dataset_id(key)

    # Broker raising RateLimitError (HTTP 429)
    rate_broker = ConfigurableBroker(
        name="upstox",
        raise_exc=RateLimitError(
            what="Upstox rate limit exceeded (429)",
            where="ConfigurableBroker",
            fix="Back off",
        ),
    )
    limiter = RateLimiter()
    fetcher = Fetcher(store=store, limiter=limiter, sources={"upstox": rate_broker}, retries=0)

    chunk = FetchChunk(
        chunk_id="chunk_429",
        start_day=d1,
        end_day=d1,
        start=datetime(2026, 11, 11, 0, 0, tzinfo=UTC),
        end=datetime(2026, 11, 11, 23, 59, 59, tzinfo=UTC),
        days=(d1,),
    )
    plan = FetchPlan(
        requested_key=key,
        base_key=key,
        chunks=(chunk,),
        missing_days=(d1,),
        complete_days=(),
        closed_days=(),
        unavailable_days=(),
    )
    results = fetcher.fetch(plan)
    assert results[0].status == "EMPTY"
    assert results[0].error is not None

    # Verify coverage_days is still COMPLETE with rows=1
    recs_after = manifest.get_coverage_days(ds_id, start_day="2026-11-11", end_day="2026-11-11")
    assert recs_after[0].status == CoverageStatus.COMPLETE
    assert recs_after[0].rows == 1


def test_auth_error_raises_typed_error_naming_login_never_empty(tmp_project: Any) -> None:
    """Auth failures raise typed AuthError naming login command and never store EMPTY."""
    data_dir = tmp_project / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(m_path)
    cal = FakeCalendar()
    store = BarStore(data_dir, manifest, calendar=cal)

    key = SeriesKey(InstrumentId("NSE:EQ:TCS"), "bars", "1m", "raw", "upstox")
    d1 = date(2026, 11, 11)
    auth_broker = ConfigurableBroker(
        name="upstox",
        raise_exc=AuthError(
            what="Upstox API authentication failed with HTTP 401",
            where="UpstoxHistoryClient",
            fix="Refresh token using 'algodesk login upstox'",
        ),
    )
    limiter = RateLimiter()
    fetcher = Fetcher(store=store, limiter=limiter, sources={"upstox": auth_broker}, retries=0)

    chunk = FetchChunk(
        chunk_id="chunk_auth",
        start_day=d1,
        end_day=d1,
        start=datetime(2026, 11, 11, 0, 0, tzinfo=UTC),
        end=datetime(2026, 11, 11, 23, 59, 59, tzinfo=UTC),
        days=(d1,),
    )
    plan = FetchPlan(
        requested_key=key,
        base_key=key,
        chunks=(chunk,),
        missing_days=(d1,),
        complete_days=(),
        closed_days=(),
        unavailable_days=(),
    )

    with pytest.raises(AuthError) as exc_info:
        fetcher.fetch(plan)

    assert "run algodesk login upstox" in str(exc_info.value.fix).lower()

    # Verify coverage_days has NO records written for this day (never stored as EMPTY)
    ds_id = store.dataset_id(key)
    recs = manifest.get_coverage_days(ds_id, start_day="2026-11-11", end_day="2026-11-11")
    assert len(recs) == 0

    # Verify fetch_log recorded outcome="AUTH_ERROR"
    cur = manifest._conn.execute(
        "SELECT outcome, error FROM fetch_log WHERE source = 'upstox' ORDER BY id DESC LIMIT 1"
    )
    log_row = cur.fetchone()
    assert log_row[0] == "AUTH_ERROR"


def test_refetch_complete_days_only_with_refresh(tmp_project: Any) -> None:
    """GapPlanner and accumulate refetch COMPLETE days only when refresh=True."""
    data_dir = tmp_project / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(m_path)
    cal = FakeCalendar()

    target_d = date(2026, 11, 4)
    for tf in ("1m", "1d"):
        for inst in ("HDFCBANK", "RELIANCE", "INFY", "TCS", "SBIN"):
            ds_id = f"bars:upstox:{tf}:NSE:EQ:{inst}:raw"
            manifest.record_coverage_day(
                CoverageDayRecord(
                    dataset_id=ds_id,
                    day=target_d.isoformat(),
                    status=CoverageStatus.COMPLETE,
                    rows=361 if tf == "1m" else 1,
                    expected_rows=361 if tf == "1m" else 1,
                    part_path="parts/bars.parquet",
                    fetched_at="2026-11-04T16:00:00Z",
                    note="",
                )
            )

    now_dt = datetime(2026, 11, 5, 10, 0, tzinfo=_IST).astimezone(UTC)

    # 1. Without refresh: days are stored, 0 fetchable
    _, items_no_ref = plan_accumulation(
        group="demo5",
        source="upstox",
        days=1,
        runtime=tmp_project,
        calendar=cal,
        now_dt=now_dt,
        refresh=False,
    )
    for it in items_no_ref:
        assert target_d in it.stored_sessions
        assert it.fetchable_sessions == []

    # 2. With refresh: COMPLETE days become fetchable
    _, items_ref = plan_accumulation(
        group="demo5",
        source="upstox",
        days=1,
        runtime=tmp_project,
        calendar=cal,
        now_dt=now_dt,
        refresh=True,
    )
    for it in items_ref:
        assert target_d in it.fetchable_sessions
        assert target_d not in it.stored_sessions


# ---------------------------------------------------------------------------
# Step 7: data coverage repair re-derives coverage_days from stored parquet
# ---------------------------------------------------------------------------


def test_coverage_repair_dry_run_identifies_regressions(tmp_project: Any) -> None:
    """coverage repair --dry-run returns repair items without altering manifest."""
    data_dir = tmp_project / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(m_path)
    cal = FakeCalendar()
    store = BarStore(data_dir, manifest, calendar=cal)

    key = SeriesKey(InstrumentId("NSE:EQ:TCS"), "bars", "1m", "raw", "upstox")
    d1 = date(2026, 11, 10)
    bar = _make_bar("NSE:EQ:TCS", datetime(2026, 11, 10, 4, 0, tzinfo=UTC), tf="1m")
    store.commit(key, [bar])
    ds_id = store.dataset_id(key)

    # Corrupt coverage record to EMPTY
    manifest.record_coverage_day(
        CoverageDayRecord(
            dataset_id=ds_id,
            day=d1.isoformat(),
            status=CoverageStatus.EMPTY,
            rows=0,
            expected_rows=0,
            part_path="",
            fetched_at="2026-11-10T16:00:00Z",
            note="Simulated regression to EMPTY",
        )
    )

    repaired = repair_coverage(
        manifest=manifest,
        data_dir=data_dir,
        calendar=cal,
        source="upstox",
        timeframe="1m",
        dry_run=True,
        now_dt=datetime(2026, 11, 11, 16, 0, tzinfo=UTC),
    )
    assert len(repaired) == 1
    assert repaired[0].dataset_id == ds_id
    assert repaired[0].day == "2026-11-10"
    assert repaired[0].old_status == "EMPTY"
    assert repaired[0].new_status == "COMPLETE"
    assert repaired[0].new_rows == 1

    # Manifest is NOT modified in dry run
    rec_after = manifest.get_coverage_days(ds_id, start_day="2026-11-10", end_day="2026-11-10")[0]
    assert rec_after.status == CoverageStatus.EMPTY


def test_coverage_repair_updates_manifest_and_journals(tmp_project: Any) -> None:
    """coverage repair without dry run restores COMPLETE status and journals event."""
    data_dir = tmp_project / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(m_path)
    cal = FakeCalendar()
    store = BarStore(data_dir, manifest, calendar=cal)

    key = SeriesKey(InstrumentId("NSE:EQ:TCS"), "bars", "1m", "raw", "upstox")
    d1 = date(2026, 11, 10)
    bar = _make_bar("NSE:EQ:TCS", datetime(2026, 11, 10, 4, 0, tzinfo=UTC), tf="1m")
    store.commit(key, [bar])
    ds_id = store.dataset_id(key)

    # Corrupt coverage record to EMPTY
    manifest.record_coverage_day(
        CoverageDayRecord(
            dataset_id=ds_id,
            day=d1.isoformat(),
            status=CoverageStatus.EMPTY,
            rows=0,
            expected_rows=0,
            part_path="",
            fetched_at="2026-11-10T16:00:00Z",
            note="Corrupted",
        )
    )

    class DummyJournal:
        def __init__(self) -> None:
            self.events: list[dict[str, Any]] = []

        def append(self, kind: str, payload: dict[str, Any], durable: bool = False) -> None:
            self.events.append({"kind": kind, "payload": payload})

    journal = DummyJournal()
    repaired = repair_coverage(
        manifest=manifest,
        data_dir=data_dir,
        calendar=cal,
        source="upstox",
        timeframe="1m",
        dry_run=False,
        journal=journal,
        now_dt=datetime(2026, 11, 11, 16, 0, tzinfo=UTC),
    )
    assert len(repaired) == 1
    assert repaired[0].new_status == "COMPLETE"

    # Manifest record is now COMPLETE
    rec_after = manifest.get_coverage_days(ds_id, start_day="2026-11-10", end_day="2026-11-10")[0]
    assert rec_after.status == CoverageStatus.COMPLETE
    assert rec_after.rows == 1

    # Journal recorded event
    assert len(journal.events) == 1
    assert journal.events[0]["kind"] == "data.coverage.repair"
    assert journal.events[0]["payload"]["repair_count"] == 1


def test_coverage_repair_cli_command(tmp_project: Any) -> None:
    """CLI data coverage repair executes dry-run and live runs properly."""
    runner = CliRunner()
    res_dry = runner.invoke(
        app,
        ["data", "coverage", "repair", "--source", "upstox", "--timeframe", "1m", "--dry-run"],
    )
    assert res_dry.exit_code == 0
    assert "DRY RUN" in res_dry.output or "Coverage repair complete" in res_dry.output

    res_live = runner.invoke(
        app,
        ["data", "coverage", "repair", "--source", "upstox", "--timeframe", "1m"],
    )
    assert res_live.exit_code == 0


# ---------------------------------------------------------------------------
# Step 8: Retention lost days not in planned; loud failure logic
# ---------------------------------------------------------------------------


def test_days_beyond_retention_are_lost_not_planned(tmp_project: Any) -> None:
    """Days older than source retention are listed as lost and not counted in fetchable."""
    cal = FakeCalendar()
    # On 2026-11-20, a 10-day window reaches back past yfinance 7d 1m retention
    now_dt = datetime(2026, 11, 20, 16, 0, tzinfo=_IST).astimezone(UTC)
    insts, items = plan_accumulation(
        group="demo5",
        source="yfinance",
        days=10,
        runtime=tmp_project,
        calendar=cal,
        now_dt=now_dt,
    )
    for it in items:
        if it.timeframe == "1m":
            assert len(it.lost_sessions) > 0
            for lost_d in it.lost_sessions:
                assert lost_d not in it.fetchable_sessions


def test_first_yfinance_run_reproduction_succeeds(tmp_project: Any) -> None:
    """Reproduction of first yfinance run: succeeds with rows added, reporting lost days."""
    cal = FakeCalendar()
    now_dt = datetime(2026, 11, 12, 16, 0, tzinfo=_IST).astimezone(UTC)

    # Setup dummy broker returning 361 bars for 1m and 1 bar for 1d for 2026-11-12
    s_dt = datetime(2026, 11, 12, 4, 0, tzinfo=UTC)

    class YFinanceMockDataService:
        def __init__(self) -> None:
            self.calls: list[Any] = []

        def get(
            self,
            series: SeriesKey,
            start: datetime,
            end: datetime,
            download: str = "raw",
            fill_only: bool = False,
            refresh: bool = False,
        ) -> Any:
            count = 1 if series.timeframe == "1d" else 360

            class MockSlice:
                bars = [
                    _make_bar(str(series.instrument), s_dt, tf=series.timeframe)
                    for _ in range(count)
                ]

            return MockSlice()

    ds = YFinanceMockDataService()
    report = execute_accumulation(
        group="demo5",
        source="yfinance",
        days=7,
        runtime=tmp_project,
        data_service=ds,
        calendar=cal,
        now_dt=now_dt,
    )
    assert report.rows_added > 0
    # Sessions beyond 7d retention are reported as lost
    assert report.days_lost_beyond_window > 0
    assert report.days_still_missing == 0


def test_loud_zero_row_failure_fires_when_fetch_returns_zero_rows_no_error(
    tmp_project: Any,
) -> None:
    """Loud DataError fires when fetchable days were fetched without error but added 0 rows."""
    cal = FakeCalendar()
    now_dt = datetime(2026, 11, 12, 16, 0, tzinfo=_IST).astimezone(UTC)

    class ZeroRowDataService:
        def get(self, *args: Any, **kwargs: Any) -> Any:
            class EmptySlice:
                bars: list[Any] = []

            return EmptySlice()

    ds = ZeroRowDataService()
    with pytest.raises(DataError) as exc_info:
        execute_accumulation(
            group="demo5",
            source="upstox",
            days=1,
            runtime=tmp_project,
            data_service=ds,
            calendar=cal,
            now_dt=now_dt,
        )
    assert "zero rows were added with no error" in str(exc_info.value.what)


# ---------------------------------------------------------------------------
# Step 9: data compare explains same-day refusal from Upstox history endpoint
# ---------------------------------------------------------------------------


def test_compare_refusal_explains_same_day_upstox(tmp_project: Any) -> None:
    """data compare on same-day IST for Upstox explains that history endpoint cannot serve it."""
    today = datetime.now(_IST).date()

    class EmptyDataService:
        def get(self, *args: Any, **kwargs: Any) -> Any:
            class EmptySlice:
                bars: list[Any] = []

            return EmptySlice()

    ds = EmptyDataService()

    with pytest.raises(DataError) as exc_info:
        compare_ticks_with_source(
            date_val=today,
            symbols=["RELIANCE", "TCS"],
            source="upstox",
            runtime=tmp_project,
            data_service=ds,
            builder_factory=lambda on_close: None,
        )

    err = str(exc_info.value.what)
    assert "same-day candles are not served by the history endpoint" in err


def test_compare_refusal_explains_future_date(tmp_project: Any) -> None:
    """data compare on a future date explains that date is in the future."""
    future_date = datetime.now(_IST).date() + timedelta(days=5)

    class EmptyDataService:
        def get(self, *args: Any, **kwargs: Any) -> Any:
            class EmptySlice:
                bars: list[Any] = []

            return EmptySlice()

    ds = EmptyDataService()

    with pytest.raises(DataError) as exc_info:
        compare_ticks_with_source(
            date_val=future_date,
            symbols=["RELIANCE"],
            source="upstox",
            runtime=tmp_project,
            data_service=ds,
            builder_factory=lambda on_close: None,
        )

    err = str(exc_info.value.what)
    assert "in the future" in err
