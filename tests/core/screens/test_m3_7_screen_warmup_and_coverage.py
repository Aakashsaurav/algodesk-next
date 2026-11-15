"""Unit and integration tests for M3-7 (ADR-076):

1. Screen window = largest warm-up among indicators + registry lookback_margin_sessions
   converted to dates through the calendar; gap check applies only inside this window.
2. Skip reasons: no_data (no bars), insufficient_history (fewer sessions than required),
   gap_in_window (message names first missing date and fix command).
3. SUMMARY.md and manifest.json list counts by reason; skipped.csv carries message.
4. table.csv lists every evaluated symbol with passed YES or NO and values used.
5. Finality of EMPTY: EMPTY is final only if (a) symbol has bars on later date (pre-listing)
   or (b) empty range <= registry data.empty_final_max_sessions (default 5).
   Longer all-empty range or fetch errors stored as non-final (reported as MISSING by gaps)
   and journaled as data.fetch_empty_suspect.
6. Accumulate: per-symbol table shows rows added; summary lists symbols with zero rows and
   non-final coverage; dry-run counts non-final days as to fetch; fails loudly (exit code 1)
   when zero-row symbols exceed data.accumulate_max_empty_pct (default 2%).
7. data coverage reset-empty resets existing EMPTY records for instruments with zero stored bars.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from algodesk.api.manifest import PluginManifest
from algodesk.api.market import Bar, CoverageStatus, InstrumentId, SeriesKey
from algodesk.api.protocols import Calendar, DataSource
from algodesk.api.screens import (
    ScreenBars,
    ScreenDataReader,
    ScreenRefusal,
    ScreenResult,
    ScreenRow,
    SkipReason,
)
from algodesk.data.accumulate import (
    AccumulationPlanItem,
    execute_accumulation,
    plan_accumulation,
)
from algodesk.data.coverage import (
    CoverageIndex,
    evaluate_empty_record,
    is_empty_record_final,
    reset_stale_empty_coverage,
)
from algodesk.data.fetcher import FetchChunk, Fetcher, FetchPlan
from algodesk.data.manifest import CoverageDayRecord, DataManifest
from algodesk.data.service import DataService
from algodesk.data.store import BarStore
from algodesk.ops.screens import DataServiceScreenAdapter
from algodesk.results.screen_writer import write_screen_results
from algodesk.screens.models import OperandModel, RequireRule, TomlScreenModel
from algodesk.screens.runner import ScreenRunner

IST = ZoneInfo("Asia/Kolkata")


class FakeCalendar(Calendar):
    """Hermetic test calendar covering 2024 to 2027."""

    def coverage(self) -> tuple[date, date]:
        return date(2024, 1, 1), date(2027, 12, 31)

    def is_trading_day(self, d: date, segment: str = "") -> bool:
        return d.weekday() < 5  # Mon-Fri

    def sessions(self, d: date, segment: str = "") -> list[Any]:
        return []

    def next_session(self, ts: datetime, segment: str = "") -> Any:
        return None


class FakeParamReader:
    """In-memory parameter reader for test registries."""

    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self._values = values or {}

    def get(self, name: str, default: Any = None) -> Any:
        return self._values.get(name, default)


class FakeJournal:
    """In-memory journal capturing emitted events."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def append(self, kind: str, payload: dict[str, Any], **kwargs: Any) -> int:
        self.events.append({"kind": kind, "payload": payload})
        return len(self.events)


class DummyDataReader(ScreenDataReader):
    """Records get_bars calls and returns configurable responses."""

    def __init__(self, response_map: dict[str, ScreenBars | ScreenRefusal]) -> None:
        self.response_map = response_map
        self.calls: list[dict[str, Any]] = []

    def get_bars(
        self,
        instrument: InstrumentId | str,
        timeframe: str,
        start: datetime,
        end: datetime,
        adjust: str = "splits",
        accept_unverified: bool = False,
    ) -> ScreenBars | ScreenRefusal:
        self.calls.append(
            {
                "instrument": str(instrument),
                "timeframe": timeframe,
                "start": start,
                "end": end,
                "adjust": adjust,
            }
        )
        sym = str(instrument).split(":")[-1]
        return self.response_map.get(
            sym,
            ScreenRefusal(reason=SkipReason.NO_DATA, detail=f"No data for {sym}"),
        )


