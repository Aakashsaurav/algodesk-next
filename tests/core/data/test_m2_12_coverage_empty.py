"""Tests for M2-12: Coverage EMPTY evaluation, stale record reset, and accumulation."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from plugins.calendars.exchange.plugin import ExchangeCalendarPlugin

from algodesk.api.errors import DataError
from algodesk.api.market import Bar, CoverageStatus, InstrumentId, SeriesKey
from algodesk.data.accumulate import AccumulationPlanItem, execute_accumulation
from algodesk.data.closures import check_market_closure_disagreements
from algodesk.data.coverage import (
    CoverageIndex,
    evaluate_empty_record,
    is_empty_record_final,
    reset_stale_empty_coverage,
)
from algodesk.data.manifest import CoverageDayRecord, DataManifest
from algodesk.data.planner import GapPlanner

_IST = ZoneInfo("Asia/Kolkata")


def test_is_empty_record_final_before_session_close() -> None:
    """Empty record created before session end plus settle margin is non-final."""
    rec = CoverageDayRecord(
        dataset_id="bars:upstox:1m:NSE:EQ:TCS:raw",
        day="2026-11-09",
        status=CoverageStatus.EMPTY,
        rows=0,
        expected_rows=0,
        part_path="",
        fetched_at="2026-11-09T04:14:00Z",  # 09:44 IST (market opened at 09:15)
        note="Expected session returned no bars from source",
    )
    is_final, reason = evaluate_empty_record(rec=rec, settle_minutes=15)
    assert is_final is False
    assert "settle margin" in reason


def test_is_empty_record_final_pre_m2_11_yfinance() -> None:
    """yfinance intraday record created before M2-11 cutoff is non-final."""
    rec = CoverageDayRecord(
        dataset_id="bars:yfinance:1m:NSE:EQ:HDFCBANK:raw",
        day="2026-11-06",
        status=CoverageStatus.EMPTY,
        rows=0,
        expected_rows=0,
        part_path="",
        # 17:30 IST on 6 Nov (after close, before M2-11 cutoff)
        fetched_at="2026-11-06T12:00:00Z",
        note="Expected session returned no bars from source",
    )
    is_final, reason = evaluate_empty_record(rec=rec, settle_minutes=15)
    assert is_final is False
    assert "pre-M2-11" in reason


def test_is_empty_record_final_fetch_crash_in_note() -> None:
    """Empty record with crash/exception in note is non-final."""
    rec = CoverageDayRecord(
        dataset_id="bars:yfinance:1d:NSE:EQ:RELIANCE:raw",
        day="2024-01-20",
        status=CoverageStatus.EMPTY,
        rows=0,
        expected_rows=0,
        part_path="",
        fetched_at="2026-11-09T04:12:22Z",
        note="Fetch failed after 3 retries: 'CoverageIndex' object is not callable",
    )
    is_final, reason = evaluate_empty_record(rec=rec, settle_minutes=15)
    assert is_final is False
    assert "Fetch error" in reason


def test_is_empty_record_final_fetch_log_error(tmp_path: Path) -> None:
    """Manifest fetch_log error marks EMPTY record as non-final."""
    manifest = DataManifest(tmp_path / "manifest.db")
    fetched_iso = "2026-11-09T12:00:00Z"
    manifest.record_fetch_log(
        chunk="c1",
        source="upstox",
        started="2026-11-09T11:59:00Z",
        finished=fetched_iso,
        rows=0,
        outcome="ERROR",
        error="Gateway timeout 504",
    )
    rec = CoverageDayRecord(
        dataset_id="bars:upstox:1d:NSE:EQ:TCS:raw",
        day="2026-11-05",
        status=CoverageStatus.EMPTY,
        rows=0,
        expected_rows=0,
        part_path="",
        fetched_at=fetched_iso,
        note="Expected session returned no bars from source",
    )
    is_final, reason = evaluate_empty_record(rec=rec, manifest=manifest, settle_minutes=15)
    assert is_final is False
    assert "Fetch log error" in reason


def test_is_empty_record_final_missing_or_invalid_timestamp() -> None:
    """Missing or unparseable fetched_at returns False."""
    rec1 = CoverageDayRecord(
        dataset_id="bars:upstox:1d:NSE:EQ:TCS:raw",
        day="2026-11-05",
        status=CoverageStatus.EMPTY,
        rows=0,
        expected_rows=0,
        part_path="",
        fetched_at="",
    )
    assert is_empty_record_final(rec1) is False

    rec2 = CoverageDayRecord(
        dataset_id="bars:upstox:1d:NSE:EQ:TCS:raw",
        day="2026-11-05",
        status=CoverageStatus.EMPTY,
        rows=0,
        expected_rows=0,
        part_path="",
        fetched_at="invalid-date",
    )
    assert is_empty_record_final(rec2) is False


def test_is_empty_record_final_valid_final() -> None:
    """Valid empty chunk fetched after session close and settle margin is final."""
    rec = CoverageDayRecord(
        dataset_id="bars:upstox:1d:NSE:EQ:RELIANCE:raw",
        day="2026-11-05",
        status=CoverageStatus.EMPTY,
        rows=0,
        expected_rows=0,
        part_path="",
        fetched_at="2026-11-05T12:00:00Z",  # 17:30 IST, market closed at 15:30 IST
        note="Expected session returned no bars from source",
    )
    is_final, reason = evaluate_empty_record(rec=rec, settle_minutes=15)
    assert is_final is True
    assert "Final" in reason


def test_reset_stale_empty_coverage_dry_run(tmp_path: Path) -> None:
    """Dry run identifies stale EMPTY records without mutating them."""
    manifest = DataManifest(tmp_path / "manifest.db")
    ds_id = "bars:yfinance:1m:NSE:EQ:HDFCBANK:raw"
    manifest.record_coverage_day(
        CoverageDayRecord(
            dataset_id=ds_id,
            day="2026-11-06",
            status=CoverageStatus.EMPTY,
            rows=0,
            expected_rows=0,
            part_path="",
            fetched_at="2026-11-06T05:13:54Z",
        )
    )

    res = reset_stale_empty_coverage(manifest=manifest, dry_run=True)
    assert len(res) == 1
    assert res[0][0].day == "2026-11-06"

    # Verify record still exists in database as EMPTY
    recs = manifest.get_coverage_days(ds_id)
    assert len(recs) == 1
    assert recs[0].status == CoverageStatus.EMPTY


def test_reset_stale_empty_coverage_resets_and_journals(tmp_path: Path) -> None:
    """Reset updates stale EMPTY records to MISSING and appends journal entry."""
    manifest = DataManifest(tmp_path / "manifest.db")
    journal = MagicMock()
    ds_id = "bars:yfinance:1m:NSE:EQ:HDFCBANK:raw"
    manifest.record_coverage_day(
        CoverageDayRecord(
            dataset_id=ds_id,
            day="2026-11-06",
            status=CoverageStatus.EMPTY,
            rows=0,
            expected_rows=0,
            part_path="",
            fetched_at="2026-11-06T05:13:54Z",
        )
    )

    res = reset_stale_empty_coverage(manifest=manifest, dry_run=False, journal=journal)
    assert len(res) == 1
    assert res[0][0].day == "2026-11-06"

    # Verify record was reset to MISSING
    recs = manifest.get_coverage_days(ds_id)
    assert len(recs) == 1
    assert recs[0].status == CoverageStatus.MISSING
    assert journal.append.called is True


def test_reset_stale_empty_coverage_filters_source_and_timeframe(tmp_path: Path) -> None:
    """Filtering by source and timeframe only touches matching dataset_ids."""
    manifest = DataManifest(tmp_path / "manifest.db")
    ds_match = "bars:yfinance:1m:NSE:EQ:HDFCBANK:raw"
    ds_other_tf = "bars:yfinance:1d:NSE:EQ:HDFCBANK:raw"
    ds_other_src = "bars:upstox:1m:NSE:EQ:HDFCBANK:raw"

    for ds in (ds_match, ds_other_tf, ds_other_src):
        manifest.record_coverage_day(
            CoverageDayRecord(
                dataset_id=ds,
                day="2026-11-06",
                status=CoverageStatus.EMPTY,
                rows=0,
                expected_rows=0,
                part_path="",
                fetched_at="2026-11-06T05:13:54Z",
            )
        )

    res = reset_stale_empty_coverage(
        manifest=manifest, source="yfinance", timeframe="1m", dry_run=True
    )
    assert len(res) == 1
    assert res[0][0].dataset_id == ds_match


def test_coverage_index_is_empty_final_integration(tmp_path: Path) -> None:
    """CoverageIndex delegates is_empty_final with calendar and settle_minutes."""
    manifest = DataManifest(tmp_path / "manifest.db")
    cov = CoverageIndex(manifest=manifest, settle_minutes=15)
    rec_early = CoverageDayRecord(
        dataset_id="bars:upstox:1m:NSE:EQ:TCS:raw",
        day="2026-11-09",
        status=CoverageStatus.EMPTY,
        rows=0,
        expected_rows=0,
        part_path="",
        fetched_at="2026-11-09T04:14:00Z",
    )
    rec_late = CoverageDayRecord(
        dataset_id="bars:upstox:1d:NSE:EQ:TCS:raw",
        day="2026-11-05",
        status=CoverageStatus.EMPTY,
        rows=0,
        expected_rows=0,
        part_path="",
        fetched_at="2026-11-05T12:00:00Z",
    )
    assert cov.is_empty_final(rec_early) is False
    assert cov.is_empty_final(rec_late) is True


def test_coverage_index_detect_gaps_treats_non_final_empty_as_missing(tmp_path: Path) -> None:
    """CoverageIndex.detect_gaps classifies non-final EMPTY as MISSING."""
    manifest = DataManifest(tmp_path / "manifest.db")
    cov = CoverageIndex(manifest=manifest, settle_minutes=15)
    ds_id = "bars:upstox:1m:NSE:EQ:TCS:raw"
    manifest.record_coverage_day(
        CoverageDayRecord(
            dataset_id=ds_id,
            day="2026-11-09",
            status=CoverageStatus.EMPTY,
            rows=0,
            expected_rows=0,
            part_path="",
            fetched_at="2026-11-09T04:14:00Z",
        )
    )
    gaps = cov.detect_gaps(ds_id, date(2026, 11, 9), date(2026, 11, 9))
    assert len(gaps) == 1
    assert gaps[0].status == CoverageStatus.MISSING
    assert "settle margin" in gaps[0].reason


def test_gap_planner_schedules_non_final_empty_for_refetch(tmp_path: Path) -> None:
    """GapPlanner.plan schedules non-final EMPTY days as missing chunks."""
    manifest = DataManifest(tmp_path / "manifest.db")
    inst = InstrumentId("NSE:EQ:TCS")
    key = SeriesKey(
        instrument=inst,
        kind="bars",
        timeframe="1m",
        adjustment="raw",
        source="yfinance",
    )
    ds_id = f"bars:yfinance:1m:{inst}:raw"
    target_d = date(2026, 11, 6)
    manifest.record_coverage_day(
        CoverageDayRecord(
            dataset_id=ds_id,
            day=target_d.isoformat(),
            status=CoverageStatus.EMPTY,
            rows=0,
            expected_rows=0,
            part_path="",
            fetched_at="2026-11-06T05:13:54Z",  # Old date window
        )
    )

    st = datetime(2026, 11, 6, 9, 15, tzinfo=_IST).astimezone(UTC)
    en = datetime(2026, 11, 6, 15, 30, tzinfo=_IST).astimezone(UTC)

    plan = GapPlanner.plan(key, st, en, manifest=manifest, refresh=False)
    assert target_d in plan.missing_days
    assert target_d not in plan.empty_days
    assert plan.has_work is True


def test_gap_planner_skips_valid_final_empty(tmp_path: Path) -> None:
    """GapPlanner.plan preserves valid final EMPTY days without scheduling chunks."""
    manifest = DataManifest(tmp_path / "manifest.db")
    inst = InstrumentId("NSE:EQ:TCS")
    key = SeriesKey(
        instrument=inst,
        kind="bars",
        timeframe="1d",
        adjustment="raw",
        source="upstox",
    )
    ds_id = f"bars:upstox:1d:{inst}:raw"
    target_d = date(2026, 11, 5)
    manifest.record_coverage_day(
        CoverageDayRecord(
            dataset_id=ds_id,
            day=target_d.isoformat(),
            status=CoverageStatus.EMPTY,
            rows=0,
            expected_rows=0,
            part_path="",
            fetched_at="2026-11-05T12:00:00Z",
            note="Expected session returned no bars from source",
        )
    )

    st = datetime(2026, 11, 5, 9, 15, tzinfo=_IST).astimezone(UTC)
    en = datetime(2026, 11, 5, 15, 30, tzinfo=_IST).astimezone(UTC)

    plan = GapPlanner.plan(key, st, en, manifest=manifest, refresh=False)
    assert target_d in plan.empty_days
    assert target_d not in plan.missing_days
    assert plan.has_work is False


def test_execute_accumulation_loud_failure_when_zero_rows_added(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accumulation fails loudly when days are planned to fetch but 0 rows added."""
    mock_ds = MagicMock()
    mock_ds.get.return_value = MagicMock(bars=())  # Returns empty bars

    item = AccumulationPlanItem(
        instrument="NSE:EQ:RELIANCE",
        timeframe="1d",
        target_sessions=[date(2026, 11, 5)],
        stored_sessions=[],
        fetchable_sessions=[date(2026, 11, 5)],
        lost_sessions=[],
        rows_added=0,
        refetched_days=0,
    )
    monkeypatch.setattr(
        "algodesk.data.accumulate.plan_accumulation",
        lambda **kwargs: (["NSE:EQ:RELIANCE"], [item]),
    )

    with pytest.raises(DataError) as exc_info:
        execute_accumulation(
            group="demo5",
            source="yfinance",
            days=1,
            runtime=MagicMock(),
            data_service=mock_ds,
            dry_run=False,
        )
    assert "zero rows were added" in str(exc_info.value.what)


