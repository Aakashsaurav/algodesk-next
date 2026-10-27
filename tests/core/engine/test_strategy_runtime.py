"""Unit tests for StrategyRuntime covering dispatch, risk routing, and exception isolation."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from plugins.strategies.sma_cross.config_schema import SMACrossConfig

from algodesk.api import (
    ConfigError,
    InstrumentId,
    OrderSpec,
    OrderType,
    PlaceOrder,
    Product,
    RiskDecision,
    Side,
    Validity,
)
from algodesk.api.market import Bar, Tick
from algodesk.api.protocols import StrategyContext
from algodesk.api.trading import Fill
from algodesk.engine.strategy_runtime import StrategyRuntime


class FlakyStrategy:
    """Strategy that fails deliberately in various hooks to test isolation."""

    params_schema = SMACrossConfig

    def __init__(self, should_fail_in: str | None = None) -> None:
        self.should_fail_in = should_fail_in
        self.started = False
        self.bars_seen = 0

    def on_start(self, ctx: StrategyContext) -> None:
        if self.should_fail_in == "on_start":
            raise RuntimeError("Explosion in on_start")
        self.started = True

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> list[PlaceOrder]:
        if self.should_fail_in == "on_bar":
            raise ValueError("Corrupt bar calculation")
        self.bars_seen += 1
        spec = OrderSpec(
            instrument=bar.instrument,
            side=Side.BUY,
            qty=10,
            type=OrderType.MARKET,
            price=None,
            product=Product.CNC,
            validity=Validity.DAY,
            tag="test_intent",
        )
        return [PlaceOrder(spec=spec, intent_id=f"intent-{self.bars_seen}")]

    def on_tick(self, ctx: StrategyContext, tick: Tick) -> list[PlaceOrder]:
        if self.should_fail_in == "on_tick":
            raise ArithmeticError("Tick overflow")
        return []

    def on_fill(self, ctx: StrategyContext, fill: Fill) -> list[PlaceOrder]:
        if self.should_fail_in == "on_fill":
            raise KeyError("Fill key missing")
        return []

    def on_timer(self, ctx: StrategyContext, name: str) -> list[PlaceOrder]:
        if self.should_fail_in == "on_timer":
            raise TimeoutError("Timer boom")
        return []

    def on_stop(self, ctx: StrategyContext) -> None:
        pass

    def state(self) -> dict[str, int]:
        return {"bars_seen": self.bars_seen}

    def restore(self, st: dict[str, int]) -> None:
        self.bars_seen = st.get("bars_seen", 0)


@pytest.mark.anyio
async def test_strategy_runtime_schema_validation() -> None:
    runtime = StrategyRuntime()
    strat = FlakyStrategy()
    ctx = MagicMock(spec=StrategyContext)

    # Valid config
    runtime.load_strategy("s1", {"fast": 10, "slow": 30, "qty": 5}, ctx, strategy_instance=strat)
    assert not runtime.is_halted("s1")

    # Invalid config (fast must be >= 1)
    with pytest.raises(ConfigError):
        runtime.load_strategy("s2", {"fast": -5}, ctx, strategy_instance=strat)


@pytest.mark.anyio
async def test_strategy_runtime_exception_isolation() -> None:
    journal = MagicMock()
    bus = MagicMock()
    notifier = MagicMock()
    clock = MagicMock()
    clock.now.return_value = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)

    runtime = StrategyRuntime(
        journal=journal,
        bus=bus,
        notifier=notifier,
        clock=clock,
    )
    strat = FlakyStrategy(should_fail_in="on_bar")
    ctx = MagicMock(spec=StrategyContext)
    runtime.load_strategy("flaky", {"fast": 10, "slow": 20}, ctx, strategy_instance=strat)

    inst = InstrumentId("NSE:EQ:RELIANCE")
    bar = Bar(
        instrument=inst,
        timeframe="5m",
        start_ts=clock.now(),
        end_ts=clock.now(),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
        volume=10,
        complete=True,
        synthetic=False,
    )

    # Dispatch bar: should NOT raise, but should halt strategy and send alerts
    await runtime.dispatch_bar(bar)
    assert runtime.is_halted("flaky")

    # Journal recorded error
    recorded_errors = [c[0][1] for c in journal.append.call_args_list if c[0][0] == "error"]
    assert any(err.get("strategy") == "flaky" for err in recorded_errors)

    # Event bus published strategy.error
    assert bus.publish.called

    # Notifier sent alert
    assert notifier.send.called


@pytest.mark.anyio
async def test_strategy_runtime_intent_routing_and_signals() -> None:
    journal = MagicMock()
    clock = MagicMock()
    clock.now.return_value = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)

    order_mgr = MagicMock()
    order_mgr.submit = AsyncMock()

    risk_engine = MagicMock()
    risk_engine.evaluate.return_value = RiskDecision(
        allow=True, rule="all_pass", reason="Approved", values={}
    )

    risk_view_builder = MagicMock()

    runtime = StrategyRuntime(
        order_manager=order_mgr,
        risk_engine=risk_engine,
        risk_view_builder=risk_view_builder,
        journal=journal,
        clock=clock,
    )
    strat = FlakyStrategy()
    ctx = MagicMock(spec=StrategyContext)
    runtime.load_strategy("s1", {"fast": 10, "slow": 20}, ctx, strategy_instance=strat)

    inst = InstrumentId("NSE:EQ:TCS")
    bar = Bar(
        instrument=inst,
        timeframe="5m",
        start_ts=clock.now(),
        end_ts=clock.now(),
        open=200.0,
        high=205.0,
        low=195.0,
        close=202.0,
        volume=100,
        complete=True,
        synthetic=False,
    )

    await runtime.dispatch_bar(bar)

    # 1. Signal journaled
    signals = [c[0][1] for c in journal.append.call_args_list if c[0][0] == "signal"]
    assert any(sig.get("strategy") == "s1" for sig in signals)

    # 2. Risk engine evaluated intent
    assert risk_engine.evaluate.called

    # 3. OrderManager submitted
    assert order_mgr.submit.called


def test_strategy_runtime_state_and_restore() -> None:
    runtime = StrategyRuntime()
    strat = FlakyStrategy()
    ctx = MagicMock(spec=StrategyContext)
    runtime.load_strategy("s1", {"fast": 10, "slow": 20}, ctx, strategy_instance=strat)
    strat.bars_seen = 42

    st = runtime.state()
    assert st == {"s1": {"bars_seen": 42}}

    strat2 = FlakyStrategy()
    runtime2 = StrategyRuntime()
    runtime2.load_strategy("s1", {"fast": 10, "slow": 20}, ctx, strategy_instance=strat2)
    runtime2.restore(st)
    assert strat2.bars_seen == 42
