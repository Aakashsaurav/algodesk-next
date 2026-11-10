"""Property-based tests for OrderManager cancellation safety across all states."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from algodesk.api import (
    InstrumentId,
    Order,
    OrderSpec,
    OrderState,
    OrderType,
    Product,
    Side,
    Validity,
)
from algodesk.api.errors import SafetyViolation
from algodesk.oms.manager import OrderManager


def _make_dummy_order(
    client_ref: str,
    instrument: str,
    state: OrderState,
) -> Order:
    spec = OrderSpec(
        instrument=InstrumentId(instrument),
        side=Side.BUY,
        qty=1,
        type=OrderType.MARKET,
        price=Decimal("100.0"),
        product=Product.MIS,
        validity=Validity.DAY,
    )
    return Order(
        client_ref=client_ref,
        spec=spec,
        state=state,
        created_ts=datetime.now(UTC),
        filled_qty=0,
        avg_price=Decimal("0.0"),
        venue_ref=f"V-{client_ref}",
    )


def _make_oms() -> OrderManager:
    bus = MagicMock()
    venue = MagicMock()
    venue.cancel = MagicMock(return_value=None)
    journal = MagicMock()
    clock = MagicMock()
    clock.now = MagicMock(return_value=datetime.now(UTC))
    fee_engine = MagicMock()
    schedule = MagicMock()
    params = MagicMock()
    return OrderManager(
        bus=bus,
        venue=venue,
        journal=journal,
        clock=clock,
        fee_engine=fee_engine,
        schedule=schedule,
        params=params,
    )


@given(
    state=st.sampled_from(
        [
            OrderState.ACKNOWLEDGED,
            OrderState.PARTIALLY_FILLED,
        ]
    )
)
@settings(max_examples=25)
def test_property_oms_cancel_active_orders_transitions_to_cancel_requested(
    state: OrderState,
) -> None:
    """Active orders always transition to CANCEL_REQUESTED upon cancel request."""
    oms = _make_oms()

    order = _make_dummy_order("test-ref-1", "NSE:EQ:RELIANCE", state)
    oms._orders[order.client_ref] = order

    asyncio.run(oms.cancel("test-ref-1"))

    assert oms.orders["test-ref-1"].state == OrderState.CANCEL_REQUESTED
    oms.venue.cancel.assert_called_once_with("test-ref-1")


@given(
    state=st.sampled_from(
        [
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
        ]
    )
)
@settings(max_examples=15)
def test_property_oms_cancel_terminal_orders_safety_refusal(state: OrderState) -> None:
    """Terminal orders refuse cancellation or transition with SafetyViolation."""
    oms = _make_oms()

    order = _make_dummy_order("test-term-1", "NSE:EQ:TCS", state)
    oms._orders[order.client_ref] = order

    with pytest.raises(SafetyViolation) as exc_info:
        asyncio.run(oms.cancel("test-term-1"))

    assert "illegal order transition" in exc_info.value.what.lower()


def test_property_order_manager_cancel_unknown_ref_raises_safety_violation() -> None:
    """Cancelling a non-existent client_ref raises SafetyViolation."""
    oms = _make_oms()
    with pytest.raises(SafetyViolation) as exc_info:
        asyncio.run(oms.cancel("non_existent_ref"))
    assert "unknown order" in exc_info.value.what.lower()
