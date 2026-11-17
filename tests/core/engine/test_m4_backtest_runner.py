"""Tests for Steps 15 & 16: BacktestRunner architecture, properties, and measurement."""

from __future__ import annotations

import asyncio
import inspect
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

from plugins.venues.sim.venue import SimVenue

from algodesk.api import (
    InstrumentId,
    OrderSpec,
    OrderState,
    OrderType,
    PlaceOrder,
    Product,
    Side,
)
from algodesk.api.kinds import Mode
from algodesk.api.market import Bar, Level, Quote
from algodesk.engine.backtest import BacktestRunner
from algodesk.engine.clock import SimClock
from algodesk.engine.runner import SessionRunner


def _make_quote(
    instrument: InstrumentId,
    ts: datetime,
    bid_p: float,
    ask_p: float,
    qty: int = 100,
    ltp: float | None = None,
) -> Quote:
    p_ltp = ltp if ltp is not None else ask_p
    return Quote(
        instrument=instrument,
        exch_ts=ts,
        recv_ts=ts,
        bids=(Level(price=bid_p, qty=qty),),
        asks=(Level(price=ask_p, qty=qty),),
        ltp=p_ltp,
        volume=qty,
    )


# ============================================================================
# Step 15: Architecture test
# ============================================================================


def test_backtest_runner_architecture_composition() -> None:
    """BacktestRunner composes SessionRunner and does NOT copy risk, OMS, or ledger logic."""
    # 1. BacktestRunner class inspection
    source = inspect.getsource(BacktestRunner)

    # Must NOT define its own risk engine, order manager, ledger, or execution loop
    assert "class OrderManager" not in source
    assert "class RiskEngine" not in source
    assert "class PortfolioLedger" not in source
    assert "def execute_session" not in source
    assert "def _execute_session" not in source

    # BacktestRunner module imports must NOT import internal risk, oms, or ledger classes
    module = inspect.getmodule(BacktestRunner)
    assert module is not None
    mod_source = inspect.getsource(module)
    assert "from algodesk.oms" not in mod_source
    assert "from algodesk.risk" not in mod_source
    assert "from algodesk.portfolio" not in mod_source

    # 2. Composition verification
    mock_rt = MagicMock()
    runner = BacktestRunner(runtime=mock_rt, run_name="test_run", quiet=True)
    assert hasattr(runner, "session_runner")
    assert isinstance(runner.session_runner, SessionRunner)
    assert runner.mode == Mode.BACKTEST
    assert runner.session_runner.mode == Mode.BACKTEST

    # run() must delegate directly to session_runner.run()
    runner.session_runner.run = MagicMock(return_value=Path("/tmp/results/test"))  # type: ignore[method-assign]
    ret = runner.run()
    assert ret == Path("/tmp/results/test")
    runner.session_runner.run.assert_called_once()


# ============================================================================
# Step 16: Property and table tests
# ============================================================================


def test_property_no_fill_uses_quote_earlier_than_decision() -> None:
    """No fill uses a quote earlier than the decision time (INV-1, delay_bars=1)."""

    async def _run() -> None:
        venue = SimVenue(params={"seed": 42, "latency_ms": 0})
        inst = InstrumentId("NSE:EQ:RELIANCE")
        venue.configure([inst])

        t_quote_earlier = datetime(2026, 11, 10, 9, 59, tzinfo=UTC)
        t_decision = datetime(2026, 11, 10, 10, 0, tzinfo=UTC)  # 15:30 IST
        t_quote_later = datetime(2026, 11, 11, 3, 45, tzinfo=UTC)

        # 1. Establish initial quote at earlier timestamp
        q_early = _make_quote(inst, t_quote_earlier, 100.0, 100.05, qty=100)
        venue.on_quote(q_early)

        # 2. Submit order decided at t_decision (after q_early)
        spec = OrderSpec(
            instrument=inst,
            side=Side.BUY,
            type=OrderType.MARKET,
            qty=10,
            product=Product.CNC,
        )
        await venue.place(spec, client_ref="ord_early_check")

        # Feed an old quote (or same timestamp as earlier) -> No fills
        venue.on_quote(q_early)
        st_early = venue.order_status("ord_early_check")
        assert st_early is not None
        assert st_early.filled_qty == 0

        # 3. Feed quote from later timestamp (next bar open) -> Fills
        q_late = _make_quote(inst, t_quote_later, 101.0, 101.05, qty=100)
        venue.on_quote(q_late)

        st_late = venue.order_status("ord_early_check")
        assert st_late is not None
        assert st_late.state == OrderState.FILLED
        assert st_late.filled_qty == 10

        fills = [
            e
            for e in venue.drain_events()
            if e.kind == "fill" and e.client_ref == "ord_early_check"
        ]
        assert len(fills) == 1
        assert fills[0].ts >= t_decision
        assert fills[0].ts >= t_quote_later

    asyncio.run(_run())