def _generate_bars_sequence(
    instrument: str,
    prices: Sequence[float],
    as_of: date = date(2026, 11, 12),
) -> list[Bar]:
    inst_id = InstrumentId(instrument if ":" in instrument else f"NSE:EQ:{instrument}")
    n = len(prices)
    trading_days: list[date] = []
    curr = as_of
    while len(trading_days) < n:
        if curr.weekday() < 5:
            trading_days.append(curr)
        curr -= timedelta(days=1)
    trading_days.reverse()

    bars: list[Bar] = []
    for d, p in zip(trading_days, prices, strict=True):
        s_dt = datetime.combine(d, time(9, 15), tzinfo=IST).astimezone(UTC)
        e_dt = datetime.combine(d, time(15, 30), tzinfo=IST).astimezone(UTC)
        bars.append(
            Bar(
                instrument=inst_id,
                timeframe="1d",
                start_ts=s_dt,
                end_ts=e_dt,
                open=float(p),
                high=float(p) * 1.01,
                low=float(p) * 0.99,
                close=float(p),
                volume=1000,
                complete=True,
            )
        )
    return bars


def _build_test_data_service(
    tmp_path: Path, source_name: str = "upstox"
) -> tuple[DataService, BarStore, DataManifest]:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(data_dir / "data_manifest.db")
    cal = FakeCalendar()
    store = BarStore(data_dir, manifest, calendar=cal)

    mock_source = MagicMock(spec=DataSource)
    mock_source.history.return_value = ()

    manifest_obj = PluginManifest.model_validate(
        {
            "name": source_name,
            "kind": "broker",
            "version": "1.0.0",
            "api_version": 1,
            "entry": f"broker:{source_name}",
            "capabilities": {"candles": True, "history": True},
            "candles_adjustment_basis": "splits",
            "max_span": {"1d": 3650, "1m": 30},
        }
    )
    ds = DataService(
        sources={source_name: mock_source},
        default_source=source_name,
        store=store,
        manifests={source_name: manifest_obj},
        calendar=cal,
    )
    return ds, store, manifest


# =========================================================================
# Step 1: Window from warm-up + lookback margin
# =========================================================================


def test_screen_window_computed_from_indicator_max_warmup_plus_margin() -> None:
    """Screen window uses largest warm-up plus screen.lookback_margin_sessions."""
    cal = FakeCalendar()
    params = FakeParamReader({"screen.lookback_margin_sessions": 20})
    dummy_reader = DummyDataReader({})
    resolver = MagicMock()

    runner = ScreenRunner(
        data_reader=dummy_reader,
        indicator_resolver=resolver,
        calendar=cal,
        params=params,
    )

    screen = TomlScreenModel(
        name="sma50_screen",
        description="SMA 50 screen",
        timeframe="1d",
        adjust="splits",
        universe=None,
        require=[
            RequireRule(
                left=OperandModel(ind="sma", params={"period": 50}),
                op=">",
                right=OperandModel(const=100.0),
            )
        ],
        group=[],
        rank=None,
    )

    as_of = date(2026, 11, 12)  # Thursday
    runner.run(screen, symbols=["RELIANCE"], as_of=as_of)

    assert len(dummy_reader.calls) == 1
    call = dummy_reader.calls[0]
    start_dt: datetime = call["start"]
    end_dt: datetime = call["end"]

    assert end_dt.date() == as_of
    # SMA 50 + margin 20 = 70 sessions (~98 calendar days).
    # Never the hardcoded 730 days!
    cal_span_days = (as_of - start_dt.date()).days
    assert 90 <= cal_span_days <= 110


