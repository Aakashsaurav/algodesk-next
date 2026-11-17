"""Tests for Step 6: OrderType STOP, STOP_MARKET, OrderSpec trigger price, and venue rejection."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from plugins.venues.sim.venue import SimVenue

from algodesk.api.market import InstrumentId
from algodesk.api.trading import OrderSpec, OrderType, Side


def test_order_type_stop_and_stop_market_exist() -> None:
    assert OrderType.STOP == "STOP"
    assert OrderType.STOP_MARKET == "STOP_MARKET"
    assert OrderType.MARKET == "MARKET"
    assert OrderType.LIMIT == "LIMIT"


def test_orderspec_stop_order_validation() -> None:
    inst = InstrumentId("NSE:EQ:RELIANCE")

    # Valid STOP order
    spec = OrderSpec(
        instrument=inst,
        side=Side.BUY,
        qty=10,
        type=OrderType.STOP,
        price=Decimal("105.00"),
        trigger_price=Decimal("100.00"),
    )
    assert spec.price == Decimal("105.00")
    assert spec.trigger_price == Decimal("100.00")

    # STOP order missing price
    with pytest.raises(ValueError, match="Stop order requires positive limit price"):
        OrderSpec(
            instrument=inst,
            side=Side.BUY,
            qty=10,
            type=OrderType.STOP,
            price=None,
            trigger_price=Decimal("100.00"),
        )

    # STOP order missing trigger price
    with pytest.raises(ValueError, match="Stop order requires positive trigger price"):
        OrderSpec(
            instrument=inst,
            side=Side.BUY,
            qty=10,
            type=OrderType.STOP,
            price=Decimal("105.00"),
            trigger_price=None,
        )


def test_orderspec_stop_market_order_validation() -> None:
    inst = InstrumentId("NSE:EQ:RELIANCE")

    # Valid STOP_MARKET order
    spec = OrderSpec(
        instrument=inst,
        side=Side.SELL,
        qty=5,
        type=OrderType.STOP_MARKET,
        trigger_price=Decimal("95.00"),
    )
    assert spec.price is None
    assert spec.trigger_price == Decimal("95.00")

    # STOP_MARKET missing trigger price
    with pytest.raises(ValueError, match="Stop-market order requires positive trigger price"):
        OrderSpec(
            instrument=inst,
            side=Side.SELL,
            qty=5,
            type=OrderType.STOP_MARKET,
            trigger_price=None,
        )


def test_venue_rejects_unsupported_order_types_loudly() -> None:
    async def _run() -> None:
        venue = SimVenue()
        inst = InstrumentId("NSE:EQ:RELIANCE")
        venue.configure([inst])

        # Temporarily restrict supported order types
        object.__setattr__(venue, "supported_order_types", {OrderType.MARKET, OrderType.LIMIT})

        spec = OrderSpec(
            instrument=inst,
            side=Side.BUY,
            qty=1,
            type=OrderType.STOP_MARKET,
            trigger_price=Decimal("100.00"),
        )
        ack = await venue.place(spec, client_ref="stop_ref_1")
        assert not ack.accepted
        assert "unsupported order type" in ack.reason.lower()

    asyncio.run(_run())


def test_sim_venue_declares_and_accepts_stop_orders() -> None:
    async def _run() -> None:
        venue = SimVenue()
        inst = InstrumentId("NSE:EQ:RELIANCE")
        venue.configure([inst])

        assert OrderType.STOP in venue.supported_order_types
        assert OrderType.STOP_MARKET in venue.supported_order_types

        spec = OrderSpec(
            instrument=inst,
            side=Side.BUY,
            qty=1,
            type=OrderType.STOP,
            price=Decimal("105.00"),
            trigger_price=Decimal("100.00"),
        )
        ack = await venue.place(spec, client_ref="stop_ref_ok")
        assert ack.accepted
        assert ack.venue_ref != ""

    asyncio.run(_run())
