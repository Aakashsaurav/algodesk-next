"""Tests for engine event dataclasses."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from algodesk.api.market import Bar, InstrumentId
from algodesk.api.trading import (
    Fill,
    OrderEvent,
    OrderSpec,
    OrderState,
    OrderType,
    PlaceOrder,
    Portfolio,
    RiskDecision,
    Side,
)
from algodesk.engine.events import (
    BarClosed,
    FillEvent,
    IntentEvent,
    OrderEventMsg,
    PortfolioEvent,
    RiskEvent,
    Signal,
    SystemEvent,
    TimerEvent,
)


def test_bar_closed_event() -> None:
    now = datetime(2026, 10, 26, 9, 16, 0, tzinfo=UTC)
    bar = Bar(
        instrument=InstrumentId("NSE:EQ:INFY"),
        timeframe="1m",
        start_ts=datetime(2026, 10, 26, 9, 15, 0, tzinfo=UTC),
        end_ts=now,
        open=100.0,
        high=105.0,
        low=99.0,
        close=103.0,
        volume=500.0,
        complete=True,
    )
    event = BarClosed(bar=bar)
    assert event.instrument == bar.instrument
    assert event.ts == now
    assert str(event.bar.close) == "103.0"


def test_signal_event() -> None:
    now = datetime(2026, 10, 26, 9, 16, 0, tzinfo=UTC)
    inst = InstrumentId("NSE:EQ:INFY")
    sig = Signal(
        strategy_id="s1", instrument=inst, signal_type="BUY", ts=now, metrics={"fast": 10.0}
    )
    assert sig.strategy_id == "s1"
    assert sig.metrics["fast"] == 10.0

    with pytest.raises(ValueError, match="empty"):
        Signal(strategy_id="", instrument=inst, signal_type="BUY", ts=now)


def test_intent_and_risk_events() -> None:
    now = datetime(2026, 10, 26, 9, 16, 0, tzinfo=UTC)
    spec = OrderSpec(
        instrument=InstrumentId("NSE:EQ:INFY"),
        side=Side.BUY,
        qty=10,
        type=OrderType.MARKET,
    )
    intent = PlaceOrder(spec=spec)
    ie = IntentEvent(intent=intent, ts=now)
    assert ie.intent == intent

    dec = RiskDecision(allow=True, rule="max_pos", reason="ok")
    re = RiskEvent(decision=dec, intent=intent, ts=now)
    assert re.decision.allow is True


def test_order_and_fill_events() -> None:
    now = datetime(2026, 10, 26, 9, 16, 0, tzinfo=UTC)
    oe = OrderEvent(
        client_ref="run-1",
        from_state=OrderState.CREATED,
        to_state=OrderState.SUBMITTED,
        ts=now,
    )
    msg = OrderEventMsg(event=oe)
    assert msg.ts == now

    fill = Fill(
        fill_id="f1",
        client_ref="run-1",
        qty=10,
        price=Decimal("1500.00"),
        ts=now,
    )
    fe = FillEvent(fill=fill)
    assert fe.ts == now


def test_portfolio_timer_system_events() -> None:
    now = datetime(2026, 10, 26, 9, 16, 0, tzinfo=UTC)
    port = Portfolio(cash=Decimal("100000.00"), equity=Decimal("100000.00"))
    pe = PortfolioEvent(portfolio=port, ts=now)
    assert pe.portfolio.cash == Decimal("100000.00")

    te = TimerEvent(name="squareoff", ts=now)
    assert te.name == "squareoff"

    se = SystemEvent(kind="disk_warning", message="Low space", ts=now, details={"free_mb": 50})
    assert se.kind == "disk_warning"
    assert se.details["free_mb"] == 50