def test_property_fills_only_at_synthetic_quote_prices_plus_slippage() -> None:
    """Fills only occur at synthetic quote prices plus configured slippage."""

    async def _run() -> None:
        slippage_bps = 10  # 10 bps = 0.1%
        venue = SimVenue(
            params={"seed": 42, "backtest.slippage_bps": slippage_bps, "latency_ms": 0}
        )
        inst = InstrumentId("NSE:EQ:INFY")
        venue.configure([inst])
        venue.allow_short = True

        t0 = datetime(2026, 11, 10, 3, 45, tzinfo=UTC)
        venue.on_quote(_make_quote(inst, t0, 1000.0, 1000.0, qty=100))

        # Market BUY: ask * (1 + 0.001)
        spec_buy = OrderSpec(
            instrument=inst, side=Side.BUY, type=OrderType.MARKET, qty=5, product=Product.CNC
        )
        await venue.place(spec_buy, client_ref="buy_mkt")

        t1 = t0 + timedelta(seconds=1)
        venue.on_quote(_make_quote(inst, t1, 1000.0, 1000.0, qty=100))

        st_buy = venue.order_status("buy_mkt")
        assert st_buy is not None
        assert st_buy.state == OrderState.FILLED
        # 1000 * 1.001 = 1001.00
        assert st_buy.avg_price == Decimal("1001.00")

        # Market SELL: bid * (1 - 0.001)
        spec_sell = OrderSpec(
            instrument=inst, side=Side.SELL, type=OrderType.MARKET, qty=5, product=Product.CNC
        )
        await venue.place(spec_sell, client_ref="sell_mkt")

        t2 = t1 + timedelta(seconds=1)
        venue.on_quote(_make_quote(inst, t2, 1000.0, 1000.0, qty=100))

        st_sell = venue.order_status("sell_mkt")
        assert st_sell is not None
        assert st_sell.state == OrderState.FILLED
        # 1000 * 0.999 = 999.00
        assert st_sell.avg_price == Decimal("999.00")

        # LIMIT order: fills at limit price / quote price with 0 slippage
        spec_limit = OrderSpec(
            instrument=inst,
            side=Side.BUY,
            type=OrderType.LIMIT,
            qty=5,
            price=Decimal("1000.00"),
            product=Product.CNC,
        )
        await venue.place(spec_limit, client_ref="buy_lim")

        t3 = t2 + timedelta(seconds=1)
        venue.on_quote(_make_quote(inst, t3, 1000.0, 1000.0, qty=100))

        st_lim = venue.order_status("buy_lim")
        assert st_lim is not None
        assert st_lim.state == OrderState.FILLED
        assert st_lim.avg_price == Decimal("1000.00")

    asyncio.run(_run())


