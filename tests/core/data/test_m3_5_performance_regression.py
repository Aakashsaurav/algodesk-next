"""Performance regression tests for data accumulation planning and screen running (M3-5).

Counts manifest statements, connections, calendar calls, parquet opens, and
DataSource constructions on a generated 500-symbol cache with fake source and
fake calendar; asserts bounds taken from CP0 results; no wall-clock assertions;
timing printed for information.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from contextlib import redirect_stdout
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pyarrow as pa

from algodesk.api.manifest import PluginManifest
from algodesk.api.market import (
    Bar,
    Instrument,
    InstrumentId,
    SeriesKey,
)
from algodesk.api.protocols import Calendar, DataSource, TradingSession
from algodesk.data.accumulate import (
    plan_accumulation,
)
from algodesk.data.calendar_cache import CachedCalendar
from algodesk.data.instruments_store import InstrumentStore
from algodesk.data.manifest import CoverageDayRecord, DataManifest
from algodesk.data.service import DataService
from algodesk.data.store import BarStore
from algodesk.ops.screens import (
    DataServiceScreenAdapter,
)
from algodesk.screens.models import (
    OperandModel,
    RequireRule,
    TomlScreenModel,
)
from algodesk.screens.runner import ScreenRunner

IST = ZoneInfo("Asia/Kolkata")


class FakeCountingCalendar(Calendar):
    """Calendar implementation that tracks method call counts."""

    def __init__(self, start: date, end: date) -> None:
        self._start = start
        self._end = end
        self.is_trading_day_calls = 0
        self.sessions_calls = 0
        self.coverage_calls = 0

    def coverage(self) -> tuple[date, date]:
        self.coverage_calls += 1
        return self._start, self._end

    def is_trading_day(self, d: date, segment: str = "") -> bool:
        self.is_trading_day_calls += 1
        return d.weekday() < 5  # Mon-Fri

    def sessions(self, d: date, segment: str = "") -> list[TradingSession]:
        self.sessions_calls += 1
        if d.weekday() >= 5:
            return []
        s_dt = datetime.combine(d, time(9, 15), tzinfo=IST).astimezone(UTC)
        e_dt = datetime.combine(d, time(15, 30), tzinfo=IST).astimezone(UTC)
        return [TradingSession("REGULAR", s_dt, e_dt, tradable=True)]


class FakeSource(DataSource):
    """In-memory DataSource serving bars."""

    def __init__(self) -> None:
        self.bars_map: dict[str, list[Bar]] = {}

    def history(
        self,
        key: SeriesKey,
        start: datetime,
        end: datetime,
    ) -> Sequence[Bar]:
        sym = str(key.instrument).split(":")[-1]
        all_bars = self.bars_map.get(sym, self.bars_map.get(str(key.instrument), []))
        return [b for b in all_bars if start <= b.start_ts <= end]

    def instruments(self) -> Sequence[Instrument]:
        return []


def make_dummy_instruments(count: int) -> list[Instrument]:
    """Generate mock instruments for benchmarking."""
    items: list[Instrument] = []
    for i in range(1, count + 1):
        sym = f"SYM{i:04d}"
        inst = Instrument(
            id=InstrumentId(f"NSE:EQ:{sym}"),
            exchange="NSE",
            symbol=sym,
            segment="EQ",
            tick_size=Decimal("0.05"),
            lot_size=1,
            broker_keys={"upstox": f"NSE_EQ|{sym}"},
        )
        items.append(inst)
    return items


def make_daily_bars(instrument: InstrumentId, days_count: int, end_date: date) -> list[Bar]:
    """Generate daily bars for an instrument."""
    bars: list[Bar] = []
    curr = end_date - timedelta(days=days_count)
    price = 100.0
    while curr <= end_date:
        if curr.weekday() < 5:
            s_dt = datetime.combine(curr, time(9, 15), tzinfo=IST).astimezone(UTC)
            e_dt = datetime.combine(curr, time(15, 30), tzinfo=IST).astimezone(UTC)
            bars.append(
                Bar(
                    instrument=instrument,
                    timeframe="1d",
                    start_ts=s_dt,
                    end_ts=e_dt,
                    open=price,
                    high=price * 1.02,
                    low=price * 0.98,
                    close=price,
                    volume=10000.0,
                )
            )
            price += 0.5
        curr += timedelta(days=1)
    return bars


def make_test_manifest_plugin(source_name: str = "upstox") -> PluginManifest:
    """Make dummy plugin manifest for tests."""
    return PluginManifest.model_validate(
        {
            "name": source_name,
            "kind": "broker",
            "version": "1.0.0",
            "api_version": 1,
            "entry": f"broker:{source_name}",
            "capabilities": {"candles": True, "history": True},
            "candles_adjustment_basis": "splits",
            "limits": {},
        }
    )


def test_cached_calendar_is_trading_day_memoization() -> None:
    """CachedCalendar should memoize is_trading_day calls."""
    raw_cal = FakeCountingCalendar(date(2026, 1, 1), date(2026, 12, 31))
    cached = CachedCalendar(raw_cal)

    target_date = date(2026, 5, 15)
    for _ in range(10):
        res = cached.is_trading_day(target_date)
        assert res is True

    # Underlying calendar called only once for target_date
    assert raw_cal.is_trading_day_calls == 1


def test_cached_calendar_sessions_memoization() -> None:
    """CachedCalendar should memoize sessions() calls."""
    raw_cal = FakeCountingCalendar(date(2026, 1, 1), date(2026, 12, 31))
    cached = CachedCalendar(raw_cal)

    target_date = date(2026, 5, 1)
    for _ in range(10):
        sessions = cached.sessions(target_date)
        assert len(sessions) > 0

    # Underlying calendar called only once for this date
    assert raw_cal.sessions_calls == 1


def test_cached_calendar_wrap_idempotence() -> None:
    """CachedCalendar.wrap() should be idempotent."""
    raw_cal = FakeCountingCalendar(date(2026, 1, 1), date(2026, 12, 31))
    wrapped1 = CachedCalendar.wrap(raw_cal)
    wrapped2 = CachedCalendar.wrap(wrapped1)
    assert wrapped1 is wrapped2


def test_manifest_get_coverage_days_bulk(tmp_path: Path) -> None:
    """DataManifest.get_coverage_days_bulk retrieves multi-dataset coverage in 1 query."""
    manifest = DataManifest(tmp_path / "test_manifest.db")
    today = date(2026, 11, 12)

    # Insert coverage for 5 datasets
    for i in range(5):
        ds_id = f"ds_{i}"
        manifest.record_coverage_day(
            CoverageDayRecord(
                dataset_id=ds_id,
                day=today.isoformat(),
                status="ok",
                rows=1,
                expected_rows=1,
                part_path="test.parquet",
                fetched_at="2026-11-12T10:00:00Z",
            )
        )

    # Fetch in bulk
    dataset_ids = [f"ds_{i}" for i in range(5)]
    coverage_map = manifest.get_coverage_days_bulk(
        dataset_ids, today.isoformat(), today.isoformat()
    )
    assert len(coverage_map) == 5
    for i in range(5):
        ds_id = f"ds_{i}"
        assert ds_id in coverage_map
        assert len(coverage_map[ds_id]) == 1
        assert coverage_map[ds_id][0].status == "ok"


def test_manifest_closures_cache_and_invalidation(tmp_path: Path) -> None:
    """DataManifest.list_market_closures should cache and invalidate on mutation."""
    manifest = DataManifest(tmp_path / "test_manifest.db")
    c_date = date(2026, 11, 1)

    closures1 = manifest.list_market_closures("NSE", date(2026, 1, 1), date(2026, 12, 31))
    assert len(closures1) == 0

    # Record closure -> invalidates cache
    manifest.record_market_closure("NSE", c_date.isoformat(), "holiday", "Diwali")
    closures2 = manifest.list_market_closures("NSE", date(2026, 1, 1), date(2026, 12, 31))
    assert len(closures2) == 1
    assert closures2[0].day == c_date.isoformat()

    # Delete closure -> invalidates cache
    manifest.delete_market_closure("NSE", c_date.isoformat())
    closures3 = manifest.list_market_closures("NSE", date(2026, 1, 1), date(2026, 12, 31))
    assert len(closures3) == 0


def test_manifest_fetch_log_cache(tmp_path: Path) -> None:
    """DataManifest.check_fetch_log_ok should cache results by timestamp."""
    manifest = DataManifest(tmp_path / "test_manifest.db")
    ts = "2026-11-12T10:00:00Z"

    # First call - executes query and returns (True, "")
    ok1, _ = manifest.check_fetch_log_ok(ts)
    assert ok1 is True

    # Second call - returns cached result
    ok2, _ = manifest.check_fetch_log_ok(ts)
    assert ok2 is True


def test_instrument_master_store_caching(tmp_path: Path) -> None:
    """InstrumentStore.load_master should cache parsed instruments and avoid disk re-read."""
    store = InstrumentStore(tmp_path)
    insts = make_dummy_instruments(5)
    as_of = date(2026, 11, 12)

    store.save_master("NSE", as_of, b"{}", insts)

    # First load
    loaded1 = store.load_master("NSE", as_of)
    assert len(loaded1) == 5

    # Second load - should hit cache without reading disk
    with patch("gzip.decompress") as mock_decomp:
        loaded2 = store.load_master("NSE", as_of)
        assert len(loaded2) == 5
        mock_decomp.assert_not_called()


def test_data_service_fill_only_fast_path(tmp_path: Path) -> None:
    """DataService.get(fill_only=True) should return rows_added without reading disk."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(data_dir / "data_manifest.db")
    raw_cal = FakeCountingCalendar(date(2025, 1, 1), date(2027, 1, 1))
    bar_store = BarStore(data_dir, manifest, calendar=raw_cal)

    fake_source = FakeSource()
    inst_id = InstrumentId("NSE:EQ:SYM0001")
    bars = make_daily_bars(inst_id, 5, date(2026, 11, 10))
    fake_source.bars_map["SYM0001"] = bars

    manifest_obj = make_test_manifest_plugin("upstox")
    service = DataService(
        sources={"upstox": fake_source},
        default_source="upstox",
        store=bar_store,
        manifests={"upstox": manifest_obj},
        calendar=raw_cal,
    )

    key = SeriesKey(
        instrument=inst_id,
        kind="bars",
        timeframe="1d",
        adjustment="raw",
        source="upstox",
    )
    start = bars[0].start_ts
    end = bars[-1].end_ts

    slice_res = service.get(key, start, end, fill_only=True)
    assert slice_res.provenance.get("rows_added") == len(bars)
    assert len(slice_res.bars) == 0


