"""Tests for algodesk.api.trading — order state transitions, charges, and trading entities."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from algodesk.api.errors import CalendarCoverageError, SafetyViolation
from algodesk.api.market import InstrumentId
from algodesk.api.trading import (
    ORDER_TRANSITIONS,
    CancelOrder,
    Charges,
    Fill,
    Funds,
    GrossFill,
    Order,
    OrderEvent,
    OrderSpec,
    OrderState,
    OrderStatus,
    OrderType,
    PlaceOrder,
    Portfolio,
    Position,
    Product,
    RiskDecision,
    Side,
    Validity,
    VenueAck,
    VenueEvent,
    VenueGrossFill,
    check_transition,
)

UTC = UTC


@pytest.mark.parametrize("s1,s2", list(itertools.product(OrderState, OrderState)))
def test_order_state_transition(s1: OrderState, s2: OrderState) -> None:
    """Verify every single cell of ORDER_TRANSITIONS individually."""
    allowed = ORDER_TRANSITIONS.get(s1, set())
    if s2 in allowed:
        assert check_transition(s1, s2) is True
    else:
        with pytest.raises(SafetyViolation) as exc_info:
            check_transition(s1, s2)
        assert f"Illegal order transition from {s1.value} to {s2.value}" in str(exc_info.value)


def test_order_spec_validation() -> None:
    spec = OrderSpec(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        side=Side.BUY,
        qty=10,
        type=OrderType.LIMIT,
        price=Decimal("2500.00"),
        product=Product.MIS,
        validity=Validity.DAY,
        tag="test_strat",
    )
    assert spec.qty == 10
    assert spec.price == Decimal("2500.00")

    # Quantity <= 0
    with pytest.raises(ValueError, match="Order quantity must be positive"):
        OrderSpec(
            instrument=InstrumentId("NSE:EQ:RELIANCE"),
            side=Side.BUY,
            qty=0,
            type=OrderType.MARKET,
        )

    # Limit order without price
    with pytest.raises(ValueError, match="Limit order requires positive price"):
        OrderSpec(
            instrument=InstrumentId("NSE:EQ:RELIANCE"),
            side=Side.BUY,
            qty=5,
            type=OrderType.LIMIT,
            price=None,
        )

    # Limit order with negative price
    with pytest.raises(ValueError, match="Limit order requires positive price"):
        OrderSpec(
            instrument=InstrumentId("NSE:EQ:RELIANCE"),
            side=Side.BUY,
            qty=5,
            type=OrderType.LIMIT,
            price=Decimal("-10.00"),
        )

    # Non-empty extensions in M1
    with pytest.raises(ValueError, match="extensions mapping must be empty in M1"):
        OrderSpec(
            instrument=InstrumentId("NSE:EQ:RELIANCE"),
            side=Side.BUY,
            qty=5,
            type=OrderType.MARKET,
            extensions={"iceberg": True},
        )


def test_intents_validation() -> None:
    spec = OrderSpec(
        instrument=InstrumentId("NSE:EQ:TCS"),
        side=Side.SELL,
        qty=20,
        type=OrderType.MARKET,
    )
    place = PlaceOrder(spec=spec)
    assert place.spec.instrument == "NSE:EQ:TCS"
    assert len(place.intent_id) > 0

    place_explicit = PlaceOrder(spec=spec, intent_id="custom-intent-1")
    assert place_explicit.intent_id == "custom-intent-1"

    cancel = CancelOrder(client_ref="run1-1")
    assert cancel.client_ref == "run1-1"
    assert len(cancel.intent_id) > 0

    cancel_explicit = CancelOrder(client_ref="run1-1", intent_id="custom-cancel-1")
    assert cancel_explicit.intent_id == "custom-cancel-1"

    with pytest.raises(ValueError, match="client_ref cannot be empty"):
        CancelOrder(client_ref="")


def test_charges_calculation_and_properties() -> None:
    charges = Charges(
        items={
            "brokerage": Decimal("20.00"),
            "stt": Decimal("12.50"),
            "exchange_txn": Decimal("1.75"),
            "sebi": Decimal("0.05"),
            "stamp": Decimal("1.50"),
            "dp": Decimal("13.50"),
            "gst": Decimal("3.92"),
        }
    )
    assert charges.total == Decimal("53.22")
    assert charges.brokerage == Decimal("20.00")
    assert charges.stt == Decimal("12.50")
    assert charges.exchange_txn == Decimal("1.75")
    assert charges.sebi == Decimal("0.05")
    assert charges.stamp == Decimal("1.50")
    assert charges.dp == Decimal("13.50")
    assert charges.gst == Decimal("3.92")

    # Empty charges
    empty = Charges()
    assert empty.total == Decimal("0.00")
    assert empty.brokerage == Decimal("0.00")


def test_order_entity_validation() -> None:
    now = datetime.now(UTC)
    spec = OrderSpec(
        instrument=InstrumentId("NSE:EQ:INFY"),
        side=Side.BUY,
        qty=50,
        type=OrderType.MARKET,
    )
    order = Order(
        client_ref="run-001",
        spec=spec,
        state=OrderState.CREATED,
        created_ts=now,
    )
    assert order.client_ref == "run-001"
    assert order.filled_qty == 0

    with pytest.raises(ValueError, match="client_ref cannot be empty"):
        Order(client_ref="", spec=spec, state=OrderState.CREATED, created_ts=now)

    with pytest.raises(ValueError, match="filled_qty cannot be negative"):
        Order(client_ref="r1", spec=spec, state=OrderState.CREATED, created_ts=now, filled_qty=-1)

    with pytest.raises(ValueError, match="filled_qty.*cannot exceed spec qty"):
        Order(client_ref="r1", spec=spec, state=OrderState.CREATED, created_ts=now, filled_qty=51)

    with pytest.raises(ValueError, match="avg_price cannot be negative"):
        Order(
            client_ref="r1",
            spec=spec,
            state=OrderState.CREATED,
            created_ts=now,
            avg_price=Decimal("-1.0"),
        )


def test_order_event_validation() -> None:
    now = datetime.now(UTC)
    evt = OrderEvent(
        client_ref="run-001",
        from_state=OrderState.CREATED,
        to_state=OrderState.SUBMITTED,
        ts=now,
        reason="Order sent to venue",
    )
    assert evt.from_state == OrderState.CREATED

    with pytest.raises(ValueError, match="client_ref cannot be empty"):
        OrderEvent(
            client_ref="",
            from_state=OrderState.CREATED,
            to_state=OrderState.SUBMITTED,
            ts=now,
        )


def test_fill_validation() -> None:
    now = datetime.now(UTC)
    fill = Fill(
        fill_id="f-101",
        client_ref="run-001",
        qty=25,
        price=Decimal("1500.50"),
        ts=now,
    )
    assert fill.qty == 25
    assert fill.price == Decimal("1500.50")

    with pytest.raises(ValueError, match="fill_id cannot be empty"):
        Fill(fill_id="", client_ref="r1", qty=1, price=Decimal("100"), ts=now)

    with pytest.raises(ValueError, match="Fill qty must be positive"):
        Fill(fill_id="f1", client_ref="r1", qty=0, price=Decimal("100"), ts=now)

    with pytest.raises(ValueError, match="Fill price must be positive"):
        Fill(fill_id="f1", client_ref="r1", qty=10, price=Decimal("-10"), ts=now)


def test_position_and_portfolio() -> None:
    pos = Position(
        instrument=InstrumentId("NSE:EQ:TCS"),
        product=Product.CNC,
        qty=100,
        avg_price=Decimal("3500.00"),
        realized=Decimal("250.00"),
        unrealized=Decimal("500.00"),
    )
    assert pos.qty == 100

    with pytest.raises(ValueError, match="avg_price cannot be negative"):
        Position(
            instrument=InstrumentId("NSE:EQ:TCS"),
            product=Product.CNC,
            qty=10,
            avg_price=Decimal("-10.00"),
        )

    portfolio = Portfolio(
        cash=Decimal("100000.00"),
        equity=Decimal("100750.00"),
        positions={"NSE:EQ:TCS": pos},
        margin_used=Decimal("0.00"),
    )
    assert portfolio.cash == Decimal("100000.00")

    with pytest.raises(ValueError, match="margin_used cannot be negative"):
        Portfolio(
            cash=Decimal("1000.0"),
            equity=Decimal("1000.0"),
            margin_used=Decimal("-50.0"),
        )


def test_risk_decision() -> None:
    d_allow = RiskDecision(allow=True, rule="max_order_size", reason="within limit")
    assert d_allow.allow is True

    d_deny = RiskDecision(
        allow=False,
        rule="daily_loss_limit",
        reason="Daily loss exceeded",
        values={"loss": Decimal("5000"), "limit": Decimal("4000")},
    )
    assert d_deny.allow is False
    assert "loss" in d_deny.values


def test_venue_ack_and_event() -> None:
    now = datetime.now(UTC)
    ack = VenueAck(client_ref="run-1", venue_ref="v-99", ts=now, accepted=True)
    assert ack.accepted is True

    with pytest.raises(ValueError, match="client_ref cannot be empty"):
        VenueAck(client_ref="", venue_ref="v", ts=now)

    evt = VenueEvent(kind="ack", client_ref="run-1", ts=now, venue_ref="v-99")
    assert evt.kind == "ack"

    # GrossFill validation
    gf = GrossFill(
        client_ref="run-1",
        qty=10,
        price=Decimal("150.25"),
        ts=now,
        venue_fill_id="vf-1",
    )
    assert gf.client_ref == "run-1"
    assert gf.qty == 10
    assert gf.price == Decimal("150.25")
    assert gf.venue_fill_id == "vf-1"
    assert VenueGrossFill is GrossFill

    with pytest.raises(ValueError, match="qty must be positive"):
        GrossFill(client_ref="run-1", qty=0, price=Decimal("100"), ts=now, venue_fill_id="vf-2")

    # VenueEvent with gross fill
    fill_evt = VenueEvent(
        kind="fill",
        client_ref="run-1",
        ts=now,
        venue_ref="v-99",
        gross_fill=gf,
    )
    assert fill_evt.gross_fill == gf
    assert fill_evt.venue_fill_id == "vf-1"
    assert fill_evt.qty == 10
    assert fill_evt.price == Decimal("150.25")

    # OrderStatus
    status = OrderStatus(
        client_ref="run-1",
        state=OrderState.ACKNOWLEDGED,
        venue_ref="v-99",
    )
    assert status.client_ref == "run-1"
    assert status.state == OrderState.ACKNOWLEDGED


def test_funds() -> None:
    f1 = Funds(available=Decimal("50000.00"), used=Decimal("10000.00"))
    assert f1.total == Decimal("60000.00")

    f2 = Funds(available=Decimal("1000.00"), used=Decimal("500.00"), total=Decimal("1500.00"))
    assert f2.total == Decimal("1500.00")


def test_calendar_coverage_error() -> None:
    err = CalendarCoverageError(
        what="Date 2026-10-23 is outside calendar coverage [2026-01-01, 2026-06-30]",
        where="CalendarPlugin:is_trading_day",
        fix="Extend calendar data coverage or verify date input",
    )
    assert "outside calendar coverage" in str(err)
    assert err.what.startswith("Date 2026-10-23")
    assert err.where == "CalendarPlugin:is_trading_day"
    assert err.fix.startswith("Extend calendar")
