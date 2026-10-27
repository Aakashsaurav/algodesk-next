"""Tests for OrderManager (OMS) — state machine, write-ahead journaling, transitions, timeouts."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from algodesk.api.errors import SafetyViolation
from algodesk.api.market import InstrumentId
from algodesk.api.trading import (
    Charges,
    GrossFill,
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
from algodesk.engine.bus import EventBus
from algodesk.engine.events import FillEvent, OrderEventMsg
from algodesk.oms.manager import OrderManager


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 10, 27, 9, 15, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta

    def monotonic_ns(self) -> int:
        return int(self._now.timestamp() * 1_000_000_000)


class FakeJournal:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def append(self, kind: str, payload: dict[str, Any], *, durable: bool = False) -> int:
        self.events.append({"kind": kind, "payload": payload, "durable": durable})
        return len(self.events)


class FakeFeeEngine:
    @classmethod
    def calculate_trade_charges(
        cls,
        turnover: Decimal,
        side: Side,
        segment: str,
        product: Product,
        trade_date: Any,
        schedule: Any,
    ) -> Charges:
        brokerage = Decimal("20.00")
        stt = Decimal("5.00")
        return Charges(items={"brokerage": brokerage, "stt": stt}, total=Decimal("25.00"))


class FakeVenue:
    def __init__(self) -> None:
        self.placed: list[tuple[OrderSpec, str]] = []
        self.cancelled: list[str] = []
        self.status_return: OrderStatus | None = None

    def configure(self, instruments: Any) -> None:
        pass

    async def place(self, spec: OrderSpec, client_ref: str) -> VenueAck:
        self.placed.append((spec, client_ref))
        return VenueAck(client_ref=client_ref, venue_ref=f"v-{client_ref}", ts=datetime.now(UTC))

    async def cancel(self, client_ref: str) -> VenueAck:
        self.cancelled.append(client_ref)
        return VenueAck(client_ref=client_ref, venue_ref=f"v-{client_ref}", ts=datetime.now(UTC))

    def order_status(self, client_ref: str) -> OrderStatus | None:
        return self.status_return

    def end_of_day(self) -> None:
        pass

    def on_quote(self, quote: Any) -> None:
        pass

    async def events(self) -> Any:
        yield None  # type: ignore[misc]

    def positions(self) -> list[Any]:
        return []

    def funds(self) -> Any:
        return None


def make_spec(symbol: str = "NSE:EQ:RELIANCE", qty: int = 10, side: Side = Side.BUY) -> OrderSpec:
    return OrderSpec(
        instrument=InstrumentId(symbol),
        side=side,
        qty=qty,
        type=OrderType.MARKET,
        price=None,
        product=Product.CNC,
        validity=Validity.DAY,
    )


def test_order_manager_refuses_without_approved_decision() -> None:
    async def _run() -> None:
        clock = FakeClock()
        journal = FakeJournal()
        venue = FakeVenue()
        bus = EventBus()
        oms = OrderManager(bus, journal, clock, venue, FakeFeeEngine(), None, {}, run_id="run1")

        intent = PlaceOrder(spec=make_spec(), intent_id="intent-1")
        denied = RiskDecision(allow=False, rule="killswitch", reason="tripped", values={})

        with pytest.raises(SafetyViolation, match="without approved RiskDecision"):
            await oms.submit(intent, denied)

        assert len(venue.placed) == 0
        assert len(journal.events) == 0

    asyncio.run(_run())


def test_order_manager_write_ahead_order_and_deterministic_client_ref() -> None:
    async def _run() -> None:
        clock = FakeClock()
        journal = FakeJournal()
        venue = FakeVenue()
        bus = EventBus()
        oms = OrderManager(bus, journal, clock, venue, FakeFeeEngine(), None, {}, run_id="test_run")

        approved = RiskDecision(allow=True, rule="all_pass", reason="ok", values={})
        intent1 = PlaceOrder(spec=make_spec(qty=10), intent_id="intent-1")
        intent2 = PlaceOrder(spec=make_spec(qty=20), intent_id="intent-2")

        order1 = await oms.submit(intent1, approved)
        order2 = await oms.submit(intent2, approved)

        assert order1.client_ref == "test_run-1"
        assert order2.client_ref == "test_run-2"

        # Verify write-ahead order: intent and order.created are durable
        kinds = [e["kind"] for e in journal.events]
        assert kinds[0] == "intent"
        assert journal.events[0]["durable"] is True
        assert kinds[1] == "order.created"
        assert journal.events[1]["durable"] is True
        assert kinds[2] == "order.event"  # CREATED -> SUBMITTED

    asyncio.run(_run())


def test_order_manager_rejects_duplicate_intent() -> None:
    async def _run() -> None:
        clock = FakeClock()
        journal = FakeJournal()
        venue = FakeVenue()
        bus = EventBus()
        oms = OrderManager(bus, journal, clock, venue, FakeFeeEngine(), None, {}, run_id="run")

        approved = RiskDecision(allow=True, rule="pass", reason="ok", values={})
        intent = PlaceOrder(spec=make_spec(), intent_id="unique-1")

        await oms.submit(intent, approved)

        with pytest.raises(SafetyViolation, match="Duplicate or empty intent_id"):
            await oms.submit(intent, approved)

    asyncio.run(_run())


def test_order_manager_venue_ack_and_fill_events() -> None:
    async def _run() -> None:
        clock = FakeClock()
        journal = FakeJournal()
        venue = FakeVenue()
        bus = EventBus()

        received_order_events: list[OrderEventMsg] = []
        received_fills: list[FillEvent] = []
        bus.subscribe(OrderEventMsg, lambda m: received_order_events.append(m))
        bus.subscribe(FillEvent, lambda m: received_fills.append(m))

        oms = OrderManager(bus, journal, clock, venue, FakeFeeEngine(), None, {}, run_id="run")
        approved = RiskDecision(allow=True, rule="pass", reason="ok", values={})
        intent = PlaceOrder(spec=make_spec(qty=10), intent_id="intent-fill")

        order = await oms.submit(intent, approved)
        assert order.state == OrderState.SUBMITTED

        # Venue Ack event
        ack_evt = VenueEvent(
            kind="ack", client_ref=order.client_ref, ts=clock.now(), venue_ref="v-100"
        )
        oms.apply_venue_event(ack_evt)
        assert order.state == OrderState.ACKNOWLEDGED
        assert order.venue_ref == "v-100"

        # Partial Fill event
        gf1 = GrossFill(
            client_ref=order.client_ref,
            qty=4,
            price=Decimal("150.00"),
            ts=clock.now(),
            venue_fill_id="vf-1",
        )
        partial_evt = VenueEvent(
            kind="partial", client_ref=order.client_ref, ts=clock.now(), gross_fill=gf1
        )
        oms.apply_venue_event(partial_evt)

        assert order.state == OrderState.PARTIALLY_FILLED
        assert order.filled_qty == 4
        assert order.avg_price == Decimal("150.00")
        assert len(received_fills) == 1
        assert received_fills[0].fill.charges.total == Decimal("25.00")

        # Complete Fill event
        gf2 = GrossFill(
            client_ref=order.client_ref,
            qty=6,
            price=Decimal("152.00"),
            ts=clock.now(),
            venue_fill_id="vf-2",
        )
        fill_evt = VenueEvent(
            kind="fill", client_ref=order.client_ref, ts=clock.now(), gross_fill=gf2
        )
        oms.apply_venue_event(fill_evt)

        assert order.state == OrderState.FILLED
        assert order.filled_qty == 10
        # Weighted avg price: (4 * 150 + 6 * 152) / 10 = (600 + 912) / 10 = 151.20
        assert order.avg_price == Decimal("151.20")
        assert len(received_fills) == 2

    asyncio.run(_run())


def test_order_manager_cancel_flow() -> None:
    async def _run() -> None:
        clock = FakeClock()
        journal = FakeJournal()
        venue = FakeVenue()
        bus = EventBus()
        oms = OrderManager(bus, journal, clock, venue, FakeFeeEngine(), None, {}, run_id="run")

        approved = RiskDecision(allow=True, rule="pass", reason="ok", values={})
        order = await oms.submit(PlaceOrder(spec=make_spec(qty=10), intent_id="intent-c"), approved)

        # Ack from venue
        oms.apply_venue_event(VenueEvent(kind="ack", client_ref=order.client_ref, ts=clock.now()))
        assert order.state == OrderState.ACKNOWLEDGED

        # Request cancel
        await oms.cancel(order.client_ref)
        assert order.state == OrderState.CANCEL_REQUESTED
        assert order.client_ref in venue.cancelled

        # Venue confirms cancel
        oms.apply_venue_event(
            VenueEvent(kind="cancel", client_ref=order.client_ref, ts=clock.now())
        )
        assert order.state == OrderState.CANCELLED

    asyncio.run(_run())


def test_order_manager_illegal_transition_raises_safety_violation_and_journals() -> None:
    async def _run() -> None:
        clock = FakeClock()
        journal = FakeJournal()
        venue = FakeVenue()
        bus = EventBus()
        oms = OrderManager(bus, journal, clock, venue, FakeFeeEngine(), None, {}, run_id="run")

        approved = RiskDecision(allow=True, rule="pass", reason="ok", values={})
        order = await oms.submit(
            PlaceOrder(spec=make_spec(qty=10), intent_id="intent-err"), approved
        )

        # Directly driving illegal transition from SUBMITTED to FILLED (must go through ACK)
        with pytest.raises(SafetyViolation):
            oms._transition(order, OrderState.FILLED)

        # Verify that error was journaled with durable=True
        err_events = [e for e in journal.events if e["kind"] == "error"]
        assert len(err_events) == 1
        assert err_events[0]["durable"] is True
        assert err_events[0]["payload"]["error"] == "SafetyViolation"

    asyncio.run(_run())


def test_order_manager_submit_timeout_reconciliation() -> None:
    async def _run() -> None:
        clock = FakeClock()
        journal = FakeJournal()
        venue = FakeVenue()
        bus = EventBus()
        params = {"oms.submit_timeout_ms": 3000}
        oms = OrderManager(bus, journal, clock, venue, FakeFeeEngine(), None, params, run_id="run")

        approved = RiskDecision(allow=True, rule="pass", reason="ok", values={})
        order = await oms.submit(
            PlaceOrder(spec=make_spec(qty=10), intent_id="intent-to"), approved
        )
        assert order.state == OrderState.SUBMITTED

        # Advance clock by 2s -> not timed out yet
        clock.advance(timedelta(seconds=2))
        timed_out = await oms.check_timeouts()
        assert len(timed_out) == 0
        assert order.state == OrderState.SUBMITTED

        # Advance clock by another 1.5s -> total 3.5s > 3s timeout
        clock.advance(timedelta(milliseconds=1500))

        # Configure venue order_status return
        venue.status_return = OrderStatus(
            client_ref=order.client_ref,
            state=OrderState.ACKNOWLEDGED,
            venue_ref="v-late-ack",
        )

        timed_out = await oms.check_timeouts()
        assert timed_out == [order.client_ref]

        # Verify order reconciled through UNKNOWN -> RECONCILING -> ACKNOWLEDGED
        assert order.state == OrderState.ACKNOWLEDGED
        assert order.venue_ref == "v-late-ack"

        states_journaled = [
            e["payload"]["to_state"] for e in journal.events if e["kind"] == "order.event"
        ]
        assert OrderState.UNKNOWN.value in states_journaled
        assert OrderState.RECONCILING.value in states_journaled
        assert OrderState.ACKNOWLEDGED.value in states_journaled

    asyncio.run(_run())
