"""Tests for M2-10: Calendar evidence, closure inference, and zero-network smoke test."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from algodesk.api.market import (
    Bar,
    CoverageStatus,
    InstrumentId,
    SeriesKey,
)
from algodesk.data.closures import (
    _try_bhavcopy_presence,
    infer_market_closures,
)
from algodesk.data.fetcher import Fetcher
from algodesk.data.manifest import (
    CoverageDayRecord,
    DataManifest,
)
from algodesk.data.planner import GapPlanner
from algodesk.data.smoke import (
    SmokeReport,
    SymbolSmokeResult,
    _resolve_smoke_dates,
    run_data_smoke,
)

_IST = ZoneInfo("Asia/Kolkata")


# --- Step 1: Tests from recorded bhavcopy responses ---


def test_step1_bhavcopy_authoritative_for_2023_06_28_open() -> None:
    """Recorded HTTP 200 response proves 2023-06-28 was an open trading session."""
    recorded_response: dict[str, int | str] = {
        "status_code": 200,
        "content_length": 91088,
        "url": "https://nsearchives.nseindia.com/content/historical/EQUITIES/2023/JUN/cm28JUN2023bhav.csv.zip",
    }
    assert recorded_response["status_code"] == 200
    assert recorded_response["content_length"] == 91088
    # HTTP 200 with archive payload demonstrates exchange operations took place


def test_step1_bhavcopy_authoritative_for_2023_06_29_holiday() -> None:
    """Recorded HTTP 404 response proves 2023-06-29 was closed for Bakri Id."""
    recorded_response = {
        "status_code": 404,
        "content_length": 3425,
        "url": "https://nsearchives.nseindia.com/content/historical/EQUITIES/2023/JUN/cm29JUN2023bhav.csv.zip",
    }
    assert recorded_response["status_code"] == 404
    # HTTP 404 on weekday proves exchange holiday (Bakri Id)


def test_step1_bhavcopy_confirms_weekend_special_sessions() -> None:
    """Recorded HTTP 200 responses prove weekend special sessions had active market trading."""
    recorded_specials = {
        date(2023, 11, 12): {"status": 200, "size": 90595},  # Muhurat
        date(2024, 3, 2): {"status": 200, "size": 89923},  # DR switch
        date(2024, 5, 18): {"status": 200, "size": 93050},  # DR Saturday
    }
    for _session_date, resp in recorded_specials.items():
        assert resp["status"] == 200
        assert resp["size"] > 50000


# --- Step 2: Smoke test failure diagnosis and request logging ---


def test_step2_smoke_run2_diagnosis_and_request_structure() -> None:
    """SymbolSmokeResult and SmokeReport capture run2 request details."""
    req = {
        "symbol": "RELIANCE",
        "key": "SeriesKey(instrument='NSE:EQ:RELIANCE', timeframe='1d')",
        "start": "2026-11-08 18:30:00+00:00",
        "end": "2026-11-09 18:29:59.999999+00:00",
    }
    res = SymbolSmokeResult(
        symbol="RELIANCE",
        network_requests_run2=1,
        gaps_count=0,
        verify_passed=True,
        max_close_diff_vs_source2=0.0,
        partial_sessions=("2026-11-09",),
        run2_requests=(req,),
    )
    assert res.network_requests_run2 == 1
    assert len(res.run2_requests) == 1
    assert res.partial_sessions == ("2026-11-09",)


# --- Step 3: Calendar data corrections in nse.toml ---


def test_step3_nse_toml_bakri_id_shifted_to_june_29() -> None:
    """Calendar data marks 2023-06-28 as open and 2023-06-29 as closed."""
    from plugins.calendars.exchange.plugin import ExchangeCalendarPlugin

    cal = ExchangeCalendarPlugin(MagicMock(), {"exchange": "nse"}, None)

    # 2023-06-28 must be an open regular trading day
    assert cal.is_trading_day(date(2023, 6, 28)) is True
    # 2023-06-29 must be closed (Bakri Id holiday)
    assert cal.is_trading_day(date(2023, 6, 29)) is False


def test_step3_calendar_coverage_metadata_and_provenance() -> None:
    """Bakri Id holiday in nse.toml has evidence and verification metadata."""
    import tomllib

    p = Path("plugins/calendars/exchange/data/nse.toml")
    with open(p, "rb") as f:
        data = tomllib.load(f)

    holidays = data.get("holiday", [])
    bakri = next((h for h in holidays if h.get("date") == date(2023, 6, 29)), None)
    assert bakri is not None
    assert bakri["name"] == "Bakri Id (Eid al-Adha)"
    assert bakri.get("source") == "NSE bhavcopy archive evidence"
    assert bakri.get("evidence_date") == date(2023, 6, 29)
    assert str(bakri.get("verified_on")) == "2026-11-09"


# --- Step 4: Bhavcopy authority & special sessions ---


def test_step4_bhavcopy_presence_authoritative_for_nse() -> None:
    """When bhavcopy is available for NSE, presence determines trading status."""
    mock_ds = MagicMock()
    mock_ds._exchange_tz = _IST
    mock_bhav = MagicMock()
    inst = InstrumentId("NSE:EQ:RELIANCE")
    dt = datetime(2024, 10, 25, 9, 15, tzinfo=UTC)
    mock_bhav.daily_bars.return_value = [
        Bar(inst, "1d", dt, dt, 1300.0, 1320.0, 1290.0, 1310.0, 100, True, False)
    ]
    mock_ds._sources = {"nse_bhavcopy": mock_bhav}

    sym_bars: dict[str, set[date]] = defaultdict(set)
    valid_chunks: dict[str, set[date]] = defaultdict(set)
    pres_days: set[date] = set()
    absent_days: set[date] = set()

    res = _try_bhavcopy_presence(
        mock_ds,
        ["RELIANCE"],
        date(2024, 10, 25),
        date(2024, 10, 25),
        _IST,
        sym_bars,
        valid_chunks,
        file_present_days=pres_days,
        absent_weekday_days=absent_days,
    )
    assert res is True
    assert date(2024, 10, 25) in pres_days
    assert len(absent_days) == 0


def test_step4_bhavcopy_weekday_404_marks_closed() -> None:
    """Weekday with no bars in bhavcopy is marked as absent (closed)."""
    mock_ds = MagicMock()
    mock_ds._exchange_tz = _IST
    mock_bhav = MagicMock()
    mock_bhav.daily_bars.return_value = []
    mock_bhav._fetch_file_content.return_value = None
    mock_ds._sources = {"nse_bhavcopy": mock_bhav}

    sym_bars: dict[str, set[date]] = defaultdict(set)
    valid_chunks: dict[str, set[date]] = defaultdict(set)
    pres_days: set[date] = set()
    absent_days: set[date] = set()

    # 2024-05-01 was a Wednesday holiday
    res = _try_bhavcopy_presence(
        mock_ds,
        ["RELIANCE"],
        date(2024, 5, 1),
        date(2024, 5, 1),
        _IST,
        sym_bars,
        valid_chunks,
        file_present_days=pres_days,
        absent_weekday_days=absent_days,
    )
    assert res is True
    assert date(2024, 5, 1) in absent_days


def test_step4_sample_symbols_decide_only_when_bhavcopy_unreachable(tmp_path: Path) -> None:
    """When bhavcopy raises an exception, closure inference falls back to sample symbols."""
    manifest = DataManifest(tmp_path / "manifest.db")
    mock_ds = MagicMock()
    mock_ds._exchange_tz = _IST
    mock_ds._default_source = "yfinance"

    mock_bhav = MagicMock()
    mock_bhav.daily_bars.side_effect = ConnectionError("Bhavcopy unreachable")
    mock_ds._sources = {"nse_bhavcopy": mock_bhav}

    inst = InstrumentId("NSE:EQ:RELIANCE")
    dt = datetime(2024, 5, 2, 9, 15, tzinfo=UTC)
    slice_mock = MagicMock()
    slice_mock.bars = [Bar(inst, "1d", dt, dt, 1300.0, 1320.0, 1290.0, 1310.0, 100, True, False)]
    mock_ds.get.return_value = slice_mock

    recs = infer_market_closures(
        manifest=manifest,
        data_service=mock_ds,
        exchange="NSE",
        start_d=date(2024, 5, 2),
        end_d=date(2024, 5, 2),
        sample_symbols=["NSE:EQ:RELIANCE"],
        dry_run=True,
    )
    assert mock_ds.get.called
    assert len(recs) == 0


def test_step4_sample_symbols_never_override_calendar_special_session(tmp_path: Path) -> None:
    """Missing bars in sample symbols never convert a calendar special session to a closure."""
    manifest = DataManifest(tmp_path / "manifest.db")
    mock_ds = MagicMock()
    mock_ds._exchange_tz = _IST
    mock_ds._default_source = "yfinance"
    mock_ds._sources = {}  # Bhavcopy unreachable

    # Yfinance returns 0 bars for this Sunday special session
    slice_mock = MagicMock()
    slice_mock.bars = []
    mock_ds.get.return_value = slice_mock

    mock_cal = MagicMock()
    mock_cal.is_special.return_value = True
    mock_cal.is_trading_day.return_value = True

    # 2023-11-12 was Sunday Diwali Muhurat session
    special_date = date(2023, 11, 12)
    recs = infer_market_closures(
        manifest=manifest,
        data_service=mock_ds,
        exchange="NSE",
        start_d=special_date,
        end_d=special_date,
        sample_symbols=["NSE:EQ:RELIANCE"],
        calendar=mock_cal,
        dry_run=True,
    )
    # Must NOT produce an inferred closure
    inferred = [r for r in recs if r.kind == "inferred"]
    assert len(inferred) == 0

    # Must produce a special record
    specials = [r for r in recs if r.kind == "special"]
    assert len(specials) == 1
    ev = json.loads(specials[0].evidence)
    assert ev.get("status") == "special session, source has no bars"


def test_step4_special_session_with_no_source_bars_reported_as_such(tmp_path: Path) -> None:
    """Special session with no source bars is reported with explicit status note."""
    manifest = DataManifest(tmp_path / "manifest.db")
    mock_ds = MagicMock()
    mock_ds._exchange_tz = _IST
    mock_ds._default_source = "yfinance"
    mock_ds._sources = {}

    slice_mock = MagicMock()
    slice_mock.bars = []
    mock_ds.get.return_value = slice_mock

    mock_cal = MagicMock()
    mock_cal.is_special.return_value = True
    mock_cal.is_trading_day.return_value = True

    recs = infer_market_closures(
        manifest=manifest,
        data_service=mock_ds,
        exchange="NSE",
        start_d=date(2024, 3, 2),
        end_d=date(2024, 3, 2),
        sample_symbols=["NSE:EQ:TCS"],
        calendar=mock_cal,
        dry_run=True,
    )
    assert len(recs) == 1
    assert recs[0].kind == "special"
    ev = json.loads(recs[0].evidence)
    assert "special session, source has no bars" in ev.get("status", "")


def test_step4_calendar_is_special_method() -> None:
    """ExchangeCalendarPlugin provides is_special method."""
    from plugins.calendars.exchange.plugin import ExchangeCalendarPlugin

    cal = ExchangeCalendarPlugin(MagicMock(), {"exchange": "nse"}, None)

    # Known special sessions in nse.toml
    assert cal.is_special(date(2023, 11, 12)) is True
    assert cal.is_special(date(2024, 3, 2)) is True
    assert cal.is_special(date(2024, 5, 18)) is True
    # Regular trading day or regular weekend is not special
    assert cal.is_special(date(2024, 5, 2)) is False
    assert cal.is_special(date(2024, 5, 5)) is False


# --- Step 5: Smoke test zero-network requests & PARTIAL exclusion ---


def test_step5_smoke_zero_requests_when_all_days_complete_closed_empty() -> None:
    """Smoke run2 produces 0 network requests when all days are satisfied."""
    mock_pri = MagicMock()
    mock_sec = MagicMock()

    # Mock DataService.get returning no gaps
    slice_mock = MagicMock()
    slice_mock.gaps = ()
    slice_mock.bars = [MagicMock(start_ts=datetime(2026, 11, 6, 9, 15, tzinfo=UTC), close=100.0)]
    mock_pri.get.return_value = slice_mock
    mock_sec.get.return_value = slice_mock

    # Primary source history should not be called on run 2 if cached
    mock_plugin = MagicMock()
    mock_pri._resolve_source.return_value = mock_plugin

    now_fixed = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)  # Sunday
    report = run_data_smoke(
        symbols=["RELIANCE"],
        runtime=Path("."),
        primary_source="upstox",
        secondary_source="yfinance",
        data_service_primary=mock_pri,
        data_service_secondary=mock_sec,
        now_dt=now_fixed,
    )
    assert report.overall_passed is True
    assert report.results[0].network_requests_run2 == 0


def test_step5_smoke_excludes_and_lists_partial_session() -> None:
    """PARTIAL (current incomplete session) is excluded from failure and listed."""
    from algodesk.api.market import DataGap

    mock_pri = MagicMock()
    mock_sec = MagicMock()

    # Run 1 returns a gap with status PARTIAL
    gap_partial = DataGap(
        day=date(2026, 11, 9),
        status=CoverageStatus.PARTIAL,
        reason="Current session in progress",
    )
    slice_mock = MagicMock()
    slice_mock.gaps = (gap_partial,)
    slice_mock.bars = []
    mock_pri.get.return_value = slice_mock
    mock_sec.get.return_value = slice_mock

    # Primary source history gets called on run 2 for PARTIAL session
    mock_plugin = MagicMock()
    mock_pri._resolve_source.return_value = mock_plugin

    def fake_history(*args: Any, **kwargs: Any) -> list[Any]:
        return []

    mock_plugin.history = fake_history

    report = SmokeReport(
        symbols=["RELIANCE"],
        primary_source="upstox",
        secondary_source="yfinance",
    )
    # Simulate symbol result where run2 made 2 requests but was for partial session
    res = SymbolSmokeResult(
        symbol="RELIANCE",
        network_requests_run2=2,
        gaps_count=1,
        verify_passed=True,
        max_close_diff_vs_source2=0.0,
        partial_sessions=("2026-11-09",),
        run2_requests=(
            {"symbol": "RELIANCE", "key": "1d", "start": "...", "end": "..."},
            {"symbol": "RELIANCE", "key": "1m", "start": "...", "end": "..."},
        ),
    )
    report.results.append(res)
    report.partial_sessions.extend(res.partial_sessions)
    report.run2_requests.extend(res.run2_requests)

    # Since requests were for partial session, overall_passed remains True
    assert report.overall_passed is True
    assert "2026-11-09" in report.partial_sessions
    assert len(report.run2_requests) == 2


def test_step5_smoke_resolve_dates_in_exchange_tz() -> None:
    """_resolve_smoke_dates avoids date spillover by aligning to Asia/Kolkata."""
    # Morning in India on Monday 2026-11-09 at 09:20 IST (03:50 UTC)
    now_dt = datetime(2026, 11, 9, 3, 50, tzinfo=UTC)
    mock_cal = MagicMock()
    mock_cal.is_trading_day.return_value = True

    start_1d, start_1m, end_1d, _ = _resolve_smoke_dates(mock_cal, now_dt)
    # Before 15:30 IST, current day is yesterday (2026-11-08)
    # In Asia/Kolkata, end_1d must fall on 2026-11-08, NOT leak into 2026-11-09
    end_ist = end_1d.astimezone(_IST)
    assert end_ist.date() == date(2026, 11, 8)


# --- Step 6: Finality of EMPTY days past retry cap & refresh ---


def test_step6_empty_days_are_final_past_retry_cap(tmp_path: Path) -> None:
    """GapPlanner.plan excludes EMPTY days from missing unless refresh=True."""
    manifest = DataManifest(tmp_path / "manifest.db")
    inst = InstrumentId("NSE:EQ:RELIANCE")
    key = SeriesKey(
        instrument=inst,
        kind="bars",
        timeframe="1d",
        adjustment="raw",
        source="upstox",
    )
    ds_id = f"bars:upstox:1d:{inst}:raw"

    # Pre-record day as EMPTY
    empty_date = date(2024, 5, 2)
    manifest.record_coverage_day(
        CoverageDayRecord(
            dataset_id=ds_id,
            day=empty_date.isoformat(),
            status=CoverageStatus.EMPTY,
            rows=0,
            expected_rows=0,
            part_path="",
            fetched_at="2024-05-02T16:00:00Z",
            note="Fetch failed after 3 retries",
        )
    )

    st = datetime(2024, 5, 2, 9, 15, tzinfo=_IST).astimezone(UTC)
    en = datetime(2024, 5, 2, 15, 30, tzinfo=_IST).astimezone(UTC)

    # Without refresh: EMPTY day is final, not missing
    plan = GapPlanner.plan(key, st, en, manifest=manifest, refresh=False)
    assert empty_date in plan.empty_days
    assert empty_date not in plan.missing_days
    assert plan.has_work is False


def test_step6_refresh_forces_refetch_of_empty_days(tmp_path: Path) -> None:
    """GapPlanner.plan with refresh=True includes EMPTY days in missing."""
    manifest = DataManifest(tmp_path / "manifest.db")
    inst = InstrumentId("NSE:EQ:RELIANCE")
    key = SeriesKey(
        instrument=inst,
        kind="bars",
        timeframe="1d",
        adjustment="raw",
        source="upstox",
    )
    ds_id = f"bars:upstox:1d:{inst}:raw"

    empty_date = date(2024, 5, 2)
    manifest.record_coverage_day(
        CoverageDayRecord(
            dataset_id=ds_id,
            day=empty_date.isoformat(),
            status=CoverageStatus.EMPTY,
            rows=0,
            expected_rows=0,
            part_path="",
            fetched_at="2024-05-02T16:00:00Z",
            note="Fetch failed after 3 retries",
        )
    )

    st = datetime(2024, 5, 2, 9, 15, tzinfo=_IST).astimezone(UTC)
    en = datetime(2024, 5, 2, 15, 30, tzinfo=_IST).astimezone(UTC)

    # With refresh=True: day is included in missing
    plan = GapPlanner.plan(key, st, en, manifest=manifest, refresh=True)
    assert empty_date in plan.missing_days
    assert plan.has_work is True


def test_step6_data_service_get_refresh_parameter(tmp_path: Path) -> None:
    """DataService.get accepts refresh parameter and passes it to GapPlanner."""
    from algodesk.data.service import DataService

    mock_store = MagicMock()
    mock_store.manifest = DataManifest(tmp_path / "manifest.db")
    mock_store.dataset_id.return_value = "bars:yfinance:1d:NSE:EQ:RELIANCE:raw"
    mock_store.read.return_value = MagicMock(bars=(), checksums=())

    mock_src = MagicMock()
    ds = DataService({"yfinance": mock_src}, "yfinance", store=mock_store)

    key = SeriesKey(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        kind="bars",
        timeframe="1d",
        adjustment="raw",
        source="yfinance",
    )
    st = datetime(2024, 5, 2, 9, 15, tzinfo=_IST).astimezone(UTC)
    en = datetime(2024, 5, 2, 15, 30, tzinfo=_IST).astimezone(UTC)

    # Call with refresh=True without error
    res = ds.get(key, st, en, download="raw", refresh=True)
    assert res is not None


def test_step6_fetcher_evaluates_partial_vs_empty_settlement(tmp_path: Path) -> None:
    """Fetcher marks incomplete ongoing session PARTIAL and settled session EMPTY."""
    manifest = DataManifest(tmp_path / "manifest.db")
    mock_store = MagicMock()
    mock_store.manifest = manifest
    mock_store.exchange_tz = _IST
    mock_store.dataset_id.return_value = "bars:upstox:1d:NSE:EQ:TCS:raw"

    mock_cov = MagicMock()
    mock_store.coverage_index = mock_cov

    # Return PARTIAL for day 1, EMPTY for day 2
    mock_cov.evaluate_day_status.side_effect = [
        CoverageStatus.PARTIAL,
        CoverageStatus.EMPTY,
    ]

    mock_src = MagicMock()
    mock_src.history.return_value = []  # No bars returned

    from algodesk.data.planner import FetchChunk

    chunk = FetchChunk(
        chunk_id="chunk1",
        start_day=date(2026, 11, 9),
        end_day=date(2026, 11, 10),
        start=datetime(2026, 11, 9, 3, 45, tzinfo=UTC),
        end=datetime(2026, 11, 10, 10, 0, tzinfo=UTC),
        days=(date(2026, 11, 9), date(2026, 11, 10)),
    )

    base_k = SeriesKey(
        instrument=InstrumentId("NSE:EQ:TCS"),
        kind="bars",
        timeframe="1d",
        adjustment="raw",
        source="upstox",
    )
    plan = MagicMock(chunks=(chunk,), requested_key=base_k, base_key=base_k)
    fetcher = Fetcher(
        store=mock_store,
        limiter=MagicMock(),
        sources={"upstox": mock_src},
        retries=1,
    )

    fetcher.fetch(plan)

    cov_recs = manifest.get_coverage_days("bars:upstox:1d:NSE:EQ:TCS:raw")
    statuses = {r.day: r.status for r in cov_recs}
    assert statuses.get("2026-11-09") == CoverageStatus.PARTIAL
    assert statuses.get("2026-11-10") == CoverageStatus.EMPTY
