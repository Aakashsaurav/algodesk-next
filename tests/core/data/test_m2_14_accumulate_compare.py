"""Tests for M2-14: GapPlanner-based accumulation planning and data compare improvements."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from algodesk.api.errors import DataError
from algodesk.api.manifest import PluginManifest
from algodesk.api.market import Bar, CoverageStatus, InstrumentId, SeriesKey, Tick
from algodesk.api.protocols import Calendar, DataSource, TradingSession
from algodesk.cli.app import app
from algodesk.data.accumulate import (
    AccumulationPlanItem,
    execute_accumulation,
    plan_accumulation,
)
from algodesk.data.compare import _compare_bar_series
from algodesk.data.manifest import CoverageDayRecord, DataManifest
from algodesk.data.service import DataService
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


class FakeSourceBroker(DataSource):
    """Configurable fake broker for compare and accumulation tests."""

    def __init__(self, name: str = "fake_src", bars: list[Bar] | None = None) -> None:
        self.name = name
        self.bars = bars or []
        self.manifest = PluginManifest(
            name=name,
            version="1.0.0",
            kind="broker",
            api_version=1,
            entry=f"fake:{name}",
            capabilities={"history": True},
            retention={"1m": 7, "1d": 36500},
            max_span={"1m": 7, "1d": 3650},
        )

    def history(self, key: SeriesKey, start: datetime, end: datetime) -> Iterable[Bar]:
        return [b for b in self.bars if start <= b.start_ts <= end]

    def instruments(self) -> Iterable[Any]:
        return []

    def stream(self, instruments: Any) -> Any:
        raise NotImplementedError


def _setup_manifest_with_coverage(
    data_dir: Path,
    dataset_id: str,
    days_status: list[tuple[date, CoverageStatus, str]],
) -> DataManifest:
    m_path = data_dir / "state" / "data_manifest.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(m_path)
    for d, st, note in days_status:
        manifest.record_coverage_day(
            CoverageDayRecord(
                dataset_id=dataset_id,
                day=d.isoformat(),
                status=st,
                rows=100 if st == CoverageStatus.COMPLETE else 0,
                expected_rows=100,
                part_path="parts/day.parquet",
                fetched_at=datetime(d.year, d.month, d.day, 16, 0, tzinfo=UTC).isoformat(),
                note=note,
            )
        )
    return manifest


def test_accumulation_plan_skips_complete_sessions(tmp_project: Any) -> None:
    """GapPlanner accumulation plan leaves complete days in stored and 0 fetchable."""
    data_dir = tmp_project / "data"
    d1 = date(2026, 11, 4)
    d2 = date(2026, 11, 3)
    d3 = date(2026, 11, 2)
    # Populate COMPLETE for both 1m and 1d for RELIANCE
    for tf in ("1m", "1d"):
        ds_id = f"bars:upstox:{tf}:NSE:EQ:RELIANCE:raw"
        _setup_manifest_with_coverage(
            data_dir,
            ds_id,
            [
                (d1, CoverageStatus.COMPLETE, "ok"),
                (d2, CoverageStatus.COMPLETE, "ok"),
                (d3, CoverageStatus.COMPLETE, "ok"),
            ],
        )

    cal = FakeCalendar()
    now_dt = datetime(2026, 11, 5, 10, 0, tzinfo=_IST).astimezone(UTC)
    insts, items = plan_accumulation(
        group="demo5",
        source="upstox",
        days=3,
        runtime=tmp_project,
        calendar=cal,
        now_dt=now_dt,
    )
    rel_items = [it for it in items if it.instrument == "NSE:EQ:RELIANCE"]
    for it in rel_items:
        assert len(it.stored_sessions) == 3
        assert len(it.fetchable_sessions) == 0
        assert len(it.lost_sessions) == 0


def test_accumulation_plan_schedules_missing_and_non_final_empty(tmp_project: Any) -> None:
    """GapPlanner accumulation plan schedules non-final EMPTY records as fetchable."""
    data_dir = tmp_project / "data"
    target_d = date(2026, 11, 4)
    # Non-final EMPTY record (fetched before close)
    ds_id = "bars:yfinance:1m:NSE:EQ:RELIANCE:raw"
    m_path = data_dir / "state" / "data_manifest.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(m_path)
    manifest.record_coverage_day(
        CoverageDayRecord(
            dataset_id=ds_id,
            day=target_d.isoformat(),
            status=CoverageStatus.EMPTY,
            rows=0,
            expected_rows=375,
            part_path="",
            fetched_at="2026-11-04T04:14:00Z",  # 09:44 IST, market open
            note="Expected session returned no bars",
        )
    )

    cal = FakeCalendar()
    now_dt = datetime(2026, 11, 4, 16, 0, tzinfo=_IST).astimezone(UTC)
    insts, items = plan_accumulation(
        group="demo5",
        source="yfinance",
        days=1,
        runtime=tmp_project,
        calendar=cal,
        now_dt=now_dt,
    )
    rel_1m = [it for it in items if it.instrument == "NSE:EQ:RELIANCE" and it.timeframe == "1m"][0]
    assert target_d in rel_1m.fetchable_sessions
    assert rel_1m.refetched_days == 1


def test_accumulation_plan_marks_days_beyond_retention_as_lost(tmp_project: Any) -> None:
    """GapPlanner accumulation plan puts sessions older than retention in lost_sessions."""
    cal = FakeCalendar()
    # As of 20 Nov 2026, a 10-day window reaches back past 7d yfinance 1m limit
    now_dt = datetime(2026, 11, 20, 16, 0, tzinfo=_IST).astimezone(UTC)
    insts, items = plan_accumulation(
        group="demo5",
        source="yfinance",
        days=10,
        runtime=tmp_project,
        calendar=cal,
        now_dt=now_dt,
    )
    m_1m = [it for it in items if it.timeframe == "1m"][0]
    assert len(m_1m.lost_sessions) > 0
    # Lost sessions must NOT be scheduled for fetch
    for lost_d in m_1m.lost_sessions:
        assert lost_d not in m_1m.fetchable_sessions


def test_accumulation_up_to_date_cache_cli_exit_zero(
    tmp_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI prints up-to-date message and exits 0 when no missing completed sessions."""
    from algodesk.data import accumulate as acc_mod

    item = AccumulationPlanItem(
        instrument="NSE:EQ:RELIANCE",
        timeframe="1m",
        target_sessions=[date(2026, 11, 4)],
        stored_sessions=[date(2026, 11, 4)],
        fetchable_sessions=[],
        lost_sessions=[],
    )
    monkeypatch.setattr(
        acc_mod,
        "plan_accumulation",
        lambda **kwargs: (["NSE:EQ:RELIANCE"], [item]),
    )

    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "data",
            "accumulate",
            "--group",
            "demo5",
            "--source",
            "upstox",
        ],
    )
    assert res.exit_code == 0
    assert "Up to date: no missing completed sessions" in res.output