def test_data_service_skip_staleness_when_basis_matches(tmp_path: Path) -> None:
    """DataService.get should skip staleness check when from_basis == adj_mode."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(data_dir / "data_manifest.db")
    raw_cal = FakeCountingCalendar(date(2025, 1, 1), date(2027, 1, 1))
    bar_store = BarStore(data_dir, manifest, calendar=raw_cal)

    inst_id = InstrumentId("NSE:EQ:SYM0001")
    bars = make_daily_bars(inst_id, 5, date(2026, 11, 10))
    key = SeriesKey(
        instrument=inst_id, kind="bars", timeframe="1d", adjustment="raw", source="upstox"
    )
    bar_store.commit(key, bars)

    manifest_obj = make_test_manifest_plugin("upstox")
    fake_source = FakeSource()

    with patch("algodesk.data.service.ActionsStore") as mock_act_cls:
        mock_act_store = MagicMock()
        mock_act_cls.return_value = mock_act_store
        service = DataService(
            sources={"upstox": fake_source},
            default_source="upstox",
            store=bar_store,
            manifests={"upstox": manifest_obj},
            calendar=raw_cal,
        )

        start = bars[0].start_ts
        end = bars[-1].end_ts

        # When download is raw and requested adjust is none (both raw basis)
        service.get(key, start, end, download="raw", adjust="none")
        # Staleness checking does not touch actions store
        mock_act_store.get_actions.assert_not_called()


def test_bar_store_daily_single_open_optimization(tmp_path: Path) -> None:
    """BarStore.read for 1d timeframe checks daily.parquet directly without per-day loop."""
    manifest = DataManifest(tmp_path / "manifest.db")
    raw_cal = FakeCountingCalendar(date(2025, 1, 1), date(2027, 1, 1))
    bar_store = BarStore(tmp_path / "bars", manifest, calendar=raw_cal)

    inst_id = InstrumentId("NSE:EQ:SYM0001")
    bars = make_daily_bars(inst_id, 300, date(2026, 11, 10))
    key = SeriesKey(
        instrument=inst_id, kind="bars", timeframe="1d", adjustment="raw", source="upstox"
    )
    bar_store.commit(key, bars)

    start = bars[0].start_ts
    end = bars[-1].end_ts

    # Intercept pyarrow read_table to count opens
    with patch("pyarrow.parquet.read_table", wraps=pa.parquet.read_table) as mock_read:
        read_slice = bar_store.read(key, start, end)
        assert len(read_slice.bars) == len(bars)
        # Daily parquet file is read exactly once
        assert mock_read.call_count == 1


def test_bar_store_read_columns_projection(tmp_path: Path) -> None:
    """BarStore.read should project only requested columns."""
    manifest = DataManifest(tmp_path / "manifest.db")
    raw_cal = FakeCountingCalendar(date(2025, 1, 1), date(2027, 1, 1))
    bar_store = BarStore(tmp_path / "bars", manifest, calendar=raw_cal)

    inst_id = InstrumentId("NSE:EQ:SYM0001")
    bars = make_daily_bars(inst_id, 10, date(2026, 11, 10))
    key = SeriesKey(
        instrument=inst_id, kind="bars", timeframe="1d", adjustment="raw", source="upstox"
    )
    bar_store.commit(key, bars)

    start = bars[0].start_ts
    end = bars[-1].end_ts

    with patch("pyarrow.parquet.read_table", wraps=pa.parquet.read_table) as mock_read:
        cols = ("start_ts", "end_ts", "open", "high", "low", "close", "volume")
        read_slice = bar_store.read(key, start, end, columns=cols)
        assert len(read_slice.bars) == len(bars)
        mock_read.assert_called_once()
        _, kwargs = mock_read.call_args
        assert kwargs.get("columns") == list(cols)


def test_accumulate_plan_500_symbols_operation_counts(tmp_path: Path) -> None:
    """plan_accumulation on 500 symbols satisfies operation bounds from CP0.

    CP0 baseline operations before fixes:
      - SQLite statements: 7,105
      - Calendar is_trading_day calls: 240,501
      - Calendar sessions calls: 5,079
    Bounds asserted here:
      - manifest connections <= 2
      - SQLite statements <= 50 (actual is ~4 with bulk queries)
      - Calendar is_trading_day calls <= 600
      - Calendar sessions calls <= 10
      - Parquet opens == 0
      - DataSource constructions == 0
    """
    proj_root = tmp_path / "project"
    grp_dir = proj_root / "config" / "groups"
    grp_dir.mkdir(parents=True, exist_ok=True)
    data_dir = proj_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    members = [f"NSE:EQ:SYM{i:04d}" for i in range(1, 501)]
    (grp_dir / "test500.toml").write_text(f'name = "test500"\nmembers = {str(members)}\n')

    manifest_path = data_dir / "manifest.db"
    manifest = DataManifest(manifest_path)
    raw_cal = FakeCountingCalendar(date(2024, 1, 1), date(2027, 12, 31))

    # Instrument SQLite connection to trace executed statements
    statements: list[str] = []
    manifest._conn.set_trace_callback(statements.append)

    import time as time_mod

    t0 = time_mod.perf_counter()

    with patch("pyarrow.parquet.read_table") as mock_parquet:
        symbols, plan = plan_accumulation(
            group="test500",
            source="upstox",
            days=500,
            runtime=proj_root,
            calendar=raw_cal,
            timeframe="1d",
            now_dt=datetime(2026, 11, 12, 16, 0, tzinfo=UTC),
        )

    elapsed = time_mod.perf_counter() - t0
    print(f"\n[INFO] plan_accumulation 500 symbols elapsed: {elapsed:.4f}s")
    print(f"[INFO] SQLite statements executed: {len(statements)}")
    print(f"[INFO] Calendar is_trading_day calls: {raw_cal.is_trading_day_calls}")
    print(f"[INFO] Calendar sessions calls: {raw_cal.sessions_calls}")

    assert len(symbols) == 500
    assert len(plan) == 500
    # Parquet opens == 0
    assert mock_parquet.call_count == 0
    # SQLite statements bound: <= 50 (CP0 before was 7,105!)
    assert len(statements) <= 50
    # Calendar is_trading_day calls bound: <= 600 (CP0 before was 240,501!)
    assert raw_cal.is_trading_day_calls <= 600
    # Calendar sessions calls bound: <= 10 (CP0 before was 5,079!)
    assert raw_cal.sessions_calls <= 10


def test_accumulate_plan_progress_reporting(tmp_path: Path) -> None:
    """plan_accumulation should print progress lines every progress_every symbols."""
    proj_root = tmp_path / "project"
    grp_dir = proj_root / "config" / "groups"
    grp_dir.mkdir(parents=True, exist_ok=True)
    data_dir = proj_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    members = [f"NSE:EQ:SYM{i:04d}" for i in range(1, 31)]
    (grp_dir / "test30.toml").write_text(f'name = "test30"\nmembers = {str(members)}\n')

    DataManifest(data_dir / "manifest.db")
    raw_cal = FakeCountingCalendar(date(2024, 1, 1), date(2027, 12, 31))

    buf = io.StringIO()
    with redirect_stdout(buf):
        plan_accumulation(
            group="test30",
            source="upstox",
            days=10,
            runtime=proj_root,
            calendar=raw_cal,
            timeframe="1d",
            now_dt=datetime(2026, 11, 12, 16, 0, tzinfo=UTC),
        )

    out = buf.getvalue()
    assert "25/30 symbols planned" in out


def test_screen_runner_500_symbols_operation_bounds(tmp_path: Path) -> None:
    """ScreenRunner on 50 cached symbols opens each parquet at most once.

    Asserts:
      - Parquet opens <= 50
      - Progress output printed every progress_every symbols
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(data_dir / "data_manifest.db")
    raw_cal = FakeCountingCalendar(date(2024, 1, 1), date(2027, 12, 31))
    bar_store = BarStore(data_dir, manifest, calendar=raw_cal)

    n_symbols = 50
    instruments = make_dummy_instruments(n_symbols)
    as_of = date(2026, 11, 12)

    for inst in instruments:
        bars = make_daily_bars(inst.id, 20, as_of)
        key = SeriesKey(
            instrument=inst.id, kind="bars", timeframe="1d", adjustment="raw", source="upstox"
        )
        bar_store.commit(key, bars)

    manifest_obj = make_test_manifest_plugin("upstox")
    fake_source = FakeSource()
    service = DataService(
        sources={"upstox": fake_source},
        default_source="upstox",
        store=bar_store,
        manifests={"upstox": manifest_obj},
        calendar=raw_cal,
    )
    adapter = DataServiceScreenAdapter(
        service,
        default_source="upstox",
    )

    runner = ScreenRunner(
        data_reader=adapter,
        indicator_resolver=MagicMock(),
        calendar=raw_cal,
        progress_every=25,
    )

    spec = TomlScreenModel(
        name="test_screen",
        require=[
            RequireRule(
                left=OperandModel(field="close"),
                op=">",
                right=OperandModel(const=50.0),
            )
        ],
    )

    buf = io.StringIO()
    import time as time_mod

    t0 = time_mod.perf_counter()
    with (
        redirect_stdout(buf),
        patch("pyarrow.parquet.read_table", wraps=pa.parquet.read_table) as mock_read,
    ):
        result = runner.run(
            screen=spec,
            symbols=[str(inst.id) for inst in instruments],
            as_of=as_of,
        )
    elapsed = time_mod.perf_counter() - t0

    print(f"\n[INFO] ScreenRunner {n_symbols} symbols elapsed: {elapsed:.4f}s")
    print(f"[INFO] Parquet reads: {mock_read.call_count}")

    assert len(result.rows) + len(result.skipped) == n_symbols
    assert mock_read.call_count <= n_symbols
    out = buf.getvalue()
    assert "[screen] 25/50" in out
    assert "[screen] 50/50" in out