def test_screen_window_custom_lookback_margin_from_param_registry() -> None:
    """Custom lookback_margin_sessions in param registry scales window accordingly."""
    cal = FakeCalendar()
    params = FakeParamReader({"screen.lookback_margin_sessions": 30})
    dummy_reader = DummyDataReader({})

    runner = ScreenRunner(
        data_reader=dummy_reader,
        indicator_resolver=MagicMock(),
        calendar=cal,
        params=params,
    )

    screen = TomlScreenModel(
        name="sma20_screen",
        description="SMA 20 screen",
        timeframe="1d",
        adjust="splits",
        universe=None,
        require=[
            RequireRule(
                left=OperandModel(ind="sma", params={"period": 20}),
                op=">",
                right=OperandModel(const=50.0),
            )
        ],
        group=[],
        rank=None,
    )

    as_of = date(2026, 11, 12)
    runner.run(screen, symbols=["TCS"], as_of=as_of)

    assert len(dummy_reader.calls) == 1
    call = dummy_reader.calls[0]
    # SMA 20 + margin 30 = 50 sessions (~70 calendar days)
    cal_span_days = (as_of - call["start"].date()).days
    assert 65 <= cal_span_days <= 80


# =========================================================================
# Step 2: Skip reasons: no_data, insufficient_history, gap_in_window
# =========================================================================


def test_screen_skip_reason_no_data(tmp_path: Path) -> None:
    """Symbol with 0 bars is skipped with SkipReason.NO_DATA."""
    ds, store, manifest = _build_test_data_service(tmp_path)
    adapter = DataServiceScreenAdapter(ds)
    cal = FakeCalendar()

    runner = ScreenRunner(
        data_reader=adapter,
        indicator_resolver=MagicMock(),
        calendar=cal,
    )

    screen = TomlScreenModel(
        name="test_screen",
        description="Test",
        timeframe="1d",
        adjust="splits",
        universe=None,
        require=[
            RequireRule(
                left=OperandModel(field="close"),
                op=">",
                right=OperandModel(const=10.0),
            )
        ],
        group=[],
        rank=None,
    )

    res = runner.run(screen, symbols=["EMPTY_STOCK"], as_of=date(2026, 11, 12), group="testgrp")
    assert len(res.skipped) == 1
    sym, reason, detail = res.skipped[0]
    assert reason == SkipReason.NO_DATA
    assert "no_data" in detail
    assert "Fix with: algodesk data accumulate --group testgrp --timeframe 1d" in detail


def test_screen_skip_reason_insufficient_history(tmp_path: Path) -> None:
    """Symbol with fewer bars than indicator warm-up is skipped with INSUFFICIENT_HISTORY."""
    ds, store, manifest = _build_test_data_service(tmp_path)
    # Store 30 bars for SHORT_STOCK
    bars = _generate_bars_sequence("SHORT_STOCK", [100.0] * 30, as_of=date(2026, 11, 12))
    key = SeriesKey(InstrumentId("NSE:EQ:SHORT_STOCK"), "bars", "1d", "raw", "upstox")
    store.commit(key, bars)

    adapter = DataServiceScreenAdapter(ds)
    cal = FakeCalendar()
    runner = ScreenRunner(
        data_reader=adapter,
        indicator_resolver=MagicMock(),
        calendar=cal,
    )

    # Requires SMA(50), but only 30 bars available
    screen = TomlScreenModel(
        name="sma50_screen",
        description="SMA 50",
        timeframe="1d",
        adjust="splits",
        universe=None,
        require=[
            RequireRule(
                left=OperandModel(ind="sma", params={"period": 50}),
                op=">",
                right=OperandModel(const=50.0),
            )
        ],
        group=[],
        rank=None,
    )

    res = runner.run(screen, symbols=["SHORT_STOCK"], as_of=date(2026, 11, 12), group="testgrp")
    assert len(res.skipped) == 1
    sym, reason, detail = res.skipped[0]
    assert reason == SkipReason.INSUFFICIENT_HISTORY
    assert "fewer than required warm-up (50 bars)" in detail


