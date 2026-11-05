"""Tests for M2-6: Closure inference hardening and corporate actions reconciliation on real data."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from algodesk.api.checks import Severity
from algodesk.api.errors import DataError
from algodesk.api.market import ActionRecord, Bar, CoverageStatus, DataSlice, SeriesKey
from algodesk.api.protocols import Calendar
from algodesk.cli.app import app
from algodesk.data.actions_store import ActionsStore
from algodesk.data.closures import (
    infer_market_closures,
)
from algodesk.data.coverage import CoverageIndex
from algodesk.data.manifest import CoverageDayRecord, DataManifest
from algodesk.data.planner import GapPlanner
from algodesk.data.reconciler import CorporateActionsReconciler
from algodesk.ops.checks import _check_market_closure_disagreements


class SimpleCalendar(Calendar):
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


def _make_bar(inst: str, dt: datetime) -> Bar:
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


def _load_real_reliance_actions() -> tuple[list[ActionRecord], list[ActionRecord]]:
    from plugins.references.nse_actions.parser import parse_nse_item

    fixtures_dir = Path(__file__).parents[2] / "fixtures" / "reference"
    with open(fixtures_dir / "nse" / "reliance_actions.json") as f:
        nse_raw = json.load(f)
    with open(fixtures_dir / "yfinance" / "reliance_actions.json") as f:
        yf_raw = json.load(f)

    nse_acts: list[ActionRecord] = []
    for item in nse_raw:
        for a in parse_nse_item(item, now_iso=datetime.now(UTC).isoformat()):
            nse_acts.append(a)

    yf_acts: list[ActionRecord] = []
    for row in yf_raw:
        dt = datetime.fromisoformat(row["Date"]).date()
        if row.get("Dividends", 0.0) > 0.0:
            yf_acts.append(
                ActionRecord(
                    ex_date=dt,
                    type="dividend",
                    amount=Decimal(str(row["Dividends"])),
                    source="yfinance",
                    symbol="RELIANCE",
                    exchange="NSE",
                )
            )
        if row.get("Stock Splits", 0.0) > 0.0:
            yf_acts.append(
                ActionRecord(
                    ex_date=dt,
                    type="split",
                    ratio=float(row["Stock Splits"]),
                    source="yfinance",
                    symbol="RELIANCE",
                    exchange="NSE",
                )
            )
    return nse_acts, yf_acts


# ===========================================================================
# CP0 & Step 1: Real Fixtures and Diagnosis
# ===========================================================================


def test_cp0_fixture_availability_and_diagnosis_counts() -> None:
    """Step 1: Trimmed real fixtures for RELIANCE are saved and parseable."""
    nse_acts, yf_acts = _load_real_reliance_actions()
    assert len(nse_acts) >= 20
    assert len(yf_acts) >= 20
    assert any(a.ex_date == date(2024, 8, 19) and a.type == "dividend" for a in nse_acts)
    assert any(a.ex_date == date(2024, 10, 28) and a.type == "bonus" for a in nse_acts)
    assert any(a.ex_date == date(2024, 8, 19) and a.type == "dividend" for a in yf_acts)


# ===========================================================================
# CP1 & Steps 2 to 7: Closure Inference Hardening
# ===========================================================================


def test_cp1_unfetched_missing_days_never_count_as_no_bar(tmp_path: Path) -> None:
    """Step 2: MISSING or unfetched days never count as 'no bar', preventing spurious closures."""
    manifest = DataManifest(tmp_path / "manifest.db")
    sample_symbols = ["NSE:EQ:SYM1", "NSE:EQ:SYM2"]
    d_open = date(2024, 5, 20)

    # Mock DataService that returns empty bars
    mock_ds = MagicMock()
    mock_ds._exchange_tz = UTC
    mock_ds.get.return_value = DataSlice(
        key=SeriesKey("NSE:EQ:SYM1", "bars", "1d", "raw", "yfinance"),
        start=datetime(2024, 5, 20, tzinfo=UTC),
        end=datetime(2024, 5, 20, 23, 59, 59, tzinfo=UTC),
        bars=(),
        gaps=(),
        dataset_ids=("ds1",),
        checksums=(),
        source="yfinance",
        adjustment="raw",
        fetched_at=datetime.now(UTC).isoformat(),
    )

    # Coverage days in manifest: status is MISSING (not EMPTY)
    manifest.record_coverage_day(
        CoverageDayRecord(
            dataset_id="bars:yfinance:1d:NSE:EQ:SYM1:raw",
            day=d_open.isoformat(),
            status=CoverageStatus.MISSING,
            rows=0,
            expected_rows=1,
            part_path="",
            fetched_at=datetime.now(UTC).isoformat(),
        )
    )

    cal = SimpleCalendar(closed_days=set())
    recs = infer_market_closures(
        manifest=manifest,
        data_service=mock_ds,
        exchange="NSE",
        start_d=d_open,
        end_d=d_open,
        sample_symbols=sample_symbols,
        calendar=cal,
        closure_max_pct=100.0,
    )
    # MISSING day must NOT count as closure
    assert len(recs) == 0


def test_cp1_failed_fetch_chunk_never_counts_as_no_bar(tmp_path: Path) -> None:
    """Step 2: Days from a failed or 0-row chunk never count as 'no bar'."""
    manifest = DataManifest(tmp_path / "manifest.db")
    sample_symbols = ["NSE:EQ:SYM1"]
    d_open = date(2024, 5, 20)

    mock_ds = MagicMock()
    mock_ds._exchange_tz = UTC
    mock_ds.get.return_value = DataSlice(
        key=SeriesKey("NSE:EQ:SYM1", "bars", "1d", "raw", "yfinance"),
        start=datetime(2024, 5, 20, tzinfo=UTC),
        end=datetime(2024, 5, 20, 23, 59, 59, tzinfo=UTC),
        bars=(),
        gaps=(),
        dataset_ids=("ds1",),
        checksums=(),
        source="yfinance",
        adjustment="raw",
        fetched_at=datetime.now(UTC).isoformat(),
    )

    manifest.record_fetch_log(
        chunk="NSE:EQ:SYM1:yfinance:1d:2024-05-20_2024-05-20",
        source="yfinance",
        started=datetime.now(UTC).isoformat(),
        finished=datetime.now(UTC).isoformat(),
        rows=0,  # 0 rows returned
        outcome="OK",
        error="",
    )
    manifest.record_coverage_day(
        CoverageDayRecord(
            dataset_id="bars:yfinance:1d:NSE:EQ:SYM1:raw",
            day=d_open.isoformat(),
            status=CoverageStatus.EMPTY,
            rows=0,
            expected_rows=1,
            part_path="",
            fetched_at=datetime.now(UTC).isoformat(),
        )
    )

    cal = SimpleCalendar(closed_days=set())
    recs = infer_market_closures(
        manifest=manifest,
        data_service=mock_ds,
        exchange="NSE",
        start_d=d_open,
        end_d=d_open,
        sample_symbols=sample_symbols,
        calendar=cal,
        closure_max_pct=100.0,
    )
    # Chunk with 0 rows does NOT count as valid evidence
    assert len(recs) == 0


def test_cp1_empty_coverage_with_ok_chunk_counts_as_no_bar(tmp_path: Path) -> None:
    """Step 2: Day with EMPTY coverage from an OK chunk returning >= 1 bar counts as no bar."""
    manifest = DataManifest(tmp_path / "manifest.db")
    sample_symbols = ["NSE:EQ:SYM1"]
    d_open = date(2024, 5, 20)

    mock_ds = MagicMock()
    mock_ds._exchange_tz = UTC
    mock_ds.get.return_value = DataSlice(
        key=SeriesKey("NSE:EQ:SYM1", "bars", "1d", "raw", "yfinance"),
        start=datetime(2024, 5, 20, tzinfo=UTC),
        end=datetime(2024, 5, 20, 23, 59, 59, tzinfo=UTC),
        bars=(),
        gaps=(),
        dataset_ids=("ds1",),
        checksums=(),
        source="yfinance",
        adjustment="raw",
        fetched_at=datetime.now(UTC).isoformat(),
    )

    manifest.record_fetch_log(
        chunk="NSE:EQ:SYM1:yfinance:1d:2024-05-15_2024-05-25",
        source="yfinance",
        started=datetime.now(UTC).isoformat(),
        finished=datetime.now(UTC).isoformat(),
        rows=5,  # Chunk returned bars
        outcome="OK",
        error="",
    )
    manifest.record_coverage_day(
        CoverageDayRecord(
            dataset_id="bars:yfinance:1d:NSE:EQ:SYM1:raw",
            day=d_open.isoformat(),
            status=CoverageStatus.EMPTY,
            rows=0,
            expected_rows=1,
            part_path="",
            fetched_at=datetime.now(UTC).isoformat(),
        )
    )

    cal = SimpleCalendar(closed_days=set())
    recs = infer_market_closures(
        manifest=manifest,
        data_service=mock_ds,
        exchange="NSE",
        start_d=d_open,
        end_d=d_open,
        sample_symbols=sample_symbols,
        calendar=cal,
        closure_max_pct=100.0,
    )
    assert len(recs) == 1
    assert recs[0].kind == "inferred"


def test_cp1_infer_aborts_with_clear_error_on_chunk_failure(tmp_path: Path) -> None:
    """Step 2: infer aborts with actionable DataError if any sample chunk failed."""
    manifest = DataManifest(tmp_path / "manifest.db")
    sample_symbols = ["NSE:EQ:SYM1"]
    d_open = date(2024, 5, 20)

    mock_ds = MagicMock()
    mock_ds._exchange_tz = UTC
    mock_ds.get.side_effect = DataError(
        what="Network connection refused",
        where="DataService.get",
        fix="Check internet connection",
    )

    with pytest.raises(DataError) as exc_info:
        infer_market_closures(
            manifest=manifest,
            data_service=mock_ds,
            exchange="NSE",
            start_d=d_open,
            end_d=d_open,
            sample_symbols=sample_symbols,
        )
    assert "Sample series fetch failed" in str(exc_info.value)


def test_cp1_safety_cap_exceeded_writes_nothing_and_raises(tmp_path: Path) -> None:
    """Step 3: If inferred closures exceed data.closure_max_pct, write nothing and raise."""
    manifest = DataManifest(tmp_path / "manifest.db")
    sample_symbols = ["NSE:EQ:SYM1"]
    start_d = date(2024, 5, 1)
    end_d = date(2024, 5, 20)  # ~14 trading days

    mock_ds = MagicMock()
    mock_ds._exchange_tz = UTC
    mock_ds.get.return_value = DataSlice(
        key=SeriesKey("NSE:EQ:SYM1", "bars", "1d", "raw", "yfinance"),
        start=datetime(2024, 5, 1, tzinfo=UTC),
        end=datetime(2024, 5, 20, 23, 59, 59, tzinfo=UTC),
        bars=(),
        gaps=(),
        dataset_ids=("ds1",),
        checksums=(),
        source="yfinance",
        adjustment="raw",
        fetched_at=datetime.now(UTC).isoformat(),
    )

    # 100% missing with cap=8.0%
    with pytest.raises(DataError) as exc_info:
        infer_market_closures(
            manifest=manifest,
            data_service=mock_ds,
            exchange="NSE",
            start_d=start_d,
            end_d=end_d,
            sample_symbols=sample_symbols,
            closure_max_pct=8.0,
        )
    assert "exceed safety cap data.closure_max_pct" in str(exc_info.value)
    # Manifest must have zero records written
    assert len(manifest.list_market_closures(exchange="NSE")) == 0


def test_cp1_safety_cap_within_limit_writes_closures(tmp_path: Path) -> None:
    """Step 3: Within safety cap, inferred closures are written."""
    manifest = DataManifest(tmp_path / "manifest.db")
    sample_symbols = ["NSE:EQ:SYM1"]
    start_d = date(2024, 5, 1)
    end_d = date(2024, 5, 20)  # 14 weekdays

    mock_ds = MagicMock()
    mock_ds._exchange_tz = UTC

    # Only 1 day has empty bars, others have bars
    def _mock_get(*args: Any, **kwargs: Any) -> DataSlice:
        bars = [
            _make_bar("NSE:EQ:SYM1", datetime(2024, 5, d, 9, 15, tzinfo=UTC))
            for d in range(1, 21)
            if d != 15 and date(2024, 5, d).weekday() < 5
        ]
        return DataSlice(
            key=SeriesKey("NSE:EQ:SYM1", "bars", "1d", "raw", "yfinance"),
            start=datetime(2024, 5, 1, tzinfo=UTC),
            end=datetime(2024, 5, 20, 23, 59, 59, tzinfo=UTC),
            bars=tuple(bars),
            gaps=(),
            dataset_ids=("ds1",),
            checksums=(),
            source="yfinance",
            adjustment="raw",
            fetched_at=datetime.now(UTC).isoformat(),
        )

    mock_ds.get.side_effect = _mock_get

    recs = infer_market_closures(
        manifest=manifest,
        data_service=mock_ds,
        exchange="NSE",
        start_d=start_d,
        end_d=end_d,
        sample_symbols=sample_symbols,
        closure_max_pct=15.0,  # 1/14 is ~7.1%, <= 15.0%
    )
    assert len(recs) == 1
    assert recs[0].day == "2024-05-15"
    assert len(manifest.list_market_closures(exchange="NSE")) == 1


def test_cp1_special_session_budget_day_2025_02_01_never_closure(tmp_path: Path) -> None:
    """Step 4: Union budget session 2025-02-01 (Saturday) is special session, never closure."""
    manifest = DataManifest(tmp_path / "manifest.db")
    sample_symbols = ["NSE:EQ:RELIANCE", "NSE:EQ:TCS", "NSE:EQ:INFY"]
    budget_d = date(2025, 2, 1)  # Saturday

    mock_ds = MagicMock()
    mock_ds._exchange_tz = UTC

    def _mock_get(series: Any, **kwargs: Any) -> DataSlice:
        sym = str(series.instrument if hasattr(series, "instrument") else series)
        return DataSlice(
            key=SeriesKey(sym, "bars", "1d", "raw", "yfinance"),
            start=datetime(2025, 2, 1, tzinfo=UTC),
            end=datetime(2025, 2, 1, 23, 59, 59, tzinfo=UTC),
            bars=(_make_bar(sym, datetime(2025, 2, 1, 9, 15, tzinfo=UTC)),),
            gaps=(),
            dataset_ids=("ds1",),
            checksums=(),
            source="yfinance",
            adjustment="raw",
            fetched_at=datetime.now(UTC).isoformat(),
        )

    mock_ds.get.side_effect = _mock_get
    cal = SimpleCalendar(closed_days=set())  # Saturday is calendar closed

    recs = infer_market_closures(
        manifest=manifest,
        data_service=mock_ds,
        exchange="NSE",
        start_d=budget_d,
        end_d=budget_d,
        sample_symbols=sample_symbols,
        min_symbols=3,
        calendar=cal,
    )
    assert len(recs) == 1
    assert recs[0].kind == "special"
    assert recs[0].day == "2025-02-01"


def test_cp1_special_session_requires_min_symbols(tmp_path: Path) -> None:
    """Step 4: Fewer than min_symbols on a calendar-closed day does NOT become special session."""
    manifest = DataManifest(tmp_path / "manifest.db")
    sample_symbols = ["NSE:EQ:SYM1", "NSE:EQ:SYM2", "NSE:EQ:SYM3"]
    sat_d = date(2025, 2, 1)

    mock_ds = MagicMock()
    mock_ds._exchange_tz = UTC

    def _mock_get(series: Any, **kwargs: Any) -> DataSlice:
        sym = str(series.instrument if hasattr(series, "instrument") else series)
        # Only SYM1 has bars (1 symbol < min 3)
        bars = (
            (_make_bar(sym, datetime(2025, 2, 1, 9, 15, tzinfo=UTC)),)
            if sym == "NSE:EQ:SYM1"
            else ()
        )
        return DataSlice(
            key=SeriesKey(sym, "bars", "1d", "raw", "yfinance"),
            start=datetime(2025, 2, 1, tzinfo=UTC),
            end=datetime(2025, 2, 1, 23, 59, 59, tzinfo=UTC),
            bars=bars,
            gaps=(),
            dataset_ids=("ds1",),
            checksums=(),
            source="yfinance",
            adjustment="raw",
            fetched_at=datetime.now(UTC).isoformat(),
        )

    mock_ds.get.side_effect = _mock_get
    cal = SimpleCalendar(closed_days=set())

    recs = infer_market_closures(
        manifest=manifest,
        data_service=mock_ds,
        exchange="NSE",
        start_d=sat_d,
        end_d=sat_d,
        sample_symbols=sample_symbols,
        min_symbols=3,
        calendar=cal,
    )
    assert len(recs) == 0


def test_cp1_closures_infer_dry_run_cli(tmp_project: Path) -> None:
    """Step 5: closures infer --dry-run outputs preview without writing to manifest."""
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
            "2024-05-20",
            "--to",
            "2024-05-20",
            "--dry-run",
        ],
    )
    assert res.exit_code == 0
    m = DataManifest(tmp_project / "data" / "manifest.db")
    assert len(m.list_market_closures()) == 0


def test_cp1_closures_clear_removes_rows_with_journal(tmp_project: Path) -> None:
    """Step 5: closures clear removes records and records a journal event."""
    m = DataManifest(tmp_project / "data" / "manifest.db")
    m.record_market_closure("NSE", "2024-05-20", "inferred", evidence="test")
    m.record_market_closure("NSE", "2025-02-01", "special", evidence="budget")
    assert len(m.list_market_closures()) == 2

    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "data",
            "closures",
            "clear",
            "--exchange",
            "NSE",
        ],
    )
    assert res.exit_code == 0
    assert "Cleared 2 market closure record(s)" in res.output
    assert len(m.list_market_closures()) == 0


def test_cp1_closures_clear_with_inferred_filter(tmp_project: Path) -> None:
    """Step 5: closures clear --inferred removes only inferred closures."""
    m = DataManifest(tmp_project / "data" / "manifest.db")
    m.record_market_closure("NSE", "2024-05-20", "inferred", evidence="test")
    m.record_market_closure("NSE", "2025-02-01", "special", evidence="budget")

    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "data",
            "closures",
            "clear",
            "--inferred",
        ],
    )
    assert res.exit_code == 0
    assert "Cleared 1 market closure record(s)" in res.output
    remaining = m.list_market_closures()
    assert len(remaining) == 1
    assert remaining[0].kind == "special"


def test_cp1_inferred_closure_without_all_samples_does_not_suppress_gaps(tmp_path: Path) -> None:
    """Step 6: Inferred closure without evidence from all sample symbols does not suppress gaps."""
    manifest = DataManifest(tmp_path / "manifest.db")
    # Evidence with only 2 of 5 symbols missing
    ev = json.dumps(
        {
            "type": "inferred_closure",
            "sample_count": 5,
            "missing_symbols": ["S1", "S2"],
            "all_samples": False,
        }
    )
    manifest.record_market_closure("NSE", "2024-05-20", "inferred", evidence=ev)

    cov = CoverageIndex(manifest=manifest)
    gaps = cov.detect_gaps(
        "NSE:EQ:RELIANCE:bars:1d:raw:yfinance", date(2024, 5, 20), date(2024, 5, 20)
    )
    # It must NOT be suppressed as closed; reported as gap
    assert len(gaps) == 1
    assert gaps[0].day == date(2024, 5, 20)


def test_cp1_inferred_closure_without_all_samples_does_not_suppress_planner(tmp_path: Path) -> None:
    """Step 6: Inferred closure without all samples does not suppress fetching missing days."""
    manifest = DataManifest(tmp_path / "manifest.db")
    ev = json.dumps(
        {
            "type": "inferred_closure",
            "sample_count": 5,
            "missing_symbols": ["S1"],
            "all_samples": False,
        }
    )
    manifest.record_market_closure("NSE", "2024-05-20", "inferred", evidence=ev)

    key = SeriesKey("NSE:EQ:RELIANCE", "bars", "1d", "raw", "yfinance")
    plan = GapPlanner.plan(
        key=key,
        start=datetime(2024, 5, 20, 0, 0, tzinfo=UTC),
        end=datetime(2024, 5, 20, 23, 59, tzinfo=UTC),
        manifest=manifest,
        exchange_tz=UTC,
    )
    assert date(2024, 5, 20) in plan.missing_days
    assert date(2024, 5, 20) not in plan.closed_inferred_days


def test_cp1_data_gaps_cli_prints_inferred_closures_count(tmp_project: Path) -> None:
    """Step 6: data gaps prints how many days relied on inferred closures."""
    m = DataManifest(tmp_project / "data" / "manifest.db")
    ev = json.dumps(
        {
            "type": "inferred_closure",
            "sample_count": 5,
            "missing_symbols": ["S1", "S2", "S3", "S4", "S5"],
            "all_samples": True,
        }
    )
    m.record_market_closure("NSE", "2024-05-20", "inferred", evidence=ev)

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
            "2024-05-20",
            "--to",
            "2024-05-20",
            "--timeframe",
            "1d",
        ],
    )
    assert res.exit_code == 0
    assert "1 day(s) relied on inferred closures" in res.output


def test_cp1_data_gaps_cli_warns_above_5_percent(tmp_project: Path) -> None:
    """Step 6: data gaps warns when inferred closures exceed 5% of the range."""
    m = DataManifest(tmp_project / "data" / "manifest.db")
    ev = json.dumps({"all_samples": True, "sample_count": 1, "missing_symbols": ["S1"]})
    m.record_market_closure("NSE", "2024-05-20", "inferred", evidence=ev)

    runner = CliRunner()
    # 1 closure in a 1-day range is 100% > 5%
    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "data",
            "gaps",
            "RELIANCE",
            "--from",
            "2024-05-20",
            "--to",
            "2024-05-20",
            "--timeframe",
            "1d",
        ],
    )
    assert res.exit_code == 0
    assert (
        "Warning: 1 days (100.0%) relied on inferred closures, exceeding 5% threshold."
        in res.output
    )


def test_cp1_doctor_closure_disagreements_warning_not_critical(tmp_path: Path) -> None:
    """Step 7: Doctor closure disagreements check produces WARNING with count, never CRITICAL."""
    from algodesk.ops.runtime import open_runtime

    with open_runtime(root=tmp_path) as rt:
        m_path = rt.paths.data / "state" / "data_manifest.db"
        m = DataManifest(m_path)
        # Add an unresolved disagreement: calendar is open on Monday, but marked inferred closure
        ev = json.dumps({"all_samples": True, "sample_count": 1, "missing_symbols": ["S1"]})
        m.record_market_closure("NSE", "2024-05-20", "inferred", evidence=ev)

        res = _check_market_closure_disagreements(rt)
        assert res.ok is False
        assert res.severity == Severity.WARNING
        assert "unresolved market closure disagreement(s) found" in res.message


# ===========================================================================
# CP2 & Steps 8 to 11: Corporate Actions Reconciliation
# ===========================================================================


def test_cp2_unit_normalisation_2024_08_19_dividend_confirms() -> None:
    """Step 8: 2024-08-19 (NSE 10, yfinance 5.0) confirms via later 2x bonus."""
    rec = CorporateActionsReconciler(dividend_tolerance_pct=1.0)
    # Event 1: Dividend 2024-08-19
    d_nse = ActionRecord(
        ex_date=date(2024, 8, 19),
        type="dividend",
        amount=Decimal("10"),
        source="nse",
        symbol="RELIANCE",
        exchange="NSE",
    )
    d_yf = ActionRecord(
        ex_date=date(2024, 8, 19),
        type="dividend",
        amount=Decimal("5.0"),
        source="yfinance",
        symbol="RELIANCE",
        exchange="NSE",
    )
    # Later event: Bonus 2024-10-28 1:1 (factor 2.0)
    b_nse = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=1.0,
        ratio_new=1.0,
        ratio_old=1.0,
        source="nse",
        symbol="RELIANCE",
        exchange="NSE",
    )

    results = rec.reconcile("NSE", "RELIANCE", [d_nse, d_yf, b_nse])
    div_rec = next(r for r in results if r.ex_date == date(2024, 8, 19))
    assert div_rec.status == "confirmed"
    assert "normalised to Rs 5 via later split/bonus factor 2" in div_rec.reason


def test_cp2_unit_normalisation_2011_05_05_dividend_confirms() -> None:
    """Step 8: 2011-05-05 (NSE 8, yfinance 1.98122) confirms via cumulative ~4x later factor."""
    rec = CorporateActionsReconciler(dividend_tolerance_pct=1.0)
    d_nse = ActionRecord(
        ex_date=date(2011, 5, 5),
        type="dividend",
        amount=Decimal("8"),
        source="nse",
        symbol="RELIANCE",
        exchange="NSE",
    )
    d_yf = ActionRecord(
        ex_date=date(2011, 5, 5),
        type="dividend",
        amount=Decimal("1.98122"),
        source="yfinance",
        symbol="RELIANCE",
        exchange="NSE",
    )
    # Later events: 2017 bonus 1:1 and 2024 bonus 1:1
    b1 = ActionRecord(
        ex_date=date(2017, 9, 7),
        type="bonus",
        ratio=1.0,
        ratio_new=1.0,
        ratio_old=1.0,
        source="nse",
        symbol="RELIANCE",
        exchange="NSE",
    )
    b2 = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=1.0,
        ratio_new=1.0,
        ratio_old=1.0,
        source="nse",
        symbol="RELIANCE",
        exchange="NSE",
    )

    results = rec.reconcile("NSE", "RELIANCE", [d_nse, d_yf, b1, b2])
    div_rec = next(r for r in results if r.ex_date == date(2011, 5, 5))
    assert div_rec.status == "confirmed"
    assert "normalised to Rs 2 via later split/bonus factor 4" in div_rec.reason


def test_cp2_unit_normalisation_beyond_tolerance_conflicts() -> None:
    """Step 8: Dividends differing beyond dividend_tolerance_pct become conflict."""
    rec = CorporateActionsReconciler(dividend_tolerance_pct=1.0)
    d_nse = ActionRecord(
        ex_date=date(2024, 8, 19),
        type="dividend",
        amount=Decimal("10"),
        source="nse",
        symbol="RELIANCE",
        exchange="NSE",
    )
    d_yf = ActionRecord(
        ex_date=date(2024, 8, 19),
        type="dividend",
        amount=Decimal("4.0"),  # 20% difference from expected 5.0
        source="yfinance",
        symbol="RELIANCE",
        exchange="NSE",
    )
    b = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=1.0,
        ratio_new=1.0,
        ratio_old=1.0,
        source="nse",
        symbol="RELIANCE",
        exchange="NSE",
    )

    results = rec.reconcile("NSE", "RELIANCE", [d_nse, d_yf, b])
    div_rec = next(r for r in results if r.ex_date == date(2024, 8, 19))
    assert div_rec.status == "conflict"
    assert div_rec.reason == "ratio disagreement"


def test_cp2_implied_split_evidence_bonus_2024_10_28_confirmed_implied() -> None:
    """Step 9: Implied split evidence from dividends confirms 2024-10-28 bonus."""
    rec = CorporateActionsReconciler(dividend_tolerance_pct=1.0)
    # Dividend before: 2024-08-19 NSE=10, YF=5.0 (scale 2.0)
    # Dividend after: 2025-08-14 NSE=5.5, YF=5.5 (scale 1.0)
    # Ratio = 2.0 / 1.0 = 2.0 (matches bonus 1:1)
    d_before_nse = ActionRecord(
        date(2024, 8, 19), "dividend", amount=Decimal("10"), source="nse", symbol="RELIANCE"
    )
    d_before_yf = ActionRecord(
        date(2024, 8, 19), "dividend", amount=Decimal("5.0"), source="yfinance", symbol="RELIANCE"
    )
    b_nse = ActionRecord(
        date(2024, 10, 28),
        "bonus",
        ratio=1.0,
        ratio_new=1.0,
        ratio_old=1.0,
        source="nse",
        symbol="RELIANCE",
    )
    d_after_nse = ActionRecord(
        date(2025, 8, 14), "dividend", amount=Decimal("5.5"), source="nse", symbol="RELIANCE"
    )
    d_after_yf = ActionRecord(
        date(2025, 8, 14), "dividend", amount=Decimal("5.5"), source="yfinance", symbol="RELIANCE"
    )

    results = rec.reconcile(
        "NSE", "RELIANCE", [d_before_nse, d_before_yf, b_nse, d_after_nse, d_after_yf]
    )
    b_rec = next(r for r in results if r.ex_date == date(2024, 10, 28))
    assert b_rec.status == "confirmed (implied)"
    assert "dividends before and after differ by factor 2" in b_rec.reason


def test_cp2_implied_split_evidence_bonus_2017_09_07_confirmed_implied() -> None:
    """Step 9: Implied split evidence confirms 2017-09-07 bonus."""
    rec = CorporateActionsReconciler(dividend_tolerance_pct=1.0)
    # Dividend before: 2017-07-13 NSE=11, YF=2.724178 (scale ~4.038)
    # Dividend after: 2018-06-27 NSE=6, YF=2.97183 (scale ~2.019)
    # Ratio = 4.038 / 2.019 = 2.0
    d_before_nse = ActionRecord(
        date(2017, 7, 13), "dividend", amount=Decimal("11"), source="nse", symbol="RELIANCE"
    )
    d_before_yf = ActionRecord(
        date(2017, 7, 13),
        "dividend",
        amount=Decimal("2.724178"),
        source="yfinance",
        symbol="RELIANCE",
    )
    b_nse = ActionRecord(
        date(2017, 9, 7),
        "bonus",
        ratio=1.0,
        ratio_new=1.0,
        ratio_old=1.0,
        source="nse",
        symbol="RELIANCE",
    )
    d_after_nse = ActionRecord(
        date(2018, 6, 27), "dividend", amount=Decimal("6"), source="nse", symbol="RELIANCE"
    )
    d_after_yf = ActionRecord(
        date(2018, 6, 27),
        "dividend",
        amount=Decimal("2.97183"),
        source="yfinance",
        symbol="RELIANCE",
    )

    results = rec.reconcile(
        "NSE", "RELIANCE", [d_before_nse, d_before_yf, b_nse, d_after_nse, d_after_yf]
    )
    b_rec = next(r for r in results if r.ex_date == date(2017, 9, 7))
    assert b_rec.status == "confirmed (implied)"


def test_cp2_implied_split_evidence_lacking_stays_unverified() -> None:
    """Step 9: Without implied evidence, single-source bonus stays unverified."""
    rec = CorporateActionsReconciler(dividend_tolerance_pct=1.0)
    # Only the bonus, no surrounding dividends
    b_nse = ActionRecord(
        date(2024, 10, 28),
        "bonus",
        ratio=1.0,
        ratio_new=1.0,
        ratio_old=1.0,
        source="nse",
        symbol="RELIANCE",
    )
    results = rec.reconcile("NSE", "RELIANCE", [b_nse])
    assert results[0].status == "unverified"
    assert "single-source bonus" in results[0].reason


def test_cp2_single_source_event_is_never_conflict() -> None:
    """Step 10: Single-source events are never conflict (unverified or manual_required)."""
    rec = CorporateActionsReconciler()
    a_div = ActionRecord(
        date(2024, 8, 19), "dividend", amount=Decimal("10"), source="nse", symbol="TCS"
    )
    a_other = ActionRecord(date(2023, 7, 20), "other", source="nse", symbol="TCS")

    res = rec.reconcile("NSE", "TCS", [a_div, a_other])
    assert res[0].status == "manual_required"  # 2023-07-20 other
    assert res[1].status == "unverified"  # 2024-08-19 dividend
    assert not any(r.status == "conflict" for r in res)


def test_cp2_nse_only_bonuses_reported_with_yfinance_gap_info() -> None:
    """Step 10: Fix NSE-only bonuses and report yfinance gap info."""
    rec = CorporateActionsReconciler()
    b_nse = ActionRecord(
        date(2024, 10, 28),
        "bonus",
        ratio=1.0,
        ratio_new=1.0,
        ratio_old=1.0,
        source="nse",
        symbol="RELIANCE",
    )
    res = rec.reconcile("NSE", "RELIANCE", [b_nse])
    assert res[0].status in ("unverified", "confirmed (implied)")
    assert "yfinance actions lack the NSE bonus of 2024-10-28" in res[0].reason


def test_cp2_actions_compare_shows_normalised_value_and_evidence_kind(tmp_project: Path) -> None:
    """Step 11: data actions compare shows normalised value and evidence kind column."""
    store = ActionsStore(data_dir=tmp_project / "data")
    d_nse = ActionRecord(
        date(2024, 8, 19), "dividend", amount=Decimal("10"), source="nse", symbol="RELIANCE"
    )
    d_yf = ActionRecord(
        date(2024, 8, 19), "dividend", amount=Decimal("5.0"), source="yfinance", symbol="RELIANCE"
    )
    b_nse = ActionRecord(
        date(2024, 10, 28),
        "bonus",
        ratio=1.0,
        ratio_new=1.0,
        ratio_old=1.0,
        source="nse",
        symbol="RELIANCE",
    )
    store.write("NSE", "RELIANCE", [d_nse, d_yf, b_nse])

    runner = CliRunner()
    res = runner.invoke(app, ["--root", str(tmp_project), "data", "actions", "compare", "RELIANCE"])
    assert res.exit_code == 0
    out = res.output
    assert "Evidence" in out
    assert "normalised" in out
    assert "norm: 5" in out


def test_cp2_actions_compare_summary_counts_full_taxonomy(tmp_project: Path) -> None:
    """Step 11: data actions compare prints full taxonomy summary counts."""
    store = ActionsStore(data_dir=tmp_project / "data")
    d = ActionRecord(
        date(2025, 8, 14), "dividend", amount=Decimal("5.5"), source="nse", symbol="RELIANCE"
    )
    store.write("NSE", "RELIANCE", [d])

    runner = CliRunner()
    res = runner.invoke(app, ["--root", str(tmp_project), "data", "actions", "compare", "RELIANCE"])
    assert res.exit_code == 0
    out = res.output
    assert "Summary counts: confirmed=" in out
    assert "confirmed (implied)=" in out
    assert "unverified=" in out
    assert "conflict=" in out
    assert "manual_required=" in out


def test_cp2_reconciliation_on_full_reliance_real_fixtures() -> None:
    """Step 8, 9, 10, 11: Real fixtures for RELIANCE reconcile with zero conflicts."""
    nse_acts, yf_acts = _load_real_reliance_actions()
    reconciler = CorporateActionsReconciler(dividend_tolerance_pct=1.0)
    all_raw = nse_acts + yf_acts

    results = reconciler.reconcile("NSE", "RELIANCE", all_raw)
    statuses = [r.status for r in results]

    # No conflicts
    assert "conflict" not in statuses
    # Both bonuses are confirmed (implied)
    b_2017 = next(r for r in results if r.ex_date == date(2017, 9, 7))
    assert b_2017.type == "bonus"
    assert b_2017.status == "confirmed (implied)"

    b_2024 = next(r for r in results if r.ex_date == date(2024, 10, 28))
    assert b_2024.type == "bonus"
    assert b_2024.status == "confirmed (implied)"

    # Dividends are confirmed
    d_2024 = next(r for r in results if r.ex_date == date(2024, 8, 19))
    assert d_2024.status == "confirmed"

    d_2011 = next(r for r in results if r.ex_date == date(2011, 5, 5))
    assert d_2011.status == "confirmed"
