"""Extensive drills and unit tests for the SMA Cross Strategy plugin."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pydantic
import pytest
from plugins.strategies.sma_cross.config_schema import SMACrossConfig
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


def _make_bar(
    seq: int,
    symbol: str = "NSE:EQ:RELIANCE",
    timeframe: str = "5m",
    close: float = 100.0,
    complete: bool = True,
) -> Bar:
    base = datetime(2026, 10, 26, 9, 15, tzinfo=UTC)
    start_ts = base + timedelta(minutes=seq * 5)
    end_ts = start_ts + timedelta(minutes=5)
    return Bar(
        instrument=InstrumentId(symbol),
        timeframe=timeframe,
        start_ts=start_ts,
        end_ts=end_ts,
        open=close - 0.5,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=1000,
        complete=complete,
    )


def _make_strategy(
    fast: int = 5,
    slow: int = 20,
    timeframe: str = "5m",
    qty: int = 10,
    product: str = "MIS",
    stop_pct: float | None = None,
    instruments: list[str] | None = None,
) -> SMACrossStrategy:
    manifest = PluginManifest(
        name="sma_cross",
        kind="strategy",  # type: ignore[arg-type]
        version="0.1.0",
        api_version=1,
        entry="plugin:SMACrossStrategy",
        capabilities={"paper": True, "backtest": True},
    )
    config: dict[str, Any] = {
        "fast": fast,
        "slow": slow,
        "timeframe": timeframe,
        "qty": qty,
        "product": product,
    }
    if stop_pct is not None:
        config["stop_pct"] = stop_pct
    if instruments is not None:
        config["instruments"] = instruments

    return SMACrossStrategy(manifest=manifest, config=config)


# ---------------------------------------------------------------------------
# Configuration and Validation Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fast", "slow", "expect_ok"),
    [
        (5, 20, True),
        (3, 8, True),
        (10, 50, True),
        (20, 20, False),
        (25, 20, False),
        (50, 10, False),
    ],
)
def test_sma_cross_doctor_check_fast_slow_comparison(fast: int, slow: int, expect_ok: bool) -> None:
    strat = _make_strategy(fast=fast, slow=slow)
    res = strat.doctor_checks()[0].run()
    assert res.ok is expect_ok


@pytest.mark.parametrize("invalid_period", [0, -1, -5, -20])
def test_sma_cross_config_period_bounds(invalid_period: int) -> None:
    with pytest.raises(pydantic.ValidationError):
        SMACrossConfig(fast=invalid_period, slow=20)
    with pytest.raises(pydantic.ValidationError):
        SMACrossConfig(fast=5, slow=invalid_period)


@pytest.mark.parametrize("invalid_qty", [0, -1, -10])
def test_sma_cross_config_invalid_qty(invalid_qty: int) -> None:
    with pytest.raises(pydantic.ValidationError):
        SMACrossConfig(fast=5, slow=20, qty=invalid_qty)


@pytest.mark.parametrize("invalid_stop_pct", [-0.05, -1.0, 1.05, 2.0])
def test_sma_cross_config_invalid_stop_pct(invalid_stop_pct: float) -> None:
    with pytest.raises(pydantic.ValidationError):
        SMACrossConfig(fast=5, slow=20, stop_pct=invalid_stop_pct)


def test_sma_cross_doctor_check_clean() -> None:
    strat = _make_strategy(fast=5, slow=20)
    checks = strat.doctor_checks()
    assert len(checks) >= 1
    res = checks[0].run()
    assert res.ok is True


# ---------------------------------------------------------------------------
# Filter and Incomplete Bar Tests
# ---------------------------------------------------------------------------


def test_sma_cross_ignores_incomplete_bar() -> None:
    strat = _make_strategy()
    ctx = MagicMock(spec=StrategyContext)
    bar = _make_bar(0, complete=False)

    intents = strat.on_bar(ctx, bar)
    assert intents == ()
    ctx.indicator.assert_not_called()


@pytest.mark.parametrize("bar_tf", ["1m", "15m", "1h", "1d"])
def test_sma_cross_ignores_mismatched_timeframe(bar_tf: str) -> None:
    strat = _make_strategy(timeframe="5m")
    ctx = MagicMock(spec=StrategyContext)
    bar = _make_bar(0, timeframe=bar_tf)

    intents = strat.on_bar(ctx, bar)
    assert intents == ()
    ctx.indicator.assert_not_called()


def test_sma_cross_ignores_unmonitored_symbol() -> None:
    strat = _make_strategy(instruments=["NSE:EQ:RELIANCE"])
    ctx = MagicMock(spec=StrategyContext)
    bar = _make_bar(0, symbol="NSE:EQ:TCS")

    intents = strat.on_bar(ctx, bar)
    assert intents == ()
    ctx.indicator.assert_not_called()


# ---------------------------------------------------------------------------
# Crossover Signals Tests (Golden Cross & Death Cross)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fast", "slow", "product_str", "expected_product", "qty"),
    [
        (3, 8, "MIS", Product.MIS, 10),
        (5, 15, "CNC", Product.CNC, 25),
        (10, 20, "MIS", Product.MIS, 50),
        (12, 26, "CNC", Product.CNC, 100),
        (20, 50, "MIS", Product.MIS, 5),
    ],
)
def test_sma_cross_golden_cross_entry(
    fast: int,
    slow: int,
    product_str: str,
    expected_product: Product,
    qty: int,
) -> None:
    strat = _make_strategy(
        fast=fast,
        slow=slow,
        product=product_str,
        qty=qty,
        instruments=["NSE:EQ:RELIANCE"],
    )

    ctx = MagicMock(spec=StrategyContext)
    ctx.position.return_value = None  # Flat position

    inst = InstrumentId("NSE:EQ:RELIANCE")

    # Bar 0: Fast below slow (e.g. fast=98, slow=100)
    ctx.indicator.side_effect = lambda name, i, tf, period: 98.0 if period == fast else 100.0
    bar0 = _make_bar(0, symbol=str(inst), close=100.0)
    intents0 = strat.on_bar(ctx, bar0)
    assert intents0 == ()

    # Bar 1: Fast crosses ABOVE slow (e.g. fast=102, slow=101) -> Golden Cross!
    ctx.indicator.side_effect = lambda name, i, tf, period: 102.0 if period == fast else 101.0
    bar1 = _make_bar(1, symbol=str(inst), close=103.0)
    intents1 = strat.on_bar(ctx, bar1)

    assert len(intents1) == 1
    intent = intents1[0]
    assert isinstance(intent, PlaceOrder)
    assert intent.spec.instrument == inst
    assert intent.spec.side == Side.BUY
    assert intent.spec.qty == qty
    assert intent.spec.product == expected_product
    assert intent.spec.type == OrderType.MARKET


def test_sma_cross_death_cross_exit() -> None:
    strat = _make_strategy(fast=5, slow=20, qty=10, instruments=["NSE:EQ:RELIANCE"])
    inst = InstrumentId("NSE:EQ:RELIANCE")
    ctx = MagicMock(spec=StrategyContext)

    # Position is long 10 shares
    pos = Position(
        instrument=inst,
        product=Product.MIS,
        qty=10,
        avg_price=Decimal("100.0"),
    )
    ctx.position.return_value = pos

    # Bar 0: Fast above slow (e.g. fast=105, slow=100)
    ctx.indicator.side_effect = lambda name, i, tf, period: 105.0 if period == 5 else 100.0
    bar0 = _make_bar(0, symbol=str(inst))
    assert strat.on_bar(ctx, bar0) == ()

    # Bar 1: Fast crosses BELOW slow (e.g. fast=99, slow=101) -> Death Cross!
    ctx.indicator.side_effect = lambda name, i, tf, period: 99.0 if period == 5 else 101.0
    bar1 = _make_bar(1, symbol=str(inst))
    intents1 = strat.on_bar(ctx, bar1)

    assert len(intents1) == 1
    intent = intents1[0]
    assert isinstance(intent, PlaceOrder)
    assert intent.spec.instrument == inst
    assert intent.spec.side == Side.SELL
    assert intent.spec.qty == 10
    assert intent.spec.type == OrderType.MARKET


def test_sma_cross_death_cross_ignored_when_flat() -> None:
    strat = _make_strategy(fast=5, slow=20, instruments=["NSE:EQ:RELIANCE"])
    inst = InstrumentId("NSE:EQ:RELIANCE")
    ctx = MagicMock(spec=StrategyContext)
    ctx.position.return_value = None  # Flat position

    # Bar 0: Fast above slow
    ctx.indicator.side_effect = lambda name, i, tf, period: 105.0 if period == 5 else 100.0
    strat.on_bar(ctx, _make_bar(0, symbol=str(inst)))

    # Bar 1: Fast crosses below slow
    ctx.indicator.side_effect = lambda name, i, tf, period: 99.0 if period == 5 else 101.0
    intents = strat.on_bar(ctx, _make_bar(1, symbol=str(inst)))

    # Long-only strategy does not enter short positions
    assert intents == ()


def test_sma_cross_golden_cross_ignored_when_already_long() -> None:
    strat = _make_strategy(fast=5, slow=20, instruments=["NSE:EQ:RELIANCE"])
    inst = InstrumentId("NSE:EQ:RELIANCE")
    ctx = MagicMock(spec=StrategyContext)

    # Already long
    ctx.position.return_value = Position(
        instrument=inst,
        product=Product.MIS,
        qty=10,
        avg_price=Decimal("100.0"),
    )

    # Fast crosses above slow
    ctx.indicator.side_effect = lambda name, i, tf, period: 98.0 if period == 5 else 100.0
    strat.on_bar(ctx, _make_bar(0, symbol=str(inst)))

    ctx.indicator.side_effect = lambda name, i, tf, period: 105.0 if period == 5 else 100.0
    intents = strat.on_bar(ctx, _make_bar(1, symbol=str(inst)))

    # No duplicate entry
    assert intents == ()


# ---------------------------------------------------------------------------
# Stop Loss Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stop_pct", "entry_price", "exit_bar_close", "expect_stop_loss"),
    [
        (0.02, 100.0, 97.9, True),  # 100 * 0.98 = 98.0; 97.9 <= 98.0 -> triggers
        (0.02, 100.0, 98.5, False),  # 98.5 > 98.0 -> does not trigger
        (0.05, 200.0, 189.0, True),  # 200 * 0.95 = 190.0; 189 <= 190 -> triggers
        (0.05, 200.0, 195.0, False),  # 195 > 190 -> does not trigger
        (0.01, 50.0, 49.0, True),  # 50 * 0.99 = 49.5; 49.0 <= 49.5 -> triggers
        (0.01, 50.0, 49.8, False),  # 49.8 > 49.5 -> does not trigger
    ],
)
def test_sma_cross_stop_loss_trigger(
    stop_pct: float,
    entry_price: float,
    exit_bar_close: float,
    expect_stop_loss: bool,
) -> None:
    strat = _make_strategy(
        fast=5,
        slow=20,
        stop_pct=stop_pct,
        instruments=["NSE:EQ:RELIANCE"],
    )
    inst = InstrumentId("NSE:EQ:RELIANCE")
    ctx = MagicMock(spec=StrategyContext)

    # 1. Trigger Golden Cross to establish entry price
    ctx.position.return_value = None
    ctx.indicator.side_effect = lambda name, i, tf, period: 95.0 if period == 5 else 100.0
    strat.on_bar(ctx, _make_bar(0, symbol=str(inst), close=entry_price - 1.0))

    ctx.indicator.side_effect = lambda name, i, tf, period: 105.0 if period == 5 else 100.0
    intents_entry = strat.on_bar(ctx, _make_bar(1, symbol=str(inst), close=entry_price))
    assert len(intents_entry) == 1
    assert intents_entry[0].spec.side == Side.BUY

    # 2. Simulate position opened
    ctx.position.return_value = Position(
        instrument=inst,
        product=Product.MIS,
        qty=10,
        avg_price=Decimal(str(entry_price)),
    )

    # 3. Bar price drops to exit_bar_close (SMA still bullish so exit is purely stop-loss)
    ctx.indicator.side_effect = lambda name, i, tf, period: 105.0 if period == 5 else 100.0
    intents_check = strat.on_bar(ctx, _make_bar(2, symbol=str(inst), close=exit_bar_close))

    if expect_stop_loss:
        assert len(intents_check) == 1
        assert intents_check[0].spec.side == Side.SELL
        assert intents_check[0].spec.qty == 10
        assert "stop" in intents_check[0].spec.tag
    else:
        assert intents_check == ()


# ---------------------------------------------------------------------------
# State Serialization & Restoration Tests
# ---------------------------------------------------------------------------


def test_sma_cross_state_persistence() -> None:
    strat1 = _make_strategy(fast=5, slow=20, instruments=["NSE:EQ:RELIANCE"])
    inst = InstrumentId("NSE:EQ:RELIANCE")
    ctx = MagicMock(spec=StrategyContext)

    # Feed bar to populate indicators & entry price
    ctx.position.return_value = None
    ctx.indicator.side_effect = lambda name, i, tf, period: 95.0 if period == 5 else 100.0
    strat1.on_bar(ctx, _make_bar(0, symbol=str(inst), close=99.0))
    ctx.indicator.side_effect = lambda name, i, tf, period: 105.0 if period == 5 else 100.0
    strat1.on_bar(ctx, _make_bar(1, symbol=str(inst), close=100.0))

    # Serialize
    saved_state = strat1.state()
    assert isinstance(saved_state, dict)
    assert "prev_fast" in saved_state
    assert "prev_slow" in saved_state
    assert "entry_prices" in saved_state

    # Restore in new strategy instance
    strat2 = _make_strategy(fast=5, slow=20, instruments=["NSE:EQ:RELIANCE"])
    strat2.restore(saved_state)

    # Verify state was restored
    assert strat2._prev_fast[inst] == 105.0
    assert strat2._prev_slow[inst] == 100.0
    assert strat2._entry_prices[inst] == 100.0


# ---------------------------------------------------------------------------
# Timer and Square-Off Exit Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("timer_name", ["exit", "exit_time", "square_off", "15:15"])
def test_sma_cross_timer_square_off(timer_name: str) -> None:
    strat = _make_strategy(fast=5, slow=20, instruments=["NSE:EQ:RELIANCE"])
    inst = InstrumentId("NSE:EQ:RELIANCE")
    ctx = MagicMock(spec=StrategyContext)
    ctx.clock.now.return_value = datetime(2026, 10, 26, 15, 15, tzinfo=UTC)

    # Open position
    ctx.position.return_value = Position(
        instrument=inst,
        product=Product.MIS,
        qty=15,
        avg_price=Decimal("100.0"),
    )

    intents = strat.on_timer(ctx, timer_name)
    assert len(intents) == 1
    intent = intents[0]
    assert isinstance(intent, PlaceOrder)
    assert intent.spec.instrument == inst
    assert intent.spec.side == Side.SELL
    assert intent.spec.qty == 15
    assert intent.spec.tag == "sma_cross_timer_exit"


def test_sma_cross_unrecognized_timer_ignored() -> None:
    strat = _make_strategy(fast=5, slow=20, instruments=["NSE:EQ:RELIANCE"])
    ctx = MagicMock(spec=StrategyContext)
    intents = strat.on_timer(ctx, "unrecognized_timer_event")
    assert intents == ()


# ---------------------------------------------------------------------------
# Lifecycle and Multi-Instrument Isolation Tests
# ---------------------------------------------------------------------------


def test_sma_cross_on_start_populates_from_context() -> None:
    strat = _make_strategy(fast=5, slow=20, instruments=[])
    ctx = MagicMock(spec=StrategyContext)
    inst1 = InstrumentId("NSE:EQ:RELIANCE")
    inst2 = InstrumentId("NSE:EQ:TCS")
    ctx.instruments = {inst1: MagicMock(), inst2: MagicMock()}

    assert len(strat._instruments) == 0
    strat.on_start(ctx)
    assert strat._instruments == {inst1, inst2}


def test_sma_cross_on_stop_clears_state() -> None:
    strat = _make_strategy(fast=5, slow=20, instruments=["NSE:EQ:RELIANCE"])
    inst = InstrumentId("NSE:EQ:RELIANCE")
    ctx = MagicMock(spec=StrategyContext)

    strat._prev_fast[inst] = 100.0
    strat._prev_slow[inst] = 95.0
    strat._entry_prices[inst] = 98.0

    strat.on_stop(ctx)
    assert len(strat._instruments) == 0
    assert len(strat._prev_fast) == 0
    assert len(strat._prev_slow) == 0
    assert len(strat._entry_prices) == 0


@pytest.mark.parametrize(
    "symbol",
    [
        "NSE:EQ:RELIANCE",
        "NSE:EQ:TCS",
        "NSE:EQ:INFY",
        "NSE:EQ:HDFCBANK",
        "NSE:EQ:ICICIBANK",
        "NSE:EQ:SBIN",
    ],
)
def test_sma_cross_multi_instrument_isolation(symbol: str) -> None:
    strat = _make_strategy(fast=3, slow=8, instruments=[symbol])
    inst = InstrumentId(symbol)
    ctx = MagicMock(spec=StrategyContext)
    ctx.position.return_value = None

    # First bar below slow
    ctx.indicator.side_effect = lambda name, i, tf, period: 98.0 if period == 3 else 100.0
    strat.on_bar(ctx, _make_bar(0, symbol=symbol, close=99.0))

    # Second bar crosses above slow -> golden cross
    ctx.indicator.side_effect = lambda name, i, tf, period: 102.0 if period == 3 else 100.0
    intents = strat.on_bar(ctx, _make_bar(1, symbol=symbol, close=101.0))

    assert len(intents) == 1
    assert intents[0].spec.instrument == inst
    assert intents[0].spec.side == Side.BUY


@pytest.mark.parametrize("cycle", [1, 2, 3, 4, 5])
def test_sma_cross_whipsaw_fluctuations(cycle: int) -> None:
    strat = _make_strategy(fast=3, slow=5, instruments=["NSE:EQ:INFY"])
    inst = InstrumentId("NSE:EQ:INFY")
    ctx = MagicMock(spec=StrategyContext)

    # Alternate between BUY and SELL across cycles
    for c in range(cycle):
        # Bullish cross -> BUY (if flat)
        ctx.position.return_value = None
        ctx.indicator.side_effect = lambda name, i, tf, period: 95.0 if period == 3 else 100.0
        strat.on_bar(ctx, _make_bar(c * 4, symbol=str(inst), close=98.0))

        ctx.indicator.side_effect = lambda name, i, tf, period: 105.0 if period == 3 else 100.0
        buy_intents = strat.on_bar(ctx, _make_bar(c * 4 + 1, symbol=str(inst), close=102.0))
        assert len(buy_intents) == 1
        assert buy_intents[0].spec.side == Side.BUY

        # Bearish cross -> SELL (if long)
        ctx.position.return_value = Position(
            instrument=inst, product=Product.MIS, qty=10, avg_price=Decimal("102.0")
        )
        ctx.indicator.side_effect = lambda name, i, tf, period: 105.0 if period == 3 else 100.0
        strat.on_bar(ctx, _make_bar(c * 4 + 2, symbol=str(inst), close=102.0))

        ctx.indicator.side_effect = lambda name, i, tf, period: 95.0 if period == 3 else 100.0
        sell_intents = strat.on_bar(ctx, _make_bar(c * 4 + 3, symbol=str(inst), close=96.0))
        assert len(sell_intents) == 1
        assert sell_intents[0].spec.side == Side.SELL


@pytest.mark.parametrize("price_level", [10.0, 50.0, 100.0, 500.0, 1000.0, 2500.0])
def test_sma_cross_constant_prices_no_cross(price_level: float) -> None:
    strat = _make_strategy(fast=5, slow=20, instruments=["NSE:EQ:RELIANCE"])
    ctx = MagicMock(spec=StrategyContext)
    ctx.position.return_value = None

    # Both fast and slow remain exactly equal to price_level
    ctx.indicator.side_effect = lambda name, i, tf, period: price_level

    for i in range(5):
        intents = strat.on_bar(ctx, _make_bar(i, symbol="NSE:EQ:RELIANCE", close=price_level))
        assert intents == ()


@pytest.mark.parametrize("gap_percent", [0.03, 0.05, 0.10, 0.15, 0.20, 0.30])
def test_sma_cross_gap_down_stop_loss(gap_percent: float) -> None:
    strat = _make_strategy(fast=5, slow=20, stop_pct=0.02, instruments=["NSE:EQ:RELIANCE"])
    inst = InstrumentId("NSE:EQ:RELIANCE")
    ctx = MagicMock(spec=StrategyContext)

    # Establish entry at 100.0
    ctx.position.return_value = None
    ctx.indicator.side_effect = lambda name, i, tf, period: 95.0 if period == 5 else 100.0
    strat.on_bar(ctx, _make_bar(0, symbol=str(inst), close=98.0))

    ctx.indicator.side_effect = lambda name, i, tf, period: 105.0 if period == 5 else 100.0
    strat.on_bar(ctx, _make_bar(1, symbol=str(inst), close=100.0))

    # Gap down well below stop loss (stop is 98.0)
    gap_close = 100.0 * (1.0 - gap_percent)
    ctx.position.return_value = Position(
        instrument=inst, product=Product.MIS, qty=10, avg_price=Decimal("100.0")
    )
    ctx.indicator.side_effect = lambda name, i, tf, period: 105.0 if period == 5 else 100.0
    intents = strat.on_bar(ctx, _make_bar(2, symbol=str(inst), close=gap_close))

    assert len(intents) == 1
    assert intents[0].spec.side == Side.SELL
    assert intents[0].spec.tag == "sma_cross_stop_loss"