def test_screen_skip_reason_gap_in_window(tmp_path: Path) -> None:
    """Symbol with gap inside the read window is skipped with GAP_IN_WINDOW."""
    ds, store, manifest = _build_test_data_service(tmp_path)
    # Store 70 bars up to 2026-11-12
    bars = _generate_bars_sequence("GAP_STOCK", [100.0] * 70, as_of=date(2026, 11, 12))
    key = SeriesKey(InstrumentId("NSE:EQ:GAP_STOCK"), "bars", "1d", "raw", "upstox")
    store.commit(key, bars)

    # Inject a non-final EMPTY day (stale gap) inside the window on 2026-11-10
    ds_id = "bars:upstox:1d:NSE:EQ:GAP_STOCK:raw"
    manifest.record_coverage_day(
        CoverageDayRecord(
            dataset_id=ds_id,
            day="2026-11-10",
            status=CoverageStatus.EMPTY,
            rows=0,
            expected_rows=0,
            part_path="",
            fetched_at="2026-11-10T04:00:00Z",  # before settle -> stale gap
            note="Premature fetch",
        )
    )

    adapter = DataServiceScreenAdapter(ds)
    cal = FakeCalendar()
    runner = ScreenRunner(
        data_reader=adapter,
        indicator_resolver=MagicMock(),
        calendar=cal,
    )

    screen = TomlScreenModel(
        name="sma20_screen",
        description="SMA 20",
        timeframe="1d",
        adjust="splits",
        universe=None,
        require=[
            RequireRule(
                left=OperandModel(ind="sma", params={"period": 20}),
                op=">",
                right=OperandModel(const=50.0),
            )
        ],
        group=[],
        rank=None,
    )

    res = runner.run(screen, symbols=["GAP_STOCK"], as_of=date(2026, 11, 12), group="mygroup")
    assert len(res.skipped) == 1
    sym, reason, detail = res.skipped[0]
    assert reason == SkipReason.GAP_IN_WINDOW
    assert "gap_in_window" in detail
    assert "2026-11-10" in detail
    assert "Fix with: algodesk data accumulate --group mygroup --timeframe 1d" in detail


# =========================================================================
# Step 3: Summary, manifest.json, and skipped.csv counts and messages
# =========================================================================


def test_screen_summary_and_manifest_counts_by_reason(tmp_path: Path) -> None:
    """SUMMARY.md and manifest.json report counts by skip reason; skipped.csv carries details."""
    result = ScreenResult(
        name="multi_skip_screen",
        as_of=date(2026, 11, 12),
        timeframe="1d",
        rows=[],
        skipped=[
            ("S1", SkipReason.NO_DATA, "S1: no_data: No cached bars. Fix with: accumulate"),
            (
                "S2",
                SkipReason.INSUFFICIENT_HISTORY,
                "S2: insufficient_history: 10 bars < 50. Fix with: accumulate",
            ),
            (
                "S3",
                SkipReason.GAP_IN_WINDOW,
                "S3: gap_in_window: Gap in window on 2026-11-10. Fix with: accumulate",
            ),
        ],
        universe_hash="univ_hash",
        result_hash="res_hash",
        provenance={},
        parameters={},
        flags={"adjust": "splits"},
        verdict="FAIL",
        fail_reason="100% skipped",
        run_id="screen_test_skip_counts_1",
    )

    run_dir = write_screen_results(tmp_path / "results", result)

    # 1. SUMMARY.md
    summary_text = (run_dir / "SUMMARY.md").read_text(encoding="utf-8")
    assert "Skipped: 3 (gap_in_window: 1, insufficient_history: 1, no_data: 1)" in summary_text

    # 2. manifest.json
    manifest_data = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    stats = manifest_data["stats"]
    assert stats["skipped"] == 3
    assert stats["skipped_by_reason"] == {
        "gap_in_window": 1,
        "insufficient_history": 1,
        "no_data": 1,
    }

    # 3. skipped.csv
    skipped_rows = list(
        csv.reader((run_dir / "skipped.csv").read_text(encoding="utf-8").strip().splitlines())
    )
    assert len(skipped_rows) == 4  # header + 3 skipped
    assert skipped_rows[0] == ["symbol", "reason", "detail"]
    assert any(r[1] == "gap_in_window" and "2026-11-10" in r[2] for r in skipped_rows)


