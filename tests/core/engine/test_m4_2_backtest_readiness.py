"""Tests for CP1-CP3 Steps 6-10: backtest check CLI, pre-check, universe, and feed adjustments."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from algodesk.api.errors import DataError
from algodesk.api.manifest import PluginManifest
from algodesk.api.market import Bar, InstrumentId, SeriesKey
from algodesk.cli.app import app
from algodesk.data.historical_feed import HistoricalFeed
from algodesk.data.manifest import DataManifest
from algodesk.data.readiness import check_backtest_readiness
from algodesk.data.service import DataService
from algodesk.data.store import BarStore
from algodesk.fees import FeeEngine, FeeScheduleError
from algodesk.ops.readiness import resolve_universe_for_backtest

runner = CliRunner()


class FakeSource:
    """Hermetic source for tests."""

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


class MockCalendar:
    """Mock calendar with custom holidays and special sessions."""

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


def _setup_run_config(
    proj: Path,
    name: str = "bt_test_run",
    symbols: list[str] | None = None,
    start: str = "2025-01-06",
    end: str = "2025-01-10",
    fallback: str | None = None,
    max_missing_pct: float = 10.0,
    membership: str = "at_start",
    adjust: str = "splits",
) -> Path:
    cfg_dir = proj / "config" / "runs"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    p = cfg_dir / f"{name}.toml"

    sym_list = symbols or ["NSE:EQ:INFY"]
    syms_str = ", ".join(f'"{s}"' for s in sym_list)
    fb_line = f'fallback = "{fallback}"' if fallback else ""

    content = f"""allocated_capital = 100000

[run]
name = "{name}"
mode = "backtest"

[backtest]
start = "{start}"
end = "{end}"
fidelity = "bar"
timeframe = "1d"
adjust = "{adjust}"
warmup_bars = 0
max_missing_pct = {max_missing_pct}

[data_source]
plugin = "upstox"

[execution_venue]
plugin = "sim"
seed = 42

[fees]
schedule = "zerodha_nse"
{fb_line}

[[calendars]]
plugin = "exchange"
exchange = "nse"

[secrets]
plugin = "keychain"

[universe]
symbols = [{syms_str}]
membership = "{membership}"

