"""Tests for CP2: no silent gaps, phantom bar drops, and scoped yfinance warning filter."""

from __future__ import annotations

import warnings
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from plugins.brokers.yfinance.plugin import YFinanceBrokerPlugin
from typer.testing import CliRunner

from algodesk.api.manifest import PluginManifest
from algodesk.api.market import Bar, CoverageStatus, DataGap, DataSlice, InstrumentId, SeriesKey
from algodesk.api.protocols import DataSource
from algodesk.api.services import JournalWriter
from algodesk.cli.app import app
from algodesk.data.fetcher import Fetcher
from algodesk.data.limiter import RateLimiter
from algodesk.data.manifest import DataManifest
from algodesk.data.planner import GapPlanner
from algodesk.data.service import DataService
from algodesk.data.store import BarStore
from algodesk.data.validator import BarValidator

_TZ = ZoneInfo("Asia/Kolkata")


class FakeJournal(JournalWriter):
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def append(self, kind: str, payload: Any, durable: bool = False) -> int:
        self.entries.append({"kind": kind, "payload": payload})
        return len(self.entries)


class PartialDataSource(DataSource):
    """Returns bars for only specified dates."""

    def __init__(self, available_dates: set[date], timeframe: str = "1d") -> None:
        self.available_dates = available_dates
        self.timeframe = timeframe

    def history(self, key: SeriesKey, start: datetime, end: datetime) -> Iterable[Bar]:
        bars: list[Bar] = []
        for d in sorted(self.available_dates):
            # Stamp at 18:30Z previous day (midnight IST) for daily candles
            dt_utc = datetime(d.year, d.month, d.day, 0, 0, tzinfo=_TZ).astimezone(UTC)
            bars.append(
                Bar(
                    instrument=key.instrument,
                    timeframe=key.timeframe,
                    start_ts=dt_utc,
                    end_ts=dt_utc + timedelta(days=1),
                    open=100.0,
                    high=105.0,
                    low=98.0,
                    close=102.0,
                    volume=1000,
                    complete=True,
                )
            )
        return bars

    def instruments(self) -> Iterable[Any]:
        return []

    def stream(self, instruments: Any) -> Any:
        raise NotImplementedError


def test_expected_sessions_minus_returned_days_become_empty_never_complete(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    manifest = DataManifest(data_dir / "manifest.db")
    store = BarStore(data_dir=data_dir, manifest=manifest)
    limiter = RateLimiter()

    # Plan 5 trading days: Mon 2026-10-26 to Fri 2026-10-30
    # Source only returns Monday 2026-10-26
    source = PartialDataSource(available_dates={date(2026, 10, 26)})
    fetcher = Fetcher(store=store, limiter=limiter, sources={"upstox": source})

    key = SeriesKey(InstrumentId("NSE:EQ:RELIANCE"), "bars", "1d", "adjusted", "upstox")
    t_start = datetime(2026, 10, 26, 0, 0, tzinfo=UTC)
    t_end = datetime(2026, 10, 30, 23, 59, tzinfo=UTC)

    plan = GapPlanner.plan(key, t_start, t_end, manifest=manifest)
    fetcher.fetch(plan)

    ds_id = store.dataset_id(key)
    cov_recs = {r.day: r for r in manifest.get_coverage_days(ds_id, "2026-10-26", "2026-10-30")}

    # Monday should have bars
    st = cov_recs["2026-10-26"].status
    assert st in (CoverageStatus.PARTIAL, CoverageStatus.COMPLETE)
    # Tue, Wed, Thu, Fri MUST be EMPTY, never COMPLETE
    for missing_day in ("2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"):
        assert missing_day in cov_recs
        assert cov_recs[missing_day].status == CoverageStatus.EMPTY
        assert cov_recs[missing_day].rows == 0


def test_missing_sessions_show_in_dataslice_gaps(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    manifest = DataManifest(data_dir / "manifest.db")
    store = BarStore(data_dir=data_dir, manifest=manifest)
    limiter = RateLimiter()

    source = PartialDataSource(available_dates={date(2026, 10, 26)})
    service = DataService(
        sources={"upstox": source},
        default_source="upstox",
        store=store,
        limiter=limiter,
    )

    key = SeriesKey(InstrumentId("NSE:EQ:RELIANCE"), "bars", "1d", "adjusted", "upstox")
    t_start = datetime(2026, 10, 26, 0, 0, tzinfo=UTC)
    t_end = datetime(2026, 10, 30, 23, 59, tzinfo=UTC)

    d_slice = service.get(key, t_start, t_end)
    assert len(d_slice.bars) == 1
    assert len(d_slice.gaps) == 4
    gap_days = [g.day for g in d_slice.gaps]
    assert date(2026, 10, 27) in gap_days
    assert date(2026, 10, 28) in gap_days
    assert date(2026, 10, 29) in gap_days
    assert date(2026, 10, 30) in gap_days
    assert all(g.status == CoverageStatus.EMPTY for g in d_slice.gaps)


def test_missing_sessions_show_in_data_gaps_cli(
    monkeypatch: Any,
    tmp_project: Path,
) -> None:
    data_dir = tmp_project / "data"
    manifest = DataManifest(data_dir / "manifest.db")
    store = BarStore(data_dir=data_dir, manifest=manifest)

    # Record EMPTY days in manifest
    inst = InstrumentId("NSE:EQ:RELIANCE")
    ds_id = store.dataset_id(SeriesKey(inst, "bars", "1d", "adjusted", "upstox"))
    from algodesk.data.manifest import CoverageDayRecord, DatasetRecord

    manifest.record_dataset(
        DatasetRecord(
            dataset_id=ds_id,
            instrument="NSE:EQ:RELIANCE",
            kind="bars",
            timeframe="1d",
            adjustment="adjusted",
            source="upstox",
            schema_version=1,
            created_at=datetime.now(UTC).isoformat(),
            actions_epoch=None,
            adjustment_basis="splits",
        )
    )
    manifest.record_coverage_days(
        [
            CoverageDayRecord(
                dataset_id=ds_id,
                day="2026-10-27",
                status=CoverageStatus.EMPTY,
                rows=0,
                expected_rows=0,
                part_path="",
                fetched_at=datetime.now(UTC).isoformat(),
                note="Expected session returned no bars",
            )
        ]
    )

    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "data",
            "gaps",
            "RELIANCE",
            "--from",
            "2026-10-25",
            "--to",
            "2026-10-30",
            "--timeframe",
            "1d",
            "--source",
            "upstox",
        ],
    )
    assert res.exit_code == 0
    assert "2026-10-27" in res.output
    assert "EMPTY" in res.output