def test_accumulation_execute_up_to_date_cache_no_error(
    tmp_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """execute_accumulation with 0 fetchable days succeeds with 0 rows added."""
    from algodesk.data import accumulate as acc_mod

    item = AccumulationPlanItem(
        instrument="NSE:EQ:RELIANCE",
        timeframe="1m",
        target_sessions=[date(2026, 11, 4)],
        stored_sessions=[date(2026, 11, 4)],
        fetchable_sessions=[],
        lost_sessions=[],
    )
    monkeypatch.setattr(
        acc_mod,
        "plan_accumulation",
        lambda **kwargs: (["NSE:EQ:RELIANCE"], [item]),
    )
    rep = execute_accumulation(
        group="demo5",
        source="upstox",
        days=1,
        runtime=tmp_project,
        dry_run=False,
        data_service=MagicMock(),
        calendar=FakeCalendar(),
    )
    assert rep.rows_added == 0
    assert rep.days_still_missing == 0


def test_accumulation_execute_retention_lost_days_no_error(
    tmp_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """execute_accumulation with only retention-lost days completes without error."""
    from algodesk.data import accumulate as acc_mod

    item = AccumulationPlanItem(
        instrument="NSE:EQ:RELIANCE",
        timeframe="1m",
        target_sessions=[date(2026, 10, 20)],
        stored_sessions=[],
        fetchable_sessions=[],
        lost_sessions=[date(2026, 10, 20)],
    )
    monkeypatch.setattr(
        acc_mod,
        "plan_accumulation",
        lambda **kwargs: (["NSE:EQ:RELIANCE"], [item]),
    )

    rep = execute_accumulation(
        group="demo5",
        source="yfinance",
        days=1,
        runtime=tmp_project,
        dry_run=False,
        data_service=MagicMock(),
        calendar=FakeCalendar(),
    )
    assert rep.rows_added == 0
    assert rep.days_lost_beyond_window == 1
    assert rep.days_still_missing == 0


def test_accumulation_execute_real_zero_row_fetch_raises_data_error(
    tmp_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When chunks are fetched but source returns 0 rows and not final EMPTY, raise DataError."""
    from algodesk.data import accumulate as acc_mod

    item = AccumulationPlanItem(
        instrument="NSE:EQ:RELIANCE",
        timeframe="1m",
        target_sessions=[date(2026, 11, 4)],
        stored_sessions=[],
        fetchable_sessions=[date(2026, 11, 4)],
        lost_sessions=[],
    )
    monkeypatch.setattr(
        acc_mod,
        "plan_accumulation",
        lambda **kwargs: (["NSE:EQ:RELIANCE"], [item]),
    )
    mock_ds = MagicMock()
    mock_ds.get.return_value = MagicMock(bars=())

    with pytest.raises(DataError) as exc_info:
        execute_accumulation(
            group="demo5",
            source="upstox",
            days=1,
            runtime=tmp_project,
            dry_run=False,
            data_service=mock_ds,
            calendar=FakeCalendar(),
        )
    assert "zero rows were added with no error" in str(exc_info.value.what)


def test_accumulation_execute_valid_final_empty_no_error(
    tmp_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When chunks return 0 rows for valid final-empty days, no error is raised."""
    from algodesk.data import accumulate as acc_mod

    data_dir = tmp_project / "data"
    d = date(2026, 11, 4)
    # Pre-record day as final EMPTY (after settle margin)
    ds_id = "bars:upstox:1m:NSE:EQ:RELIANCE:raw"
    m_path = data_dir / "state" / "data_manifest.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(m_path)
    manifest.record_coverage_day(
        CoverageDayRecord(
            dataset_id=ds_id,
            day=d.isoformat(),
            status=CoverageStatus.EMPTY,
            rows=0,
            expected_rows=0,
            part_path="",
            fetched_at="2026-11-05T05:00:00Z",  # next IST date, final EMPTY
            note="Expected session returned no bars from source",
        )
    )

    item = AccumulationPlanItem(
        instrument="NSE:EQ:RELIANCE",
        timeframe="1m",
        target_sessions=[d],
        stored_sessions=[],
        fetchable_sessions=[d],
        lost_sessions=[],
    )
    monkeypatch.setattr(
        acc_mod,
        "plan_accumulation",
        lambda **kwargs: (["NSE:EQ:RELIANCE"], [item]),
    )
    mock_ds = MagicMock()
    mock_ds.get.return_value = MagicMock(bars=())

    rep = execute_accumulation(
        group="demo5",
        source="upstox",
        days=1,
        runtime=tmp_project,
        dry_run=False,
        data_service=mock_ds,
        calendar=FakeCalendar(),
    )
    assert rep.rows_added == 0


def test_compare_mid_session_start_excludes_first_bar_vol_diff_fixtures() -> None:
    """Mid-session start excludes first bar volume from volume difference."""
    inst = InstrumentId("NSE:EQ:RELIANCE")
    dt_open = datetime(2026, 11, 11, 3, 45, tzinfo=UTC)  # 09:15 IST
    dt_mid1 = datetime(2026, 11, 11, 9, 21, tzinfo=UTC)  # 14:51 IST
    dt_mid2 = datetime(2026, 11, 11, 9, 22, tzinfo=UTC)  # 14:52 IST

    # Source has full session starting at 09:15
    s_open = Bar(
        inst, "1m", dt_open, dt_open + timedelta(minutes=1), 100, 101, 99, 100, 5000, complete=True
    )
    s_bar1 = Bar(
        inst, "1m", dt_mid1, dt_mid1 + timedelta(minutes=1), 100, 101, 99, 100, 30000, complete=True
    )
    s_bar2 = Bar(
        inst, "1m", dt_mid2, dt_mid2 + timedelta(minutes=1), 100, 101, 99, 100, 25000, complete=True
    )

    # Built starts mid-session at 14:51 with cumulative day volume in bar 1
    b_bar1 = Bar(
        inst,
        "1m",
        dt_mid1,
        dt_mid1 + timedelta(minutes=1),
        100,
        101,
        99,
        100,
        15000000,
        complete=True,
    )
    b_bar2 = Bar(
        inst, "1m", dt_mid2, dt_mid2 + timedelta(minutes=1), 100, 101, 99, 100, 25005, complete=True
    )

    res = _compare_bar_series(
        [b_bar1, b_bar2],
        [s_open, s_bar1, s_bar2],
        tolerance_pct=0.1,
        symbol="RELIANCE",
    )
    assert res.first_bar_vol_excluded is True
    assert res.matched_count == 2
    # Bar 1 volume diff (15000000 - 30000) was excluded; bar 2 diff is |25005 - 25000| = 5
    assert res.max_volume_diff == 5


def test_compare_normal_session_start_includes_first_bar_vol_diff_fixtures() -> None:
    """When both sides start at the same time, first bar volume is included."""
    inst = InstrumentId("NSE:EQ:RELIANCE")
    dt = datetime(2026, 11, 11, 3, 45, tzinfo=UTC)  # 09:15 IST

    b_bar = Bar(inst, "1m", dt, dt + timedelta(minutes=1), 100, 101, 99, 100, 5050, complete=True)
    s_bar = Bar(inst, "1m", dt, dt + timedelta(minutes=1), 100, 101, 99, 100, 5000, complete=True)

    res = _compare_bar_series([b_bar], [s_bar], tolerance_pct=0.1, symbol="RELIANCE")
    assert res.first_bar_vol_excluded is False
    assert res.max_volume_diff == 50


def test_compare_src_missing_bars_explanation_line_fixtures() -> None:
    """When built has bars where source is missing, report sets SRC MISSING explanation."""
    inst = InstrumentId("NSE:EQ:RELIANCE")
    dt1 = datetime(2026, 11, 11, 9, 50, tzinfo=UTC)  # 15:20 IST
    dt2 = datetime(2026, 11, 11, 9, 51, tzinfo=UTC)  # 15:21 IST

    b1 = Bar(inst, "1m", dt1, dt1 + timedelta(minutes=1), 100, 101, 99, 100, 10, complete=True)
    b2 = Bar(inst, "1m", dt2, dt2 + timedelta(minutes=1), 100, 101, 99, 100, 10, complete=True)
    # Source only has b1, missing b2
    res = _compare_bar_series([b1, b2], [b1], tolerance_pct=0.1, symbol="RELIANCE")
    assert res.built_only_count == 1
    assert res.src_missing_explanation is not None
    assert "SRC MISSING:" in res.src_missing_explanation
    assert "vendor source has no bars at those timestamps" in res.src_missing_explanation


def test_compare_cli_displays_vol_note_and_src_missing(
    tmp_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI data compare prints MAX VOL DIFF*, footnote, and SRC MISSING line."""
    inst = InstrumentId("NSE:EQ:RELIANCE")
    dt_open = datetime(2026, 11, 11, 3, 45, tzinfo=UTC)  # 09:15 IST
    dt_mid = datetime(2026, 11, 11, 9, 21, tzinfo=UTC)  # 14:51 IST
    dt_extra = datetime(2026, 11, 11, 9, 50, tzinfo=UTC)  # 15:20 IST

    s_bars = [
        Bar(
            inst,
            "1m",
            dt_open,
            dt_open + timedelta(minutes=1),
            100,
            101,
            99,
            100,
            1000,
            complete=True,
        ),
        Bar(
            inst, "1m", dt_mid, dt_mid + timedelta(minutes=1), 100, 101, 99, 100, 500, complete=True
        ),
    ]
    b_bars = [
        Bar(
            inst,
            "1m",
            dt_mid,
            dt_mid + timedelta(minutes=1),
            100,
            101,
            99,
            100,
            500000,
            complete=True,
        ),
        Bar(
            inst,
            "1m",
            dt_extra,
            dt_extra + timedelta(minutes=1),
            100,
            101,
            99,
            100,
            10,
            complete=True,
        ),
    ]

    broker = FakeSourceBroker(name="fake_src", bars=s_bars)
    data_dir = tmp_project / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(m_path)

    from algodesk.cli import data as data_cli

    def _fake_build_ds(rt: Any, source: str = "fake_src") -> DataService:
        store = BarStore(data_dir, manifest, params=None)
        return DataService(
            sources={"fake_src": broker},
            default_source="fake_src",
            store=store,
            limiter=None,
            calendar=FakeCalendar(),
            params=None,
        )

    monkeypatch.setattr(data_cli, "_build_data_service", _fake_build_ds)
    monkeypatch.setattr(
        "algodesk.data.compare._load_ticks_for_symbol_date",
        lambda rt, sym, dt_val: (inst, [Tick(inst, dt_mid, dt_mid, 100.0, 10, 500000)]),
    )
    monkeypatch.setattr(
        "algodesk.data.compare._build_bars_from_ticks",
        lambda ticks, builder_factory=None: b_bars,
    )

    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "data",
            "compare",
            "--date",
            "2026-11-11",
            "--symbols",
            "RELIANCE",
            "--source",
            "fake_src",
        ],
    )
    assert res.exit_code == 0
    assert "MAX VOL DIFF*" in res.output
    assert "* Note: Volume of first bar built from mid-session start is excluded" in res.output
    assert "SRC MISSING: RELIANCE has 1 built bar(s) unmatched" in res.output