# =========================================================================
# Step 4: table.csv lists every evaluated symbol with passed YES or NO
# =========================================================================


def test_table_csv_lists_every_evaluated_symbol_with_passed_yes_and_no(tmp_path: Path) -> None:
    """table.csv contains both passed and unpassed evaluated symbols with YES/NO."""
    result = ScreenResult(
        name="eval_screen",
        as_of=date(2026, 11, 12),
        timeframe="1d",
        rows=[
            ScreenRow(
                symbol="PASS_SYM",
                passed=True,
                score=100.0,
                values={"sma_50": 150.0, "close": 155.0},
            ),
            ScreenRow(
                symbol="FAIL_SYM", passed=False, score=0.0, values={"sma_50": 90.0, "close": 85.0}
            ),
        ],
        skipped=[("SKIP_SYM", SkipReason.NO_DATA, "no data")],
        universe_hash="uh",
        result_hash="rh",
        provenance={},
        parameters={},
        flags={"adjust": "splits"},
        verdict="PASS",
        run_id="screen_test_table_csv_1",
    )

    run_dir = write_screen_results(tmp_path / "results", result)
    table_csv = (run_dir / "table.csv").read_text(encoding="utf-8").strip().splitlines()

    assert len(table_csv) == 3  # Header + 2 evaluated rows
    reader = csv.DictReader(table_csv)
    rows = list(reader)
    assert len(rows) == 2

    pass_row = next(r for r in rows if r["symbol"] == "PASS_SYM")
    assert pass_row["passed"] == "YES"
    assert float(pass_row["close"]) == 155.0

    fail_row = next(r for r in rows if r["symbol"] == "FAIL_SYM")
    assert fail_row["passed"] == "NO"
    assert float(fail_row["close"]) == 85.0


# =========================================================================
# Step 5: Finality of EMPTY: pre-listing, short range, long range, errors
# =========================================================================


def test_empty_finality_pre_listing_days_is_final(tmp_path: Path) -> None:
    """EMPTY days before listing date (with bars on later date) are final."""
    manifest = DataManifest(tmp_path / "manifest.db")
    ds_id = "bars:upstox:1d:NSE:EQ:NEW_IPO:raw"

    # Pre-listing EMPTY day in 2024
    rec = CoverageDayRecord(
        dataset_id=ds_id,
        day="2024-11-12",
        status=CoverageStatus.EMPTY,
        rows=0,
        expected_rows=0,
        part_path="",
        fetched_at="2026-11-13T10:00:00Z",
        note="Source returned no bars",
    )
    manifest.record_coverage_day(rec)

    # Later date has bars / COMPLETE status in 2025
    manifest.record_coverage_day(
        CoverageDayRecord(
            dataset_id=ds_id,
            day="2025-06-01",
            status=CoverageStatus.COMPLETE,
            rows=1,
            expected_rows=1,
            part_path="p.parquet",
            fetched_at="2026-11-13T10:00:00Z",
        )
    )

    is_final, reason = evaluate_empty_record(rec=rec, manifest=manifest, settle_minutes=15)
    assert is_final is True
    assert "pre-listing" in reason.lower()
    assert is_empty_record_final(rec=rec, manifest=manifest, settle_minutes=15) is True


