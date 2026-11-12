"""Unit tests for SMA crossover strategy plugin."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

from algodesk.api import (
    InstrumentId,
    OrderType,
    PlaceOrder,
    PluginManifest,
    Product,
    Side,
)
from algodesk.api.conformance import strategy_problems
from algodesk.api.market import Bar
from algodesk.api.protocols import StrategyContext
from algodesk.api.trading import Position

from ..plugin import SMACrossStrategy


def _make_manifest() -> PluginManifest:
    return PluginManifest(
        name="sma_cross",
        kind="strategy",  # type: ignore[arg-type]
        version="0.1.0",
        api_version=1,
        entry="plugin:SMACrossStrategy",
        capabilities={"paper": True, "backtest": True, "live": False},
    )


def test_sma_cross_conformance() -> None:
    manifest = _make_manifest()
    strat = SMACrossStrategy(manifest, config={"fast": 5, "slow": 10, "qty": 10})
    problems = strategy_problems(strat)
    assert not problems, f"Conformance problems: {problems}"


def test_sma_cross_signals_and_timers() -> None:
    manifest = _make_manifest()
    inst = InstrumentId("NSE:EQ:RELIANCE")
    cfg = {"instruments": ["NSE:EQ:RELIANCE"], "fast": 5, "slow": 10, "qty": 15, "stop_pct": 0.05}
    strat = SMACrossStrategy(manifest, config=cfg)

    # Mock context
    clock = MagicMock()
    now_dt = datetime(2026, 10, 26, 9, 30, tzinfo=UTC)  # 15:00 IST
    clock.now.return_value = now_dt

    indicator_vals: dict[tuple[str, int], float] = {
        ("sma", 5): 100.0,
        ("sma", 10): 102.0,
    }
    pos_mock: Position | None = None

    def _ind_fn(name: str, instrument: Any, tf: str, **kw: Any) -> float | None:
        period = kw.get("period", 5)
        return indicator_vals.get((name, period))

    ctx = StrategyContext(
        clock=clock,
        instruments={inst: MagicMock()},
        indicator=_ind_fn,
        position=lambda i: pos_mock,
    )
    strat.on_start(ctx)

    # Bar 1: Fast = 100, Slow = 102 (Fast < Slow)
    b1 = Bar(
        instrument=inst,
        timeframe="5m",
        start_ts=now_dt,
        end_ts=now_dt,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
        volume=1000,
        complete=True,
        synthetic=False,
    )
    intents = strat.on_bar(ctx, b1)
    assert len(intents) == 0

    # Bar 2: Fast crosses strictly above slow -> Fast = 105, Slow = 103
    indicator_vals[("sma", 5)] = 105.0
    indicator_vals[("sma", 10)] = 103.0
    b2 = Bar(
        instrument=inst,
        timeframe="5m",
        start_ts=now_dt,
        end_ts=now_dt,
        open=102.0,
        high=106.0,
        low=102.0,
        close=105.0,
        volume=1200,
        complete=True,
        synthetic=False,
    )
    intents = strat.on_bar(ctx, b2)
    assert len(intents) == 1
    intent = intents[0]
    assert isinstance(intent, PlaceOrder)
    assert intent.spec.side == Side.BUY
    assert intent.spec.qty == 15
    assert intent.spec.type == OrderType.MARKET

    # Simulate position opened
    pos_mock = Position(
        instrument=inst,
        product=Product.CNC,
        qty=15,
        avg_price=Decimal("105.0"),
        realized=Decimal("0.0"),
        unrealized=Decimal("0.0"),
    )

    # Bar 3: Fast crosses below slow -> Fast = 101, Slow = 104 -> Exits
    indicator_vals[("sma", 5)] = 101.0
    indicator_vals[("sma", 10)] = 104.0
    b3 = Bar(
        instrument=inst,
        timeframe="5m",
        start_ts=now_dt,
        end_ts=now_dt,
        open=105.0,
        high=105.0,
        low=100.0,
        close=101.0,
        volume=800,
        complete=True,
        synthetic=False,
    )
    intents = strat.on_bar(ctx, b3)
    assert len(intents) == 1
    assert intents[0].spec.side == Side.SELL
    assert intents[0].spec.qty == 15

    # Test state and restore
    state = strat.state()
    strat2 = SMACrossStrategy(manifest, config=cfg)
    strat2.restore(state)
    assert strat2._prev_fast == strat._prev_fast
    assert strat2._prev_slow == strat._prev_slow

    # Test on_timer square-off
    timer_intents = strat.on_timer(ctx, "exit_time")
    assert len(timer_intents) == 1
    assert timer_intents[0].spec.side == Side.SELL


def test_sma_cross_no_new_entry_at_or_after_exit_time() -> None:
    """Strategy must emit no new entry intents at or after exit_time."""
    manifest = _make_manifest()
    strat = SMACrossStrategy(
        manifest=manifest,
        config={
            "instruments": ["NSE:EQ:INFY"],
            "fast": 3,
            "slow": 8,
            "qty": 1,
            "exit_time": "15:15",
            "product": "MIS",
        },
    )
    inst = InstrumentId("NSE:EQ:INFY")

    clock = MagicMock()
    # 09:45 UTC is 15:15 IST (at exit_time)
    t_exit = datetime(2026, 11, 11, 9, 45, tzinfo=UTC)
    clock.now.return_value = t_exit

    ctx = StrategyContext(
        clock=clock,
        instruments={inst: MagicMock()},
        indicator=lambda name, instrument, tf, **kw: 105.0 if kw.get("period") == 3 else 100.0,
        position=lambda i: None,
    )
    strat.on_start(ctx)

    # Initialize previous values with fast <= slow
    strat._prev_fast[inst] = 99.0
    strat._prev_slow[inst] = 101.0

    b_at_exit = Bar(
        instrument=inst,
        timeframe="1m",
        start_ts=t_exit - timedelta(minutes=1),
        end_ts=t_exit,
        open=100.0,
        high=106.0,
        low=100.0,
        close=105.0,
        volume=1000,
        complete=True,
        synthetic=False,
    )

    # Cross occurs at 15:15 IST -> blocked
    intents = strat.on_bar(ctx, b_at_exit)
    assert len(intents) == 0

    # Cross occurs after exit_time (15:20 IST) -> blocked
    t_after = datetime(2026, 11, 11, 9, 50, tzinfo=UTC)
    clock.now.return_value = t_after
    b_after = Bar(
        instrument=inst,
        timeframe="1m",
        start_ts=t_after - timedelta(minutes=1),
        end_ts=t_after,
        open=100.0,
        high=106.0,
        low=100.0,
        close=105.0,
        volume=1000,
        complete=True,
        synthetic=False,
    )
    intents_after = strat.on_bar(ctx, b_after)
    assert len(intents_after) == 0