def test_data_get_prints_gap_summary(
    monkeypatch: Any,
    tmp_project: Path,
) -> None:
    inst = InstrumentId("NSE:EQ:RELIANCE")
    key = SeriesKey(inst, "bars", "1d", "adjusted", "yfinance")
    t0 = datetime(2026, 10, 26, 0, 0, tzinfo=UTC)

    bar = Bar(
        instrument=inst,
        timeframe="1d",
        start_ts=t0,
        end_ts=t0 + timedelta(days=1),
        open=100.0,
        high=105.0,
        low=99.0,
        close=102.0,
        volume=1000,
        complete=True,
    )
    gaps = (
        DataGap(day=date(2026, 10, 27), status=CoverageStatus.EMPTY, reason="No data"),
        DataGap(day=date(2026, 10, 28), status=CoverageStatus.EMPTY, reason="No data"),
    )

    fake_slice = DataSlice(
        key=key,
        start=t0,
        end=t0 + timedelta(days=5),
        bars=(bar,),
        gaps=gaps,
    )
    monkeypatch.setattr(DataService, "get", lambda *args, **kwargs: fake_slice)

    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "data",
            "get",
            "RELIANCE",
            "1d",
            "2026-10-26",
            "2026-10-30",
        ],
    )
    assert res.exit_code == 0
    assert "Gap summary:" in res.output
    assert "2 gap session(s) detected (2 EMPTY)" in res.output


def test_reproduce_evidence_d_daily_candles_stamped_18_30_utc_boundary_included(
    tmp_path: Path,
) -> None:
    """Reproduce evidence (d): 2024-01-01 daily candle stamped 18:30Z the previous day."""
    data_dir = tmp_path / "data"
    manifest = DataManifest(data_dir / "manifest.db")
    store = BarStore(data_dir=data_dir, manifest=manifest)

    inst = InstrumentId("NSE:EQ:RELIANCE")
    key = SeriesKey(inst, "bars", "1d", "adjusted", "yfinance")

    # Stamped at 2023-12-31 18:30:00 UTC (i.e. 2024-01-01 00:00:00 IST)
    dt_bar = datetime(2023, 12, 31, 18, 30, 0, tzinfo=UTC)
    bar_20240101 = Bar(
        instrument=inst,
        timeframe="1d",
        start_ts=dt_bar,
        end_ts=dt_bar + timedelta(days=1),
        open=2580.0,
        high=2600.0,
        low=2570.0,
        close=2590.0,
        volume=500000,
        complete=True,
    )
    store.commit(key, [bar_20240101])

    # Requested with UTC start boundary on 2024-01-01 00:00:00Z
    req_start = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
    req_end = datetime(2024, 1, 5, 23, 59, 59, tzinfo=UTC)

    slice_out = store.read(key, req_start, req_end)
    assert len(slice_out.bars) == 1
    assert slice_out.bars[0].start_ts == dt_bar
    assert slice_out.bars[0].close == 2590.0


