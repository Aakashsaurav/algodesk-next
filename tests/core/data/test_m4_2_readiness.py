"""Tests for Step 4 & 5: data/readiness.py per-symbol checks and exact remediation commands."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from algodesk.api.manifest import PluginManifest
from algodesk.api.market import Bar, CoverageStatus, InstrumentId, SeriesKey
from algodesk.data.manifest import CoverageDayRecord, DataManifest, QuarantineRecord
from algodesk.data.readiness import check_backtest_readiness, check_symbol_readiness
from algodesk.data.service import DataService
from algodesk.data.store import BarStore


class CountingFakeSource:
    """Fake data source that counts history calls to verify cache-only execution."""

    def __init__(self, bars_map: dict[str, list[Bar]]) -> None:
        self.bars_map = bars_map
        self.call_count = 0
        self.manifest = PluginManifest(
            name="fake_broker",
            version="1.0.0",
            kind="broker",
            api_version=1,
            entry="fake:Broker",
            capabilities={"history": True},
            retention={"1d": 36500},
            max_span={"1d": 3650},
            candles_adjustment_basis="splits",
        )

    def history(self, key: SeriesKey, start: datetime, end: datetime) -> Iterable[Bar]:
        self.call_count += 1
        inst_str = str(key.instrument)
        bars = self.bars_map.get(inst_str, [])
        return [b for b in bars if start <= b.start_ts <= end]

    def instruments(self) -> Iterable[Any]:
        return []

    def stream(self, insts: Any) -> Any:
        raise NotImplementedError


class FakeCalendar:
    """Calendar implementation for testing trading sessions."""

    def __init__(
        self, holidays: set[date] | None = None, specials: set[date] | None = None
    ) -> None:
        self.holidays = holidays or set()
        self.specials = specials or set()

    def is_trading_day(self, d: date) -> bool:
        if d in self.specials:
            return True
        if d in self.holidays:
            return False
        return d.weekday() < 5


def _make_bar(inst: InstrumentId, d: date, price: float = 100.0) -> Bar:
    start_ts = datetime(d.year, d.month, d.day, 9, 15, tzinfo=UTC)
    end_ts = datetime(d.year, d.month, d.day, 15, 30, tzinfo=UTC)
    return Bar(
        instrument=inst,
        timeframe="1d",
        start_ts=start_ts,
        end_ts=end_ts,
        open=price,
        high=price + 2.0,
        low=price - 2.0,
        close=price,
        volume=1000,
        complete=True,
        synthetic=False,
    )


def _make_cov(
    ds_id: str,
    day_str: str,
    status: Any = CoverageStatus.COMPLETE,
    rows: int = 1,
    note: str = "",
) -> CoverageDayRecord:
    return CoverageDayRecord(
        dataset_id=ds_id,
        day=day_str,
        status=str(status),
        rows=rows,
        expected_rows=rows,
        part_path="part.parquet",
        fetched_at=f"{day_str}T16:00:00Z",
        note=note,
    )


@pytest.fixture
def readiness_setup(
    tmp_path: Any,
) -> tuple[DataService, DataManifest, CountingFakeSource, FakeCalendar]:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    m_path = data_dir / "state" / "data_manifest.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(m_path)
    store = BarStore(data_dir=data_dir, manifest=manifest)
    fake_src = CountingFakeSource({})
    ds = DataService(sources={"upstox": fake_src}, default_source="upstox", store=store)
    cal = FakeCalendar()
    return ds, manifest, fake_src, cal


def test_readiness_all_symbols_ready_coverage_100(readiness_setup: Any) -> None:
    """Symbols with all trading sessions stored pass with 100% coverage."""
    ds, manifest, fake_src, cal = readiness_setup
    inst = InstrumentId("NSE:EQ:RELIANCE")

    days = [date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 8)]
    bars = [_make_bar(inst, d) for d in days]
    ds.store.commit(SeriesKey(inst, "bars", "1d", "adjusted", "upstox"), bars)

    cov_recs = [
        _make_cov(f"{inst}_1d_upstox", d.isoformat(), CoverageStatus.COMPLETE, 1) for d in days
    ]
    manifest.record_coverage_days(cov_recs)

    report = check_backtest_readiness(
        symbols=[str(inst)],
        start=date(2025, 1, 6),
        end=date(2025, 1, 8),
        data_service=ds,
        manifest=manifest,
        calendar=cal,
        timeframe="1d",
        source="upstox",
        warmup_bars_count=0,
    )

    assert report.coverage_pct == 100.0
    assert report.missing_pct == 0.0
    assert len(report.with_data) == 1
    assert len(report.skipped) == 0
    assert report.symbol_reports[str(inst)].is_ready


def test_readiness_missing_day_detected(readiness_setup: Any) -> None:
    """Missing trading sessions inside backtest range are flagged."""
    ds, manifest, fake_src, cal = readiness_setup
    inst = InstrumentId("NSE:EQ:TCS")

    # Day 2025-01-07 is missing
    days = [date(2025, 1, 6), date(2025, 1, 8)]
    bars = [_make_bar(inst, d) for d in days]
    ds.store.commit(SeriesKey(inst, "bars", "1d", "adjusted", "upstox"), bars)

    report = check_backtest_readiness(
        symbols=[str(inst)],
        start=date(2025, 1, 6),
        end=date(2025, 1, 8),
        data_service=ds,
        manifest=manifest,
        calendar=cal,
        timeframe="1d",
        source="upstox",
        warmup_bars_count=0,
    )

    sr = report.symbol_reports[str(inst)]
    assert not sr.is_ready
    assert date(2025, 1, 7) in sr.missing_sessions
    assert sr.primary_skip_reason == "missing_sessions"
    assert report.missing_pct == 100.0


def test_readiness_pending_day_detected(readiness_setup: Any) -> None:
    """Coverage day recorded as PENDING is flagged as readiness problem."""
    ds, manifest, fake_src, cal = readiness_setup
    inst = InstrumentId("NSE:EQ:INFY")

    days = [date(2025, 1, 6), date(2025, 1, 7)]
    bars = [_make_bar(inst, d) for d in days]
    ds.store.commit(SeriesKey(inst, "bars", "1d", "adjusted", "upstox"), bars)

    cov_recs = [
        _make_cov(f"{inst}_1d_upstox", days[0].isoformat(), CoverageStatus.COMPLETE, 1),
        _make_cov(f"{inst}_1d_upstox", days[1].isoformat(), CoverageStatus.PENDING, 0),
    ]
    manifest.record_coverage_days(cov_recs)

    report = check_backtest_readiness(
        symbols=[str(inst)],
        start=date(2025, 1, 6),
        end=date(2025, 1, 7),
        data_service=ds,
        manifest=manifest,
        calendar=cal,
        timeframe="1d",
        source="upstox",
        warmup_bars_count=0,
    )

    sr = report.symbol_reports[str(inst)]
    assert not sr.is_ready
    assert days[1] in sr.pending_empty_days
    assert sr.primary_skip_reason == "pending_empty"


def test_readiness_non_final_empty_detected(readiness_setup: Any) -> None:
    """An EMPTY record that is non-final (fetched before session end / settle) is flagged."""
    ds, manifest, fake_src, cal = readiness_setup
    inst = InstrumentId("NSE:EQ:NEWCO")

    days = [date(2025, 1, 6), date(2025, 1, 7)]
    # Fetched at 04:00 UTC (09:30 IST), well before session end + settle margin
    cov_recs = [
        CoverageDayRecord(
            dataset_id=f"bars:upstox:1d:{inst}:raw",
            day=d.isoformat(),
            status=str(CoverageStatus.EMPTY),
            rows=0,
            expected_rows=0,
            part_path="part.parquet",
            fetched_at=f"{d.isoformat()}T04:00:00Z",
            note="",
        )
        for d in days
    ]
    manifest.record_coverage_days(cov_recs)

    report = check_backtest_readiness(
        symbols=[str(inst)],
        start=date(2025, 1, 6),
        end=date(2025, 1, 7),
        data_service=ds,
        manifest=manifest,
        calendar=cal,
        timeframe="1d",
        source="upstox",
        warmup_bars_count=0,
    )

    sr = report.symbol_reports[str(inst)]
    assert not sr.is_ready
    assert len(sr.pending_empty_days) > 0


def test_readiness_final_empty_ignored(readiness_setup: Any) -> None:
    """A final EMPTY pre-listing session (where later bars exist) is not a missing gap."""
    ds, manifest, fake_src, cal = readiness_setup
    inst = InstrumentId("NSE:EQ:IPO_STOCK")

    # Bar on day 2
    day1 = date(2025, 1, 6)
    day2 = date(2025, 1, 7)
    ds.store.commit(SeriesKey(inst, "bars", "1d", "adjusted", "upstox"), [_make_bar(inst, day2)])

    cov_recs = [
        _make_cov(
            f"bars:upstox:1d:{inst}:raw",
            day1.isoformat(),
            CoverageStatus.EMPTY,
            0,
            note="pre-listing",
        ),
        _make_cov(f"bars:upstox:1d:{inst}:raw", day2.isoformat(), CoverageStatus.COMPLETE, 1),
    ]
    manifest.record_coverage_days(cov_recs)

    sr = check_symbol_readiness(
        symbol=str(inst),
        data_service=ds,
        manifest=manifest,
        calendar=cal,
        expected_sessions=[day1, day2],
        start=day1,
        end=day2,
        timeframe="1d",
        source="upstox",
        warmup_bars_count=0,
    )

    assert day1 not in sr.missing_sessions
    assert sr.is_ready


def test_readiness_quarantined_day_detected(readiness_setup: Any) -> None:
    """Quarantined chunk for a symbol is flagged."""
    ds, manifest, fake_src, cal = readiness_setup
    inst = InstrumentId("NSE:EQ:BAD_TICK")

    manifest.record_quarantine(
        QuarantineRecord(
            id="q_12345",
            chunk=f"{inst} 1d (10 bars)",
            reason="Bad timestamps",
            report_path="report.json",
            created="2025-01-06T10:00:00Z",
        )
    )

    sr = check_symbol_readiness(
        symbol=str(inst),
        data_service=ds,
        manifest=manifest,
        calendar=cal,
        expected_sessions=[date(2025, 1, 6)],
        start=date(2025, 1, 6),
        end=date(2025, 1, 6),
        timeframe="1d",
        source="upstox",
        warmup_bars_count=0,
    )

    assert len(sr.quarantined_days) == 1
    assert sr.primary_skip_reason == "quarantined"


def test_readiness_coverage_without_bars_detected(readiness_setup: Any) -> None:
    """Day marked COMPLETE in coverage manifest but 0 bars stored is flagged."""
    ds, manifest, fake_src, cal = readiness_setup
    inst = InstrumentId("NSE:EQ:GHOST")

    d = date(2025, 1, 6)
    cov = _make_cov(f"{inst}_1d_upstox", d.isoformat(), CoverageStatus.COMPLETE, 10)
    manifest.record_coverage_days([cov])

    sr = check_symbol_readiness(
        symbol=str(inst),
        data_service=ds,
        manifest=manifest,
        calendar=cal,
        expected_sessions=[d],
        start=d,
        end=d,
        timeframe="1d",
        source="upstox",
        warmup_bars_count=0,
    )

    assert d in sr.coverage_without_bars
    assert sr.primary_skip_reason == "coverage_without_bars"


def test_readiness_insufficient_warmup_detected(readiness_setup: Any) -> None:
    """When warm-up count > stored bars before start, insufficient_warmup is raised."""
    ds, manifest, fake_src, cal = readiness_setup
    inst = InstrumentId("NSE:EQ:WARM")

    # 5 bars before start, but 20 needed
    pre_days = [date(2025, 1, 1) + timedelta(days=i) for i in range(5)]
    bars = [_make_bar(inst, d) for d in pre_days]
    ds.store.commit(SeriesKey(inst, "bars", "1d", "adjusted", "upstox"), bars)

    start_d = date(2025, 1, 10)
    end_d = date(2025, 1, 12)

    sr = check_symbol_readiness(
        symbol=str(inst),
        data_service=ds,
        manifest=manifest,
        calendar=cal,
        expected_sessions=[start_d],
        start=start_d,
        end=end_d,
        timeframe="1d",
        source="upstox",
        warmup_bars_count=20,
    )

    assert sr.insufficient_warmup
    assert sr.warmup_bars_found == 5
    assert sr.warmup_bars_needed == 20
    assert sr.primary_skip_reason == "insufficient_warmup"


def test_readiness_sufficient_warmup_passes(readiness_setup: Any) -> None:
    """When enough bars exist before start, warm-up check passes."""
    ds, manifest, fake_src, cal = readiness_setup
    inst = InstrumentId("NSE:EQ:WARM_OK")

    pre_days = [date(2025, 1, 1) + timedelta(days=i) for i in range(15)]
    active_days = [date(2025, 1, 20), date(2025, 1, 21)]
    bars = [_make_bar(inst, d) for d in pre_days + active_days]
    ds.store.commit(SeriesKey(inst, "bars", "1d", "adjusted", "upstox"), bars)

    sr = check_symbol_readiness(
        symbol=str(inst),
        data_service=ds,
        manifest=manifest,
        calendar=cal,
        expected_sessions=active_days,
        start=date(2025, 1, 20),
        end=date(2025, 1, 21),
        timeframe="1d",
        source="upstox",
        warmup_bars_count=10,
    )

    assert not sr.insufficient_warmup
    assert sr.is_ready


def test_readiness_exact_fix_command_with_group(readiness_setup: Any) -> None:
    """Exact accumulate fix command names group and computes days N from missing range."""
    ds, manifest, fake_src, cal = readiness_setup
    inst = InstrumentId("NSE:EQ:FIXTEST")

    # Start 60 days ago
    today = date.today()
    start_d = today - timedelta(days=60)
    end_d = today - timedelta(days=10)

    sr = check_symbol_readiness(
        symbol=str(inst),
        data_service=ds,
        manifest=manifest,
        calendar=cal,
        expected_sessions=[start_d, end_d],
        start=start_d,
        end=end_d,
        timeframe="1d",
        source="upstox",
        warmup_bars_count=0,
        group="demo5",
    )

    assert len(sr.fix_commands) == 1
    cmd = sr.fix_commands[0]
    assert "--group demo5" in cmd
    assert "--source upstox" in cmd
    assert "--timeframe 1d" in cmd
    assert f"--days {60 + 1}" in cmd or f"--days {60}" in cmd


def test_readiness_exact_fix_command_with_symbols(readiness_setup: Any) -> None:
    """Exact accumulate fix command uses --symbols when group is None."""
    ds, manifest, fake_src, cal = readiness_setup
    inst = InstrumentId("NSE:EQ:SOLO")

    today = date.today()
    start_d = today - timedelta(days=30)
    end_d = today - timedelta(days=5)

    sr = check_symbol_readiness(
        symbol=str(inst),
        data_service=ds,
        manifest=manifest,
        calendar=cal,
        expected_sessions=[start_d],
        start=start_d,
        end=end_d,
        timeframe="1d",
        source="upstox",
        warmup_bars_count=0,
        group=None,
    )

    cmd = sr.fix_commands[0]
    assert f"--symbols {inst}" in cmd or "--symbols NSE:EQ:SOLO" in cmd


def test_readiness_zero_network_calls(readiness_setup: Any) -> None:
    """Readiness check executes 100% cache-only and makes zero network calls."""
    ds, manifest, fake_src, cal = readiness_setup
    inst = InstrumentId("NSE:EQ:AIRGAP")

    d = date(2025, 1, 6)
    ds.store.commit(SeriesKey(inst, "bars", "1d", "adjusted", "upstox"), [_make_bar(inst, d)])

    check_backtest_readiness(
        symbols=[str(inst)],
        start=d,
        end=d,
        data_service=ds,
        manifest=manifest,
        calendar=cal,
        timeframe="1d",
        source="upstox",
    )

    assert fake_src.call_count == 0


def test_readiness_first_and_last_stored_day(readiness_setup: Any) -> None:
    """Reports exact first and last stored day for symbols."""
    ds, manifest, fake_src, cal = readiness_setup
    inst = InstrumentId("NSE:EQ:RANGE")

    d1 = date(2025, 1, 6)
    d2 = date(2025, 1, 10)
    ds.store.commit(
        SeriesKey(inst, "bars", "1d", "adjusted", "upstox"),
        [_make_bar(inst, d1), _make_bar(inst, d2)],
    )

    sr = check_symbol_readiness(
        symbol=str(inst),
        data_service=ds,
        manifest=manifest,
        calendar=cal,
        expected_sessions=[d1, d2],
        start=d1,
        end=d2,
        timeframe="1d",
        source="upstox",
    )

    assert sr.first_stored_day == d1
    assert sr.last_stored_day == d2


def test_readiness_json_schema_conformant(readiness_setup: Any) -> None:
    """ReadinessReport.to_dict matches the required readiness.json specification."""
    ds, manifest, fake_src, cal = readiness_setup
    inst = InstrumentId("NSE:EQ:SCHEMA")

    d = date(2025, 1, 6)
    ds.store.commit(SeriesKey(inst, "bars", "1d", "adjusted", "upstox"), [_make_bar(inst, d)])

    report = check_backtest_readiness(
        symbols=[str(inst)],
        start=d,
        end=d,
        data_service=ds,
        manifest=manifest,
        calendar=cal,
        warmup_bars_count=0,
        group="demo5",
        as_of="2025-01-06",
        universe_hash="abcdef0123456789",
    )

    data = report.to_dict()
    assert data["version"] == 1
    assert data["range"] == {"start": "2025-01-06", "end": "2025-01-06"}
    u = data["universe"]
    assert u["group"] == "demo5"
    assert u["as_of"] == "2025-01-06"
    assert u["hash"] == "abcdef0123456789"
    assert u["members"] == [str(inst)]
    assert u["with_data"] == [str(inst)]
    assert u["coverage_pct"] == 100.0
    assert u["skipped"] == []
    assert data["missing_pct"] == 0.0
    assert data["problems"] == []