[[strategies]]
plugin = "sma_cross"
risk_profile = "conservative"
params = {{ timeframe = "1d", fast = 2, slow = 5, qty = 1, product = "CNC" }}
"""
    p.write_text(content, encoding="utf-8")
    return p


# ============================================================================
# Step 5 & 6: backtest check CLI and pre-run check
# ============================================================================


def test_backtest_check_cli_all_ready(tmp_project: Path) -> None:
    """backtest check outputs status table and exits 0 when symbols have complete data."""
    _setup_run_config(
        tmp_project, "run_ready", ["NSE:EQ:INFY"], start="2025-01-06", end="2025-01-07"
    )

    # Commit bars to tmp_project data dir
    data_dir = tmp_project / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    manifest = DataManifest(m_path)
    store = BarStore(data_dir=data_dir, manifest=manifest)
    inst = InstrumentId("NSE:EQ:INFY")
    d1, d2 = date(2025, 1, 6), date(2025, 1, 7)
    store.commit(
        SeriesKey(inst, "bars", "1d", "adjusted", "upstox"),
        [_make_bar(inst, d1), _make_bar(inst, d2)],
    )

    res = runner.invoke(app, ["--root", str(tmp_project), "backtest", "check", "run_ready"])
    assert res.exit_code == 0
    assert "Backtest Readiness Check" in res.stdout
    assert "NSE:EQ:INFY" in res.stdout
    assert "OK" in res.stdout


def test_backtest_check_cli_json(tmp_project: Path) -> None:
    """backtest check --json outputs valid JSON conforming to the schema."""
    _setup_run_config(
        tmp_project, "run_json", ["NSE:EQ:INFY"], start="2025-01-06", end="2025-01-07"
    )

    data_dir = tmp_project / "data"
    manifest = DataManifest(data_dir / "state" / "data_manifest.db")
    store = BarStore(data_dir=data_dir, manifest=manifest)
    inst = InstrumentId("NSE:EQ:INFY")
    d1, d2 = date(2025, 1, 6), date(2025, 1, 7)
    store.commit(
        SeriesKey(inst, "bars", "1d", "adjusted", "upstox"),
        [_make_bar(inst, d1), _make_bar(inst, d2)],
    )

    res = runner.invoke(
        app, ["--root", str(tmp_project), "backtest", "check", "run_json", "--json"]
    )
    assert res.exit_code == 0
    data = json.loads(res.stdout)
    assert data["version"] == 1
    assert "range" in data
    assert "universe" in data
    assert "missing_pct" in data
    assert data["missing_pct"] == 0.0


def test_backtest_check_cli_date_overrides(tmp_project: Path) -> None:
    """backtest check --start and --end override range without altering run config file."""
    cfg_file = _setup_run_config(
        tmp_project, "run_ovr", ["NSE:EQ:INFY"], start="2025-01-06", end="2025-01-10"
    )
    orig_text = cfg_file.read_text(encoding="utf-8")

    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "backtest",
            "check",
            "run_ovr",
            "--start",
            "2022-01-03",
            "--end",
            "2022-01-05",
            "--json",
        ],
    )
    # File is unchanged
    assert cfg_file.read_text(encoding="utf-8") == orig_text

    data = json.loads(res.stdout)
    assert data["range"]["start"] == "2022-01-03"
    assert data["range"]["end"] == "2022-01-05"


def test_backtest_check_cli_missing_exceeds_limit_exits_1(tmp_project: Path) -> None:
    """Missing share exceeding max_missing_pct prints exact accumulate command and exits 1."""
    _setup_run_config(
        tmp_project,
        "run_missing",
        ["NSE:EQ:MISSING1"],
        start="2025-01-06",
        end="2025-01-10",
        max_missing_pct=10.0,
    )

    res = runner.invoke(app, ["--root", str(tmp_project), "backtest", "check", "run_missing"])
    assert res.exit_code == 1
    assert "algodesk data accumulate" in res.stdout
    assert "--symbols NSE:EQ:MISSING1" in res.stdout
    assert "exceeds backtest.max_missing_pct" in res.stdout


def test_backtest_check_cli_skip_below_limit(tmp_project: Path) -> None:
    """Missing share below max_missing_pct lists skipped symbols and exits 0."""
    # 2 symbols: one ready, one missing = 50% missing. Limit set to 60%.
    _setup_run_config(
        tmp_project,
        "run_skip_ok",
        ["NSE:EQ:READY_SYM", "NSE:EQ:MISSING_SYM"],
        start="2025-01-06",
        end="2025-01-07",
        max_missing_pct=60.0,
    )

    data_dir = tmp_project / "data"
    manifest = DataManifest(data_dir / "state" / "data_manifest.db")
    store = BarStore(data_dir=data_dir, manifest=manifest)
    inst = InstrumentId("NSE:EQ:READY_SYM")
    d1, d2 = date(2025, 1, 6), date(2025, 1, 7)
    store.commit(
        SeriesKey(inst, "bars", "1d", "adjusted", "upstox"),
        [_make_bar(inst, d1), _make_bar(inst, d2)],
    )

    res = runner.invoke(app, ["--root", str(tmp_project), "backtest", "check", "run_skip_ok"])
    assert res.exit_code == 0
    assert "Skipped symbols" in res.stdout
    assert "NSE:EQ:MISSING_SYM: missing_sessions" in res.stdout


def test_backtest_run_precheck_skips_symbols_in_ops(tmp_project: Path) -> None:
    """Pre-run check removes skipped symbols in ops, keeping engine logic unchanged."""
    _setup_run_config(
        tmp_project,
        "run_prerun_skip",
        ["NSE:EQ:SYM_A", "NSE:EQ:SYM_B"],
        start="2025-01-06",
        end="2025-01-07",
        max_missing_pct=60.0,
    )

    data_dir = tmp_project / "data"
    manifest = DataManifest(data_dir / "state" / "data_manifest.db")
    store = BarStore(data_dir=data_dir, manifest=manifest)
    inst_a = InstrumentId("NSE:EQ:SYM_A")
    d1, d2 = date(2025, 1, 6), date(2025, 1, 7)
    store.commit(
        SeriesKey(inst_a, "bars", "1d", "adjusted", "upstox"),
        [_make_bar(inst_a, d1), _make_bar(inst_a, d2)],
    )

    with patch("algodesk.cli.backtest.BacktestRunner") as mock_runner_cls:
        res = runner.invoke(app, ["--root", str(tmp_project), "backtest", "run", "run_prerun_skip"])
        assert res.exit_code == 0
        assert mock_runner_cls.called
        # Check that effective config passed to BacktestRunner only contains SYM_A
        passed_cfg = (
            mock_runner_cls.call_args.kwargs.get("run_config") or mock_runner_cls.call_args[0][0]
        )
        assert passed_cfg.universe.symbols == ["NSE:EQ:SYM_A"]


def test_backtest_run_precheck_fails_above_limit(tmp_project: Path) -> None:
    """Pre-run check fails closed with code 1 when missing_pct exceeds limit."""
    _setup_run_config(
        tmp_project,
        "run_prerun_fail",
        ["NSE:EQ:NO_DATA"],
        start="2025-01-06",
        end="2025-01-07",
        max_missing_pct=10.0,
    )

    with patch("algodesk.engine.backtest.BacktestRunner.run") as mock_run:
        res = runner.invoke(app, ["--root", str(tmp_project), "backtest", "run", "run_prerun_fail"])
        assert res.exit_code == 1
        assert not mock_run.called
        assert "exceeds backtest.max_missing_pct" in res.stdout


def test_backtest_run_writes_readiness_json(tmp_project: Path) -> None:
    """backtest run writes readiness.json with schema conforming version 1."""
    _setup_run_config(
        tmp_project,
        "run_writer",
        ["NSE:EQ:IN_DATA"],
        start="2025-01-06",
        end="2025-01-07",
        max_missing_pct=10.0,
    )

    data_dir = tmp_project / "data"
    manifest = DataManifest(data_dir / "state" / "data_manifest.db")
    store = BarStore(data_dir=data_dir, manifest=manifest)
    inst = InstrumentId("NSE:EQ:IN_DATA")
    d1, d2 = date(2025, 1, 6), date(2025, 1, 7)
    store.commit(
        SeriesKey(inst, "bars", "1d", "adjusted", "upstox"),
        [_make_bar(inst, d1), _make_bar(inst, d2)],
    )

    with patch("algodesk.cli.backtest.BacktestRunner") as mock_runner_cls:
        res_dir = tmp_project / "results" / "run_writer_backtest_20250106_120000_111111"
        res_dir.mkdir(parents=True, exist_ok=True)
        mock_runner = mock_runner_cls.return_value
        mock_runner.run.return_value = str(res_dir)

        res = runner.invoke(app, ["--root", str(tmp_project), "backtest", "run", "run_writer"])
        assert res.exit_code == 0

        r_json_path = res_dir / "readiness.json"
        assert r_json_path.exists()

        data = json.loads(r_json_path.read_text(encoding="utf-8"))
        assert data["version"] == 1
        assert data["range"]["start"] == "2025-01-06"
        assert data["range"]["end"] == "2025-01-07"
        assert data["universe"]["members"] == ["NSE:EQ:IN_DATA"]
        assert data["universe"]["with_data"] == ["NSE:EQ:IN_DATA"]
        assert data["missing_pct"] == 0.0


# ============================================================================
# Step 7: Universe point-in-time and union resolution
# ============================================================================


def test_universe_membership_at_start(tmp_project: Path) -> None:
    """resolve_universe_for_backtest resolves group members as-of start date."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    u_dir = tmp_project / "data" / "universe"
    u_dir.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_arrays(
        [
            pa.array(["STOCK1", "STOCK2", "STOCK3"]),
            pa.array(["2024-01-01", "2024-01-01", "2025-01-01"]),
            pa.array(["", "2024-12-31", ""]),
            pa.array(["NSE:EQ:STOCK1", "NSE:EQ:STOCK2", "NSE:EQ:STOCK3"]),
        ],
        names=["symbol", "from", "to", "canonical_id"],
    )
    pq.write_table(table, u_dir / "test_hist.parquet")

    # Start date 2024-06-01: STOCK1 and STOCK2 are active, STOCK3 was not added yet
    cfg_file = _setup_run_config(
        tmp_project,
        "run_at_start",
        start="2024-06-01",
        end="2024-06-10",
        membership="at_start",
    )
    text = cfg_file.read_text(encoding="utf-8").replace(
        'symbols = ["NSE:EQ:INFY"]', 'group = "test_hist"'
    )
    cfg_file.write_text(text, encoding="utf-8")

    from algodesk.cli.common import open_runtime
    from algodesk.config.snapshot import build_effective

    rt = open_runtime(tmp_project)
    eff = build_effective(rt.root, "run_at_start")
    members, g_name, as_of_str, u_hash, _, _ = resolve_universe_for_backtest(eff.run, rt)
    assert members == ["NSE:EQ:STOCK1", "NSE:EQ:STOCK2"]
    assert g_name == "test_hist"
    assert as_of_str == "2024-06-01"