def test_reproduce_evidence_d_missing_weeks_reported_as_empty_gaps(tmp_path: Path) -> None:
    """Reproduce evidence (d): 2026-09-30 to 2026-10-21 with only Mondays returned."""
    data_dir = tmp_path / "data"
    manifest = DataManifest(data_dir / "manifest.db")
    store = BarStore(data_dir=data_dir, manifest=manifest)
    limiter = RateLimiter()

    # Source returns only Mondays: 2026-10-05, 2026-10-12, 2026-10-19
    mondays = {date(2026, 10, 5), date(2026, 10, 12), date(2026, 10, 19)}
    source = PartialDataSource(available_dates=mondays)

    fetcher = Fetcher(store=store, limiter=limiter, sources={"yfinance": source})
    key = SeriesKey(InstrumentId("NSE:EQ:RELIANCE"), "bars", "1d", "adjusted", "yfinance")

    t_start = datetime(2026, 9, 30, 0, 0, tzinfo=UTC)
    t_end = datetime(2026, 10, 21, 23, 59, tzinfo=UTC)

    plan = GapPlanner.plan(key, t_start, t_end, manifest=manifest)
    fetcher.fetch(plan)

    read_slice = store.read(key, t_start, t_end)
    # The returned bars should only be the 3 Mondays
    assert len(read_slice.bars) == 3
    # Gaps must be reported for the missing sessions!
    assert len(read_slice.gaps) > 0
    assert all(g.status == CoverageStatus.EMPTY for g in read_slice.gaps)
    # 2026-10-06 (Tuesday) must be reported as EMPTY gap
    gap_days = {g.day for g in read_slice.gaps}
    assert date(2026, 10, 6) in gap_days
    assert date(2026, 10, 7) in gap_days


def test_validator_drops_flat_zero_volume_bar_marks_empty_and_journals(tmp_path: Path) -> None:
    manifest = DataManifest(tmp_path / "manifest.db")
    journal = FakeJournal()
    validator = BarValidator(
        data_dir=tmp_path / "data",
        manifest=manifest,
        journal=journal,
    )

    inst = InstrumentId("NSE:EQ:RELIANCE")
    key = SeriesKey(inst, "bars", "1d", "raw", "upstox")

    dt_good = datetime(2026, 10, 26, 9, 15, tzinfo=_TZ).astimezone(UTC)
    dt_flat = datetime(2026, 10, 27, 9, 15, tzinfo=_TZ).astimezone(UTC)

    bar_good = Bar(
        instrument=inst,
        timeframe="1d",
        start_ts=dt_good,
        end_ts=dt_good + timedelta(days=1),
        open=100.0,
        high=105.0,
        low=98.0,
        close=102.0,
        volume=1000,
        complete=True,
    )
    # Phantom flat zero-volume bar
    bar_flat = Bar(
        instrument=inst,
        timeframe="1d",
        start_ts=dt_flat,
        end_ts=dt_flat + timedelta(days=1),
        open=100.0,
        high=100.0,
        low=100.0,
        close=100.0,
        volume=0,
        complete=True,
    )

    report = validator.validate_chunk([bar_good, bar_flat], key)
    assert report.is_valid is True
    assert report.dropped_flat_bars == 1
    assert len(report.clean_bars) == 1
    assert report.clean_bars[0].start_ts == dt_good

    # Check journaled event
    phantom_events = [e for e in journal.entries if e["kind"] == "data.phantom_bar_dropped"]
    assert len(phantom_events) == 1
    assert phantom_events[0]["payload"]["day"] == "2026-10-27"
    assert phantom_events[0]["payload"]["volume"] == 0

    # Check day marked EMPTY in manifest
    cov_recs = manifest.get_coverage_days(f"bars:upstox:1d:{inst}:raw", "2026-10-27", "2026-10-27")
    assert len(cov_recs) == 1
    assert cov_recs[0].status == CoverageStatus.EMPTY
    assert "Dropped flat" in (cov_recs[0].note or "")


