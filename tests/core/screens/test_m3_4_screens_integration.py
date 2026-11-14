"""Integration tests for M3-4: real DataService, skip reasons,
accumulate, universe resolution, and timing.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from algodesk.api.errors import ConfigError
from algodesk.api.kinds import Mode
from algodesk.api.manifest import PluginManifest
from algodesk.api.market import (
    ActionRecord,
    Bar,
    Instrument,
    InstrumentId,
    SeriesKey,
)
from algodesk.api.protocols import Calendar, DataSource
from algodesk.api.screens import (
    ScreenResult,
    ScreenRow,
    SkipReason,
)
from algodesk.cli.app import app
from algodesk.data.actions_store import ActionsStore
from algodesk.data.manifest import DataManifest
from algodesk.data.service import DataService
from algodesk.data.store import BarStore
from algodesk.engine.runner import SessionRunner
from algodesk.ops.screens import (
    DataServiceScreenAdapter,
    PluginIndicatorResolver,
)
from algodesk.screens.models import (
    OperandModel,
    RequireRule,
    TomlScreenModel,
)
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


class FakeSource(DataSource):
    """In-memory DataSource serving bars and corporate actions."""

    def __init__(self) -> None:
        self.bars_map: dict[str, list[Bar]] = {}
        self.actions_map: dict[str, list[ActionRecord]] = {}

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


def _generate_bars(
    instrument: str,
    prices: Sequence[float],
    as_of: date = date(2026, 11, 12),
) -> list[Bar]:
    inst_id = InstrumentId(instrument if ":" in instrument else f"NSE:EQ:{instrument}")
    n = len(prices)
    bars: list[Bar] = []
    for i, p in enumerate(prices):
        day_offset = n - 1 - i
        d = as_of - timedelta(days=day_offset)
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
    tmp_path: Path,
    fake_source: FakeSource,
    source_name: str = "upstox",
) -> tuple[DataService, BarStore, ActionsStore]:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(data_dir / "data_manifest.db")
    store = BarStore(data_dir, manifest)
    act_store = ActionsStore(data_dir, manifest)
    cal = FakeCalendar()

    manifest_obj = PluginManifest.model_validate(
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

    ds = DataService(
        sources={source_name: fake_source},
        default_source=source_name,
        store=store,
        manifests={source_name: manifest_obj},
        calendar=cal,
    )
    return ds, store, act_store


# =========================================================================
# Step 6: Integration test with real DataService, fake source, tmp_project
# =========================================================================


def test_screen_runner_real_data_service_integration(tmp_project: Any) -> None:
    """Run ScreenRunner with real DataService:

    1 passing symbol, 1 too little history, 1 refused adjustment, 1 no data.
    Verifies honest skip messages naming symbol, reason, and fix command.
    """
    fake_src = FakeSource()
    ds, store, act_store = _build_test_data_service(tmp_project, fake_src)

    as_of = date(2026, 11, 12)

    # Symbol 1 (PASS): 100 bars, prices high enough to pass sma(50) > 100
    sym_pass = "NSE:EQ:SYM_PASS"
    pass_bars = _generate_bars(sym_pass, [150.0] * 100, as_of=as_of)
    fake_src.bars_map["SYM_PASS"] = pass_bars
    k_pass = SeriesKey(InstrumentId(sym_pass), "bars", "1d", "raw", "upstox")
    store.commit(k_pass, pass_bars)

    # Symbol 2 (TOO_FEW): only 5 bars when indicator needs 50
    sym_few = "NSE:EQ:SYM_FEW"
    few_bars = _generate_bars(sym_few, [150.0] * 5, as_of=as_of)
    fake_src.bars_map["SYM_FEW"] = few_bars
    k_few = SeriesKey(InstrumentId(sym_few), "bars", "1d", "raw", "upstox")
    store.commit(k_few, few_bars)

    # Symbol 3 (REFUSED): 100 bars, but has manual_required action when adjust="total" requested
    sym_refused = "NSE:EQ:SYM_REFUSED"
    ref_bars = _generate_bars(sym_refused, [150.0] * 100, as_of=as_of)
    fake_src.bars_map["SYM_REFUSED"] = ref_bars
    k_ref = SeriesKey(InstrumentId(sym_refused), "bars", "1d", "raw", "upstox")
    store.commit(k_ref, ref_bars)
    # Add a manual_required action
    bad_act = ActionRecord(
        ex_date=date(2026, 11, 10),
        type="other",
        ratio=None,
        amount=None,
        text="Unrecognised Scheme of Demerger",
        source="upstox",
        source_ref="SYM_REFUSED",
        fetched_at="2026-11-13T00:00:00Z",
        symbol="SYM_REFUSED",
        exchange="NSE",
        status="manual_required",
        id="act_bad_1",
    )
    act_store.write("NSE", "SYM_REFUSED", [bad_act])

    # Symbol 4 (NO_DATA): no bars in fake_src or store
    sym_nodata = "NSE:EQ:SYM_NODATA"

    adapter = DataServiceScreenAdapter(ds, default_source="upstox")
    resolver = PluginIndicatorResolver()
    runner = ScreenRunner(adapter, resolver, max_skipped_pct=100.0)

    screen = TomlScreenModel(
        name="test_integration_screen",
        timeframe="1d",
        adjust="total",  # Request total adjustment to trigger adjustment check
        require=[
            RequireRule(
                left=OperandModel(ind="sma", n=50),
                op=">",
                right=OperandModel(const=100.0),
            )
        ],
    )

    result = runner.run(
        screen,
        symbols=[sym_pass, sym_few, sym_refused, sym_nodata],
        as_of=as_of,
    )

    assert len(result.rows) == 1
    assert result.rows[0].symbol == sym_pass
    assert result.rows[0].passed is True

    assert len(result.skipped) == 3
    skipped_dict = {sym: (reason, detail) for sym, reason, detail in result.skipped}

    # Verify SYM_FEW: insufficient_history
    assert sym_few in skipped_dict
    reason_few, detail_few = skipped_dict[sym_few]
    assert reason_few == SkipReason.INSUFFICIENT_HISTORY
    assert "SYM_FEW" in detail_few
    assert "Fix with:" in detail_few

    # Verify SYM_REFUSED: adjustment_refused
    assert sym_refused in skipped_dict
    reason_ref, detail_ref = skipped_dict[sym_refused]
    assert reason_ref == SkipReason.ADJUSTMENT_REFUSED
    assert "SYM_REFUSED" in detail_ref
    assert "Fix with:" in detail_ref
    assert "override add" in detail_ref

    # Verify SYM_NODATA: no_data
    assert sym_nodata in skipped_dict
    reason_no, detail_no = skipped_dict[sym_nodata]
    assert reason_no == SkipReason.NO_DATA
    assert "SYM_NODATA" in detail_no
    assert "Fix with:" in detail_no


# =========================================================================
# Step 7: Screen adjust default "splits": stored == requested -> no actions
# =========================================================================


def test_screen_adjust_default_splits_skips_actions(tmp_project: Any) -> None:
    """When stored basis equals requested basis (splits == splits), no actions are read.

    Symbol with a manual_required corporate action evaluates cleanly.
    """
    fake_src = FakeSource()
    ds, store, act_store = _build_test_data_service(tmp_project, fake_src)

    as_of = date(2026, 11, 12)
    sym = "NSE:EQ:SYM_SPLITS_OK"
    bars = _generate_bars(sym, [150.0] * 100, as_of=as_of)
    fake_src.bars_map["SYM_SPLITS_OK"] = bars
    k = SeriesKey(InstrumentId(sym), "bars", "1d", "raw", "upstox")
    store.commit(k, bars)

    # Action that would cause a refusal if adjust="total" was requested
    bad_act = ActionRecord(
        ex_date=date(2026, 11, 10),
        type="other",
        ratio=None,
        amount=None,
        text="Unrecognised Scheme of Demerger",
        source="upstox",
        source_ref="SYM_SPLITS_OK",
        fetched_at="2026-11-13T00:00:00Z",
        symbol="SYM_SPLITS_OK",
        exchange="NSE",
        status="manual_required",
        id="act_bad_2",
    )
    act_store.write("NSE", "SYM_SPLITS_OK", [bad_act])

    adapter = DataServiceScreenAdapter(ds, default_source="upstox")
    resolver = PluginIndicatorResolver()
    runner = ScreenRunner(adapter, resolver)

    # Default adjust="splits" in screen
    screen = TomlScreenModel(
        name="test_splits_screen",
        timeframe="1d",
        adjust="splits",
        require=[
            RequireRule(
                left=OperandModel(ind="sma", n=50),
                op=">",
                right=OperandModel(const=100.0),
            )
        ],
    )

    result = runner.run(screen, symbols=[sym], as_of=as_of)
    assert len(result.rows) == 1
    assert result.rows[0].passed is True
    assert len(result.skipped) == 0


# =========================================================================
# Step 8: Accumulate raw reads never perform adjustment checks
# =========================================================================


def test_accumulate_raw_read_never_fails_on_adjustment(tmp_project: Any) -> None:
    """data accumulate / fill-only raw reads do not execute adjustment logic.

    Reproduces the INFY (manual_required) and TCS (conflict) scenarios.
    """
    fake_src = FakeSource()
    ds, store, act_store = _build_test_data_service(tmp_project, fake_src)

    # Store raw bars for INFY and TCS
    start_dt = datetime(2025, 1, 1, 9, 15, tzinfo=IST).astimezone(UTC)
    end_dt = datetime(2026, 11, 12, 15, 30, tzinfo=IST).astimezone(UTC)

    infy_bars = _generate_bars("NSE:EQ:INFY", [1500.0] * 50, as_of=date(2026, 11, 12))
    tcs_bars = _generate_bars("NSE:EQ:TCS", [3500.0] * 50, as_of=date(2026, 11, 12))

    k_infy = SeriesKey(InstrumentId("NSE:EQ:INFY"), "bars", "1d", "raw", "upstox")
    k_tcs = SeriesKey(InstrumentId("NSE:EQ:TCS"), "bars", "1d", "raw", "upstox")
    store.commit(k_infy, infy_bars)
    store.commit(k_tcs, tcs_bars)

    # Add INFY manual_required action
    act_store.write(
        "NSE",
        "INFY",
        [
            ActionRecord(
                ex_date=date(2025, 11, 14),
                type="other",
                ratio=None,
                amount=None,
                text="Buy Back",
                source="nse",
                source_ref="INFY",
                fetched_at="2026-11-13T00:00:00Z",
                symbol="INFY",
                exchange="NSE",
                status="manual_required",
                id="infy_act_1",
            )
        ],
    )

    # Add TCS conflict action (ratio disagreement)
    act_store.write(
        "NSE",
        "TCS",
        [
            ActionRecord(
                ex_date=date(2026, 1, 16),
                type="dividend",
                ratio=None,
                amount=Decimal("10.0"),
                text="Dividend",
                source="nse",
                source_ref="TCS",
                fetched_at="2026-11-13T00:00:00Z",
                symbol="TCS",
                exchange="NSE",
                status="conflict",
                id="tcs_act_1",
                reason="ratio disagreement",
            )
        ],
    )

    # Fetch through accumulate path: download="raw", adjust="none", fill_only=False
    slice_infy = ds.get(
        series=k_infy,
        start=start_dt,
        end=end_dt,
        download="raw",
        adjust="none",
        fill_only=False,
    )
    assert len(slice_infy.bars) == 50

    slice_tcs = ds.get(
        series=k_tcs,
        start=start_dt,
        end=end_dt,
        download="raw",
        adjust="none",
        fill_only=False,
    )
    assert len(slice_tcs.bars) == 50


# =========================================================================
# Step 9: Non-price actions purpose classification TOML rules
# =========================================================================


def test_corporate_action_purposes_toml_classification() -> None:
    from plugins.references.nse_actions.parser import parse_nse_item

    # Buy back -> ignored
    recs_bb = parse_nse_item(
        {
            "symbol": "INFY",
            "exDate": "14-Nov-2025",
            "subject": "Buy Back of Equity Shares",
        }
    )
    assert len(recs_bb) == 1
    assert recs_bb[0].type == "ignored"
    assert recs_bb[0].status == "ignored"

    # Annual general meeting -> ignored
    recs_agm = parse_nse_item(
        {
            "symbol": "HDFCLIFE",
            "exDate": "12-Jul-2018",
            "subject": "Annual General Meeting",
        }
    )
    assert len(recs_agm) == 1
    assert recs_agm[0].type == "ignored"
    assert recs_agm[0].status == "ignored"

    # Extraordinary general meeting -> ignored
    recs_egm = parse_nse_item(
        {
            "symbol": "ADANIENT",
            "exDate": "20-Jan-2021",
            "subject": "Extraordinary General Meeting",
        }
    )
    assert len(recs_egm) == 1
    assert recs_egm[0].type == "ignored"
    assert recs_egm[0].status == "ignored"

    # Demerger -> other / manual_required (today's classification kept)
    recs_demerger = parse_nse_item(
        {
            "symbol": "ITC",
            "exDate": "06-Jan-2025",
            "subject": "Demerger",
        }
    )
    assert len(recs_demerger) == 1
    assert recs_demerger[0].type == "other"
    assert recs_demerger[0].status == "manual_required"


# =========================================================================
# Step 11: results list formatting for screen runs
# =========================================================================


def test_results_list_screen_formatting(tmp_project: Any) -> None:
    """results list shows DATE=as-of, START_IST=real start, TRADES='-', NET P&L='-'."""
    from algodesk.results.screen_writer import write_screen_results

    result = ScreenResult(
        name="test_results_fmt_screen",
        as_of=date(2026, 11, 12),
        timeframe="1d",
        rows=[ScreenRow(symbol="SYM1", passed=True, score=1.0)],
        universe_hash="hash1",
        result_hash="res1",
        run_id="screen_test_results_fmt_screen_20261112_20261113_103138",
        verdict="PASS",
    )

    results_dir = tmp_project / "results"
    write_screen_results(results_dir, result, effective_config_text="# config\n")

    runner = CliRunner()
    res = runner.invoke(app, ["results", "list"])
    assert res.exit_code == 0

    lines = [line for line in res.output.splitlines() if "screen_test_results_fmt" in line]
    assert len(lines) == 1
    row_text = lines[0]
    # Verify columns: DATE is 2026-11-12 without ". Mode: screen."
    assert "2026-11-12" in row_text
    assert ". Mode: screen." not in row_text
    # Verify trades and net_pnl are "-"
    parts = [p.strip() for p in row_text.split() if p.strip()]
    # Index 1: DATE, 2: MODE, 3: TRADES, 4: NET P&L
    assert parts[1] == "2026-11-12"
    assert parts[2] == "screen"
    assert parts[3] == "-"
    assert parts[4] == "-"


# =========================================================================
# Step 12: universe.screen resolution at run start
# =========================================================================


def test_universe_screen_resolution_normal(tmp_project: Any) -> None:
    """Run config with universe.screen resolves passed symbols and freezes in journal."""
    from algodesk.config.models import PlatformConfig, RunConfig

    class DummyEvent:
        def __init__(self, payload: dict[str, Any]) -> None:
            self.payload = payload

    class DummyJournal:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict[str, Any]]] = []

        def append(self, name: str, payload: dict[str, Any]) -> None:
            self.events.append((name, payload))

        def read(self, name: str | None = None) -> list[DummyEvent]:
            return [DummyEvent(payload) for n, payload in self.events if name is None or n == name]

    journal = DummyJournal()

    plt = PlatformConfig()

    run_cfg = RunConfig.model_validate(
        {
            "allocated_capital": Decimal("100000"),
            "run": {"name": "screen_univ_test", "mode": Mode.BACKTEST},
            "data_source": {"plugin": "upstox"},
            "execution_venue": {"plugin": "sim"},
            "calendar": {"plugin": "exchange"},
            "secrets": {"plugin": "keychain"},
            "fees": {"schedule": "zerodha_nse"},
            "universe": {"screen": "test_scr"},
            "strategies": [{"plugin": "sma_cross", "risk_profile": "conservative"}],
        }
    )

    class DummyRuntime:
        def __init__(self, journal_obj: Any) -> None:
            self.root = tmp_project
            self.paths = plt.paths
            self.params = None
            self.journal = journal_obj

    rt = DummyRuntime(journal)
    # Inject fake screen result with 2 passed symbols
    fake_screen_res = ScreenResult(
        name="test_scr",
        as_of=date(2026, 11, 12),
        rows=[
            ScreenRow(symbol="NSE:EQ:RELIANCE", passed=True, score=1.0),
            ScreenRow(symbol="NSE:EQ:TCS", passed=True, score=0.8),
            ScreenRow(symbol="NSE:EQ:INFY", passed=False, score=0.2),
        ],
        verdict="PASS",
    )
    rt._fake_screen_result = fake_screen_res

    runner = SessionRunner(
        rt,
        run_config=run_cfg,
        observe=False,
        dry_run=True,
        secrets=None,
        quiet=True,
    )

    inst_map = runner._resolve_universe()
    assert len(inst_map) >= 2
    assert InstrumentId("NSE:EQ:RELIANCE") in inst_map
    assert InstrumentId("NSE:EQ:TCS") in inst_map
    assert InstrumentId("NSE:EQ:INFY") not in inst_map

    # Check journal recorded universe.frozen
    events = list(journal.read("universe.frozen"))
    assert len(events) == 1
    payload = events[0].payload
    assert payload["screen"] == "test_scr"
    assert "NSE:EQ:RELIANCE" in payload["instruments"]
    assert "NSE:EQ:TCS" in payload["instruments"]


def test_universe_screen_missing_refuses(tmp_project: Any) -> None:
    """Missing screen specified in universe.screen raises ConfigError."""
    from algodesk.config.models import PlatformConfig, RunConfig

    plt = PlatformConfig()
    run_cfg = RunConfig.model_validate(
        {
            "allocated_capital": Decimal("100000"),
            "run": {"name": "screen_missing_test", "mode": Mode.BACKTEST},
            "data_source": {"plugin": "upstox"},
            "execution_venue": {"plugin": "sim"},
            "calendar": {"plugin": "exchange"},
            "secrets": {"plugin": "keychain"},
            "fees": {"schedule": "zerodha_nse"},
            "universe": {"screen": "non_existent_screen_xyz"},
            "strategies": [{"plugin": "sma_cross", "risk_profile": "conservative"}],
        }
    )

    class DummyRuntime:
        root = tmp_project
        paths = plt.paths
        params = None
        journal = None
        plugins = None

    rt = DummyRuntime()
    runner = SessionRunner(
        rt, run_config=run_cfg, observe=False, dry_run=True, secrets=None, quiet=True
    )

    with pytest.raises(ConfigError) as exc:
        runner._resolve_universe()
    assert (
        "Screen 'non_existent_screen_xyz' specified in universe.screen not found" in exc.value.what
    )


def test_universe_screen_stale_refuses(tmp_project: Any) -> None:
    """Screen result older than screen.max_age_days raises ConfigError."""
    from algodesk.config.models import PlatformConfig, RunConfig

    plt = PlatformConfig()
    run_cfg = RunConfig.model_validate(
        {
            "allocated_capital": Decimal("100000"),
            "run": {"name": "screen_stale_test", "mode": Mode.BACKTEST},
            "data_source": {"plugin": "upstox"},
            "execution_venue": {"plugin": "sim"},
            "calendar": {"plugin": "exchange"},
            "secrets": {"plugin": "keychain"},
            "fees": {"schedule": "zerodha_nse"},
            "universe": {"screen": "stale_scr"},
            "strategies": [{"plugin": "sma_cross", "risk_profile": "conservative"}],
        }
    )

    class DummyParams:
        def get(self, key: str, default: Any = None) -> Any:
            if key == "screen.max_age_days":
                return 7
            return default

    class DummyRuntime:
        root = tmp_project
        paths = plt.paths
        params = DummyParams()
        journal = None

    rt = DummyRuntime()
    # As of date 60 days ago
    stale_date = date.today() - timedelta(days=60)
    fake_screen_res = ScreenResult(
        name="stale_scr",
        as_of=stale_date,
        rows=[ScreenRow(symbol="NSE:EQ:RELIANCE", passed=True, score=1.0)],
        verdict="PASS",
    )
    rt._fake_screen_result = fake_screen_res

    runner = SessionRunner(
        rt, run_config=run_cfg, observe=False, dry_run=True, secrets=None, quiet=True
    )

    with pytest.raises(ConfigError) as exc:
        runner._resolve_universe()
    assert "exceeding screen.max_age_days" in exc.value.what


def test_universe_screen_skip_guard_failed_refuses(tmp_project: Any) -> None:
    """Screen result with verdict FAIL raises ConfigError."""
    from algodesk.config.models import PlatformConfig, RunConfig

    plt = PlatformConfig()
    run_cfg = RunConfig.model_validate(
        {
            "allocated_capital": Decimal("100000"),
            "run": {"name": "screen_fail_test", "mode": Mode.BACKTEST},
            "data_source": {"plugin": "upstox"},
            "execution_venue": {"plugin": "sim"},
            "calendar": {"plugin": "exchange"},
            "secrets": {"plugin": "keychain"},
            "fees": {"schedule": "zerodha_nse"},
            "universe": {"screen": "failing_scr"},
            "strategies": [{"plugin": "sma_cross", "risk_profile": "conservative"}],
        }
    )

    class DummyRuntime:
        root = tmp_project
        paths = plt.paths
        params = None
        journal = None

    rt = DummyRuntime()
    fake_screen_res = ScreenResult(
        name="failing_scr",
        as_of=date.today(),
        rows=[],
        verdict="FAIL",
        fail_reason="Skipped percentage 100.0% exceeds maximum allowed screen.max_skipped_pct",
    )
    rt._fake_screen_result = fake_screen_res

    runner = SessionRunner(
        rt, run_config=run_cfg, observe=False, dry_run=True, secrets=None, quiet=True
    )

    with pytest.raises(ConfigError) as exc:
        runner._resolve_universe()
    assert "failed" in exc.value.what


# =========================================================================
# Step 13: screen run --timing output
# =========================================================================


def test_screen_run_timing_output(tmp_project: Any) -> None:
    """screen run --timing prints breakdown (reads, indicators, rules, writing) and throughput."""
    # Write a test screen file in config/screens
    cfg_screens = tmp_project / "config" / "screens"
    cfg_screens.mkdir(parents=True, exist_ok=True)
    screen_toml = cfg_screens / "timing_test.toml"
    screen_toml.write_text(
        'name = "timing_test"\n'
        'timeframe = "1d"\n'
        'adjust = "splits"\n'
        "[[require]]\n"
        'left = { ind = "sma", n = 5 }\n'
        'op = ">"\n'
        "right = { const = 50.0 }\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "screen",
            "run",
            "timing_test",
            "--symbols",
            "RELIANCE,TCS",
            "--timing",
        ],
    )
    # Output should contain Timing line
    assert "Timing:" in res.output
    assert "reads=" in res.output
    assert "indicators=" in res.output
    assert "rules=" in res.output
    assert "writing=" in res.output
    assert "symbols/s" in res.output
