"""Tests for duplicate terminal order events in OrderManager and SimVenue.

Verifies:
1. Exact sequence reproduction: order synchronously rejected -> subsequent reject event
   is journaled as order.duplicate_terminal and counted, never silently ignored.
2. Duplicate cancel events are also journaled and counted.
3. SimVenue validation rejection does not enqueue a redundant async reject event.
4. Property-based testing for order terminal idempotency and state safety.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from algodesk.api.market import Instrument, InstrumentId
from algodesk.api.trading import (
    Charges,
    OrderSpec,
    OrderState,
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
from algodesk.oms.manager import OrderManager


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 11, 4, 8, 30, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

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
    def calculate_trade_charges(cls, **kwargs: Any) -> Charges:
        return Charges(
            brokerage=Decimal("0.00"),
            exchange_txn=Decimal("0.00"),
            sebi=Decimal("0.00"),
            stamp=Decimal("0.00"),
            gst=Decimal("0.00"),
            total=Decimal("0.00"),
            items={},
        )


class RejectingVenue:
    """Venue that synchronously rejects on place and emits an async reject event."""

    def __init__(self, emit_async_reject: bool = True) -> None:
        self.emit_async_reject = emit_async_reject
        self._queue: asyncio.Queue[VenueEvent] = asyncio.Queue()

    async def place(self, spec: OrderSpec, client_ref: str) -> VenueAck:
        ack = VenueAck(
            client_ref=client_ref,
            venue_ref="",
            ts=datetime.now(UTC),
            accepted=False,
            reason="sell beyond position",
        )
        if self.emit_async_reject:
            self._queue.put_nowait(
                VenueEvent(
                    kind="reject",
                    client_ref=client_ref,
                    ts=datetime.now(UTC),
                    reason="sell beyond position",
                )
            )
        return ack

    async def cancel(self, client_ref: str) -> VenueAck:
        return VenueAck(
            client_ref=client_ref,
            venue_ref="",
            ts=datetime.now(UTC),
            accepted=True,
        )

    def drain_events(self) -> list[VenueEvent]:
        events: list[VenueEvent] = []
        while not self._queue.empty():
            events.append(self._queue.get_nowait())
        return events


def test_duplicate_reject_journals_and_counts() -> None:
    async def _run() -> None:
        bus = EventBus()
        journal = FakeJournal()
        clock = FakeClock()
        venue = RejectingVenue(emit_async_reject=True)
        fee_engine = FakeFeeEngine()

        oms = OrderManager(
            bus=bus,
            journal=journal,
            clock=clock,
            venue=venue,
            fee_engine=fee_engine,
            schedule=None,
            params={"oms.submit_timeout_ms": 1000},
            run_id="test_run",
        )

        spec = OrderSpec(
            instrument=InstrumentId("NSE:EQ:TCS"),
            side=Side.SELL,
            qty=1,
            type=OrderType.MARKET,
            product=Product.MIS,
            validity=Validity.DAY,
        )
        intent = PlaceOrder(
            spec=spec,
            intent_id="intent_sell_1",
        )

        # 1. Place order: synchronous reject occurs
        order = await oms.submit(intent, RiskDecision(allow=True, rule="approved"))
        assert order.state == OrderState.REJECTED
        assert oms.duplicate_terminal_count == 0

        # 2. Duplicate async reject event arrives from venue
        async_events = venue.drain_events()
        assert len(async_events) == 1
        dup_event = async_events[0]
        assert dup_event.kind == "reject"

        # Applying duplicate reject must NOT raise SafetyViolation, must journal and count
        oms.apply_venue_event(dup_event)
        assert order.state == OrderState.REJECTED
        assert oms.duplicate_terminal_count == 1

        dup_journals = [e for e in journal.events if e["kind"] == "order.duplicate_terminal"]
        assert len(dup_journals) == 1
        assert dup_journals[0]["payload"]["client_ref"] == order.client_ref
        assert dup_journals[0]["payload"]["from_state"] == "REJECTED"
        assert dup_journals[0]["payload"]["event_kind"] == "reject"
        assert dup_journals[0]["payload"]["reason"] == "sell beyond position"

    asyncio.run(_run())


def test_duplicate_cancel_journals_and_counts() -> None:
    async def _run() -> None:
        bus = EventBus()
        journal = FakeJournal()
        clock = FakeClock()

        class AcceptingVenue:
            async def place(self, spec: OrderSpec, client_ref: str) -> VenueAck:
                return VenueAck(
                    client_ref=client_ref,
                    venue_ref="V-1",
                    ts=datetime.now(UTC),
                    accepted=True,
                )

            async def cancel(self, client_ref: str) -> VenueAck:
                return VenueAck(
                    client_ref=client_ref,
                    venue_ref="V-1",
                    ts=datetime.now(UTC),
                    accepted=True,
                )

        oms = OrderManager(
            bus=bus,
            journal=journal,
            clock=clock,
            venue=AcceptingVenue(),
            fee_engine=FakeFeeEngine(),
            schedule=None,
            params={},
            run_id="test_run",
        )

        spec = OrderSpec(
            instrument=InstrumentId("NSE:EQ:INFY"),
            side=Side.BUY,
            qty=1,
            type=OrderType.LIMIT,
            price=Decimal("1500.00"),
            product=Product.CNC,
            validity=Validity.DAY,
        )
        intent = PlaceOrder(
            spec=spec,
            intent_id="intent_buy_1",
        )

        order = await oms.submit(intent, RiskDecision(allow=True, rule="approved"))
        # Ack
        oms.apply_venue_event(VenueEvent(kind="ack", client_ref=order.client_ref, ts=clock.now()))
        assert order.state == OrderState.ACKNOWLEDGED

        # First cancel
        oms.apply_venue_event(
            VenueEvent(
                kind="cancel", client_ref=order.client_ref, ts=clock.now(), reason="user_cancel"
            )
        )
        assert order.state == OrderState.CANCELLED
        assert oms.duplicate_terminal_count == 0

        # Duplicate cancel
        oms.apply_venue_event(
            VenueEvent(
                kind="cancel", client_ref=order.client_ref, ts=clock.now(), reason="user_cancel"
            )
        )
        assert order.state == OrderState.CANCELLED
        assert oms.duplicate_terminal_count == 1

        dup_journals = [e for e in journal.events if e["kind"] == "order.duplicate_terminal"]
        assert len(dup_journals) == 1
        assert dup_journals[0]["payload"]["from_state"] == "CANCELLED"
        assert dup_journals[0]["payload"]["event_kind"] == "cancel"

    asyncio.run(_run())


def test_sim_venue_no_duplicate_async_reject() -> None:
    async def _run() -> None:
        from plugins.venues.sim.venue import SimVenue

        venue = SimVenue(params={"sim.allow_short": False})
        inst = Instrument(
            id=InstrumentId("NSE:EQ:TCS"),
            exchange="NSE",
            segment="EQ",
            symbol="TCS",
            kind="EQ",
            lot_size=1,
            tick_size=Decimal("0.05"),
        )
        venue.configure([inst])

        spec = OrderSpec(
            instrument=InstrumentId("NSE:EQ:TCS"),
            side=Side.SELL,
            qty=1,
            type=OrderType.MARKET,
            product=Product.MIS,
            validity=Validity.DAY,
        )

        ack = await venue.place(spec, "ref-test-sell-beyond")
        assert not ack.accepted
        assert "sell beyond position" in ack.reason

        # Verify no reject event was placed in event queue
        events = list(venue.drain_events()) if hasattr(venue, "drain_events") else []
        assert len(events) == 0

    asyncio.run(_run())


@given(
    terminal_event=st.sampled_from(["reject", "cancel"]),
    dup_count=st.integers(min_value=1, max_value=5),
)
def test_property_oms_terminal_idempotence(terminal_event: str, dup_count: int) -> None:
    """Property test: Repeated terminal events never crash OMS and are counted accurately."""
    bus = EventBus()
    journal = FakeJournal()
    clock = FakeClock()

    class SimpleVenue:
        async def place(self, spec: OrderSpec, client_ref: str) -> VenueAck:
            return VenueAck(
                client_ref=client_ref, venue_ref="V-1", ts=datetime.now(UTC), accepted=True
            )

        async def cancel(self, client_ref: str) -> VenueAck:
            return VenueAck(
                client_ref=client_ref, venue_ref="V-1", ts=datetime.now(UTC), accepted=True
            )

    oms = OrderManager(
        bus=bus,
        journal=journal,
        clock=clock,
        venue=SimpleVenue(),
        fee_engine=FakeFeeEngine(),
        schedule=None,
        params={},
        run_id="prop_run",
    )

    spec = OrderSpec(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        side=Side.BUY,
        qty=1,
        type=OrderType.MARKET,
        product=Product.CNC,
        validity=Validity.DAY,
    )
    intent = PlaceOrder(spec=spec, intent_id="p1")
    order = asyncio.run(oms.submit(intent, RiskDecision(allow=True, rule="approved")))

    if terminal_event == "cancel":
        ack_ev = VenueEvent(kind="ack", client_ref=order.client_ref, ts=clock.now())
        oms.apply_venue_event(ack_ev)

    # Transition to terminal state via first event
    first_ev = VenueEvent(kind=terminal_event, client_ref=order.client_ref, ts=clock.now())
    oms.apply_venue_event(first_ev)
    expected_state = OrderState.REJECTED if terminal_event == "reject" else OrderState.CANCELLED
    assert order.state == expected_state
    assert oms.duplicate_terminal_count == 0

    # Now apply dup_count subsequent terminal events
    for _ in range(dup_count):
        oms.apply_venue_event(first_ev)

    assert order.state == expected_state
    assert oms.duplicate_terminal_count == dup_count
