"""Tests for M2-3a: Market closure inference, multi-year trading calendar, and doctor checks."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from plugins.calendars.exchange.plugin import ExchangeCalendarPlugin
from typer.testing import CliRunner

from algodesk.api.errors import CalendarCoverageError
from algodesk.api.manifest import PluginManifest
from algodesk.api.market import Bar, CoverageStatus, DataSlice, SeriesKey
from algodesk.api.protocols import Calendar
from algodesk.cli.app import app
from algodesk.data.closures import (
    check_market_closure_disagreements,
    infer_market_closures,
)
from algodesk.data.coverage import CoverageIndex
from algodesk.data.manifest import DataManifest
from algodesk.data.planner import GapPlanner
from algodesk.ops.checks import (
    _check_calendar_coverage_horizon,
    _check_market_closure_disagreements,
)
from algodesk.ops.runtime import open_runtime


class DummyCalendar(Calendar):
    """Simple test calendar with configurable open/closed days."""

    def __init__(self, closed_days: set[date], special_sessions: set[date] | None = None) -> None:
        self.closed_days = closed_days
        self.special_sessions = special_sessions or set()

    def is_trading_day(self, d: date, segment: str = "") -> bool:
        if d in self.special_sessions:
            return True
        if d in self.closed_days:
            return False
        return d.weekday() < 5

    def sessions(self, d: date, segment: str = "") -> list[Any]:
        return []

    def next_session(self, ts: datetime, segment: str = "") -> Any:
        return None


def _make_dummy_bar(inst: str, dt: datetime) -> Bar:
    return Bar(
        instrument=inst,
        timeframe="1d",
        start_ts=dt,
        end_ts=dt + timedelta(hours=6, minutes=15),
        open=Decimal("100.00"),
        high=Decimal("105.00"),
        low=Decimal("99.00"),
        close=Decimal("102.00"),
        volume=1000,
        complete=True,
    )


# ---------------------------------------------------------------------------
# Manifest Market Closures CRUD
# ---------------------------------------------------------------------------


def test_manifest_market_closures_crud(tmp_path: Path) -> None:
    """DataManifest supports inserting, querying, listing, and deleting market closures."""
    m = DataManifest(tmp_path / "manifest.db")
    m.record_market_closure("NSE", "2024-05-20", "inferred", evidence="5/5 missing")
    m.record_market_closure("NSE", "2025-02-01", "special", evidence="budget session")

    rec = m.get_market_closure("NSE", "2024-05-20")
    assert rec is not None
    assert rec.exchange == "NSE"
    assert rec.day == "2024-05-20"
    assert rec.kind == "inferred"
    assert rec.evidence == "5/5 missing"

    all_nse = m.list_market_closures(exchange="NSE")
    assert len(all_nse) == 2

    inferred = m.list_market_closures(exchange="NSE", kind="inferred")
    assert len(inferred) == 1
    assert inferred[0].day == "2024-05-20"

    deleted = m.delete_market_closure("NSE", "2024-05-20")
    assert deleted is True
    assert m.get_market_closure("NSE", "2024-05-20") is None
    m.close()


# ---------------------------------------------------------------------------
# Closure Inference Engine
# ---------------------------------------------------------------------------


def test_infer_closures_all_five_missing_becomes_inferred_closure(tmp_path: Path) -> None:
    """When all sample symbols return 0 bars on a calendar-open day, day is inferred closed."""
    manifest = DataManifest(tmp_path / "manifest.db")
    sample_symbols = [f"NSE:EQ:SYM{i}" for i in range(1, 6)]
    d_open = date(2024, 5, 20)  # Monday, expected open

    mock_ds = MagicMock()
    mock_ds._exchange_tz = UTC
    # Returns empty slice for all symbols
    empty_slice = DataSlice(
        key=SeriesKey("NSE:EQ:SYM1", "bars", "1d", "raw", "upstox"),
        start=datetime(2024, 5, 20, tzinfo=UTC),
        end=datetime(2024, 5, 20, 23, 59, 59, tzinfo=UTC),
        bars=(),
        gaps=(),
        dataset_ids=("ds1",),
        checksums=(),
        source="upstox",
        adjustment="raw",
        fetched_at=datetime.now(UTC).isoformat(),
    )
    mock_ds.get.return_value = empty_slice

    cal = DummyCalendar(closed_days=set())
    recs = infer_market_closures(
        manifest=manifest,
        data_service=mock_ds,
        exchange="NSE",
        start_d=d_open,
        end_d=d_open,
        sample_symbols=sample_symbols,
        min_symbols=3,
        calendar=cal,
    )

    assert len(recs) == 1
    assert recs[0].kind == "inferred"
    assert recs[0].day == "2024-05-20"
    stored = manifest.get_market_closure("NSE", "2024-05-20")
    assert stored is not None
    assert stored.kind == "inferred"
    manifest.close()


def test_infer_closures_single_missing_symbol_stays_empty(tmp_path: Path) -> None:
    """A single symbol with no bar is never an inferred closure."""
    manifest = DataManifest(tmp_path / "manifest.db")
    sample_symbols = [f"NSE:EQ:SYM{i}" for i in range(1, 6)]
    d_open = date(2024, 5, 21)

    mock_ds = MagicMock()
    mock_ds._exchange_tz = UTC

    def _mock_get(series: Any, **kwargs: Any) -> DataSlice:
        sym = str(series.instrument if hasattr(series, "instrument") else series)
        # SYM1 has no bars, SYM2..SYM5 have bars
        bars = (
            ()
            if sym == "NSE:EQ:SYM1"
            else (_make_dummy_bar(sym, datetime(2024, 5, 21, 9, 15, tzinfo=UTC)),)
        )
        return DataSlice(
            key=SeriesKey(sym, "bars", "1d", "raw", "upstox"),
            start=datetime(2024, 5, 21, tzinfo=UTC),
            end=datetime(2024, 5, 21, 23, 59, 59, tzinfo=UTC),
            bars=bars,
            gaps=(),
            dataset_ids=("ds1",),
            checksums=(),
            source="upstox",
            adjustment="raw",
            fetched_at=datetime.now(UTC).isoformat(),
        )

    mock_ds.get.side_effect = _mock_get
    cal = DummyCalendar(closed_days=set())

    recs = infer_market_closures(
        manifest=manifest,
        data_service=mock_ds,
        exchange="NSE",
        start_d=d_open,
        end_d=d_open,
        sample_symbols=sample_symbols,
        min_symbols=3,
        calendar=cal,
    )

    assert len(recs) == 0
    assert manifest.get_market_closure("NSE", "2024-05-21") is None
    manifest.close()


def test_infer_closures_two_missing_symbols_stays_open(tmp_path: Path) -> None:
    """When only 2 symbols are missing, day is not an inferred closure."""
    manifest = DataManifest(tmp_path / "manifest.db")
    sample_symbols = [f"NSE:EQ:SYM{i}" for i in range(1, 6)]
    d_open = date(2024, 5, 22)

    mock_ds = MagicMock()
    mock_ds._exchange_tz = UTC

    def _mock_get(series: Any, **kwargs: Any) -> DataSlice:
        sym = str(series.instrument if hasattr(series, "instrument") else series)
        # SYM1 and SYM2 missing, others have bars
        bars = (
            ()
            if sym in ("NSE:EQ:SYM1", "NSE:EQ:SYM2")
            else (_make_dummy_bar(sym, datetime(2024, 5, 22, 9, 15, tzinfo=UTC)),)
        )
        return DataSlice(
            key=SeriesKey(sym, "bars", "1d", "raw", "upstox"),
            start=datetime(2024, 5, 22, tzinfo=UTC),
            end=datetime(2024, 5, 22, 23, 59, 59, tzinfo=UTC),
            bars=bars,
            gaps=(),
            dataset_ids=("ds1",),
            checksums=(),
            source="upstox",
            adjustment="raw",
            fetched_at=datetime.now(UTC).isoformat(),
        )

    mock_ds.get.side_effect = _mock_get
    cal = DummyCalendar(closed_days=set())

    recs = infer_market_closures(
        manifest=manifest,
        data_service=mock_ds,
        exchange="NSE",
        start_d=d_open,
        end_d=d_open,
        sample_symbols=sample_symbols,
        min_symbols=3,
        calendar=cal,
    )
    assert len(recs) == 0
    manifest.close()


def test_infer_closures_special_session_on_weekend(tmp_path: Path) -> None:
    """Weekend day with bars from at least min_symbols is recorded as special session."""
    manifest = DataManifest(tmp_path / "manifest.db")
    sample_symbols = [f"NSE:EQ:SYM{i}" for i in range(1, 6)]
    d_sat = date(2025, 2, 1)  # Saturday Budget Day

    mock_ds = MagicMock()
    mock_ds._exchange_tz = UTC

    def _mock_get(series: Any, **kwargs: Any) -> DataSlice:
        sym = str(series.instrument if hasattr(series, "instrument") else series)
        # All 5 symbols have bars on Saturday
        return DataSlice(
            key=SeriesKey(sym, "bars", "1d", "raw", "upstox"),
            start=datetime(2025, 2, 1, tzinfo=UTC),
            end=datetime(2025, 2, 1, 23, 59, 59, tzinfo=UTC),
            bars=(_make_dummy_bar(sym, datetime(2025, 2, 1, 9, 15, tzinfo=UTC)),),
            gaps=(),
            dataset_ids=("ds1",),
            checksums=(),
            source="upstox",
            adjustment="raw",
            fetched_at=datetime.now(UTC).isoformat(),
        )

    mock_ds.get.side_effect = _mock_get
    cal = DummyCalendar(closed_days=set())  # Saturday is closed by weekday rule

    recs = infer_market_closures(
        manifest=manifest,
        data_service=mock_ds,
        exchange="NSE",
        start_d=d_sat,
        end_d=d_sat,
        sample_symbols=sample_symbols,
        min_symbols=3,
        calendar=cal,
    )

    assert len(recs) == 1
    assert recs[0].kind == "special"
    assert recs[0].day == "2025-02-01"
    manifest.close()


def test_infer_closures_special_session_on_holiday(tmp_path: Path) -> None:
    """Declared holiday with bars from >= min_symbols is recorded as special session."""
    manifest = DataManifest(tmp_path / "manifest.db")
    sample_symbols = [f"NSE:EQ:SYM{i}" for i in range(1, 6)]
    d_muhurat = date(2024, 11, 1)  # Friday Diwali evening

    mock_ds = MagicMock()
    mock_ds._exchange_tz = UTC

    def _mock_get(series: Any, **kwargs: Any) -> DataSlice:
        sym = str(series.instrument if hasattr(series, "instrument") else series)
        # 4 symbols report bars
        bars = (
            ()
            if sym == "NSE:EQ:SYM5"
            else (_make_dummy_bar(sym, datetime(2024, 11, 1, 18, 0, tzinfo=UTC)),)
        )
        return DataSlice(
            key=SeriesKey(sym, "bars", "1d", "raw", "upstox"),
            start=datetime(2024, 11, 1, tzinfo=UTC),
            end=datetime(2024, 11, 1, 23, 59, 59, tzinfo=UTC),
            bars=bars,
            gaps=(),
            dataset_ids=("ds1",),
            checksums=(),
            source="upstox",
            adjustment="raw",
            fetched_at=datetime.now(UTC).isoformat(),
        )

    mock_ds.get.side_effect = _mock_get
    cal = DummyCalendar(closed_days={d_muhurat})  # declared holiday

    recs = infer_market_closures(
        manifest=manifest,
        data_service=mock_ds,
        exchange="NSE",
        start_d=d_muhurat,
        end_d=d_muhurat,
        sample_symbols=sample_symbols,
        min_symbols=3,
        calendar=cal,
    )

    assert len(recs) == 1
    assert recs[0].kind == "special"
    manifest.close()


def test_infer_closures_weekend_below_min_symbols_not_special(tmp_path: Path) -> None:
    """Weekend day with only 1 symbol having phantom bar is not marked special session."""
    manifest = DataManifest(tmp_path / "manifest.db")
    sample_symbols = [f"NSE:EQ:SYM{i}" for i in range(1, 6)]
    d_sun = date(2025, 2, 2)  # Sunday

    mock_ds = MagicMock()
    mock_ds._exchange_tz = UTC

    def _mock_get(series: Any, **kwargs: Any) -> DataSlice:
        sym = str(series.instrument if hasattr(series, "instrument") else series)
        bars = (
            (_make_dummy_bar(sym, datetime(2025, 2, 2, 9, 15, tzinfo=UTC)),)
            if sym == "NSE:EQ:SYM1"
            else ()
        )
        return DataSlice(
            key=SeriesKey(sym, "bars", "1d", "raw", "upstox"),
            start=datetime(2025, 2, 2, tzinfo=UTC),
            end=datetime(2025, 2, 2, 23, 59, 59, tzinfo=UTC),
            bars=bars,
            gaps=(),
            dataset_ids=("ds1",),
            checksums=(),
            source="upstox",
            adjustment="raw",
            fetched_at=datetime.now(UTC).isoformat(),
        )

    mock_ds.get.side_effect = _mock_get
    cal = DummyCalendar(closed_days=set())

    recs = infer_market_closures(
        manifest=manifest,
        data_service=mock_ds,
        exchange="NSE",
        start_d=d_sun,
        end_d=d_sun,
        sample_symbols=sample_symbols,
        min_symbols=3,
        calendar=cal,
    )

    assert len(recs) == 0
    assert manifest.get_market_closure("NSE", "2025-02-02") is None
    manifest.close()


# ---------------------------------------------------------------------------
# GapPlanner and CoverageIndex Integration
# ---------------------------------------------------------------------------


def test_gap_planner_excludes_inferred_closure(tmp_path: Path) -> None:
    """GapPlanner excludes inferred market closures from fetch work and tags as closed_inferred."""
    manifest = DataManifest(tmp_path / "manifest.db")
    manifest.record_market_closure("NSE", "2024-05-20", "inferred", evidence="5/5 missing")

    key = SeriesKey("NSE:EQ:RELIANCE", "bars", "1d", "raw", "upstox")
    plan = GapPlanner.plan(
        key=key,
        start=datetime(2024, 5, 20, 0, 0, tzinfo=UTC),
        end=datetime(2024, 5, 20, 23, 59, tzinfo=UTC),
        manifest=manifest,
        chunk_days=30,
        exchange_tz=UTC,
    )

    assert not plan.has_work
    assert date(2024, 5, 20) in plan.closed_days
    assert date(2024, 5, 20) in plan.closed_inferred_days
    assert date(2024, 5, 20) not in plan.closed_calendar_days
    manifest.close()


def test_gap_planner_includes_special_session_as_open(tmp_path: Path) -> None:
    """GapPlanner treats weekend recorded as special session as an open trading session."""
    manifest = DataManifest(tmp_path / "manifest.db")
    manifest.record_market_closure("NSE", "2025-02-01", "special", evidence="budget session")

    key = SeriesKey("NSE:EQ:RELIANCE", "bars", "1d", "raw", "upstox")
    plan = GapPlanner.plan(
        key=key,
        start=datetime(2025, 2, 1, 0, 0, tzinfo=UTC),
        end=datetime(2025, 2, 1, 23, 59, tzinfo=UTC),
        manifest=manifest,
        chunk_days=30,
        exchange_tz=UTC,
    )

    assert plan.has_work
    assert date(2025, 2, 1) in plan.missing_days
    assert date(2025, 2, 1) not in plan.closed_days
    assert date(2025, 2, 1) in plan.special_session_days
    manifest.close()


def test_gap_planner_counts_calendar_and_inferred_closed(tmp_path: Path) -> None:
    """GapPlanner records separate counts for closed_calendar and closed_inferred days."""
    manifest = DataManifest(tmp_path / "manifest.db")
    manifest.record_market_closure("NSE", "2024-05-20", "inferred", evidence="missing")

    key = SeriesKey("NSE:EQ:RELIANCE", "bars", "1d", "raw", "upstox")
    # 2024-05-18 (Sat), 2024-05-19 (Sun), 2024-05-20 (Mon inferred closed)
    plan = GapPlanner.plan(
        key=key,
        start=datetime(2024, 5, 18, 0, 0, tzinfo=UTC),
        end=datetime(2024, 5, 20, 23, 59, tzinfo=UTC),
        manifest=manifest,
        chunk_days=30,
        exchange_tz=UTC,
    )

    assert len(plan.closed_calendar_days) == 2  # Sat, Sun
    assert len(plan.closed_inferred_days) == 1  # Mon
    assert len(plan.closed_days) == 3
    manifest.close()


def test_coverage_index_treats_inferred_closure_as_closed(tmp_path: Path) -> None:
    """CoverageIndex.evaluate_day_status returns CLOSED for an inferred closure."""
    manifest = DataManifest(tmp_path / "manifest.db")
    manifest.record_market_closure("NSE", "2024-05-20", "inferred", evidence="missing")

    idx = CoverageIndex(manifest=manifest)
    status = idx.evaluate_day_status(
        "NSE:EQ:RELIANCE:bars:1d:raw:upstox", date(2024, 5, 20), has_bars=False
    )
    assert status == CoverageStatus.CLOSED
    manifest.close()


def test_coverage_index_detect_gaps_treats_inferred_closure_as_closed(tmp_path: Path) -> None:
    """CoverageIndex.detect_gaps does not report gaps for inferred closure and tracks counts."""
    manifest = DataManifest(tmp_path / "manifest.db")
    manifest.record_market_closure("NSE", "2024-05-20", "inferred", evidence="missing")
    manifest.record_market_closure("NSE", "2025-02-01", "special", evidence="budget")

    idx = CoverageIndex(manifest=manifest)
    gaps = idx.detect_gaps(
        "NSE:EQ:RELIANCE:bars:1d:raw:upstox", date(2024, 5, 20), date(2024, 5, 20)
    )
    assert len(gaps) == 0
    counts = idx.last_closure_counts
    assert counts["closed_inferred"] == 1
    assert counts["closed_calendar"] == 0
    manifest.close()


# ---------------------------------------------------------------------------
# Disagreement Reporting
# ---------------------------------------------------------------------------


def test_check_disagreements_missing_holiday(tmp_path: Path) -> None:
    """Detects when calendar says open but market closure is inferred closed (missing holiday)."""
    manifest = DataManifest(tmp_path / "manifest.db")
    manifest.record_market_closure("NSE", "2024-05-20", "inferred", evidence="5/5 symbols missing")

    cal = DummyCalendar(closed_days=set())  # calendar says Monday is open
    disagreements = check_market_closure_disagreements(
        manifest, cal, "NSE", date(2024, 5, 20), date(2024, 5, 20)
    )

    assert len(disagreements) == 1
    assert "missing holiday" in disagreements[0]["disagreement_type"]
    assert disagreements[0]["day"] == "2024-05-20"
    manifest.close()


def test_check_disagreements_special_session(tmp_path: Path) -> None:
    """Detects when calendar says closed but data has a special session."""
    manifest = DataManifest(tmp_path / "manifest.db")
    manifest.record_market_closure("NSE", "2025-02-01", "special", evidence="budget session")

    cal = DummyCalendar(closed_days={date(2025, 2, 1)})
    disagreements = check_market_closure_disagreements(
        manifest, cal, "NSE", date(2025, 2, 1), date(2025, 2, 1)
    )

    assert len(disagreements) == 1
    assert "special session" in disagreements[0]["disagreement_type"]
    manifest.close()


def test_check_disagreements_calendar_closed_bars_exist(tmp_path: Path) -> None:
    """Detects when calendar declares a holiday but coverage_days records bars on disk."""
    manifest = DataManifest(tmp_path / "manifest.db")
    d_holiday = "2024-08-15"
    ds_id = "bars:upstox:1d:NSE:EQ:RELIANCE:raw"
    from algodesk.data.manifest import CoverageDayRecord, DatasetRecord

    manifest.record_dataset(
        DatasetRecord(
            dataset_id=ds_id,
            instrument="NSE:EQ:RELIANCE",
            kind="bars",
            timeframe="1d",
            adjustment="raw",
            source="upstox",
            schema_version="v1",
            created_at=datetime.now(UTC).isoformat(),
            actions_epoch="epoch0",
            adjustment_basis="raw",
        )
    )
    manifest.record_coverage_day(
        CoverageDayRecord(
            dataset_id=ds_id,
            day=d_holiday,
            status="COMPLETE",
            rows=1,
            expected_rows=1,
            part_path="part.parquet",
            fetched_at=datetime.now(UTC).isoformat(),
        )
    )

    cal = DummyCalendar(closed_days={date(2024, 8, 15)})
    disagreements = check_market_closure_disagreements(
        manifest, cal, "NSE", date(2024, 8, 15), date(2024, 8, 15)
    )

    assert len(disagreements) == 1
    assert "calendar closed but bars exist" in disagreements[0]["disagreement_type"]
    manifest.close()


def test_check_disagreements_empty_when_no_conflicts(tmp_path: Path) -> None:
    """When calendar and recorded closures agree, zero disagreements are returned."""
    manifest = DataManifest(tmp_path / "manifest.db")
    cal = DummyCalendar(closed_days=set())
    disagreements = check_market_closure_disagreements(
        manifest, cal, "NSE", date(2024, 5, 20), date(2024, 5, 20)
    )
    assert len(disagreements) == 0
    manifest.close()


# ---------------------------------------------------------------------------
# CLI Commands
# ---------------------------------------------------------------------------


def test_cli_closures_infer_command(tmp_project: Path) -> None:
    """data closures infer CLI command runs and reports inferred closures."""
    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "data",
            "closures",
            "infer",
            "--exchange",
            "NSE",
            "--from",
            "2026-10-26",
            "--to",
            "2026-10-26",
        ],
    )
    assert res.exit_code == 0
    assert "Inferred:" in res.output or "No market closures" in res.output


def test_cli_closures_infer_json(tmp_project: Path) -> None:
    """data closures infer --json outputs valid JSON array."""
    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "data",
            "closures",
            "infer",
            "--exchange",
            "NSE",
            "--from",
            "2026-10-26",
            "--to",
            "2026-10-26",
            "--json",
        ],
    )
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert isinstance(data, list)


def test_cli_closures_check_with_disagreements(tmp_project: Path) -> None:
    """data closures check CLI command lists disagreements in table format."""
    manifest = DataManifest(tmp_project / "data" / "state" / "data_manifest.db")
    manifest.record_market_closure("NSE", "2026-07-15", "inferred", evidence="test")
    manifest.close()

    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "data",
            "closures",
            "check",
            "--exchange",
            "NSE",
        ],
    )
    assert res.exit_code == 0
    assert "2026-07-15" in res.output
    assert "missing holiday" in res.output


def test_cli_closures_check_no_disagreements(tmp_project: Path) -> None:
    """data closures check CLI command reports clean status when no disagreements exist."""
    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "data",
            "closures",
            "check",
            "--exchange",
            "NSE",
            "--from",
            "2026-10-23",
            "--to",
            "2026-10-23",
        ],
    )
    assert res.exit_code == 0
    assert "No market closure disagreements found" in res.output


def test_cli_closures_check_json(tmp_project: Path) -> None:
    """data closures check --json outputs JSON list of disagreements."""
    manifest = DataManifest(tmp_project / "data" / "state" / "data_manifest.db")
    manifest.record_market_closure("NSE", "2026-07-15", "inferred", evidence="test")
    manifest.close()

    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "data",
            "closures",
            "check",
            "--exchange",
            "NSE",
            "--json",
        ],
    )
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert isinstance(data, list)
    assert any(d["day"] == "2026-07-15" for d in data)


def test_cli_closures_list_command(tmp_project: Path) -> None:
    """data closures list CLI command lists closures from manifest."""
    manifest = DataManifest(tmp_project / "data" / "state" / "data_manifest.db")
    manifest.record_market_closure("NSE", "2024-05-20", "inferred", evidence="test")
    manifest.close()

    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "data",
            "closures",
            "list",
            "--exchange",
            "NSE",
        ],
    )
    assert res.exit_code == 0
    assert "2024-05-20" in res.output
    assert "inferred" in res.output


def test_cli_data_gaps_prints_closure_counts(tmp_project: Path) -> None:
    """data gaps command outputs closure summary counts."""
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
            "2026-10-24",
            "--to",
            "2026-10-27",
        ],
    )
    assert res.exit_code == 0
    assert "Closures:" in res.output
    assert "closed (calendar)" in res.output
    assert "closed (inferred)" in res.output
    assert "special session(s)" in res.output


# ---------------------------------------------------------------------------
# Doctor Diagnostic Checks
# ---------------------------------------------------------------------------


def test_doctor_calendar_coverage_horizon_warning(tmp_project: Path) -> None:
    """Doctor warns when calendar coverage expiration falls within calendar.warn_days horizon."""
    import dataclasses
    from unittest.mock import MagicMock

    with open_runtime(tmp_project, ensure=False) as rt:
        # Default 30 days: 58 days remaining until 2026-12-31 is OK
        res_ok = _check_calendar_coverage_horizon(rt)
        assert res_ok.ok is True

        # When warn_days is 90: 58 days remaining triggers warning
        mock_params = MagicMock()
        mock_params.get.side_effect = lambda k, *args: 90 if k == "calendar.warn_days" else None
        rt_warn = dataclasses.replace(rt, params=mock_params)
        res_warn = _check_calendar_coverage_horizon(rt_warn)
        assert res_warn.ok is False
        assert "horizon warning" in res_warn.message


def test_doctor_closure_disagreements_warning(tmp_project: Path) -> None:
    """Doctor warns with disagreement count when unresolved closure conflicts exist."""
    manifest = DataManifest(tmp_project / "data" / "state" / "data_manifest.db")
    manifest.record_market_closure("NSE", "2026-07-15", "inferred", evidence="test")
    manifest.close()

    with open_runtime(tmp_project, ensure=False) as rt:
        res = _check_market_closure_disagreements(rt)
        assert res.ok is False
        assert "unresolved market closure disagreement(s) found" in res.message


# ---------------------------------------------------------------------------
# Multi-Year Trading Calendar Verification
# ---------------------------------------------------------------------------


def test_multi_year_nse_calendar_coverage_2022_to_2026() -> None:
    """NSE calendar covers years 2022-2026, including election holidays and Budget Saturday."""
    manifest = PluginManifest(
        name="exchange",
        kind="calendar",  # type: ignore[arg-type]
        version="0.2.0",
        api_version=1,
        entry="plugin:ExchangeCalendarPlugin",
        capabilities={"holidays": True, "sessions": True, "special_sessions": True},
    )
    cal = ExchangeCalendarPlugin(manifest, {"exchange": "nse"}, None)
    c_start, c_end = cal.coverage()
    assert c_start == date(2022, 1, 1)
    assert c_end == date(2026, 12, 31)

    # 2022 holiday: Republic Day 2022-01-26
    assert cal.is_trading_day(date(2022, 1, 26)) is False

    # 2023 holiday: Holi 2023-03-07
    assert cal.is_trading_day(date(2023, 3, 7)) is False

    # 2024 holidays: Ayodhya 2024-01-22 and Parliamentary elections 2024-05-20
    assert cal.is_trading_day(date(2024, 1, 22)) is False
    assert cal.is_trading_day(date(2024, 5, 20)) is False

    # 2025 holiday: Mahashivratri 2025-02-26
    assert cal.is_trading_day(date(2025, 2, 26)) is False

    # 2025 Budget Saturday special session: 2025-02-01
    assert cal.is_trading_day(date(2025, 2, 1)) is True
    sessions = cal.sessions(date(2025, 2, 1), segment="eq")
    assert len(sessions) == 1
    assert sessions[0].name == "regular"


def test_multi_year_bse_calendar_coverage_2022_to_2026() -> None:
    """BSE calendar covers years 2022 through 2026."""
    manifest = PluginManifest(
        name="exchange",
        kind="calendar",  # type: ignore[arg-type]
        version="0.2.0",
        api_version=1,
        entry="plugin:ExchangeCalendarPlugin",
        capabilities={"holidays": True, "sessions": True, "special_sessions": True},
    )
    cal = ExchangeCalendarPlugin(manifest, {"exchange": "bse"}, None)
    c_start, c_end = cal.coverage()
    assert c_start == date(2022, 1, 1)
    assert c_end == date(2026, 12, 31)

    # 2024 election holiday
    assert cal.is_trading_day(date(2024, 5, 20)) is False


def test_mcx_calendar_coverage_and_uncovered_past_years() -> None:
    """MCX calendar covers 2026; unverified past years raise CalendarCoverageError."""
    manifest = PluginManifest(
        name="exchange",
        kind="calendar",  # type: ignore[arg-type]
        version="0.2.0",
        api_version=1,
        entry="plugin:ExchangeCalendarPlugin",
        capabilities={"holidays": True, "sessions": True, "special_sessions": True},
    )
    cal = ExchangeCalendarPlugin(manifest, {"exchange": "mcx"}, None)
    c_start, c_end = cal.coverage()
    assert c_start == date(2026, 1, 1)

    # Past years (e.g. 2025) are unverified from official publications
    # and raise CalendarCoverageError
    with pytest.raises(CalendarCoverageError):
        cal.is_trading_day(date(2025, 12, 31))