def test_validator_drop_flat_disabled_by_param(tmp_path: Path) -> None:
    manifest = DataManifest(tmp_path / "manifest.db")
    params = MagicMock()
    params.get.return_value = False  # data.drop_flat_zero_volume_bars = false
    validator = BarValidator(
        data_dir=tmp_path / "data",
        manifest=manifest,
        params=params,
    )

    inst = InstrumentId("NSE:EQ:RELIANCE")
    key = SeriesKey(inst, "bars", "1d", "raw", "upstox")
    dt_flat = datetime(2026, 10, 27, 9, 15, tzinfo=_TZ).astimezone(UTC)

    bar_flat = Bar(
        instrument=inst,
        timeframe="1d",
        start_ts=dt_flat,
        end_ts=dt_flat + timedelta(days=1),
        open=100.0,
        high=100.0,
        low=100.0,
        close=100.0,
        volume=0,
        complete=True,
    )

    report = validator.validate_chunk([bar_flat], key)
    assert report.dropped_flat_bars == 0
    assert len(report.clean_bars) == 1


def test_validator_non_flat_zero_volume_or_non_zero_volume_kept(tmp_path: Path) -> None:
    manifest = DataManifest(tmp_path / "manifest.db")
    validator = BarValidator(
        data_dir=tmp_path / "data",
        manifest=manifest,
    )

    inst = InstrumentId("NSE:EQ:RELIANCE")
    key = SeriesKey(inst, "bars", "1d", "raw", "upstox")
    dt1 = datetime(2026, 10, 26, 9, 15, tzinfo=_TZ).astimezone(UTC)
    dt2 = datetime(2026, 10, 27, 9, 15, tzinfo=_TZ).astimezone(UTC)

    # Volume=0 but high > low (trading occurred or spread existed) -> kept
    b1 = Bar(
        instrument=inst,
        timeframe="1d",
        start_ts=dt1,
        end_ts=dt1 + timedelta(days=1),
        open=100.0,
        high=105.0,
        low=99.0,
        close=102.0,
        volume=0,
        complete=True,
    )
    # Flat prices but volume > 0 -> kept
    b2 = Bar(
        instrument=inst,
        timeframe="1d",
        start_ts=dt2,
        end_ts=dt2 + timedelta(days=1),
        open=100.0,
        high=100.0,
        low=100.0,
        close=100.0,
        volume=50,
        complete=True,
    )

    report = validator.validate_chunk([b1, b2], key)
    assert report.dropped_flat_bars == 0
    assert len(report.clean_bars) == 2


def test_yfinance_pandas4warning_scoped_filter() -> None:
    """Verify Pandas4Warning / FutureWarning is caught and scoped within yfinance call."""
    manifest = PluginManifest(
        name="yfinance",
        kind="broker",  # type: ignore[arg-type]
        version="0.1.0",
        api_version=1,
        entry="plugin:YFinanceBrokerPlugin",
        capabilities={"history": True},
    )

    class WarningEmittingTicker:
        def history(self, *args: Any, **kwargs: Any) -> Any:
            warnings.warn(
                "Pandas4Warning: DataFrame.apply is deprecated",
                UserWarning,
                stacklevel=2,
            )
            return None

    class WarningEmittingYF:
        def Ticker(self, sym: str) -> Any:
            return WarningEmittingTicker()

    plugin = YFinanceBrokerPlugin(manifest=manifest, _yf_module=WarningEmittingYF())

    # Outside the call, warnings should be captured if emitted
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        # Run history through plugin; warning should be caught by scoped filter
        key = SeriesKey(InstrumentId("NSE:EQ:RELIANCE"), "bars", "1d", "raw", "yfinance")
        now = datetime.now(UTC)
        res = list(plugin.history(key, now - timedelta(days=2), now))
        assert res == []

    # Filter was scoped inside _execute_with_retry, so no warning was leaked to outer context
    assert not any("Pandas4Warning" in str(w.message) for w in recorded)


def test_validator_multiple_consecutive_flat_zero_volume_bars_dropped(tmp_path: Path) -> None:
    manifest = DataManifest(tmp_path / "manifest.db")
    journal = MagicMock()
    validator = BarValidator(
        data_dir=tmp_path / "data",
        manifest=manifest,
        journal=journal,
    )
    inst = InstrumentId("NSE:EQ:RELIANCE")
    key = SeriesKey(inst, "bars", "1d", "raw", "upstox")
    dt1 = datetime(2026, 10, 26, 9, 15, tzinfo=_TZ).astimezone(UTC)
    dt2 = datetime(2026, 10, 27, 9, 15, tzinfo=_TZ).astimezone(UTC)

    b1 = Bar(inst, "1d", dt1, dt1 + timedelta(days=1), 100.0, 100.0, 100.0, 100.0, 0, True)
    b2 = Bar(inst, "1d", dt2, dt2 + timedelta(days=1), 100.0, 100.0, 100.0, 100.0, 0, True)

    rep = validator.validate_chunk([b1, b2], key)
    assert rep.dropped_flat_bars == 2
    assert len(rep.clean_bars) == 0
    assert journal.append.call_count == 2


