"""Tests for M1-18: Paper and replay determinism, timer exit ordering, and latency separation.

Covers:
1. Stable iteration order (instrument ID ascending) for intents, orders, and timer callbacks.
2. Shuffled insertion order and multiple PYTHONHASHSEED subprocess runs giving
   identical ordered signals, orders, fills.
3. Fake-feed session crossing exit_time with 4 positions open, paper and replay equal.
4. Latency separation: timer-origin intents excluded from platform latency (reported as
   timer latency); timer-closed and synthetic bars excluded from bar-close delay
   (reported as timer bar delay).
5. Fake clock boundary timer execution in scheduler.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from plugins.strategies.sma_cross.plugin import SMACrossStrategy
from typer.testing import CliRunner

from algodesk.api import (
    Bar,
    InstrumentId,
    Level,
    Order,
    OrderSpec,
    OrderState,
    OrderType,
    PluginManifest,
    Position,
    Product,
    Quote,
    Side,
    Tick,
    Validity,
)
from algodesk.api.trading import PlaceOrder
from algodesk.cli import app
from algodesk.engine.clock import SimClock
from algodesk.engine.runner import SessionRunner
from algodesk.engine.scheduler import Scheduler
from algodesk.engine.strategy_runtime import StrategyRuntime
from algodesk.ops.runtime import open_runtime
from algodesk.results.compare import compare_runs

IST = ZoneInfo("Asia/Kolkata")


def _make_manifest() -> PluginManifest:
    return PluginManifest(
        name="sma_cross",
        kind="strategy",  # type: ignore[arg-type]
        version="0.1.0",
        api_version=1,
        entry="plugin:SMACrossStrategy",
        capabilities={"paper": True, "backtest": True, "live": False},
    )


def _seed_cache(proj: Path) -> None:
    real_cache = (
        Path(__file__).resolve().parent.parent.parent.parent / ".local" / "cache" / "instruments"
    )
    today = datetime.now(IST).date()
    target = proj / ".local" / "cache" / "instruments"
    target.mkdir(parents=True, exist_ok=True)
    if real_cache.is_dir():
        for f in real_cache.glob("upstox_master_*.json.gz"):
            shutil.copyfile(f, target / f"upstox_master_{today.isoformat()}.json.gz")
            break


# ---------------------------------------------------------------------------
# 1. Deterministic ordering unit tests
# ---------------------------------------------------------------------------


def test_sma_cross_exit_order_ascending_with_shuffled_instruments() -> None:
    """Exit signals are always emitted in ascending instrument ID order regardless of input."""
    manifest = _make_manifest()
    symbols = [
        InstrumentId("NSE:EQ:RELIANCE"),
        InstrumentId("NSE:EQ:TCS"),
        InstrumentId("NSE:EQ:HDFCBANK"),
        InstrumentId("NSE:EQ:INFY"),
        InstrumentId("NSE:EQ:ICICIBANK"),
    ]
    expected_order = sorted(symbols, key=str)

    clock = MagicMock()
    clock.now.return_value = datetime(2026, 11, 18, 9, 45, tzinfo=UTC)

    positions = {
        s: Position(instrument=s, qty=10, avg_price=Decimal("1000"), product=Product.MIS)
        for s in symbols
    }

    ctx = MagicMock()
    ctx.clock = clock
    ctx.position = lambda inst: positions.get(inst)
    ctx.instruments = {s: MagicMock() for s in symbols}

    for perm in itertools.permutations(symbols):
        cfg = {"instruments": [str(s) for s in perm], "fast": 5, "slow": 10, "qty": 10}
        strat = SMACrossStrategy(manifest, config=cfg)
        strat.on_start(ctx)

        intents = strat.on_timer(ctx, "exit_time")
        assert len(intents) == 5
        emitted_instruments = [it.spec.instrument for it in intents]
        assert emitted_instruments == expected_order


def test_sma_cross_on_start_context_instruments_sorted_ascending() -> None:
    """When instruments are unconfigured, on_start sorts context instruments ascending."""
    manifest = _make_manifest()
    cfg = {"fast": 5, "slow": 10, "qty": 10}
    strat = SMACrossStrategy(manifest, config=cfg)

    symbols = [
        InstrumentId("NSE:EQ:TCS"),
        InstrumentId("NSE:EQ:RELIANCE"),
        InstrumentId("NSE:EQ:INFY"),
    ]
    ctx = MagicMock()
    ctx.instruments = {s: MagicMock() for s in symbols}
    strat.on_start(ctx)

    assert list(strat._instruments) == [
        InstrumentId("NSE:EQ:INFY"),
        InstrumentId("NSE:EQ:RELIANCE"),
        InstrumentId("NSE:EQ:TCS"),
    ]


def test_sma_cross_on_timer_only_exits_positive_positions_in_ascending_order() -> None:
    """On timer exit, only open positions (qty > 0) are emitted, in ascending instrument order."""
    manifest = _make_manifest()
    symbols = [
        InstrumentId("NSE:EQ:TCS"),
        InstrumentId("NSE:EQ:RELIANCE"),
        InstrumentId("NSE:EQ:INFY"),
        InstrumentId("NSE:EQ:HDFCBANK"),
    ]
    cfg = {"instruments": [str(s) for s in symbols], "fast": 5, "slow": 10, "qty": 10}
    strat = SMACrossStrategy(manifest, config=cfg)

    clock = MagicMock()
    clock.now.return_value = datetime(2026, 11, 18, 9, 45, tzinfo=UTC)

    # RELIANCE has qty=0 (closed), other 3 have open positions
    positions = {
        InstrumentId("NSE:EQ:TCS"): Position(
            instrument=InstrumentId("NSE:EQ:TCS"),
            qty=5,
            avg_price=Decimal("3000"),
            product=Product.MIS,
        ),
        InstrumentId("NSE:EQ:RELIANCE"): Position(
            instrument=InstrumentId("NSE:EQ:RELIANCE"),
            qty=0,
            avg_price=Decimal("2500"),
            product=Product.MIS,
        ),
        InstrumentId("NSE:EQ:INFY"): Position(
            instrument=InstrumentId("NSE:EQ:INFY"),
            qty=8,
            avg_price=Decimal("1500"),
            product=Product.MIS,
        ),
        InstrumentId("NSE:EQ:HDFCBANK"): Position(
            instrument=InstrumentId("NSE:EQ:HDFCBANK"),
            qty=12,
            avg_price=Decimal("1600"),
            product=Product.MIS,
        ),
    }

    ctx = MagicMock()
    ctx.clock = clock
    ctx.position = lambda inst: positions.get(inst)
    strat.on_start(ctx)

    intents = strat.on_timer(ctx, "exit_time")
    assert len(intents) == 3
    emitted = [it.spec.instrument for it in intents]
    assert emitted == [
        InstrumentId("NSE:EQ:HDFCBANK"),
        InstrumentId("NSE:EQ:INFY"),
        InstrumentId("NSE:EQ:TCS"),
    ]


def test_strategy_runtime_dispatch_timer_processes_intents_ascending() -> None:
    """StrategyRuntime._process_intents preserves stable instrument ID ascending order."""
    recorded_intents: list[str] = []

    def _track(inst: Any, now_dt: datetime, origin: str = "bar", intent: Any = None) -> None:
        recorded_intents.append(str(inst))

    rt = StrategyRuntime(
        clock=SimClock(datetime(2026, 11, 18, 9, 45, tzinfo=UTC)),
        on_intent=_track,
    )

    # Shuffled input intents
    intents = [
        PlaceOrder(
            spec=OrderSpec(
                instrument=InstrumentId("NSE:EQ:TCS"),
                side=Side.SELL,
                qty=5,
                type=OrderType.MARKET,
                product=Product.MIS,
            )
        ),
        PlaceOrder(
            spec=OrderSpec(
                instrument=InstrumentId("NSE:EQ:HDFCBANK"),
                side=Side.SELL,
                qty=10,
                type=OrderType.MARKET,
                product=Product.MIS,
            )
        ),
        PlaceOrder(
            spec=OrderSpec(
                instrument=InstrumentId("NSE:EQ:INFY"),
                side=Side.SELL,
                qty=8,
                type=OrderType.MARKET,
                product=Product.MIS,
            )
        ),
    ]

    asyncio.run(rt._process_intents("strat", intents, origin="timer"))
    assert recorded_intents == ["NSE:EQ:HDFCBANK", "NSE:EQ:INFY", "NSE:EQ:TCS"]


def test_runner_resolve_universe_returns_sorted_inst_map(tmp_project: Path) -> None:
    """SessionRunner._resolve_universe returns dictionary with insertion order sorted ascending."""
    _seed_cache(tmp_project)
    with open_runtime(tmp_project, run="demo_paper_fast") as rt:
        runner = SessionRunner(rt, mode="paper", dry_run=False)
        inst_map = runner._resolve_universe()
        keys = list(inst_map.keys())
        assert keys == sorted(keys, key=str)


def test_runner_universe_markets_sorted_ascending(tmp_project: Path) -> None:
    """SessionRunner._setup_timers sorts universe_markets ascending for timer registration."""
    _seed_cache(tmp_project)
    with open_runtime(tmp_project, run="demo_paper_fast") as rt:
        clock = SimClock(datetime(2026, 11, 18, 9, 44, 0, tzinfo=UTC))
        runner = SessionRunner(rt, mode="paper", clock=clock, dry_run=False)
        journal = runner._init_journal()
        journal.start_run("paper", "0.1.0", "hash", "reg")
        c = runner._build_components(clock, journal)
        runner._setup_timers(c, clock, journal)

        # Scheduler timers registered in sorted market sequence
        scheduled_timers = list(c["scheduler"]._timers.keys())
        close_timers = [t for t in scheduled_timers if t.startswith("market_close_")]
        assert close_timers == sorted(close_timers)


def test_runner_market_close_cancel_orders_sorted_ascending(tmp_project: Path) -> None:
    """Unfillable orders are cancelled in deterministic instrument ID ascending order on close."""
    _seed_cache(tmp_project)
    with open_runtime(tmp_project, run="demo_paper_fast") as rt:
        clock = SimClock(datetime(2026, 11, 18, 9, 44, 0, tzinfo=UTC))
        runner = SessionRunner(rt, mode="paper", clock=clock, dry_run=False)
        journal = runner._init_journal()
        journal.start_run("paper", "0.1.0", "hash", "reg")
        c = runner._build_components(clock, journal)

        cancelled_order_instruments: list[str] = []
        oms = c["oms"]

        orig_cancel = oms.cancel

        async def _mock_cancel(ref: str) -> None:
            ord_obj = oms.orders.get(ref)
            inst = getattr(ord_obj, "instrument", None) or getattr(
                getattr(ord_obj, "spec", None), "instrument", ""
            )
            cancelled_order_instruments.append(str(inst))
            await orig_cancel(ref)

        oms.cancel = _mock_cancel

        # Insert orders in reverse alphabetical order
        for idx, sym in enumerate(
            ["NSE:EQ:TCS", "NSE:EQ:RELIANCE", "NSE:EQ:INFY", "NSE:EQ:HDFCBANK"]
        ):
            o = Order(
                client_ref=f"order-{idx}",
                spec=OrderSpec(
                    instrument=InstrumentId(sym),
                    side=Side.BUY,
                    qty=1,
                    type=OrderType.LIMIT,
                    price=Decimal("10.0"),
                    product=Product.MIS,
                    validity=Validity.DAY,
                ),
                state=OrderState.ACKNOWLEDGED,
                created_ts=clock.now(),
                filled_qty=0,
                avg_price=Decimal("0.0"),
                venue_ref=f"V-{idx}",
            )
            oms._orders[o.client_ref] = o

        # Fire market.closing event
        async def _run_event() -> None:
            from algodesk.engine.sessions import MarketState

            c["session_mgr"].transition("NSE:EQ", MarketState.STARTING, "test")
            c["session_mgr"].transition("NSE:EQ", MarketState.RUNNING, "test")
            p = {"market": "NSE:EQ"}
            runner._recorded_events_by_seq[10] = [
                {"kind": "market.closing", "payload": p, "ts_utc": clock.now().isoformat()}
            ]
            await runner._apply_recorded_events(10, c, clock, journal)

        asyncio.run(_run_event())

        assert cancelled_order_instruments == [
            "NSE:EQ:HDFCBANK",
            "NSE:EQ:INFY",
            "NSE:EQ:RELIANCE",
            "NSE:EQ:TCS",
        ]


# ---------------------------------------------------------------------------
# 2. Latency separation tests
# ---------------------------------------------------------------------------


def test_timer_origin_intents_excluded_from_platform_latency(tmp_project: Path) -> None:
    """Timer-origin intents go to timer_latencies and do not distort platform_latencies."""
    _seed_cache(tmp_project)
    with open_runtime(tmp_project, run="demo_paper_fast") as rt:
        runner = SessionRunner(rt, mode="paper", dry_run=False)
        inst = InstrumentId("NSE:EQ:RELIANCE")
        t0 = datetime(2026, 11, 18, 9, 44, 0, tzinfo=UTC)

        # Bar intent
        runner._last_bar_close_mono[inst] = runner._latest_tick_mono.get(inst, 100.0)
        runner._on_strategy_intent(inst, t0, origin="bar")
        assert len(runner._platform_latencies) == 1
        assert len(runner._timer_latencies) == 0

        # Timer intent (e.g. 15:15 exit timer)
        exit_order = PlaceOrder(
            spec=OrderSpec(
                instrument=inst,
                side=Side.SELL,
                qty=1,
                type=OrderType.MARKET,
                product=Product.MIS,
                tag="exit_time",
            )
        )
        runner._on_strategy_intent(
            inst, t0 + timedelta(minutes=1), origin="timer", intent=exit_order
        )
        assert len(runner._platform_latencies) == 1
        assert len(runner._timer_latencies) == 1


def test_timer_closed_and_synthetic_bars_excluded_from_bar_close_delay(tmp_project: Path) -> None:
    """Synthetic gap-fill bars and timer-closed bars are separated into timer_bar_delays."""
    _seed_cache(tmp_project)
    with open_runtime(tmp_project, run="demo_paper_fast") as rt:
        clock = SimClock(datetime(2026, 11, 18, 9, 44, 0, tzinfo=UTC))
        runner = SessionRunner(rt, mode="paper", clock=clock, dry_run=False)
        journal = runner._init_journal()
        journal.start_run("paper", "0.1.0", "hash", "reg")
        c = runner._build_components(clock, journal)

        inst = InstrumentId("NSE:EQ:RELIANCE")
        t_bar_end = datetime(2026, 11, 18, 9, 45, 0, tzinfo=UTC)
        runner._latest_tick_ts[inst] = t_bar_end + timedelta(minutes=3)  # tick arrived 3 min later

        # Normal tick-closed bar (450ms)
        real_bar = Bar(
            instrument=inst,
            timeframe="1m",
            start_ts=t_bar_end - timedelta(minutes=1),
            end_ts=t_bar_end,
            open=100.0,
            high=105.0,
            low=100.0,
            close=102.0,
            volume=50,
            complete=True,
            synthetic=False,
        )

        async def _test() -> None:
            runner._latest_tick_ts[inst] = t_bar_end + timedelta(milliseconds=450)
            c["bar_builder"]._on_bar_closed(real_bar)

            assert len(runner._bar_close_delays) == 1
            assert runner._bar_close_delays[0] == 450.0
            assert len(runner._timer_bar_delays) == 0

            # Synthetic bar (3 min gap = 180s)
            synth_bar = Bar(
                instrument=inst,
                timeframe="1m",
                start_ts=t_bar_end,
                end_ts=t_bar_end + timedelta(minutes=1),
                open=102.0,
                high=102.0,
                low=102.0,
                close=102.0,
                volume=0,
                complete=True,
                synthetic=True,
            )
            runner._latest_tick_ts[inst] = t_bar_end + timedelta(minutes=3)
            c["bar_builder"]._on_bar_closed(synth_bar)

            # Bar close delay remains at 450ms; synthetic 3-min gap goes to timer_bar_delays
            assert len(runner._bar_close_delays) == 1
            assert len(runner._timer_bar_delays) == 1
            assert runner._timer_bar_delays[0] == 120000.0

        asyncio.run(_test())


def test_session_metrics_and_report_separate_timer_latency_lines(tmp_project: Path) -> None:
    """_finalize_session exports timer latency and timer bar delay as separate metrics."""
    _seed_cache(tmp_project)
    with open_runtime(tmp_project, run="demo_paper_fast") as rt:
        clock = SimClock(datetime(2026, 11, 18, 9, 45, 0, tzinfo=UTC))
        runner = SessionRunner(rt, mode="paper", clock=clock, dry_run=False)
        journal = runner._init_journal()
        journal.start_run("paper", "0.1.0", "hash", "reg")
        c = runner._build_components(clock, journal)

        runner._platform_latencies.append(4.2)
        runner._timer_latencies.append(59501.0)
        runner._bar_close_delays.append(451.0)
        runner._timer_bar_delays.append(180000.0)

        res_path = runner._finalize_session(c, clock, journal)
        metrics = json.loads((res_path / "metrics.json").read_text(encoding="utf-8"))

        assert metrics["platform_p95_ms"] == 4.2
        assert metrics["timer_latency_p95_ms"] == 59501.0
        assert metrics["bar_close_delay_p95_ms"] == 451.0
        assert metrics["timer_bar_delay_p95_ms"] == 180000.0

        session_md = (res_path / "session.md").read_text(encoding="utf-8")
        assert "Timer Latency (p95): 59501.00 ms" in session_md
        assert "Timer Bar Delay (p95): 180000.00 ms" in session_md


def test_scheduler_boundary_timer_with_sim_clock() -> None:
    """BarBuilder boundary timer fires cleanly when SimClock advances past period_end + grace."""
    clock = SimClock(datetime(2026, 11, 18, 9, 15, 0, tzinfo=UTC))
    scheduler = Scheduler(clock)
    closed_bars: list[Bar] = []

    from algodesk.engine.bars import BarBuilder

    builder = BarBuilder(
        timeframes=["1m"],
        scheduler=scheduler,
        bar_close_grace_ms=100,
        on_bar_closed=lambda b: closed_bars.append(b),
    )

    t0 = datetime(2026, 11, 18, 9, 15, 20, tzinfo=UTC)
    inst = InstrumentId("NSE:EQ:INFY")
    builder.on_tick(
        Tick(
            instrument=inst,
            exch_ts=t0,
            recv_ts=t0 + timedelta(milliseconds=5),
            ltp=1500.0,
            ltq=1,
            volume=10,
        )
    )
    assert len(closed_bars) == 0

    # Advance scheduler to 09:16:00.150 UTC (past boundary 09:16:00 + 100ms)
    t_boundary = datetime(2026, 11, 18, 9, 16, 0, 150000, tzinfo=UTC)
    clock.set_time(t_boundary)
    scheduler.advance_to(t_boundary)

    assert len(closed_bars) == 1
    assert closed_bars[0].complete is True
    assert closed_bars[0].end_ts == datetime(2026, 11, 18, 9, 16, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 3. Subprocess PYTHONHASHSEED and full paper/replay equality tests
# ---------------------------------------------------------------------------


def test_three_pythonhashseed_subprocesses_identical_signals_orders_fills(
    tmp_path: Path,
) -> None:
    """Fake-feed session under PYTHONHASHSEED=1, 2, 3 gives identical ordered exit signals."""
    root = Path(__file__).resolve().parent.parent.parent.parent
    results: dict[str, list[str]] = {}

    worker_file = tmp_path / "worker_step3.py"
    worker_file.write_text(
        """import os, sys, shutil, tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo
from algodesk.api import InstrumentId, Product, Side, Tick, Quote
from algodesk.api.trading import Fill
from algodesk.engine.clock import SimClock
from algodesk.engine.runner import SessionRunner
from algodesk.ops.runtime import open_runtime

root = Path(sys.argv[1])
seed = sys.argv[2]
IST = ZoneInfo("Asia/Kolkata")

with tempfile.TemporaryDirectory() as td:
    proj = Path(td) / "project"
    proj.mkdir()
    shutil.copyfile(root / "pyproject.toml", proj / "pyproject.toml")
    shutil.copytree(root / "config", proj / "config")
    shutil.copytree(root / "plugins", proj / "plugins")
    (proj / ".algodesk").mkdir(exist_ok=True)
    shutil.copyfile(
        root / ".algodesk" / "conformance.json",
        proj / ".algodesk" / "conformance.json",
    )
    try:
        os.symlink(root / "src", proj / "src")
    except OSError:
        shutil.copytree(root / "src", proj / "src")

    real_cache = root / ".local" / "cache" / "instruments"
    target = proj / ".local" / "cache" / "instruments"
    target.mkdir(parents=True, exist_ok=True)
    if real_cache.is_dir():
        d_str = datetime.now(IST).date().isoformat()
        for f in real_cache.glob("upstox_master_*.json.gz"):
            shutil.copyfile(f, target / f"upstox_master_{d_str}.json.gz")
            break

    cfg_path = proj / "config" / "runs" / "demo_paper_fast.toml"
    cfg_text = cfg_path.read_text(encoding="utf-8")
    symbols_toml = '''symbols = [
    "NSE:EQ:RELIANCE",
    "NSE:EQ:TCS",
    "NSE:EQ:HDFCBANK",
    "NSE:EQ:INFY",
    "NSE:EQ:ICICIBANK",
]'''
    import re
    cfg_text = re.sub(r"symbols\\s*=\\s*\\[[^\\]]+\\]", symbols_toml, cfg_text)
    cfg_path.write_text(cfg_text, encoding="utf-8")

    with open_runtime(proj, run="demo_paper_fast", ensure=True) as rt:
        t0 = datetime(2026, 11, 18, 9, 44, 0, tzinfo=UTC)
        clock = SimClock(t0)
        runner = SessionRunner(rt, mode="paper", clock=clock, dry_run=False)
        journal = runner._init_journal()
        journal.start_run("paper", "0.1.0", "hash", "reg")
        c = runner._build_components(clock, journal)
        runner._setup_timers(c, clock, journal)

        ledger = c["ledger"]
        pos_symbols = ["NSE:EQ:RELIANCE", "NSE:EQ:TCS", "NSE:EQ:HDFCBANK", "NSE:EQ:INFY"]
        for idx, sym in enumerate(pos_symbols):
            ledger.apply_fill(Fill(
                client_ref=f"init-fill-{idx}",
                instrument=InstrumentId(sym),
                side=Side.BUY,
                qty=10,
                price=Decimal("1000.0"),
                fill_id=f"fill-{idx}",
                ts=t0,
                product=Product.MIS,
            ))
            runner._latest_quotes[InstrumentId(sym)] = Quote(
                instrument=InstrumentId(sym),
                exch_ts=t0,
                recv_ts=t0,
                bids=(),
                asks=(),
                ltp=1000.0,
                volume=100,
            )

        signals_emitted = []
        orig_on_intent = runner._on_strategy_intent
        def _track(inst, now_dt, origin="bar", intent=None):
            signals_emitted.append(str(inst))
            orig_on_intent(inst, now_dt, origin=origin, intent=intent)
        runner._on_strategy_intent = _track
        c["strat_rt"]._on_intent = _track

        # Cross 15:15 exit_time
        t_10 = t0 + timedelta(seconds=10)
        t_70 = t0 + timedelta(seconds=70)
        ticks = [
            Tick(
                instrument=InstrumentId("NSE:EQ:RELIANCE"),
                exch_ts=t_10,
                recv_ts=t_10,
                ltp=1000.0,
                ltq=1,
                volume=100,
            ),
            Tick(
                instrument=InstrumentId("NSE:EQ:RELIANCE"),
                exch_ts=t_70,
                recv_ts=t_70,
                ltp=1000.0,
                ltq=1,
                volume=200,
            ),
        ]
        import asyncio
        for tk in ticks:
            clock.set_time(tk.exch_ts)
            c["scheduler"].advance_to(tk.exch_ts)
            c["scheduler"].fire_due()
            asyncio.run(runner._drain_venue_events(c))

        print(f"EXIT_SIGNALS:{','.join(signals_emitted)}")