def test_property_partial_fills_sum_to_order_quantity() -> None:
    """Partial fills across successive quotes strictly sum to original order quantity."""

    async def _run() -> None:
        venue = SimVenue(
            params={
                "seed": 42,
                "backtest.slippage_bps": 0,
                "sim.latency_ms": 0,
                "sim.through_ticks": 0,
            }
        )
        inst = InstrumentId("NSE:EQ:TCS")
        venue.configure([inst])

        t0 = datetime(2026, 11, 10, 3, 45, tzinfo=UTC)
        venue.on_quote(_make_quote(inst, t0, 3000.0, 3000.0, qty=100))

        spec = OrderSpec(
            instrument=inst,
            side=Side.BUY,
            type=OrderType.LIMIT,
            qty=50,
            price=Decimal("3000.00"),
            product=Product.CNC,
        )
        await venue.place(spec, client_ref="ord_multi_fill")

        # Quote 1: depth = 20
        t1 = t0 + timedelta(seconds=1)
        venue.on_quote(_make_quote(inst, t1, 3000.0, 3000.0, qty=20))
        st1 = venue.order_status("ord_multi_fill")
        assert st1 is not None
        assert st1.filled_qty == 20
        assert st1.state == OrderState.PARTIALLY_FILLED

        # Quote 2: depth = 15
        t2 = t1 + timedelta(seconds=1)
        venue.on_quote(_make_quote(inst, t2, 3000.0, 3000.0, qty=15))
        st2 = venue.order_status("ord_multi_fill")
        assert st2 is not None
        assert st2.filled_qty == 35
        assert st2.state == OrderState.PARTIALLY_FILLED

        # Quote 3: depth = 30 (more than remaining 15)
        t3 = t2 + timedelta(seconds=1)
        venue.on_quote(_make_quote(inst, t3, 3000.0, 3000.0, qty=30))
        st3 = venue.order_status("ord_multi_fill")
        assert st3 is not None
        assert st3.filled_qty == 50
        assert st3.state == OrderState.FILLED

        fills = [
            e
            for e in venue.drain_events()
            if e.kind in ("fill", "partial") and e.client_ref == "ord_multi_fill"
        ]
        total_filled = sum(e.qty for e in fills)
        assert total_filled == 50

    asyncio.run(_run())


def test_property_gap_through_stop_fills_at_open() -> None:
    """A resting stop order crossed by the opening quote fills at that open price."""

    async def _run() -> None:
        venue = SimVenue(params={"seed": 42, "backtest.slippage_bps": 0, "latency_ms": 0})
        inst = InstrumentId("NSE:EQ:HDFCBANK")
        venue.configure([inst])
        venue.allow_short = True

        t0 = datetime(2026, 11, 10, 10, 0, tzinfo=UTC)
        venue.on_quote(_make_quote(inst, t0, 100.0, 100.0, qty=100))

        # 1. BUY STOP_MARKET with trigger_price 102.0
        # Next bar opens with gap-up at 105.0. Must fill at 105.0, NOT 102.0!
        spec_buy_stop = OrderSpec(
            instrument=inst,
            side=Side.BUY,
            type=OrderType.STOP_MARKET,
            qty=10,
            trigger_price=Decimal("102.00"),
            product=Product.CNC,
        )
        await venue.place(spec_buy_stop, client_ref="gap_buy_stop")

        t_open = datetime(2026, 11, 11, 3, 45, tzinfo=UTC)
        venue.on_quote(_make_quote(inst, t_open, 105.0, 105.0, qty=100, ltp=105.0))
        st_buy = venue.order_status("gap_buy_stop")
        assert st_buy is not None
        assert st_buy.state == OrderState.FILLED
        assert st_buy.avg_price == Decimal("105.00")

        # 2. SELL STOP_MARKET with trigger_price 98.0
        # Next bar opens with gap-down at 95.0. Must fill at 95.0, NOT 98.0!
        spec_sell_stop = OrderSpec(
            instrument=inst,
            side=Side.SELL,
            type=OrderType.STOP_MARKET,
            qty=10,
            trigger_price=Decimal("98.00"),
            product=Product.CNC,
        )
        await venue.place(spec_sell_stop, client_ref="gap_sell_stop")

        venue.on_quote(
            _make_quote(inst, t_open + timedelta(seconds=1), 95.0, 95.0, qty=100, ltp=95.0)
        )
        st_sell = venue.order_status("gap_sell_stop")
        assert st_sell is not None
        assert st_sell.state == OrderState.FILLED
        assert st_sell.avg_price == Decimal("95.00")

    asyncio.run(_run())


def test_property_seed_reproducibility() -> None:
    """Same seed gives identical orders and fills; different seed alters only seeded parts."""

    async def _simulate_run(seed: int) -> tuple[Decimal, int]:
        inst = InstrumentId("NSE:EQ:SBIN")
        t0 = datetime(2026, 11, 10, 3, 45, tzinfo=UTC)
        t1 = t0 + timedelta(seconds=1)

        v = SimVenue(params={"seed": seed, "backtest.slippage_bps": 5, "latency_ms": 0})
        v.configure([inst])
        v.on_quote(_make_quote(inst, t0, 500.0, 500.0, qty=50))
        s = OrderSpec(
            instrument=inst, side=Side.BUY, type=OrderType.MARKET, qty=10, product=Product.CNC
        )
        await v.place(s, client_ref="ord_seed")
        v.on_quote(_make_quote(inst, t1, 500.0, 500.0, qty=50))
        st = v.order_status("ord_seed")
        assert st is not None
        return st.avg_price, st.filled_qty

    price_a, qty_a = asyncio.run(_simulate_run(42))
    price_b, qty_b = asyncio.run(_simulate_run(42))

    assert price_a == price_b
    assert qty_a == qty_b

    price_c, qty_c = asyncio.run(_simulate_run(999))
    # Deterministic price and quantity remain identical
    assert price_c == price_a
    assert qty_c == qty_a