def test_empty_finality_short_range_is_final(tmp_path: Path) -> None:
    """Empty range <= empty_final_max_sessions (default 5) is final."""
    manifest = DataManifest(tmp_path / "manifest.db")
    ds_id = "bars:upstox:1d:NSE:EQ:SHORT_EMPTY:raw"

    # 3 consecutive EMPTY days
    for d_str in ("2026-11-09", "2026-11-10", "2026-11-11"):
        manifest.record_coverage_day(
            CoverageDayRecord(
                dataset_id=ds_id,
                day=d_str,
                status=CoverageStatus.EMPTY,
                rows=0,
                expected_rows=0,
                part_path="",
                fetched_at="2026-11-12T10:00:00Z",
                note="Halt / no bars",
            )
        )

    recs = manifest.get_coverage_days(ds_id, start_day="2026-11-10", end_day="2026-11-10")
    assert len(recs) == 1
    rec = recs[0]

    is_final, reason = evaluate_empty_record(rec=rec, manifest=manifest, settle_minutes=15)
    assert is_final is True
    assert "short empty range" in reason.lower()


def test_empty_finality_long_range_is_non_final(tmp_path: Path) -> None:
    """Empty range > empty_final_max_sessions (5) without later bars is non-final."""
    manifest = DataManifest(tmp_path / "manifest.db")
    ds_id = "bars:upstox:1d:NSE:EQ:LONG_EMPTY:raw"

    # 10 consecutive empty days
    start_d = date(2026, 10, 1)
    for i in range(10):
        d_str = (start_d + timedelta(days=i)).isoformat()
        manifest.record_coverage_day(
            CoverageDayRecord(
                dataset_id=ds_id,
                day=d_str,
                status=CoverageStatus.EMPTY,
                rows=0,
                expected_rows=0,
                part_path="",
                fetched_at="2026-11-12T10:00:00Z",
                note="",
            )
        )

    recs = manifest.get_coverage_days(ds_id, start_day="2026-10-05", end_day="2026-10-05")
    assert len(recs) == 1
    rec = recs[0]

    is_final, reason = evaluate_empty_record(rec=rec, manifest=manifest, settle_minutes=15)
    assert is_final is False
    assert "exceeds" in reason.lower()

    # Reported as MISSING by CoverageIndex.detect_gaps
    cal = FakeCalendar()
    idx = CoverageIndex(manifest=manifest, calendar=cal)
    gaps = idx.detect_gaps(ds_id, date(2026, 10, 1), date(2026, 10, 10))
    assert any(g.status == CoverageStatus.MISSING for g in gaps)


def test_empty_finality_fetch_errors_always_non_final(tmp_path: Path) -> None:
    """Any range with fetch errors in note or fetch_log is non-final."""
    manifest = DataManifest(tmp_path / "manifest.db")
    ds_id = "bars:upstox:1d:NSE:EQ:ERROR_STOCK:raw"

    rec = CoverageDayRecord(
        dataset_id=ds_id,
        day="2026-11-10",
        status=CoverageStatus.EMPTY,
        rows=0,
        expected_rows=0,
        part_path="",
        fetched_at="2026-11-12T10:00:00Z",
        note="Fetch failed after 3 retries: [Errno 8] nodename nor servname provided",
    )
    is_final, reason = evaluate_empty_record(rec=rec, manifest=manifest, settle_minutes=15)
    assert is_final is False
    assert "fetch error" in reason.lower()


