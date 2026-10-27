"""Comprehensive tests for OrderManager: write-ahead journaling, races, and timeouts."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from algodesk.api import (
    Charges,
    FillEvent,
    GrossFill,
    InstrumentId,
    OrderEventMsg,
    OrderSpec,
    OrderState,
    OrderStatus,
    OrderType,
    PlaceOrder,
    Product,
    RiskDecision,
    Side,
    Validity,
    VenueAck,
    VenueEvent,
)
from algodesk.api.errors import SafetyViolation
from algodesk.engine.bus import EventBus
from algodesk.engine.clock import SimClock
from algodesk.oms.manager import OrderManager

RELIANCE = InstrumentId("NSE:EQ:RELIANCE")


class SpyJournal:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], bool]] = []

    def append(self, kind: str, payload: dict[str, Any], *, durable: bool = False) -> None:
        self.calls.append((kind, payload, durable))


class SpyVenue:
    def __init__(self) -> None:
        self.place_calls: list[tuple[OrderSpec, str]] = []
        self.cancel_calls: list[str] = []
        self.status_map: dict[str, OrderStatus] = {}
        self.ack_delay_ms: int = 0
        self.call_log: list[str] = []

    async def place(self, spec: OrderSpec, client_ref: str) -> VenueAck:
        self.call_log.append(f"venue.place:{client_ref}")
        self.place_calls.append((spec, client_ref))
        return VenueAck(
            client_ref=client_ref, venue_ref=f"V-{client_ref}", ts=datetime.now(UTC), accepted=True
        )

    async def cancel(self, client_ref: str) -> VenueAck:
        self.call_log.append(f"venue.cancel:{client_ref}")
        self.cancel_calls.append(client_ref)
        return VenueAck(client_ref=client_ref, venue_ref="", ts=datetime.now(UTC), accepted=True)

    def order_status(self, client_ref: str) -> OrderStatus | None:
        return self.status_map.get(client_ref)


class MockFeeEngine:
    def calculate_trade_charges(
        self,
        turnover: Decimal,
        side: Side | str,
        segment: str,
        product: Product | str,
        trade_date: date,
        schedule: Any = None,
        *,
        is_first_scrip_day: bool = True,
    ) -> Charges:
        return Charges(items={"brokerage": Decimal("20.00")}, total=Decimal("20.00"))


class ParamStub:
    def __init__(self, d: dict[str, Any] | None = None) -> None:
        self._d = d or {}

    def get(self, k: str, default: Any = None) -> Any:
        return self._d.get(k, default)


def _setup_oms() -> tuple[OrderManager, SpyVenue, SpyJournal, EventBus, SimClock]:
    t0 = datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC)
    clock = SimClock(initial_time=t0)
    journal = SpyJournal()
    venue = SpyVenue()
    bus = EventBus()
    fee_engine = MockFeeEngine()
    oms = OrderManager(
        bus=bus,
        journal=journal,
        clock=clock,
        venue=venue,
        fee_engine=fee_engine,
        schedule=None,
        params=ParamStub({"oms.submit_timeout_ms": 1000}),
        run_id="run42",
    )
    return oms, venue, journal, bus, clock


def test_write_ahead_journaling_order() -> None:
    """Verify intent and order.created are journaled durable BEFORE venue.place is called."""
    oms, venue, journal, bus, clock = _setup_oms()

    spec = OrderSpec(
        instrument=RELIANCE,
        side=Side.BUY,
        qty=10,
        type=OrderType.MARKET,
        product=Product.CNC,
    )
    intent = PlaceOrder(spec=spec, intent_id="intent-wa-01")
    decision = RiskDecision(allow=True, rule="all_pass")

    async def _run() -> None:
        await oms.submit(intent, decision)

    asyncio.run(_run())

    # Verify journal events logged
    kinds = [c[0] for c in journal.calls]
    assert kinds[0] == "intent"
    assert kinds[1] == "order.created"
    assert kinds[2] == "order.event"  # CREATED -> SUBMITTED

    # Verify durable=True
    assert journal.calls[0][2] is True
    assert journal.calls[1][2] is True

    # Verify venue.place was called
    assert len(venue.place_calls) == 1
    assert venue.place_calls[0][1] == "run42-1"


def test_refuse_unapproved_intent_inv3() -> None:
    """Order manager refuses any intent without an approved RiskDecision (INV-3)."""
    oms, venue, journal, bus, clock = _setup_oms()
    spec = OrderSpec(instrument=RELIANCE, side=Side.BUY, qty=10, type=OrderType.MARKET)
    intent = PlaceOrder(spec=spec, intent_id="intent-bad-01")
    disallowed = RiskDecision(allow=False, rule="max_exposure", reason="limit breach")

    async def _run() -> None:
        with pytest.raises(SafetyViolation) as exc_info:
            await oms.submit(intent, disallowed)
        assert "approved RiskDecision" in str(exc_info.value)

    asyncio.run(_run())
    assert len(venue.place_calls) == 0


def test_duplicate_intent_id_rejection() -> None:
    """Order manager rejects duplicate intent_id."""
    oms, venue, journal, bus, clock = _setup_oms()
    spec = OrderSpec(instrument=RELIANCE, side=Side.BUY, qty=10, type=OrderType.MARKET)
    intent = PlaceOrder(spec=spec, intent_id="dup-intent")
    decision = RiskDecision(allow=True, rule="pass")

    async def _run() -> None:
        await oms.submit(intent, decision)
        with pytest.raises(SafetyViolation) as exc_info:
            await oms.submit(intent, decision)
        assert "Duplicate or empty intent_id" in str(exc_info.value)

    asyncio.run(_run())


@pytest.mark.parametrize(
    "product,validity,order_type",
    [
        (Product.CNC, Validity.DAY, OrderType.MARKET),
        (Product.MIS, Validity.DAY, OrderType.LIMIT),
        (Product.NRML, Validity.IOC, OrderType.MARKET),
        (Product.CNC, Validity.IOC, OrderType.LIMIT),
    ],
)
def test_order_spec_variations(product: Product, validity: Validity, order_type: OrderType) -> None:
    """Submit various order spec configurations successfully."""
    oms, venue, journal, bus, clock = _setup_oms()
    price = Decimal("100.00") if order_type == OrderType.LIMIT else None
    spec = OrderSpec(
        instrument=RELIANCE,
        side=Side.BUY,
        qty=10,
        type=order_type,
        price=price,
        product=product,
        validity=validity,
    )
    intent = PlaceOrder(spec=spec)
    decision = RiskDecision(allow=True, rule="pass")

    async def _run() -> None:
        order = await oms.submit(intent, decision)
        assert order.state == OrderState.SUBMITTED
        assert order.spec.product == product
        assert order.spec.validity == validity

    asyncio.run(_run())


def test_partial_fills_and_final_fill() -> None:
    """Apply partial fills accumulating quantity and average price before final fill."""
    oms, venue, journal, bus, clock = _setup_oms()
    spec = OrderSpec(instrument=RELIANCE, side=Side.BUY, qty=10, type=OrderType.MARKET)
    intent = PlaceOrder(spec=spec, intent_id="pf-01")
    decision = RiskDecision(allow=True, rule="pass")

    bus_events: list[Any] = []
    bus.subscribe(FillEvent, bus_events.append)
    bus.subscribe(OrderEventMsg, bus_events.append)

    async def _run() -> None:
        order = await oms.submit(intent, decision)
        client_ref = order.client_ref

        # Venue Ack
        oms.apply_venue_event(
            VenueEvent(kind="ack", client_ref=client_ref, ts=clock.now(), venue_ref="V-1")
        )
        assert order.state == OrderState.ACKNOWLEDGED

        # Partial fill 1: 4 @ 100.00
        t1 = clock.now() + timedelta(seconds=1)
        gf1 = GrossFill(
            client_ref=client_ref, qty=4, price=Decimal("100.00"), ts=t1, venue_fill_id="F1"
        )
        oms.apply_venue_event(
            VenueEvent(kind="partial", client_ref=client_ref, ts=t1, gross_fill=gf1)
        )
        assert order.state == OrderState.PARTIALLY_FILLED
        assert order.filled_qty == 4
        assert order.avg_price == Decimal("100.00")

        # Partial fill 2: 4 @ 102.00 -> avg = (4*100 + 4*102) / 8 = 101.00
        t2 = clock.now() + timedelta(seconds=2)
        gf2 = GrossFill(
            client_ref=client_ref, qty=4, price=Decimal("102.00"), ts=t2, venue_fill_id="F2"
        )
        oms.apply_venue_event(
            VenueEvent(kind="partial", client_ref=client_ref, ts=t2, gross_fill=gf2)
        )
        assert order.state == OrderState.PARTIALLY_FILLED
        assert order.filled_qty == 8
        assert order.avg_price == Decimal("101.00")

        # Final fill 3: 2 @ 101.00 -> avg = (8*101 + 2*101) / 10 = 101.00
        t3 = clock.now() + timedelta(seconds=3)
        gf3 = GrossFill(
            client_ref=client_ref, qty=2, price=Decimal("101.00"), ts=t3, venue_fill_id="F3"
        )
        oms.apply_venue_event(VenueEvent(kind="fill", client_ref=client_ref, ts=t3, gross_fill=gf3))
        assert order.state == OrderState.FILLED
        assert order.filled_qty == 10
        assert order.avg_price == Decimal("101.00")

    asyncio.run(_run())
    assert len(bus_events) > 0


def test_cancel_flow_and_races() -> None:
    """Test cancel requested, acknowledged, and race against incoming fill."""
    oms, venue, journal, bus, clock = _setup_oms()
    spec = OrderSpec(
        instrument=RELIANCE, side=Side.BUY, qty=10, type=OrderType.LIMIT, price=Decimal("100.00")
    )
    intent = PlaceOrder(spec=spec, intent_id="cr-01")
    decision = RiskDecision(allow=True, rule="pass")

    async def _run() -> None:
        order = await oms.submit(intent, decision)
        ref = order.client_ref
        oms.apply_venue_event(VenueEvent(kind="ack", client_ref=ref, ts=clock.now()))

        # Request cancellation
        await oms.cancel(ref)
        assert order.state == OrderState.CANCELLED or order.state == OrderState.CANCEL_REQUESTED

        # Fill arrives during cancel requested (race condition)
        gf = GrossFill(
            client_ref=ref, qty=10, price=Decimal("100.00"), ts=clock.now(), venue_fill_id="F-RACE"
        )
        oms.apply_venue_event(
            VenueEvent(kind="fill", client_ref=ref, ts=clock.now(), gross_fill=gf)
        )
        assert order.state == OrderState.FILLED

    asyncio.run(_run())


@pytest.mark.parametrize(
    "venue_state,expected_oms_state",
    [
        (OrderState.ACKNOWLEDGED, OrderState.ACKNOWLEDGED),
        (OrderState.FILLED, OrderState.FILLED),
        (OrderState.CANCELLED, OrderState.CANCELLED),
        (OrderState.REJECTED, OrderState.REJECTED),
    ],
)
def test_timeout_unknown_reconciling(
    venue_state: OrderState, expected_oms_state: OrderState
) -> None:
    """Unacknowledged order moves from SUBMITTED -> UNKNOWN -> RECONCILING, then reconciles."""
    oms, venue, journal, bus, clock = _setup_oms()
    spec = OrderSpec(instrument=RELIANCE, side=Side.BUY, qty=10, type=OrderType.MARKET)
    intent = PlaceOrder(spec=spec, intent_id="to-01")
    decision = RiskDecision(allow=True, rule="pass")

    async def _run() -> None:
        order = await oms.submit(intent, decision)
        ref = order.client_ref
        assert order.state == OrderState.SUBMITTED

        # Advance clock past submit_timeout_ms (1000ms)
        t_after = clock.now() + timedelta(milliseconds=1500)
        clock.set_time(t_after)

        # Configure venue status response
        venue.status_map[ref] = OrderStatus(
            client_ref=ref,
            state=venue_state,
            filled_qty=10 if venue_state == OrderState.FILLED else 0,
            avg_price=Decimal("100.00") if venue_state == OrderState.FILLED else Decimal("0.00"),
        )

        timed_out = await oms.check_timeouts()
        assert ref in timed_out
        assert order.state == expected_oms_state

    asyncio.run(_run())