def test_property_risk_rules_apply_unchanged() -> None:
    """Kill-switch and risk rules apply unchanged in backtest mode."""
    from algodesk.journal.writer import Journal
    from algodesk.risk.engine import RiskEngine
    from algodesk.risk.killswitch import KillSwitch

    clock = SimClock(datetime(2026, 11, 10, 3, 45, tzinfo=UTC))
    journal = MagicMock(spec=Journal)

    ks_file = Path("/tmp/ks_test_m4.json")
    if ks_file.exists():
        ks_file.unlink()

    killswitch = KillSwitch(state_file=ks_file, journal=journal, clock=clock)
    # Trip kill-switch
    killswitch.trip(reason="Daily loss limit hit")

    risk_engine = RiskEngine(killswitch=killswitch, rules=[], journal=journal)

    # Any order submitted through RiskEngine must be rejected
    inst = InstrumentId("NSE:EQ:RELIANCE")
    intent = PlaceOrder(
        intent_id="test_ks_reject",
        spec=OrderSpec(
            instrument=inst, side=Side.BUY, type=OrderType.MARKET, qty=10, product=Product.CNC
        ),
    )

    ctx = MagicMock()
    decision = risk_engine.evaluate(intent, ctx)
    assert not decision.allow
    assert "killswitch" in decision.rule or "kill-switch" in decision.reason.lower()

    if ks_file.exists():
        ks_file.unlink()


