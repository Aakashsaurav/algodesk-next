"""Tests for M2-16: Same-day history policy, PENDING coverage, and special session repair guards."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from typer.testing import CliRunner

import algodesk.data.accumulate as acc_mod
from algodesk.api.kinds import PluginKind
from algodesk.api.manifest import PluginManifest
from algodesk.api.market import Bar, CoverageStatus, InstrumentId, SeriesKey
from algodesk.api.protocols import Calendar, DataSource, TradingSession
from algodesk.cli.app import app
from algodesk.data.accumulate import (
    plan_accumulation,
    resolve_completed_sessions,
)
from algodesk.data.coverage import (
    CoverageIndex,
    evaluate_empty_record,
    repair_coverage,
    reset_stale_empty_coverage,
)
from algodesk.data.fetcher import Fetcher
from algodesk.data.limiter import RateLimiter
from algodesk.data.manifest import CoverageDayRecord, DataManifest, DatasetRecord, PartRecord
from algodesk.data.planner import FetchChunk, FetchPlan, GapPlanner
from algodesk.data.store import BarStore
from algodesk.params.registry import ParamRegistry

_IST = ZoneInfo("Asia/Kolkata")


class FakeCalendar(Calendar):
    """Calendar recognizing weekdays as trading sessions, with optional special sessions."""

    def __init__(self, special_sessions: set[date] | None = None) -> None:
        self._special = special_sessions or set()

    def is_trading_day(self, d: date, segment: str = "") -> bool:
        if d in self._special:
            return True
        return d.weekday() < 5

    def is_special(self, d: date) -> bool:
        return d in self._special

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
            kind=PluginKind.BROKER,
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
# 1. Parameter Registry: source.upstox.same_day_history = false
# ---------------------------------------------------------------------------


def test_upstox_same_day_history_param_registered() -> None:
    """ParamRegistry loads source.upstox.same_day_history with default false."""
    reg = ParamRegistry(":memory:")
    reg.load_defaults(Path("config/params"))
    val = reg.get("source.upstox.same_day_history")
    assert val is False


# ---------------------------------------------------------------------------
# 2. Fetcher: same-day empty fetch writes PENDING, past empty writes EMPTY
# ---------------------------------------------------------------------------


def test_fetcher_same_day_empty_writes_pending(tmp_project: Any) -> None:
    """Empty fetch on same IST date from source without same-day history writes PENDING."""
    data_dir = tmp_project / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(m_path)
    cal = FakeCalendar()
    store = BarStore(data_dir, manifest, calendar=cal)

    key = SeriesKey(InstrumentId("NSE:EQ:RELIANCE"), "bars", "1m", "raw", "upstox")
    # Today's date in IST
    today_ist = datetime.now(_IST).date()

    empty_broker = ConfigurableBroker(name="upstox", bars=[])
    limiter = RateLimiter()
    fetcher = Fetcher(store=store, limiter=limiter, sources={"upstox": empty_broker})

    chunk = FetchChunk(
        chunk_id="chunk_today",
        start_day=today_ist,
        end_day=today_ist,
        start=datetime(
            today_ist.year, today_ist.month, today_ist.day, 0, 0, tzinfo=_IST
        ).astimezone(UTC),
        end=datetime(
            today_ist.year, today_ist.month, today_ist.day, 23, 59, 59, tzinfo=_IST
        ).astimezone(UTC),
        days=(today_ist,),
    )
    plan = FetchPlan(
        requested_key=key,
        base_key=key,
        chunks=(chunk,),
        missing_days=(today_ist,),
        complete_days=(),
        closed_days=(),
        unavailable_days=(),
    )
    results = fetcher.fetch(plan)
    assert len(results) == 1
    assert results[0].status == "PENDING"

    ds_id = store.dataset_id(key)
    recs = manifest.get_coverage_days(
        ds_id, start_day=today_ist.isoformat(), end_day=today_ist.isoformat()
    )
    assert len(recs) == 1
    assert recs[0].status == "PENDING"
    assert "pending next-day retry" in recs[0].note.lower()


def test_fetcher_past_date_empty_writes_empty(tmp_project: Any) -> None:
    """Empty fetch on a past IST date from Upstox writes EMPTY (final empty)."""
    data_dir = tmp_project / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(m_path)
    cal = FakeCalendar()
    store = BarStore(data_dir, manifest, calendar=cal)

    key = SeriesKey(InstrumentId("NSE:EQ:INFY"), "bars", "1m", "raw", "upstox")
    past_d = date(2026, 11, 4)

    empty_broker = ConfigurableBroker(name="upstox", bars=[])
    limiter = RateLimiter()
    fetcher = Fetcher(store=store, limiter=limiter, sources={"upstox": empty_broker})

    chunk = FetchChunk(
        chunk_id="chunk_past",
        start_day=past_d,
        end_day=past_d,
        start=datetime(2026, 11, 4, 0, 0, tzinfo=_IST).astimezone(UTC),
        end=datetime(2026, 11, 4, 23, 59, 59, tzinfo=_IST).astimezone(UTC),
        days=(past_d,),
    )
    plan = FetchPlan(
        requested_key=key,
        base_key=key,
        chunks=(chunk,),
        missing_days=(past_d,),
        complete_days=(),
        closed_days=(),
        unavailable_days=(),
    )
    results = fetcher.fetch(plan)
    assert len(results) == 1
    assert results[0].status == "EMPTY"

    ds_id = store.dataset_id(key)
    recs = manifest.get_coverage_days(
        ds_id, start_day=past_d.isoformat(), end_day=past_d.isoformat()
    )
    assert len(recs) == 1
    assert recs[0].status == CoverageStatus.EMPTY


# ---------------------------------------------------------------------------
# 3. CoverageIndex / find_gaps: reports PENDING as MISSING
# ---------------------------------------------------------------------------


def test_find_gaps_reports_pending_as_missing(tmp_project: Any) -> None:
    """CoverageIndex.find_gaps reports PENDING coverage records as CoverageStatus.MISSING."""
    data_dir = tmp_project / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(m_path)
    cal = FakeCalendar()

    ds_id = "bars:upstox:1m:NSE:EQ:SBIN:raw"
    manifest.record_dataset(
        DatasetRecord(
            dataset_id=ds_id,
            instrument="NSE:EQ:RELIANCE",
            timeframe="1m",
            kind="bars",
            source="upstox",
            adjustment="raw",
        )
    )
    manifest.record_coverage_day(
        CoverageDayRecord(
            dataset_id=ds_id,
            day="2026-11-12",
            status="PENDING",
            rows=0,
            expected_rows=0,
            part_path="",
            fetched_at="2026-11-12T10:16:45+00:00",
            note="Same-day history not served by source; pending next-day retry",
        )
    )

    cov_idx = CoverageIndex(manifest=manifest, calendar=cal)
    gaps = cov_idx.detect_gaps(ds_id, start_day=date(2026, 11, 12), end_day=date(2026, 11, 12))
    assert len(gaps) == 1
    assert gaps[0].status == CoverageStatus.MISSING
    assert "pending next-day" in gaps[0].reason.lower()


# ---------------------------------------------------------------------------
# 4. reset-empty: resets same-day stale EMPTY to MISSING
# ---------------------------------------------------------------------------


def test_reset_empty_resets_same_day_stale_empty(tmp_project: Any) -> None:
    """reset_stale_empty_coverage resets existing same-day EMPTY records to MISSING."""
    data_dir = tmp_project / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(m_path)
    cal = FakeCalendar()

    ds_id = "bars:upstox:1m:NSE:EQ:HDFCBANK:raw"
    manifest.record_dataset(
        DatasetRecord(
            dataset_id=ds_id,
            instrument="NSE:EQ:RELIANCE",
            timeframe="1m",
            kind="bars",
            source="upstox",
            adjustment="raw",
        )
    )
    # Fetched on 12 Nov at 10:16 UTC (15:46 IST), which is the session's own IST date
    manifest.record_coverage_day(
        CoverageDayRecord(
            dataset_id=ds_id,
            day="2026-11-12",
            status="EMPTY",
            rows=0,
            expected_rows=0,
            part_path="",
            fetched_at="2026-11-12T10:16:45+00:00",
            note="Expected session returned no bars from source",
        )
    )

    # Evaluate directly
    rec = manifest.get_coverage_days(ds_id, start_day="2026-11-12", end_day="2026-11-12")[0]
    is_final, reason = evaluate_empty_record(rec, calendar=cal, manifest=manifest)
    assert not is_final
    assert "same-day history not served" in reason.lower()

    # Run reset_stale_empty_coverage
    reset_recs = reset_stale_empty_coverage(manifest, calendar=cal, source="upstox")
    assert len(reset_recs) == 1
    assert reset_recs[0][0].day == "2026-11-12"

    # Confirm updated in manifest to MISSING
    updated = manifest.get_coverage_days(ds_id, start_day="2026-11-12", end_day="2026-11-12")[0]
    assert updated.status == CoverageStatus.MISSING
    assert "Reset from stale EMPTY" in updated.note


# ---------------------------------------------------------------------------
# 5. Planner: next-day refetch of PENDING day
# ---------------------------------------------------------------------------


def test_planner_refetches_pending_on_later_ist_date(tmp_project: Any) -> None:
    """GapPlanner plans PENDING record as missing on a later IST date."""
    data_dir = tmp_project / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(m_path)
    cal = FakeCalendar()

    key = SeriesKey(InstrumentId("NSE:EQ:RELIANCE"), "bars", "1m", "raw", "upstox")
    ds_id = "bars:upstox:1m:NSE:EQ:RELIANCE:raw"
    manifest.record_dataset(
        DatasetRecord(
            dataset_id=ds_id,
            instrument="NSE:EQ:RELIANCE",
            timeframe="1m",
            kind="bars",
            source="upstox",
            adjustment="raw",
        )
    )
    manifest.record_coverage_day(
        CoverageDayRecord(
            dataset_id=ds_id,
            day="2026-11-12",
            status="PENDING",
            rows=0,
            expected_rows=0,
            part_path="",
            fetched_at="2026-11-12T10:16:45+00:00",
            note="Same-day history not served by source; pending next-day retry",
        )
    )

    src_manifest = PluginManifest(
        name="upstox",
        version="1.0.0",
        kind=PluginKind.BROKER,
        api_version=1,
        entry="broker:upstox",
        capabilities={"history": True},
        retention={"1m": 1800, "1d": 36500},
        max_span={"1m": 30, "1d": 3650},
    )

    # as_of is next day (2026-11-13 09:00 IST)
    next_day_as_of = datetime(2026, 11, 13, 3, 30, tzinfo=UTC)
    plan = GapPlanner.plan(
        key=key,
        start=datetime(2026, 11, 12, 0, 0, tzinfo=_IST).astimezone(UTC),
        end=datetime(2026, 11, 12, 23, 59, 59, tzinfo=_IST).astimezone(UTC),
        manifest=manifest,
        calendar=cal,
        source_manifest=src_manifest,
        as_of=next_day_as_of,
    )
    assert date(2026, 11, 12) in plan.missing_days
    assert len(plan.chunks) == 1
    assert plan.chunks[0].days == (date(2026, 11, 12),)


def test_planner_waits_for_next_ist_date_when_pending(tmp_project: Any) -> None:
    """GapPlanner does not schedule immediate retry for PENDING on the same IST date."""
    data_dir = tmp_project / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(m_path)
    cal = FakeCalendar()

    key = SeriesKey(InstrumentId("NSE:EQ:RELIANCE"), "bars", "1m", "raw", "upstox")
    ds_id = "bars:upstox:1m:NSE:EQ:RELIANCE:raw"
    manifest.record_dataset(
        DatasetRecord(
            dataset_id=ds_id,
            instrument="NSE:EQ:RELIANCE",
            timeframe="1m",
            kind="bars",
            source="upstox",
            adjustment="raw",
        )
    )
    manifest.record_coverage_day(
        CoverageDayRecord(
            dataset_id=ds_id,
            day="2026-11-12",
            status="PENDING",
            rows=0,
            expected_rows=0,
            part_path="",
            fetched_at="2026-11-12T10:16:45+00:00",
            note="Same-day history not served by source; pending next-day retry",
        )
    )

    src_manifest = PluginManifest(
        name="upstox",
        version="1.0.0",
        kind=PluginKind.BROKER,
        api_version=1,
        entry="broker:upstox",
        capabilities={"history": True},
        retention={"1m": 1800, "1d": 36500},
        max_span={"1m": 30, "1d": 3650},
    )

    # as_of is same day (2026-11-12 16:00 IST)
    same_day_as_of = datetime(2026, 11, 12, 10, 30, tzinfo=UTC)
    plan = GapPlanner.plan(
        key=key,
        start=datetime(2026, 11, 12, 0, 0, tzinfo=_IST).astimezone(UTC),
        end=datetime(2026, 11, 12, 23, 59, 59, tzinfo=_IST).astimezone(UTC),
        manifest=manifest,
        calendar=cal,
        source_manifest=src_manifest,
        as_of=same_day_as_of,
    )
    assert date(2026, 11, 12) not in plan.missing_days
    assert len(plan.chunks) == 0


# ---------------------------------------------------------------------------
# 6. Accumulate: same-day treats session completed only from next IST date
# ---------------------------------------------------------------------------


def test_resolve_completed_sessions_upstox_same_day() -> None:
    """resolve_completed_sessions excludes today for sources with same_day_history=false."""
    cal = FakeCalendar()
    # Thursday 12 Nov 2026 at 16:00 IST (market closed)
    now_dt = datetime(2026, 11, 12, 10, 30, tzinfo=UTC)

    # yfinance (default same_day_history=true) includes 12 Nov
    sess_yf = resolve_completed_sessions(cal, count=3, now_dt=now_dt, source="yfinance")
    assert date(2026, 11, 12) in sess_yf

    # upstox (same_day_history=false) excludes 12 Nov
    sess_upstox = resolve_completed_sessions(cal, count=3, now_dt=now_dt, source="upstox")
    assert date(2026, 11, 12) not in sess_upstox
    assert sess_upstox[-1] == date(2026, 11, 11)


def test_accumulate_same_day_prints_waiting_and_exits_zero(
    tmp_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """data accumulate on same IST date after close prints waiting message and exits 0."""

    # Group configuration
    groups_dir = tmp_project / "config" / "groups"
    groups_dir.mkdir(parents=True, exist_ok=True)
    (groups_dir / "demo.toml").write_text('name = "demo"\nmembers = ["NSE:EQ:RELIANCE"]\n')

    cal = FakeCalendar()
    # Same day after market close: 2026-11-12 16:00 IST
    same_day_dt = datetime(2026, 11, 12, 10, 30, tzinfo=UTC)

    instruments, plan_items = plan_accumulation(
        group="demo",
        source="upstox",
        days=3,
        runtime=type(
            "RT",
            (),
            {
                "root": tmp_project,
                "paths": type("P", (), {"data": tmp_project / "data"})(),
                "params": None,
            },
        )(),
        calendar=cal,
        now_dt=same_day_dt,
    )
    assert len(plan_items) == 2  # 1m and 1d
    for it in plan_items:
        assert date(2026, 11, 12) not in it.target_sessions
        assert it.waiting_sessions == [date(2026, 11, 12)]

    # Test CLI invocation with waiting items in report
    from algodesk.data.accumulate import AccumulationReport

    fake_rep = AccumulationReport(
        group="demo",
        source="upstox",
        days=3,
        instruments=["NSE:EQ:RELIANCE"],
        plan_items=plan_items,
        days_lost_beyond_window=0,
        waiting_items=plan_items,
    )
    monkeypatch.setattr(acc_mod, "execute_accumulation", lambda **kwargs: fake_rep)

    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "data",
            "accumulate",
            "--group",
            "demo",
            "--source",
            "upstox",
            "--days",
            "3",
        ],
    )
    assert res.exit_code == 0
    assert "waiting: same-day history not served" in res.output


def test_accumulate_retention_lost_unaffected(tmp_project: Any) -> None:
    """Sessions beyond retention window remain classified as lost and are not affected."""
    data_dir = tmp_project / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    m = DataManifest(m_path)
    m.close()
    cal = FakeCalendar()

    groups_dir = tmp_project / "config" / "groups"
    groups_dir.mkdir(parents=True, exist_ok=True)
    (groups_dir / "demo.toml").write_text('name = "demo"\nmembers = ["NSE:EQ:RELIANCE"]\n')

    # Mock runtime with 7d retention source
    rt = type(
        "RT",
        (),
        {"root": tmp_project, "paths": type("P", (), {"data": data_dir})(), "params": None},
    )()
    now_dt = datetime(2026, 11, 13, 5, 0, tzinfo=UTC)

    instruments, plan_items = plan_accumulation(
        group="demo",
        source="yfinance",
        days=15,
        runtime=rt,
        calendar=cal,
        now_dt=now_dt,
    )
    item_1m = next(it for it in plan_items if it.timeframe == "1m")
    # 1m retention for yfinance is 7 days, so target sessions older than 7 days are lost
    assert len(item_1m.lost_sessions) > 0
    assert all(d < date(2026, 11, 6) for d in item_1m.lost_sessions)


# ---------------------------------------------------------------------------
# 7. Coverage repair: CLOSED to COMPLETE guards
# ---------------------------------------------------------------------------


def test_coverage_repair_does_not_turn_closed_into_complete_without_special(
    tmp_project: Any,
) -> None:
    """repair_coverage does not turn CLOSED into COMPLETE without disagreement."""
    data_dir = tmp_project / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(m_path)
    cal = FakeCalendar()  # Sunday 2026-11-08 is NOT special in this calendar

    ds_id = "bars:upstox:1m:NSE:EQ:RELIANCE:raw"
    manifest.record_dataset(
        DatasetRecord(
            dataset_id=ds_id,
            instrument="NSE:EQ:RELIANCE",
            timeframe="1m",
            kind="bars",
            source="upstox",
            adjustment="raw",
        )
    )
    manifest.record_coverage_day(
        CoverageDayRecord(
            dataset_id=ds_id,
            day="2026-11-08",
            status="CLOSED",
            rows=0,
            expected_rows=0,
            part_path="",
            fetched_at="2026-11-08T05:00:00+00:00",
            note="Session closed",
        )
    )

    # Write a dummy parquet file with 70 rows
    part_file = data_dir / "test_part.parquet"
    ts_list = [datetime(2026, 11, 8, 12, 30, tzinfo=UTC) + timedelta(minutes=i) for i in range(70)]
    tbl = pa.Table.from_arrays([pa.array(ts_list)], names=["start_ts"])
    pq.write_table(tbl, part_file)
    manifest.record_part(
        PartRecord(
            path=str(part_file),
            dataset_id=ds_id,
            rows=70,
            bytes=1000,
            first_ts=ts_list[0].isoformat(),
            last_ts=ts_list[-1].isoformat(),
            sha256="dummy",
            source="upstox",
            source_version="1.0",
            fetched_at="2026-11-08T15:00:00+00:00",
        )
    )

    repaired = repair_coverage(
        manifest, data_dir=data_dir, calendar=cal, now_dt=datetime(2026, 11, 13, 0, 0, tzinfo=UTC)
    )
    assert len(repaired) == 0

    # Ensure status remained CLOSED
    rec = manifest.get_coverage_days(ds_id, start_day="2026-11-08", end_day="2026-11-08")[0]
    assert rec.status == "CLOSED"


def test_coverage_repair_turns_closed_into_complete_for_declared_special_session(
    tmp_project: Any,
) -> None:
    """repair_coverage turns CLOSED into COMPLETE when the day is a declared special session."""
    data_dir = tmp_project / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(m_path)
    cal = FakeCalendar(special_sessions={date(2026, 11, 8)})

    ds_id = "bars:upstox:1m:NSE:EQ:RELIANCE:raw"
    manifest.record_dataset(
        DatasetRecord(
            dataset_id=ds_id,
            instrument="NSE:EQ:RELIANCE",
            timeframe="1m",
            kind="bars",
            source="upstox",
            adjustment="raw",
        )
    )
    manifest.record_coverage_day(
        CoverageDayRecord(
            dataset_id=ds_id,
            day="2026-11-08",
            status="CLOSED",
            rows=0,
            expected_rows=0,
            part_path="",
            fetched_at="2026-11-08T05:00:00+00:00",
            note="Session closed",
        )
    )

    part_file = data_dir / "test_part_special.parquet"
    ts_list = [datetime(2026, 11, 8, 12, 30, tzinfo=UTC) + timedelta(minutes=i) for i in range(70)]
    tbl = pa.Table.from_arrays([pa.array(ts_list)], names=["start_ts"])
    pq.write_table(tbl, part_file)
    manifest.record_part(
        PartRecord(
            path=str(part_file),
            dataset_id=ds_id,
            rows=70,
            bytes=1000,
            first_ts=ts_list[0].isoformat(),
            last_ts=ts_list[-1].isoformat(),
            sha256="dummy",
            source="upstox",
            source_version="1.0",
            fetched_at="2026-11-08T15:00:00+00:00",
        )
    )

    repaired = repair_coverage(
        manifest, data_dir=data_dir, calendar=cal, now_dt=datetime(2026, 11, 13, 0, 0, tzinfo=UTC)
    )
    assert len(repaired) == 1
    assert repaired[0].old_status == "CLOSED"
    assert repaired[0].new_status == CoverageStatus.COMPLETE
    assert repaired[0].new_rows == 70

    rec = manifest.get_coverage_days(ds_id, start_day="2026-11-08", end_day="2026-11-08")[0]
    assert rec.status == CoverageStatus.COMPLETE
    assert rec.rows == 70