def test_fetcher_journals_fetch_empty_suspect_on_long_empty_or_error(tmp_path: Path) -> None:
    """Fetcher emits data.fetch_empty_suspect when recording failed chunks or long empty ranges."""
    manifest = DataManifest(tmp_path / "manifest.db")
    cal = FakeCalendar()
    store = BarStore(tmp_path / "data", manifest, calendar=cal)

    failing_source = MagicMock(spec=DataSource)
    failing_source.history.side_effect = RuntimeError("DNS lookup failed")

    journal = FakeJournal()

    fetcher = Fetcher(
        store=store,
        limiter=MagicMock(),
        sources={"upstox": failing_source},
        journal=journal,
        retries=0,
    )

    key = SeriesKey(InstrumentId("NSE:EQ:FAIL_SYM"), "bars", "1d", "raw", "upstox")
    d_list = [date(2026, 11, 1) + timedelta(days=i) for i in range(10)]
    chunk = FetchChunk(
        chunk_id="chunk_1",
        start_day=date(2026, 11, 1),
        end_day=date(2026, 11, 10),
        start=datetime(2026, 11, 1, 0, 0, tzinfo=UTC),
        end=datetime(2026, 11, 10, 23, 59, 59, tzinfo=UTC),
        days=tuple(d_list),
    )
    plan = FetchPlan(
        requested_key=key,
        base_key=key,
        chunks=(chunk,),
        missing_days=tuple(d_list),
        complete_days=(),
        closed_days=(),
        unavailable_days=(),
    )

    fetcher.fetch(plan)

    suspect_events = [e for e in journal.events if e["kind"] == "data.fetch_empty_suspect"]
    assert len(suspect_events) >= 1
    assert "chunk_1" in suspect_events[0]["payload"]["chunk"]


# =========================================================================
# Step 6: Accumulate failure on > 2% zero-row symbols & dry run
# =========================================================================