""",
        encoding="utf-8",
    )

    for s in ["1", "2", "3"]:
        cmd_str = (
            f"source scripts/env.sh && "
            f"PYTHONHASHSEED={s} uv run python {worker_file} '{root}' '{s}'"
        )
        cmd = ["bash", "-c", cmd_str]
        res = subprocess.run(cmd, capture_output=True, text=True, cwd=root)
        out = res.stdout.strip()
        for line in out.splitlines():
            if "EXIT_SIGNALS:" in line:
                results[s] = line.split("EXIT_SIGNALS:")[1].split(",")
                break
        else:
            pytest.fail(f"Seed {s} failed: {res.stderr or res.stdout}")

    # All three seeds must produce exact identical ascending order
    assert results["1"] == results["2"] == results["3"]
    assert results["1"] == [
        "NSE:EQ:HDFCBANK",
        "NSE:EQ:INFY",
        "NSE:EQ:RELIANCE",
        "NSE:EQ:TCS",
    ]


def test_fixture_crosses_exit_time_with_4_positions_paper_replay_equal(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fixture crossing exit_time with 4 positions open replays equal to paper run."""
    _seed_cache(tmp_project)

    # Configure demo_paper_fast
    cfg_file = tmp_project / "config" / "runs" / "demo_paper_fast.toml"
    cfg_text = cfg_file.read_text(encoding="utf-8")
    cfg_text = cfg_text.replace('[[notifiers]]\\nplugin = "telegram"', "")
    cfg_text = cfg_text.replace('[[notifiers]]\nplugin = "telegram"', "")
    old_p = (
        'params = { timeframe = "1m", fast = 3, slow = 8, qty = 1, '
        'exit_time = "15:15", product = "MIS" }'
    )
    new_p = (
        'params = { timeframe = "1m", fast = 2, slow = 4, qty = 1, '
        'exit_time = "15:15", product = "MIS" }'
    )
    cfg_text = cfg_text.replace(old_p, new_p)
    cfg_file.write_text(cfg_text, encoding="utf-8")

    # Set sim venue latency to 0 for instant deterministic fills
    p_sim_file = tmp_project / "config" / "params" / "sim.toml"
    if p_sim_file.exists():
        sim_toml = p_sim_file.read_text(encoding="utf-8")
        sim_toml = sim_toml.replace("default = 300", "default = 0")
        p_sim_file.write_text(sim_toml, encoding="utf-8")

    symbols = [
        InstrumentId("NSE:EQ:RELIANCE"),
        InstrumentId("NSE:EQ:TCS"),
        InstrumentId("NSE:EQ:HDFCBANK"),
        InstrumentId("NSE:EQ:INFY"),
    ]
    t0 = datetime(2026, 11, 18, 9, 36, 0, tzinfo=UTC)  # 15:06:00 IST

    events: list[Tick | Quote] = []
    cur_t = t0

    def _add_tick_quote(inst: InstrumentId, p: float, vol: int) -> None:
        nonlocal cur_t
        cur_t += timedelta(milliseconds=20)
        t_tick = cur_t
        events.append(
            Tick(
                instrument=inst,
                exch_ts=t_tick,
                recv_ts=t_tick + timedelta(milliseconds=2),
                ltp=p,
                ltq=1,
                volume=vol,
            )
        )
        cur_t += timedelta(milliseconds=20)
        t_quote = cur_t
        events.append(
            Quote(
                instrument=inst,
                exch_ts=t_quote,
                recv_ts=t_quote + timedelta(milliseconds=2),
                bids=(Level(price=p - 1.0, qty=100, orders=1),),
                asks=(Level(price=p + 1.0, qty=100, orders=1),),
                ltp=p,
                volume=vol,
            )
        )

    # Bars 0..7 (15:06..15:13): prices trigger golden cross for fast=2, slow=4
    prices = [2500.0, 2500.0, 2500.0, 2500.0, 2500.0, 2480.0, 2470.0, 2520.0]
    vol = 100
    for m_idx, p in enumerate(prices):
        for s in (10, 30, 50):
            cur_t = t0 + timedelta(minutes=m_idx, seconds=s)
            vol += 10
            for sym in symbols:
                _add_tick_quote(sym, p, vol)

    # 15:14:05 tick: closes 15:13 bar, triggers BUY order for all 4 symbols!
    cur_t = t0 + timedelta(minutes=8, seconds=5)
    for sym in symbols:
        _add_tick_quote(sym, 2520.0, vol + 10)

    # 15:14:15 tick: fills BUY orders, so 4 positions are open
    cur_t = t0 + timedelta(minutes=8, seconds=15)
    for sym in symbols:
        _add_tick_quote(sym, 2520.0, vol + 20)

    # 15:15:05 tick: crosses exit_time (15:15:00 IST), exit timer fires!
    # Triggers SELL exit orders for all 4 symbols in ascending order!
    cur_t = t0 + timedelta(minutes=9, seconds=5)
    for sym in symbols:
        _add_tick_quote(sym, 2520.0, vol + 30)

    # 15:15:15 tick: fills SELL exit orders
    cur_t = t0 + timedelta(minutes=9, seconds=15)
    for sym in symbols:
        _add_tick_quote(sym, 2520.0, vol + 40)

    # 1. Run paper session
    paper_dir: Path
    with open_runtime(tmp_project, run="demo_paper_fast", ensure=True) as rt:
        clock = SimClock(t0)
        runner = SessionRunner(rt, mode="paper", clock=clock, dry_run=False)

        async def _feed_stream(c: dict[str, Any], clk: SimClock):
            for ev in events:
                if ev.exch_ts > clk.now():
                    clk.set_time(ev.exch_ts)
                yield ev
                await asyncio.sleep(0.001)

            # Advance clock past stop_time (15:35:00 IST) to end cleanly
            stop_dt = t0 + timedelta(minutes=30)
            if stop_dt > clk.now():
                clk.set_time(stop_dt)
            c["scheduler"].advance_to(clk.now())
            c["scheduler"].fire_due()

        monkeypatch.setattr(runner, "_build_event_stream", _feed_stream)
        paper_dir = asyncio.run(runner.run())

    assert paper_dir.is_dir()

    # 2. Replay run via CLI
    cli = CliRunner()
    res = cli.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "replay",
            "--session",
            paper_dir.name,
            "--run",
            "demo_paper_fast",
        ],
    )
    assert res.exit_code == 0, f"Replay CLI failed: {res.output}"

    replay_folders = sorted((tmp_project / "results").glob("demo_paper_fast_replay_*"))
    assert len(replay_folders) >= 1
    replay_dir = replay_folders[-1]

    # 3. Compare paper and replay runs
    is_equal, diff = compare_runs(tmp_project, paper_dir.name, replay_dir.name)
    assert is_equal is True, f"Paper and replay not equal: {diff}"