def test_universe_membership_union(tmp_project: Path) -> None:
    """resolve_universe_for_backtest with membership='union' includes all historical members."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    u_dir = tmp_project / "data" / "universe"
    u_dir.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_arrays(
        [
            pa.array(["STOCK1", "STOCK2", "STOCK3"]),
            pa.array(["2024-01-01", "2024-01-01", "2025-01-01"]),
            pa.array(["", "2024-12-31", ""]),
            pa.array(["NSE:EQ:STOCK1", "NSE:EQ:STOCK2", "NSE:EQ:STOCK3"]),
        ],
        names=["symbol", "from", "to", "canonical_id"],
    )
    pq.write_table(table, u_dir / "test_hist.parquet")

    cfg_file = _setup_run_config(
        tmp_project,
        "run_union",
        start="2024-06-01",
        end="2024-06-10",
        membership="union",
    )
    text = cfg_file.read_text(encoding="utf-8").replace(
        'symbols = ["NSE:EQ:INFY"]', 'group = "test_hist"'
    )
    cfg_file.write_text(text, encoding="utf-8")

    from algodesk.cli.common import open_runtime
    from algodesk.config.snapshot import build_effective

    rt = open_runtime(tmp_project)
    eff = build_effective(rt.root, "run_union")
    members, g_name, as_of_str, u_hash, _, _ = resolve_universe_for_backtest(eff.run, rt)
    assert set(members) == {"NSE:EQ:STOCK1", "NSE:EQ:STOCK2", "NSE:EQ:STOCK3"}
    assert g_name == "test_hist"
    assert as_of_str == "union:2024-06-01"


# ============================================================================
# Step 8: Fee fallback metric journaling
# ============================================================================


def test_backtest_fees_fallback_earliest_journals_metric(tmp_project: Path) -> None:
    """Backtest with fees.fallback='earliest' journals fallback into metrics and summary."""
    _setup_run_config(
        tmp_project,
        "run_fb",
        ["NSE:EQ:FB_STOCK"],
        start="2019-12-30",
        end="2019-12-31",
        fallback="earliest",
        max_missing_pct=50.0,
    )

    data_dir = tmp_project / "data"
    manifest = DataManifest(data_dir / "state" / "data_manifest.db")
    store = BarStore(data_dir=data_dir, manifest=manifest)
    inst = InstrumentId("NSE:EQ:FB_STOCK")
    d1, d2 = date(2019, 12, 30), date(2019, 12, 31)
    store.commit(
        SeriesKey(inst, "bars", "1d", "adjusted", "upstox"),
        [_make_bar(inst, d1), _make_bar(inst, d2)],
    )

    with patch("algodesk.cli.backtest.BacktestRunner") as mock_runner_cls:
        res_dir = tmp_project / "results" / "run_fb_backtest_20191230_120000_222222"
        res_dir.mkdir(parents=True, exist_ok=True)
        # Create metrics.json and SUMMARY.md as ResultsWriter would
        metrics_file = res_dir / "metrics.json"
        metrics_file.write_text(json.dumps({"total_trades": 5}), encoding="utf-8")
        summary_file = res_dir / "SUMMARY.md"
        summary_file.write_text("# Summary\n\nAssumptions: basis (splits)\n", encoding="utf-8")

        mock_runner = mock_runner_cls.return_value

        def _fake_run() -> str:
            FeeEngine.fallback_used_count = 3
            return str(res_dir)

        mock_runner.run.side_effect = _fake_run
        res = runner.invoke(app, ["--root", str(tmp_project), "backtest", "run", "run_fb"])
        assert res.exit_code == 0

        m_data = json.loads((res_dir / "metrics.json").read_text(encoding="utf-8"))
        assert m_data.get("fees.fallback_used") == 3

        summary_text = (res_dir / "SUMMARY.md").read_text(encoding="utf-8")
        assert "earliest verified rates used (3 fallback calculations)" in summary_text


def test_backtest_fees_fail_closed_pre_verified() -> None:
    """Backtest without fallback fails closed when trading before verified fee dates."""
    FeeEngine.reset_fallback_count()
    FeeEngine.set_fallback(None)
    import tomllib

    from algodesk.fees.models import Schedule

    s_path = (
        Path(__file__).resolve().parents[3] / "plugins" / "fees" / "zerodha_nse" / "schedule.toml"
    )
    with open(s_path, "rb") as f:
        sch = Schedule.model_validate(tomllib.load(f))

    with pytest.raises(FeeScheduleError):
        # Calculate charges on 2019-12-31 without fallback
        FeeEngine.calculate_trade_charges(
            turnover=Decimal("5000.00"),
            side="buy",
            segment="eq",
            product="CNC",
            trade_date=date(2019, 12, 31),
            schedule=sch,
            fallback=None,
        )


# ============================================================================
# Step 10: HistoricalFeed adjustments and calendar sessions
# ============================================================================


def test_historical_feed_adjust_splits(tmp_path: Path) -> None:
    """HistoricalFeed requests adjust='splits' by default and serves bars."""
    data_dir = tmp_path / "data"
    manifest = DataManifest(data_dir / "state" / "data_manifest.db")
    store = BarStore(data_dir=data_dir, manifest=manifest)
    inst = InstrumentId("NSE:EQ:SPLIT_CO")
    d = date(2025, 1, 6)
    store.commit(SeriesKey(inst, "bars", "1d", "adjusted", "upstox"), [_make_bar(inst, d, 200.0)])

    fake_src = FakeSource({})
    ds = DataService(sources={"upstox": fake_src}, default_source="upstox", store=store)
    cal = MockCalendar()

    feed = HistoricalFeed(
        data_service=ds,
        instruments=[inst],
        start=d,
        end=d,
        timeframe="1d",
        source="upstox",
        calendar=cal,
        adjust="splits",
    )
    assert feed.adjust == "splits"
    bars = feed.bars
    assert len(bars) == 1
    assert bars[0].close == 200.0


def test_historical_feed_adjust_total(tmp_path: Path) -> None:
    """HistoricalFeed accepts adjust='total' and propagates to DataService."""
    data_dir = tmp_path / "data"
    manifest = DataManifest(data_dir / "state" / "data_manifest.db")
    store = BarStore(data_dir=data_dir, manifest=manifest)
    inst = InstrumentId("NSE:EQ:TOTAL_CO")
    d = date(2025, 1, 6)
    store.commit(SeriesKey(inst, "bars", "1d", "adjusted", "upstox"), [_make_bar(inst, d, 300.0)])

    fake_src = FakeSource({})
    ds = DataService(sources={"upstox": fake_src}, default_source="upstox", store=store)
    cal = MockCalendar()

    feed = HistoricalFeed(
        data_service=ds,
        instruments=[inst],
        start=d,
        end=d,
        timeframe="1d",
        source="upstox",
        calendar=cal,
        adjust="total",
    )
    assert feed.adjust == "total"
    bars = feed.bars
    assert len(bars) == 1
    assert bars[0].close == 300.0


def test_historical_feed_total_refuses_conflict_or_manual(tmp_path: Path) -> None:
    """HistoricalFeed with adjust='total' does NOT swallow DataError on action conflict."""
    data_dir = tmp_path / "data"
    manifest = DataManifest(data_dir / "state" / "data_manifest.db")
    store = BarStore(data_dir=data_dir, manifest=manifest)
    inst = InstrumentId("NSE:EQ:CONFLICT_CO")
    d = date(2025, 1, 6)
    store.commit(SeriesKey(inst, "bars", "1d", "adjusted", "upstox"), [_make_bar(inst, d, 100.0)])

    mock_ds = MagicMock(spec=DataService)
    # Simulate DataService.get raising DataError for corporate action conflict
    mock_ds.get.side_effect = DataError(
        what="Corporate action conflict on ratio",
        where="DataService._apply_raw_adjustments",
        fix="Resolve conflict with manual corporate action record",
    )
    cal = MockCalendar()

    feed = HistoricalFeed(
        data_service=mock_ds,
        instruments=[inst],
        start=d,
        end=d,
        timeframe="1d",
        source="upstox",
        calendar=cal,
        adjust="total",
    )

    with pytest.raises(DataError) as exc_info:
        _ = feed.bars
    assert "Corporate action conflict" in exc_info.value.what


def test_historical_feed_saturday_special_session(tmp_path: Path) -> None:
    """A special Saturday session yields bars."""
    data_dir = tmp_path / "data"
    manifest = DataManifest(data_dir / "state" / "data_manifest.db")
    store = BarStore(data_dir=data_dir, manifest=manifest)
    inst = InstrumentId("NSE:EQ:SPECIAL_CO")

    # 2025-02-01 is Saturday (special session)
    sat = date(2025, 2, 1)
    store.commit(SeriesKey(inst, "bars", "1d", "adjusted", "upstox"), [_make_bar(inst, sat, 150.0)])

    fake_src = FakeSource({})
    ds = DataService(sources={"upstox": fake_src}, default_source="upstox", store=store)
    cal = MockCalendar(specials={sat})

    feed = HistoricalFeed(
        data_service=ds,
        instruments=[inst],
        start=sat,
        end=sat,
        timeframe="1d",
        source="upstox",
        calendar=cal,
    )
    bars = feed.bars
    assert len(bars) == 1
    assert bars[0].close == 150.0


def test_historical_feed_holiday_no_gap(tmp_path: Path) -> None:
    """A holiday yields no bars."""
    data_dir = tmp_path / "data"
    manifest = DataManifest(data_dir / "state" / "data_manifest.db")
    store = BarStore(data_dir=data_dir, manifest=manifest)
    inst = InstrumentId("NSE:EQ:HOLIDAY_CO")

    hol = date(2025, 1, 15)
    cal = MockCalendar(holidays={hol})

    fake_src = FakeSource({})
    ds = DataService(sources={"upstox": fake_src}, default_source="upstox", store=store)

    feed = HistoricalFeed(
        data_service=ds,
        instruments=[inst],
        start=hol,
        end=hol,
        timeframe="1d",
        source="upstox",
        calendar=cal,
    )
    bars = feed.bars
    assert len(bars) == 0


# ============================================================================
# Step 11: Zero network calls verification
# ============================================================================


def test_readiness_check_makes_zero_network_calls(tmp_path: Path) -> None:
    """Readiness check executes cache-only and makes zero network calls to sources."""
    data_dir = tmp_path / "data"
    manifest = DataManifest(data_dir / "state" / "data_manifest.db")
    store = BarStore(data_dir=data_dir, manifest=manifest)
    inst = InstrumentId("NSE:EQ:ZERO_NET")

    counting_src = FakeSource({})
    ds = DataService(sources={"upstox": counting_src}, default_source="upstox", store=store)
    cal = MockCalendar()

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
    assert report.missing_pct > 0
    # Verify fake data source had zero calls
    assert counting_src.call_count == 0