def test_accumulate_fails_when_zero_row_symbols_exceed_max_empty_pct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Accumulate fails with DataError when > data.accumulate_max_empty_pct symbols get 0 rows."""
    from algodesk.data import accumulate as acc_mod

    # 5 symbols, 1 gets 0 rows (20% > 2%)
    items = [
        AccumulationPlanItem(
            instrument=f"NSE:EQ:SYM{i}",
            timeframe="1d",
            target_sessions=[date(2026, 11, 10)],
            stored_sessions=[],
            fetchable_sessions=[date(2026, 11, 10)],
            lost_sessions=[],
        )
        for i in range(5)
    ]
    monkeypatch.setattr(
        acc_mod,
        "plan_accumulation",
        lambda **kwargs: ([it.instrument for it in items], items),
    )

    # Mock DataService: SYM0 returns 0 bars, others return 1 bar
    def fake_get(series: Any, **kwargs: Any) -> Any:
        sym = str(series.instrument).split(":")[-1]
        if sym == "SYM0":
            return MagicMock(bars=(), provenance={"rows_added": 0})
        return MagicMock(bars=(MagicMock(),), provenance={"rows_added": 1})

    mock_ds = MagicMock()
    mock_ds.get.side_effect = fake_get

    params = FakeParamReader({"data.accumulate_max_empty_pct": 2.0})
    rt = MagicMock()
    rt.params = params
    rt.paths.data = tmp_path / "data"

    rep = execute_accumulation(
        group="demo5",
        source="upstox",
        days=1,
        runtime=rt,
        dry_run=False,
        data_service=mock_ds,
        calendar=FakeCalendar(),
    )

    assert rep.zero_row_symbols == ["NSE:EQ:SYM0"]
    assert rep.zero_row_pct == 20.0
    assert rep.max_empty_pct == 2.0
    assert rep.zero_row_pct > rep.max_empty_pct

    # Also test CLI exit code 1 with the list
    from typer.testing import CliRunner

    from algodesk.cli import data as cli_data_mod
    from algodesk.cli.data import data_app

    runner = CliRunner()
    monkeypatch.setattr(acc_mod, "execute_accumulation", lambda **kwargs: rep)
    monkeypatch.setattr(
        cli_data_mod,
        "_open_rt",
        lambda *a, **k: MagicMock(__enter__=lambda s: rt, __exit__=lambda *a: None),
    )
    monkeypatch.setattr(cli_data_mod, "_build_data_service", lambda *a, **k: mock_ds)

    res = runner.invoke(
        data_app, ["accumulate", "--group", "demo5", "--source", "upstox", "--days", "1"]
    )
    assert res.exit_code == 1
    assert "exceeding data.accumulate_max_empty_pct" in res.output
    assert "NSE:EQ:SYM0" in res.output


def test_accumulate_dry_run_counts_non_final_empty_as_to_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dry-run accumulation counts non-final EMPTY days as to fetch."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(data_dir / "data_manifest.db")

    # Record 10 EMPTY days without later bars (non-final)
    ds_id = "bars:upstox:1d:NSE:EQ:NF_SYM:raw"
    for i in range(10):
        d_str = (date(2026, 11, 1) + timedelta(days=i)).isoformat()
        manifest.record_coverage_day(
            CoverageDayRecord(
                dataset_id=ds_id,
                day=d_str,
                status=CoverageStatus.EMPTY,
                rows=0,
                expected_rows=0,
                part_path="",
                fetched_at="2026-11-12T10:00:00Z",
                note="",
            )
        )

    cal = FakeCalendar()
    rt = MagicMock()
    rt.root = tmp_path
    rt.paths.data = data_dir
    rt.params = FakeParamReader()
    rt.plugins = None

    # Monkeypatch groups to return NF_SYM
    from algodesk.data import groups as grp_mod

    monkeypatch.setattr(grp_mod, "load_groups", lambda *a, **k: {"mygroup": ["NSE:EQ:NF_SYM"]})

    insts, items = plan_accumulation(
        group="mygroup",
        source="upstox",
        timeframe="1d",
        days=5,
        runtime=rt,
        calendar=cal,
        now_dt=datetime(2026, 11, 12, 10, 0, tzinfo=UTC),
    )

    assert len(items) == 1
    it = items[0]
    # Non-final empty days must be in fetchable_sessions ("to fetch")
    assert len(it.fetchable_sessions) > 0


# =========================================================================
# Step 7: Reset of all-empty symbols
# =========================================================================


def test_coverage_reset_empty_resets_zero_stored_bar_instruments(tmp_path: Path) -> None:
    """reset_stale_empty_coverage resets EMPTY records for instruments with zero stored bars."""
    manifest = DataManifest(tmp_path / "manifest.db")
    ds_id = "bars:upstox:1d:NSE:EQ:ZERO_BAR_SYM:raw"

    for i in range(10):
        manifest.record_coverage_day(
            CoverageDayRecord(
                dataset_id=ds_id,
                day=(date(2026, 10, 1) + timedelta(days=i)).isoformat(),
                status=CoverageStatus.EMPTY,
                rows=0,
                expected_rows=0,
                part_path="",
                fetched_at="2026-11-12T10:00:00Z",
                note="Empty",
            )
        )

    journal = FakeJournal()
    reset_records = reset_stale_empty_coverage(
        manifest=manifest,
        calendar=FakeCalendar(),
        dry_run=False,
        journal=journal,
    )

    assert len(reset_records) == 10
    assert all(
        "zero stored bars" in reason.lower() or "exceeds" in reason.lower()
        for _, reason in reset_records
    )

    # Check that they were reset in manifest to MISSING
    for rec, _ in reset_records:
        recs = manifest.get_coverage_days(rec.dataset_id, start_day=rec.day, end_day=rec.day)
        assert len(recs) == 1
        assert recs[0].status == CoverageStatus.MISSING

    # Check journal emission
    reset_events = [e for e in journal.events if e["kind"] == "data.coverage_reset_empty"]
    assert len(reset_events) == 1
    assert reset_events[0]["payload"]["reset_count"] == 10
    assert "ZERO_BAR_SYM" in reset_events[0]["payload"]["symbols"]