def test_benchmark_100_symbols_250_days_full_runner_measurement(tmp_path: Path) -> None:
    """Benchmark: 100 symbols x 250 daily sessions full runner."""
    import math
    import shutil
    from collections.abc import Iterable
    from typing import Any

    from algodesk.api.manifest import PluginManifest
    from algodesk.api.market import SeriesKey
    from algodesk.api.protocols import DataSource
    from algodesk.config.snapshot import build_effective
    from algodesk.data.manifest import DataManifest
    from algodesk.data.service import DataService
    from algodesk.data.store import BarStore
    from algodesk.ops.runtime import open_runtime

    root_dir = Path(__file__).resolve().parents[3]
    project_dir = tmp_path / "bench_proj"
    project_dir.mkdir(parents=True, exist_ok=True)

    shutil.copyfile(root_dir / "pyproject.toml", project_dir / "pyproject.toml")
    shutil.copytree(root_dir / "config", project_dir / "config")
    shutil.copytree(root_dir / "plugins", project_dir / "plugins")
    (project_dir / "data").mkdir(parents=True, exist_ok=True)
    (project_dir / "logs").mkdir(parents=True, exist_ok=True)
    (project_dir / "results").mkdir(parents=True, exist_ok=True)
    (project_dir / ".algodesk").mkdir(parents=True, exist_ok=True)
    conf_json = root_dir / ".algodesk" / "conformance.json"
    if conf_json.is_file():
        shutil.copyfile(conf_json, project_dir / ".algodesk" / "conformance.json")

    num_symbols = 100
    num_days = 250
    symbols = [InstrumentId(f"NSE:EQ:SYM{i:03d}") for i in range(num_symbols)]

    start_date = date(2025, 1, 1)
    sessions: list[date] = []
    cur_d = start_date
    while len(sessions) < num_days:
        if cur_d.weekday() < 5:
            sessions.append(cur_d)
        cur_d += timedelta(days=1)

    bars_by_symbol: dict[InstrumentId, list[Bar]] = {}
    for idx, sym in enumerate(symbols):
        sym_bars: list[Bar] = []
        base_price = 100.0 + (idx % 20) * 10.0
        for s_idx, d in enumerate(sessions):
            factor = 1.0 + 0.10 * math.sin(s_idx * 2 * math.pi / 30.0)
            c_price = round(base_price * factor, 2)
            o_price = round(
                base_price * (1.0 + 0.10 * math.sin((s_idx - 0.5) * 2 * math.pi / 30.0)), 2
            )
            h_price = round(max(o_price, c_price) * 1.01, 2)
            l_price = round(min(o_price, c_price) * 0.99, 2)
            start_ts = datetime(d.year, d.month, d.day, 3, 45, tzinfo=UTC)
            end_ts = datetime(d.year, d.month, d.day, 10, 0, tzinfo=UTC)
            bar = Bar(
                instrument=sym,
                timeframe="1d",
                start_ts=start_ts,
                end_ts=end_ts,
                open=o_price,
                high=h_price,
                low=l_price,
                close=c_price,
                volume=10000,
                complete=True,
                synthetic=False,
            )
            sym_bars.append(bar)
        bars_by_symbol[sym] = sym_bars

    data_dir = project_dir / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(m_path)
    store = BarStore(data_dir=data_dir, manifest=manifest)

    class FakeSource(DataSource):
        def __init__(self, b_map: dict[InstrumentId, list[Bar]]) -> None:
            self.b_map = b_map
            self.manifest = PluginManifest(
                name="upstox",
                version="1.0.0",
                kind="broker",
                api_version=1,
                entry="plugins.brokers.upstox.broker:UpstoxBroker",
                capabilities={"history": True},
                retention={"1d": 36500},
                max_span={"1d": 3650},
            )

        def history(self, key: SeriesKey, start: datetime, end: datetime) -> Iterable[Bar]:
            bars = self.b_map.get(key.instrument, [])
            return [b for b in bars if start <= b.start_ts <= end]

        def instruments(self) -> Iterable[Any]:
            return []

        def stream(self, insts: Any) -> Any:
            raise NotImplementedError

    fake_src = FakeSource(bars_by_symbol)
    ds = DataService(sources={"upstox": fake_src}, default_source="upstox", store=store)
    s_start = datetime(sessions[0].year, sessions[0].month, sessions[0].day, 0, 0, tzinfo=UTC)
    s_end = datetime(sessions[-1].year, sessions[-1].month, sessions[-1].day, 23, 59, tzinfo=UTC)
    for sym in symbols:
        ds.get(f"{sym}:1d", start=s_start, end=s_end, source="upstox", cache_only=False)

    # Configure run
    cfg_file = project_dir / "config" / "runs" / "bench_100x250.toml"
    sym_list_str = ", ".join(f'"{s}"' for s in symbols)
    cfg_content = f"""allocated_capital = 10000000.0

[run]
name = "bench_100x250"
mode = "backtest"

[universe]
symbols = [{sym_list_str}]

[backtest]
start = "{sessions[0].isoformat()}"
end = "{sessions[-1].isoformat()}"
timeframe = "1d"
fidelity = "bar"

[data_source]
plugin = "upstox"

[execution_venue]
plugin = "sim"
seed = 7

[fees]
schedule = "zerodha_nse"

[[calendars]]
plugin = "exchange"
exchange = "nse"

[secrets]
plugin = "keychain"

[[strategies]]
plugin = "sma_cross"
risk_profile = "conservative"
params = {{ timeframe = "1d", fast = 5, slow = 20, qty = 1, product = "CNC" }}
"""
    cfg_file.write_text(cfg_content)

    with open_runtime(project_dir, run="bench_100x250") as rt:
        object.__setattr__(rt, "_data_service", ds)
        eff = build_effective(rt.root, run_name="bench_100x250")
        runner = BacktestRunner(
            runtime=rt, run_config=eff.run, run_name="bench_100x250", quiet=True
        )
        runner.session_runner._data_service = ds

        t0 = time.perf_counter()
        res_path = runner.run()
        elapsed = time.perf_counter() - t0
        total_bars = num_symbols * num_days
        bars_per_sec = total_bars / elapsed
        print(
            f"\nBENCHMARK RESULT: {total_bars} bars across {num_symbols} symbols x {num_days} days "
            f"in {elapsed:.3f}s ({bars_per_sec:.1f} bars/s)"
        )
        assert res_path.is_dir()
        assert (res_path / "manifest.json").is_file()
        assert elapsed > 0