# ---------------------------------------------------------------------------
# 4. Property test with Hypothesis
# ---------------------------------------------------------------------------


@given(
    st.permutations(
        ["NSE:EQ:RELIANCE", "NSE:EQ:TCS", "NSE:EQ:HDFCBANK", "NSE:EQ:INFY", "NSE:EQ:ICICIBANK"]
    )
)
@settings(max_examples=10)
def test_property_hypothesis_shuffled_instruments_order_invariant(perm: list[str]) -> None:
    """Hypothesis property: arbitrary permutation always yields ascending exit signal order."""
    manifest = _make_manifest()
    symbols = [InstrumentId(s) for s in perm]
    cfg = {"instruments": [str(s) for s in symbols], "fast": 5, "slow": 10, "qty": 10}
    strat = SMACrossStrategy(manifest, config=cfg)

    clock = MagicMock()
    clock.now.return_value = datetime(2026, 11, 18, 9, 45, tzinfo=UTC)

    positions = {
        s: Position(instrument=s, qty=10, avg_price=Decimal("1000"), product=Product.MIS)
        for s in symbols
    }

    ctx = MagicMock()
    ctx.clock = clock
    ctx.position = lambda inst: positions.get(inst)
    strat.on_start(ctx)

    intents = strat.on_timer(ctx, "exit_time")
    emitted = [it.spec.instrument for it in intents]
    assert emitted == sorted(symbols, key=str)
