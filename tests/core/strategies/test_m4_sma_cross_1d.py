"""Tests for Step 14: sma_cross on 1d timeframe (Product CNC, crossover only, exit_time ignored)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

from plugins.strategies.sma_cross.plugin import SMACrossStrategy

from algodesk.api import (
    InstrumentId,
    OrderType,
    PlaceOrder,
    PluginManifest,
    Product,
    Side,
)
from algodesk.api.market import Bar
from algodesk.api.protocols import StrategyContext
from algodesk.api.trading import Position


def _make_manifest() -> PluginManifest:
    return PluginManifest(
        name="sma_cross",
        kind="strategy",  # type: ignore[arg-type]
        version="0.1.0",
        api_version=1,
        entry="plugin:SMACrossStrategy",
        capabilities={"paper": True, "backtest": True, "live": False},
    )


def test_sma_cross_1d_product_cnc_and_exit_time_ignored() -> None:
    manifest = _make_manifest()
    # Configuration with exit_time="15:15" and stop_pct=0.05 on 1d timeframe
    strat = SMACrossStrategy(
        manifest,
        config={
            "timeframe": "1d",
            "fast": 2,
            "slow": 3,
            "qty": 10,
            "exit_time": "15:15",
            "stop_pct": 0.05,
        },
    )
    inst = InstrumentId("NSE:EQ:RELIANCE")

    ctx = MagicMock(spec=StrategyContext)
    ctx.instruments = {inst: MagicMock()}
    ctx.position = MagicMock(
        return_value=Position(
            instrument=inst, product=Product.CNC, qty=0, avg_price=Decimal("0.00")
        )
    )
    ctx.clock = MagicMock()
    # Clock set to 15:30 IST (which is >= exit_time 15:15)
    t_end = datetime(2026, 11, 10, 10, 0, tzinfo=UTC)  # 15:30 IST
    ctx.clock.now.return_value = t_end

    strat.on_start(ctx)

    # Bar 1: fast=10.0, slow=10.0 (initial)
    bar1 = Bar(
        instrument=inst,
        timeframe="1d",
        start_ts=datetime(2026, 11, 8, 3, 45, tzinfo=UTC),
        end_ts=datetime(2026, 11, 8, 10, 0, tzinfo=UTC),
        open=100.0,
        high=102.0,
        low=99.0,
        close=100.0,
        volume=100,
        complete=True,
    )
    ctx.indicator = MagicMock(return_value=10.0)
    intents1 = strat.on_bar(ctx, bar1)
    assert len(intents1) == 0  # Establishing initial values

    # Bar 2: Bullish cross! fast=12.0, slow=10.0
    bar2 = Bar(
        instrument=inst,
        timeframe="1d",
        start_ts=datetime(2026, 11, 9, 3, 45, tzinfo=UTC),
        end_ts=t_end,
        open=100.0,
        high=105.0,
        low=99.0,
        close=104.0,
        volume=100,
        complete=True,
    )

    def ind_fn(name: str, symbol: Any, tf: str, period: int) -> float:
        return 12.0 if period == 2 else 10.0

    ctx.indicator = ind_fn

    intents2 = strat.on_bar(ctx, bar2)
    # Entry MUST be emitted! (exit_time 15:15 ignored for 1d, Product is CNC)
    assert len(intents2) == 1
    intent = intents2[0]
    assert isinstance(intent, PlaceOrder)
    assert intent.spec.side == Side.BUY
    assert intent.spec.qty == 10
    assert intent.spec.product == Product.CNC
    assert intent.spec.type == OrderType.MARKET


def test_sma_cross_1d_timer_ignored() -> None:
    manifest = _make_manifest()
    strat = SMACrossStrategy(
        manifest,
        config={"timeframe": "1d", "fast": 2, "slow": 3, "qty": 10, "exit_time": "15:15"},
    )
    ctx = MagicMock(spec=StrategyContext)
    intents = strat.on_timer(ctx, "exit_time")
    # Must be empty on 1d (no intraday square-off)
    assert len(intents) == 0
