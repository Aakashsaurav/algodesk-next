"""Tests for M2-11: data compare on real/recorded ticks and clear refusal when a side is empty."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow.parquet as pq
import pytest
from typer.testing import CliRunner

from algodesk.api.errors import DataError
from algodesk.api.manifest import PluginManifest
from algodesk.api.market import Bar, InstrumentId, SeriesKey, Tick
from algodesk.api.protocols import Calendar, DataSource, TradingSession
from algodesk.cli.app import app
from algodesk.data.compare import (
    _build_bars_from_ticks,
    _compare_bar_series,
    compare_ticks_with_source,
)
from algodesk.data.manifest import DataManifest, FileRecord
from algodesk.data.schema import events_to_table
from algodesk.data.service import DataService
from algodesk.data.store import BarStore
from algodesk.engine.bars import BarBuilder

_IST = ZoneInfo("Asia/Kolkata")


class FakeCalendar(Calendar):
    def is_trading_day(self, d: date, segment: str = "") -> bool:
        return d.weekday() < 5

    def sessions(self, d: date, segment: str = "") -> list[TradingSession]:
        s_start = datetime(d.year, d.month, d.day, 9, 15, tzinfo=_IST).astimezone(UTC)
        s_end = datetime(d.year, d.month, d.day, 15, 30, tzinfo=_IST).astimezone(UTC)
        return [TradingSession(name=f"sess_{d.isoformat()}", start=s_start, end=s_end)]

    def next_session(self, ts: datetime, segment: str = "") -> TradingSession:
        return self.sessions(ts.date())[0]


class FakeSourceBroker(DataSource):
    """Configurable fake source broker for compare tests."""

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
            retention={"1m": 30, "1d": 36500},
            max_span={"1m": 7, "1d": 3650},
        )

    def history(self, key: SeriesKey, start: datetime, end: datetime) -> Iterable[Bar]:
        return [b for b in self.bars if start <= b.start_ts <= end]

    def instruments(self) -> Iterable[Any]:
        return []

    def stream(self, instruments: Any) -> Any:
        raise NotImplementedError


def _make_test_ticks(
    inst: InstrumentId,
    session_date: date,
    start_hour_ist: int = 9,
    start_minute_ist: int = 15,
    minutes_count: int = 10,
    ticks_per_min: int = 12,
    base_price: float = 2500.0,
) -> list[Tick]:
    """Generate deterministic ticks across a sequence of minutes."""
    ticks: list[Tick] = []
    base_dt = datetime(
        session_date.year,
        session_date.month,
        session_date.day,
        start_hour_ist,
        start_minute_ist,
        tzinfo=_IST,
    ).astimezone(UTC)

    total_idx = 0
    for m in range(minutes_count):
        for s in range(ticks_per_min):
            t_dt = base_dt + timedelta(minutes=m, seconds=s * 5)
            p = base_price + (m * 0.5) + (s * 0.05)
            ticks.append(
                Tick(
                    instrument=inst,
                    exch_ts=t_dt,
                    recv_ts=t_dt,
                    ltp=round(p, 2),
                    ltq=10,
                    volume=10 * (total_idx + 1),
                )
            )
            total_idx += 1
    return ticks


def _write_tick_partition(
    data_dir: Path,
    manifest: DataManifest,
    inst: InstrumentId,
    partition_date: date,
    ticks: list[Tick],
) -> Path:
    """Write ticks into partitioned directory and register in manifest."""
    from algodesk.data.layout import instrument_partition_path
    from algodesk.data.store import _compute_sha256

    part = instrument_partition_path(inst)
    p_dir = data_dir / "ticks" / part / f"date={partition_date.isoformat()}"
    p_dir.mkdir(parents=True, exist_ok=True)
    f_path = p_dir / "day.parquet"

    tbl = events_to_table(ticks)
    pq.write_table(tbl, f_path)

    sha = _compute_sha256(f_path)
    first_ts = ticks[0].exch_ts.isoformat() if ticks else ""
    last_ts = ticks[-1].exch_ts.isoformat() if ticks else ""

    rec = FileRecord(
        path=f_path.as_posix(),
        instrument=str(inst),
        kind="ticks",
        timeframe="tick",
        date=partition_date.isoformat(),
        rows=len(ticks),
        bytes=f_path.stat().st_size,
        first_ts=first_ts,
        last_ts=last_ts,
        sha256=sha,
        source="live",
        state="day",
        created_ts=datetime.now(UTC).isoformat(),
    )
    manifest.record_file(rec)
    return f_path


def _make_builder(cb: Any) -> Any:
    return BarBuilder(timeframes=("1m",), fill_empty_bars=False, on_bar_closed=cb)


def _setup_service(tmp_project: Path, fake_broker: FakeSourceBroker) -> DataService:
    data_dir = tmp_project / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(m_path)
    store = BarStore(data_dir, manifest, params=None)
    return DataService(
        sources={"fake_src": fake_broker, "yfinance": fake_broker},
        default_source="fake_src",
        store=store,
        limiter=None,
        calendar=FakeCalendar(),
        params=None,
    )


def test_compare_exact_match(tmp_project: Any) -> None:
    data_dir = tmp_project / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(m_path)

    inst = InstrumentId("NSE:EQ:RELIANCE")
    d = date(2026, 11, 6)
    ticks = _make_test_ticks(inst, d, minutes_count=5)
    _write_tick_partition(data_dir, manifest, inst, d, ticks)

    built = _build_bars_from_ticks(ticks, builder_factory=_make_builder)
    assert len(built) == 5

    broker = FakeSourceBroker(name="fake_src", bars=built)
    ds = _setup_service(tmp_project, broker)

    rep = compare_ticks_with_source(
        date_val=d,
        symbols=["RELIANCE"],
        source="fake_src",
        runtime=tmp_project,
        data_service=ds,
        builder_factory=_make_builder,
    )

    assert len(rep.results) == 1
    r = rep.results[0]
    assert r.symbol == "RELIANCE"
    assert r.built_count == 5
    assert r.source_count == 5
    assert r.matched_count == 5
    assert r.built_only_count == 0
    assert r.source_only_count == 0
    assert r.max_close_diff == 0.0
    assert r.count_above_tolerance == 0
    assert r.built_first_ts == built[0].start_ts
    assert r.built_last_ts == built[-1].start_ts


def test_compare_price_differences(tmp_project: Any) -> None:
    data_dir = tmp_project / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(m_path)

    inst = InstrumentId("NSE:EQ:RELIANCE")
    d = date(2026, 11, 6)
    ticks = _make_test_ticks(inst, d, minutes_count=5)
    _write_tick_partition(data_dir, manifest, inst, d, ticks)
    built = _build_bars_from_ticks(ticks, builder_factory=_make_builder)

    # Source bars differ by price
    src_bars = [
        Bar(
            instrument=b.instrument,
            timeframe=b.timeframe,
            start_ts=b.start_ts,
            end_ts=b.end_ts,
            open=b.open,
            high=b.high + 25.0,
            low=b.low,
            close=b.close + 25.0,  # 1% diff
            volume=b.volume + 50,
            complete=True,
        )
        for b in built
    ]

    broker = FakeSourceBroker(name="fake_src", bars=src_bars)
    ds = _setup_service(tmp_project, broker)

    rep = compare_ticks_with_source(
        date_val=d,
        symbols=["NSE:EQ:RELIANCE"],
        source="fake_src",
        runtime=tmp_project,
        tolerance_pct=0.1,
        data_service=ds,
        builder_factory=_make_builder,
    )
    r = rep.results[0]
    assert r.matched_count == 5
    assert r.max_close_diff == 25.0
    assert r.count_above_tolerance == 5
    assert r.max_volume_diff == 50


def test_compare_missing_bars_source_has_more(tmp_project: Any) -> None:
    data_dir = tmp_project / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(m_path)

    inst = InstrumentId("NSE:EQ:RELIANCE")
    d = date(2026, 11, 6)
    ticks = _make_test_ticks(inst, d, minutes_count=6)
    _write_tick_partition(data_dir, manifest, inst, d, ticks)
    built = _build_bars_from_ticks(ticks, builder_factory=_make_builder)
    assert len(built) == 6

    # Source has 10 bars (built only has first 6)
    full_ticks = _make_test_ticks(inst, d, minutes_count=10)
    src_bars = _build_bars_from_ticks(full_ticks, builder_factory=_make_builder)
    assert len(src_bars) == 10

    broker = FakeSourceBroker(name="fake_src", bars=src_bars)
    ds = _setup_service(tmp_project, broker)

    rep = compare_ticks_with_source(
        date_val=d,
        symbols=["RELIANCE"],
        source="fake_src",
        runtime=tmp_project,
        data_service=ds,
        builder_factory=_make_builder,
    )
    r = rep.results[0]
    assert r.built_count == 6
    assert r.source_count == 10
    assert r.matched_count == 6
    assert r.built_only_count == 0
    assert r.source_only_count == 4


def test_compare_missing_bars_built_has_more(tmp_project: Any) -> None:
    data_dir = tmp_project / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(m_path)

    inst = InstrumentId("NSE:EQ:RELIANCE")
    d = date(2026, 11, 6)
    ticks = _make_test_ticks(inst, d, minutes_count=10)
    _write_tick_partition(data_dir, manifest, inst, d, ticks)
    built = _build_bars_from_ticks(ticks, builder_factory=_make_builder)
    assert len(built) == 10

    # Source only has first 6 bars
    src_bars = built[:6]

    broker = FakeSourceBroker(name="fake_src", bars=src_bars)
    ds = _setup_service(tmp_project, broker)

    rep = compare_ticks_with_source(
        date_val=d,
        symbols=["RELIANCE"],
        source="fake_src",
        runtime=tmp_project,
        data_service=ds,
        builder_factory=_make_builder,
    )
    r = rep.results[0]
    assert r.built_count == 10
    assert r.source_count == 6
    assert r.matched_count == 6
    assert r.built_only_count == 4
    assert r.source_only_count == 0


def test_refusal_when_no_ticks_recorded(tmp_project: Any) -> None:
    d = date(2026, 11, 6)
    inst = InstrumentId("NSE:EQ:RELIANCE")
    src_bar = Bar(
        instrument=inst,
        timeframe="1m",
        start_ts=datetime(2026, 11, 6, 4, 0, tzinfo=UTC),
        end_ts=datetime(2026, 11, 6, 4, 1, tzinfo=UTC),
        open=2500.0,
        high=2505.0,
        low=2495.0,
        close=2500.0,
        volume=100,
        complete=True,
    )
    broker = FakeSourceBroker(name="fake_src", bars=[src_bar])
    ds = _setup_service(tmp_project, broker)

    with pytest.raises(DataError) as exc:
        compare_ticks_with_source(
            date_val=d,
            symbols=["RELIANCE"],
            source="fake_src",
            runtime=tmp_project,
            data_service=ds,
            builder_factory=_make_builder,
        )
    assert "Refusing comparison" in exc.value.what
    assert "built side: no ticks recorded" in exc.value.what


def test_refusal_when_source_returns_nothing(tmp_project: Any) -> None:
    data_dir = tmp_project / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(m_path)

    inst = InstrumentId("NSE:EQ:RELIANCE")
    d = date(2026, 11, 6)
    ticks = _make_test_ticks(inst, d, minutes_count=5)
    _write_tick_partition(data_dir, manifest, inst, d, ticks)

    # Fake broker returns empty
    broker = FakeSourceBroker(name="fake_src", bars=[])
    ds = _setup_service(tmp_project, broker)

    with pytest.raises(DataError) as exc:
        compare_ticks_with_source(
            date_val=d,
            symbols=["RELIANCE"],
            source="fake_src",
            runtime=tmp_project,
            data_service=ds,
            builder_factory=_make_builder,
        )
    assert "Refusing comparison" in exc.value.what
    assert "source side: source returned nothing" in exc.value.what


def test_refusal_when_date_beyond_source_retention(tmp_project: Any) -> None:
    data_dir = tmp_project / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(m_path)

    inst = InstrumentId("NSE:EQ:RELIANCE")
    old_date = date(2024, 1, 10)  # Beyond yfinance 7d retention
    ticks = _make_test_ticks(inst, old_date, minutes_count=5)
    _write_tick_partition(data_dir, manifest, inst, old_date, ticks)

    broker = FakeSourceBroker(name="yfinance", bars=[])
    ds = _setup_service(tmp_project, broker)

    with pytest.raises(DataError) as exc:
        compare_ticks_with_source(
            date_val=old_date,
            symbols=["RELIANCE"],
            source="yfinance",
            runtime=tmp_project,
            data_service=ds,
            builder_factory=_make_builder,
        )
    assert "date beyond the source's retention" in exc.value.what


def test_refusal_when_both_sides_empty(tmp_project: Any) -> None:
    d = date(2026, 11, 6)
    broker = FakeSourceBroker(name="fake_src", bars=[])
    ds = _setup_service(tmp_project, broker)

    with pytest.raises(DataError) as exc:
        compare_ticks_with_source(
            date_val=d,
            symbols=["RELIANCE", "TCS"],
            source="fake_src",
            runtime=tmp_project,
            data_service=ds,
            builder_factory=_make_builder,
        )
    err = exc.value.what
    assert "Refusing comparison" in err
    assert "RELIANCE: built side: no ticks recorded; source side: source returned nothing" in err
    assert "TCS: built side: no ticks recorded; source side: source returned nothing" in err


def test_compare_structured_id_and_alias_resolution(tmp_project: Any) -> None:
    data_dir = tmp_project / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(m_path)

    inst = InstrumentId("NSE:EQ:TCS")
    d = date(2026, 11, 6)
    ticks = _make_test_ticks(inst, d, minutes_count=5)
    _write_tick_partition(data_dir, manifest, inst, d, ticks)
    built = _build_bars_from_ticks(ticks, builder_factory=_make_builder)

    broker = FakeSourceBroker(name="fake_src", bars=built)
    ds = _setup_service(tmp_project, broker)

    # Test passing bare symbol alias "TCS"
    rep = compare_ticks_with_source(
        date_val=d,
        symbols=["TCS"],
        source="fake_src",
        runtime=tmp_project,
        data_service=ds,
        builder_factory=_make_builder,
    )
    assert len(rep.results) == 1
    assert rep.results[0].symbol == "TCS"
    assert rep.results[0].matched_count == 5


def test_compare_ist_session_with_utc_partition(tmp_project: Any) -> None:
    data_dir = tmp_project / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(m_path)

    inst = InstrumentId("NSE:EQ:INFY")
    d = date(2026, 11, 6)

    # 1. Ticks in IST early morning (05:15 IST = 23:45 UTC on previous day 2026-11-05)
    dt_utc_prev = datetime(2026, 11, 5, 23, 45, tzinfo=UTC)
    tick_prev = Tick(
        instrument=inst,
        exch_ts=dt_utc_prev,
        recv_ts=dt_utc_prev,
        ltp=1500.0,
        ltq=10,
        volume=10,
    )
    _write_tick_partition(data_dir, manifest, inst, date(2026, 11, 5), [tick_prev])

    # 2. Daytime ticks on 2026-11-06 (09:15 to 09:19 IST)
    ticks_day = _make_test_ticks(inst, d, start_hour_ist=9, start_minute_ist=15, minutes_count=5)
    _write_tick_partition(data_dir, manifest, inst, d, ticks_day)

    built = _build_bars_from_ticks([tick_prev] + ticks_day, builder_factory=_make_builder)
    assert len(built) == 6

    broker = FakeSourceBroker(name="fake_src", bars=built)
    ds = _setup_service(tmp_project, broker)

    rep = compare_ticks_with_source(
        date_val=d,
        symbols=["INFY"],
        source="fake_src",
        runtime=tmp_project,
        data_service=ds,
        builder_factory=_make_builder,
    )
    r = rep.results[0]
    assert r.built_count == 6
    assert r.matched_count == 6


def test_compare_first_last_bar_times_and_metrics(tmp_project: Any) -> None:
    data_dir = tmp_project / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(m_path)

    inst = InstrumentId("NSE:EQ:SBIN")
    d = date(2026, 11, 6)
    ticks = _make_test_ticks(inst, d, start_hour_ist=10, start_minute_ist=0, minutes_count=10)
    _write_tick_partition(data_dir, manifest, inst, d, ticks)
    built = _build_bars_from_ticks(ticks, builder_factory=_make_builder)

    broker = FakeSourceBroker(name="fake_src", bars=built)
    ds = _setup_service(tmp_project, broker)

    rep = compare_ticks_with_source(
        date_val=d,
        symbols=["SBIN"],
        source="fake_src",
        runtime=tmp_project,
        data_service=ds,
        builder_factory=_make_builder,
    )
    r = rep.results[0]
    assert r.built_first_ts is not None
    assert r.built_last_ts is not None
    assert r.source_first_ts is not None
    assert r.source_last_ts is not None
    assert r.built_first_ts.astimezone(_IST).strftime("%H:%M") == "10:00"
    assert r.built_last_ts.astimezone(_IST).strftime("%H:%M") == "10:09"


def test_bar_series_missing_bars_side_counts() -> None:
    inst = InstrumentId("NSE:EQ:TEST")
    dt1 = datetime(2026, 11, 6, 4, 0, tzinfo=UTC)
    dt2 = datetime(2026, 11, 6, 4, 1, tzinfo=UTC)
    dt3 = datetime(2026, 11, 6, 4, 2, tzinfo=UTC)

    b1 = Bar(inst, "1m", dt1, dt1 + timedelta(minutes=1), 100, 101, 99, 100, 10, complete=True)
    b2 = Bar(inst, "1m", dt2, dt2 + timedelta(minutes=1), 100, 101, 99, 100, 10, complete=True)
    b3 = Bar(inst, "1m", dt3, dt3 + timedelta(minutes=1), 100, 101, 99, 100, 10, complete=True)

    # Built has b1 and b2; Source has b2 and b3
    res = _compare_bar_series([b1, b2], [b2, b3], tolerance_pct=0.1, symbol="TEST")
    assert res.built_count == 2
    assert res.source_count == 2
    assert res.matched_count == 1
    assert res.built_only_count == 1
    assert res.source_only_count == 1
    assert res.built_first_ts == dt1
    assert res.built_last_ts == dt2
    assert res.source_first_ts == dt2
    assert res.source_last_ts == dt3


def test_cli_data_compare_refusal_exit_code_1(tmp_project: Path) -> None:
    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "data",
            "compare",
            "--date",
            "2026-11-06",
            "--symbols",
            "NONEXISTENT",
            "--source",
            "yfinance",
        ],
    )
    assert res.exit_code == 1
    assert "ERROR: Refusing comparison" in res.output
    assert "no ticks recorded" in res.output
    assert "BUILT" not in res.output  # Never prints table of zeros


def test_cli_data_compare_output_columns(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_project / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(m_path)

    inst = InstrumentId("NSE:EQ:RELIANCE")
    d = date(2026, 11, 6)
    ticks = _make_test_ticks(inst, d, minutes_count=5)
    _write_tick_partition(data_dir, manifest, inst, d, ticks)
    built = _build_bars_from_ticks(ticks, builder_factory=_make_builder)

    broker = FakeSourceBroker(name="fake_src", bars=built)

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

    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "data",
            "compare",
            "--date",
            "2026-11-06",
            "--symbols",
            "RELIANCE",
            "--source",
            "fake_src",
        ],
    )
    assert res.exit_code == 0
    assert "BUILT FIRST" in res.output
    assert "BUILT LAST" in res.output
    assert "SRC FIRST" in res.output
    assert "SRC LAST" in res.output
    assert "BUILT ONLY" in res.output
    assert "SRC ONLY" in res.output