def test_execute_accumulation_counts_refetched_days_and_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accumulation tracks refetched days and per-instrument rows."""
    mock_ds = MagicMock()
    bar = Bar(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        timeframe="1d",
        start_ts=datetime(2026, 11, 5, 9, 15, tzinfo=UTC),
        end_ts=datetime(2026, 11, 5, 15, 30, tzinfo=UTC),
        open=100.0,
        high=105.0,
        low=99.0,
        close=104.0,
        volume=1000,
    )
    mock_ds.get.return_value = MagicMock(bars=(bar,))

    item = AccumulationPlanItem(
        instrument="NSE:EQ:RELIANCE",
        timeframe="1d",
        target_sessions=[date(2026, 11, 5)],
        stored_sessions=[],
        fetchable_sessions=[date(2026, 11, 5)],
        lost_sessions=[],
        rows_added=0,
        refetched_days=1,
    )
    monkeypatch.setattr(
        "algodesk.data.accumulate.plan_accumulation",
        lambda **kwargs: (["NSE:EQ:RELIANCE"], [item]),
    )

    report = execute_accumulation(
        group="demo5",
        source="yfinance",
        days=1,
        runtime=MagicMock(),
        data_service=mock_ds,
        dry_run=False,
    )
    assert report.rows_added == 1
    assert report.days_refetched == 1
    assert report.instrument_rows[("NSE:EQ:RELIANCE", "1d")] == 1


def test_calendar_recognizes_special_sessions_2024_01_20_and_2026_02_01() -> None:
    """ExchangeCalendar loads 2024-01-20 and 2026-02-01 as open special trading sessions."""
    cal = ExchangeCalendarPlugin(MagicMock(), {"exchange": "nse"}, None)

    # 2024-01-20 (Saturday DR switch)
    d1 = date(2024, 1, 20)
    assert cal.is_trading_day(d1) is True
    s1 = cal.sessions(d1)
    assert len(s1) == 2
    assert s1[0].name == "primary"
    assert s1[1].name == "dr"

    # 2026-02-01 (Sunday Union Budget)
    d2 = date(2026, 2, 1)
    assert cal.is_trading_day(d2) is True
    s2 = cal.sessions(d2)
    assert len(s2) == 1
    assert s2[0].name == "regular"


def test_closures_check_no_disagreements() -> None:
    """Manifest closures check against calendar produces zero disagreements."""
    manifest = MagicMock()
    manifest.list_market_closures.return_value = []
    manifest.list_datasets.return_value = []
    cal = ExchangeCalendarPlugin(MagicMock(), {"exchange": "nse"}, None)

    disagreements = check_market_closure_disagreements(manifest, cal, exchange="NSE")
    assert len(disagreements) == 0