def test_validator_flat_zero_vol_on_weekend_not_dropped_as_phantom(tmp_path: Path) -> None:
    manifest = DataManifest(tmp_path / "manifest.db")
    journal = MagicMock()
    validator = BarValidator(
        data_dir=tmp_path / "data",
        manifest=manifest,
        journal=journal,
    )
    inst = InstrumentId("NSE:EQ:RELIANCE")
    key = SeriesKey(inst, "bars", "1d", "raw", "upstox")
    dt_sun = datetime(2026, 10, 25, 9, 15, tzinfo=_TZ).astimezone(UTC)
    b_sun = Bar(inst, "1d", dt_sun, dt_sun + timedelta(days=1), 100.0, 100.0, 100.0, 100.0, 0, True)

    rep = validator.validate_chunk([b_sun], key)
    assert rep.dropped_flat_bars == 0
    assert len(rep.clean_bars) == 1


def test_data_gaps_cli_json_output(tmp_project: Path) -> None:
    manifest = DataManifest(tmp_project / "data" / "manifest.db")
    store = BarStore(data_dir=tmp_project / "data", manifest=manifest)
    inst = InstrumentId("NSE:EQ:RELIANCE")
    ds_id = store.dataset_id(SeriesKey(inst, "bars", "1d", "adjusted", "upstox"))
    from algodesk.data.manifest import CoverageDayRecord, DatasetRecord

    manifest.record_dataset(
        DatasetRecord(
            dataset_id=ds_id,
            instrument="NSE:EQ:RELIANCE",
            kind="bars",
            timeframe="1d",
            adjustment="adjusted",
            source="upstox",
            schema_version=1,
            created_at=datetime.now(UTC).isoformat(),
            actions_epoch=None,
            adjustment_basis="splits",
        )
    )
    manifest.record_coverage_days(
        [
            CoverageDayRecord(
                dataset_id=ds_id,
                day="2026-10-27",
                status=CoverageStatus.EMPTY,
                rows=0,
                expected_rows=0,
                part_path="",
                fetched_at=datetime.now(UTC).isoformat(),
                note="Empty session",
            )
        ]
    )
    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "data",
            "gaps",
            "RELIANCE",
            "--from",
            "2026-10-25",
            "--to",
            "2026-10-30",
            "--timeframe",
            "1d",
            "--source",
            "upstox",
            "--json",
        ],
    )
    assert res.exit_code == 0
    import json

    data = json.loads(res.output)
    assert len(data) >= 1
    assert any(g["day"] == "2026-10-27" for g in data)


def test_data_gaps_cli_no_gaps_message(tmp_project: Path) -> None:
    manifest = DataManifest(tmp_project / "data" / "manifest.db")
    store = BarStore(data_dir=tmp_project / "data", manifest=manifest)
    inst = InstrumentId("NSE:EQ:RELIANCE")
    ds_id = store.dataset_id(SeriesKey(inst, "bars", "1d", "adjusted", "upstox"))
    from algodesk.data.manifest import CoverageDayRecord, DatasetRecord

    manifest.record_dataset(
        DatasetRecord(
            dataset_id=ds_id,
            instrument="NSE:EQ:RELIANCE",
            kind="bars",
            timeframe="1d",
            adjustment="adjusted",
            source="upstox",
            schema_version=1,
            created_at=datetime.now(UTC).isoformat(),
            actions_epoch=None,
            adjustment_basis="splits",
        )
    )
    days = ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"]
    records = [
        CoverageDayRecord(
            dataset_id=ds_id,
            day=d,
            status=CoverageStatus.COMPLETE,
            rows=1,
            expected_rows=1,
            part_path="part.parquet",
            fetched_at=datetime.now(UTC).isoformat(),
        )
        for d in days
    ]
    manifest.record_coverage_days(records)

    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "data",
            "gaps",
            "RELIANCE",
            "--from",
            "2026-10-25",
            "--to",
            "2026-10-30",
            "--timeframe",
            "1d",
            "--source",
            "upstox",
        ],
    )
    assert res.exit_code == 0
    assert "No gaps found" in res.output
