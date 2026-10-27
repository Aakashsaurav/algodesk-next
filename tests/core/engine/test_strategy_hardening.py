"""Hardening and boundary tests for IndicatorManager, BarBuffer, StrategyContext,
and StrategyRuntime.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from algodesk.api import (
    Bar,
    Instrument,
    InstrumentId,
    Mode,
    OrderSpec,
    OrderType,
    PlaceOrder,
    Product,
    RiskDecision,
    Side,
    Validity,
)
from algodesk.engine.bus import EventBus
from algodesk.engine.clock import SimClock
from algodesk.engine.context import BarBuffer, StrategyContext, build_strategy_context
from algodesk.engine.indicator_manager import IndicatorManager
from algodesk.engine.strategy_runtime import StrategyRuntime
from algodesk.ops.context import build_context
from algodesk.ops.runtime import open_runtime


def _make_bar(
    seq: int,
    symbol: str = "NSE:EQ:RELIANCE",
    timeframe: str = "5m",
    close: float = 100.0,
) -> Bar:
    base = datetime(2026, 10, 26, 9, 15, 0, tzinfo=UTC)
    start_ts = base + timedelta(minutes=seq * 5)
    end_ts = start_ts + timedelta(minutes=5)
    return Bar(
        instrument=InstrumentId(symbol),
        timeframe=timeframe,
        start_ts=start_ts,
        end_ts=end_ts,
        open=float(close - 1),
        high=float(close + 2),
        low=float(close - 2),
        close=float(close),
        volume=1000,
        complete=True,
    )


@pytest.mark.parametrize("window", [2, 3, 5, 8, 10, 15, 20, 25, 30, 50])
def test_indicator_manager_sma_incremental_matches_batch(window: int) -> None:
    with open_runtime(Path("."), ensure=False) as rt:
        p_ctx = build_context(Mode.RESEARCH, run_id="test_ind")
        ind_plugin = rt.plugins.create("indicator", "builtin", config={}, ctx=p_ctx)
        ind_mgr = IndicatorManager(indicator_plugin=ind_plugin)
        inst = InstrumentId("NSE:EQ:RELIANCE")
        tf = "5m"
        ind_mgr.register("sma", inst, tf, period=window)

        prices = [100.0 + (i * 0.5) if i % 2 == 0 else 100.0 - (i * 0.3) for i in range(60)]
        for idx, p in enumerate(prices):
            bar = _make_bar(idx, symbol=str(inst), timeframe=tf, close=p)
            ind_mgr.on_bar(bar)
            val = ind_mgr.get("sma", inst, tf, period=window)
            if idx + 1 >= window:
                expected = sum(prices[idx + 1 - window : idx + 1]) / window
                assert val is not None
                assert abs(val - expected) < 1e-4
            else:
                assert val is None


@pytest.mark.parametrize("lookback", [1, 2, 5, 10, 20, 50])
def test_bar_buffer_lookback_slicing(lookback: int) -> None:
    buf = BarBuffer(max_len=100)
    inst = InstrumentId("NSE:EQ:INFY")

    for i in range(60):
        b = _make_bar(i, symbol=str(inst), close=100.0 + i)
        buf.append(b)

    bars = buf.get(inst, "5m", n=lookback)
    assert len(bars) == lookback
    assert float(bars[-1].close) == 100.0 + 59
    assert float(bars[0].close) == 100.0 + 59 - (lookback - 1)


@pytest.mark.parametrize("invalid_window", [0, -1, -5, -100])
def test_bar_buffer_invalid_lookback(invalid_window: int) -> None:
    buf = BarBuffer()
    inst = InstrumentId("NSE:EQ:INFY")
    buf.append(_make_bar(0, symbol=str(inst)))
    res = buf.get(inst, "5m", n=invalid_window)
    assert res == ()


def test_strategy_context_instruments_mapping() -> None:
    inst1 = Instrument(
        id=InstrumentId("NSE:EQ:RELIANCE"),
        exchange="NSE",
        segment="EQ",
        symbol="RELIANCE",
        kind="EQUITY",
        lot_size=1,
        tick_size=Decimal("0.05"),
    )
    inst2 = Instrument(
        id=InstrumentId("NSE:EQ:TCS"),
        exchange="NSE",
        segment="EQ",
        symbol="TCS",
        kind="EQUITY",
        lot_size=1,
        tick_size=Decimal("0.05"),
    )
    clock = SimClock(datetime(2026, 10, 26, 9, 15, tzinfo=UTC))
    ind_mgr = IndicatorManager()
    buf = BarBuffer()

    ctx = build_strategy_context(
        clock=clock,
        instruments={"NSE:EQ:RELIANCE": inst1, "NSE:EQ:TCS": inst2},
        indicator_manager=ind_mgr,
        bar_buffer=buf,
    )

    assert ctx.instruments[InstrumentId("NSE:EQ:RELIANCE")] == inst1
    assert ctx.instruments[InstrumentId("NSE:EQ:TCS")] == inst2
    assert InstrumentId("NSE:EQ:UNKNOWN") not in ctx.instruments
    assert ctx.clock.now() == clock.now()


@pytest.mark.parametrize(
    "exception_to_raise",
    [
        ValueError("bad indicator param"),
        ZeroDivisionError("division by zero in formula"),
        KeyError("missing key in state"),
        IndexError("buffer out of bounds"),
        RuntimeError("unexpected runtime failure"),
    ],
)
def test_strategy_runtime_handler_exception_isolation(
    exception_to_raise: Exception,
) -> None:
    async def _run() -> None:
        clock = SimClock(datetime(2026, 10, 26, 9, 15, tzinfo=UTC))
        journal = MagicMock()
        bus = EventBus()
        oms = MagicMock()
        risk_engine = MagicMock()
        registry = MagicMock()

        strat_rt = StrategyRuntime(
            registry=registry,
            order_manager=oms,
            risk_engine=risk_engine,
            risk_view_builder=lambda: MagicMock(),
            journal=journal,
            clock=clock,
            bus=bus,
        )

        bad_strategy = MagicMock()
        bad_strategy.on_bar.side_effect = exception_to_raise
        bad_strategy.on_tick.side_effect = exception_to_raise
        bad_strategy.on_order_event.side_effect = exception_to_raise

        ctx = MagicMock(spec=StrategyContext)
        strat_rt.load_strategy("bad_strat", {}, ctx, strategy_instance=bad_strategy)

        bar = _make_bar(0)
        await strat_rt.dispatch_bar(bar)
        assert strat_rt.is_halted("bad_strat")

    asyncio.run(_run())


@pytest.mark.parametrize(
    ("intent_side", "qty", "price", "risk_approved", "expect_order_submitted"),
    [
        (Side.BUY, 10, 2500.0, True, True),
        (Side.BUY, 20, 2500.0, False, False),
        (Side.SELL, 10, 2500.0, True, True),
        (Side.SELL, 50, 2500.0, False, False),
        (Side.BUY, 100, 100.0, True, True),
        (Side.SELL, 100, 100.0, False, False),
        (Side.BUY, 1, 3000.0, True, True),
        (Side.BUY, 1, 3000.0, False, False),
    ],
)
def test_strategy_runtime_order_intent_routing_and_risk(
    intent_side: Side,
    qty: int,
    price: float,
    risk_approved: bool,
    expect_order_submitted: bool,
) -> None:
    async def _run() -> None:
        clock = SimClock(datetime(2026, 10, 26, 9, 15, tzinfo=UTC))
        journal = MagicMock()
        bus = EventBus()
        oms = AsyncMock()
        risk_engine = MagicMock()

        risk_decision = RiskDecision(
            allow=risk_approved,
            rule="test_rule",
            reason="" if risk_approved else "risk rule breach",
        )
        risk_engine.evaluate.return_value = risk_decision

        strat_rt = StrategyRuntime(
            registry=MagicMock(),
            order_manager=oms,
            risk_engine=risk_engine,
            risk_view_builder=lambda: MagicMock(),
            journal=journal,
            clock=clock,
            bus=bus,
        )

        order_spec = OrderSpec(
            instrument=InstrumentId("NSE:EQ:RELIANCE"),
            side=intent_side,
            qty=qty,
            product=Product.MIS,
            type=OrderType.LIMIT,
            price=Decimal(str(price)),
            validity=Validity.DAY,
        )
        intent = PlaceOrder(
            spec=order_spec,
            intent_id="strat_ref_1",
        )

        strat = MagicMock()
        strat.on_bar.return_value = [intent]
        ctx = MagicMock(spec=StrategyContext)
        strat_rt.load_strategy("test_strat", {}, ctx, strategy_instance=strat)

        bar = _make_bar(0)
        await strat_rt.dispatch_bar(bar)

        if expect_order_submitted:
            oms.submit.assert_awaited_once()
        else:
            oms.submit.assert_not_awaited()

    asyncio.run(_run())


@pytest.mark.parametrize("seq_count", [5, 10, 15, 20, 25])
def test_bar_buffer_fifo_eviction(seq_count: int) -> None:
    max_capacity = 10
    buf = BarBuffer(max_len=max_capacity)
    inst = InstrumentId("NSE:EQ:SBIN")

    for i in range(seq_count):
        buf.append(_make_bar(i, symbol=str(inst), close=500.0 + i))

    all_stored = buf.get(inst, "5m", n=100)
    assert len(all_stored) <= max_capacity
    expected_len = min(seq_count, max_capacity)
    assert len(all_stored) == expected_len
    assert float(all_stored[-1].close) == 500.0 + seq_count - 1
