"""Advanced tests for OrderManager: submit timeouts, UNKNOWN to RECONCILING,
cancel races, duplicate intent rejection, write-ahead journaling guarantees,
and fee engine fill charges.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from algodesk.api import (
    Charges,
    ExecutionVenue,
    Funds,
    GrossFill,
    InstrumentId,
    OrderSpec,
    OrderState,
    OrderStatus,
    OrderType,
    PlaceOrder,
    Position,
    Product,
    RiskDecision,
    Side,
    VenueAck,
    VenueEvent,
)
from algodesk.oms.manager import OrderManager

RELIANCE = InstrumentId("NSE:EQ:RELIANCE")


class StubClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def utc_now(self) -> datetime:
        return self._now

    def set_time(self, new_time: datetime) -> None:
        self._now = new_time


class MockJournal:
    def __init__(self, fail_on_order_created: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any], bool]] = []
        self.fail_on_order_created = fail_on_order_created

    def append(self, event_type: str, payload: dict[str, Any], durable: bool = False) -> None:
        if self.fail_on_order_created and event_type == "order.created":
            raise RuntimeError("Disk write failed on order.created")
        self.calls.append((event_type, payload, durable))


class MockEventBus:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    def publish(self, *args: Any) -> None:
        self.messages.append(args[0] if len(args) == 1 else args)


class MockFeeEngine:
    def calculate_trade_charges(self, **kwargs: Any) -> Charges:
        return Charges(
            items={
                "brokerage": Decimal("15.00"),
                "stt": Decimal("2.00"),
                "exchange_txn": Decimal("1.00"),
                "gst": Decimal("1.50"),
                "sebi": Decimal("0.10"),
                "stamp": Decimal("0.40"),
            },
            total=Decimal("20.00"),
        )


class MockVenue(ExecutionVenue):
    def __init__(self, delay_ms: int = 0, no_immediate_ack: bool = False) -> None:
        self.delay_ms = delay_ms
        self.no_immediate_ack = no_immediate_ack
        self.placed_specs: list[tuple[OrderSpec, str]] = []
        self.order_statuses: dict[str, OrderStatus] = {}
        self.cancel_calls: list[str] = []

    def configure(self, instruments: Any) -> None:
        pass

    async def place(self, spec: OrderSpec, client_ref: str) -> VenueAck | None:
        self.placed_specs.append((spec, client_ref))
        if self.no_immediate_ack:
            return None
        now = datetime(2026, 1, 1, 9, 15, tzinfo=UTC)
        return VenueAck(client_ref=client_ref, venue_ref=f"V-{client_ref}", ts=now, accepted=True)

    async def cancel(self, client_ref: str) -> VenueAck:
        self.cancel_calls.append(client_ref)
        now = datetime(2026, 1, 1, 9, 15, tzinfo=UTC)
        return VenueAck(client_ref=client_ref, venue_ref=f"V-{client_ref}", ts=now, accepted=True)

    def order_status(self, client_ref: str) -> OrderStatus | None:
        return self.order_statuses.get(client_ref)

    def end_of_day(self) -> None:
        pass

    def positions(self) -> list[Position]:
        return []

    def funds(self) -> Funds:
        return Funds(
            available=Decimal("100000.00"), used=Decimal("0.00"), total=Decimal("100000.00")
        )

    async def events(self) -> Any:
        return
        yield

    def drain_events(self) -> list[VenueEvent]:
        return []


def _approved_decision() -> RiskDecision:
    return RiskDecision(allow=True, rule="all_passed", reason="Risk approved", values={})


def _order_spec(qty: int = 10, price: Decimal | None = None) -> OrderSpec:
    return OrderSpec(
        instrument=RELIANCE,
        side=Side.BUY,
        qty=qty,
        type=OrderType.MARKET if price is None else OrderType.LIMIT,
        price=price,
        product=Product.CNC,
    )


@pytest.mark.anyio
async def test_order_manager_write_ahead_guarantee() -> None:
    journal = MockJournal()
    bus = MockEventBus()
    venue = MockVenue()
    clock = StubClock(datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC))
    fee_engine = MockFeeEngine()
    params = {"oms.submit_timeout_ms": 1000}

    om = OrderManager(
        bus=bus,
        journal=journal,
        clock=clock,
        venue=venue,
        fee_engine=fee_engine,
        schedule=None,
        params=params,
        run_id="run_wa",
    )

    intent = PlaceOrder(spec=_order_spec(qty=10), intent_id="intent-wa-1")
    order = await om.submit(intent, decision=_approved_decision())

    # Verify journal events recorded with durable=True
    event_types = [call[0] for call in journal.calls]
    assert event_types[0] == "intent"
    assert event_types[1] == "order.created"
    assert journal.calls[0][2] is True  # durable=True
    assert journal.calls[1][2] is True  # durable=True

    # Verify venue place was called
    assert len(venue.placed_specs) == 1
    assert venue.placed_specs[0][1] == order.client_ref


@pytest.mark.anyio
async def test_order_manager_write_ahead_failure_aborts_venue_place() -> None:
    journal = MockJournal(fail_on_order_created=True)
    bus = MockEventBus()
    venue = MockVenue()
    clock = StubClock(datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC))
    fee_engine = MockFeeEngine()
    params = {"oms.submit_timeout_ms": 1000}

    om = OrderManager(
        bus=bus,
        journal=journal,
        clock=clock,
        venue=venue,
        fee_engine=fee_engine,
        schedule=None,
        params=params,
        run_id="run_abort",
    )

    intent = PlaceOrder(spec=_order_spec(qty=10), intent_id="intent-abort-1")
    with pytest.raises(RuntimeError, match="Disk write failed on order.created"):
        await om.submit(intent, decision=_approved_decision())

    # Venue place was NEVER called!
    assert len(venue.placed_specs) == 0


@pytest.mark.parametrize("n_orders", [1, 2, 5, 8])
@pytest.mark.anyio
async def test_order_manager_deterministic_client_ref(n_orders: int) -> None:
    journal = MockJournal()
    bus = MockEventBus()
    venue = MockVenue()
    clock = StubClock(datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC))
    fee_engine = MockFeeEngine()
    params = {"oms.submit_timeout_ms": 1000}

    om = OrderManager(
        bus=bus,
        journal=journal,
        clock=clock,
        venue=venue,
        fee_engine=fee_engine,
        schedule=None,
        params=params,
        run_id="det_run",
    )

    for i in range(1, n_orders + 1):
        intent = PlaceOrder(spec=_order_spec(qty=10), intent_id=f"intent-det-{i}")
        order = await om.submit(intent, decision=_approved_decision())
        assert order.client_ref == f"det_run-{i}"


@pytest.mark.parametrize("dup_id", ["id-alpha", "id-beta", "id-gamma"])
@pytest.mark.anyio
async def test_order_manager_duplicate_intent_rejection(dup_id: str) -> None:
    from algodesk.api import SafetyViolation

    journal = MockJournal()
    bus = MockEventBus()
    venue = MockVenue()
    clock = StubClock(datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC))
    fee_engine = MockFeeEngine()
    params = {"oms.submit_timeout_ms": 1000}

    om = OrderManager(
        bus=bus,
        journal=journal,
        clock=clock,
        venue=venue,
        fee_engine=fee_engine,
        schedule=None,
        params=params,
        run_id="run_dup",
    )

    intent1 = PlaceOrder(spec=_order_spec(qty=10), intent_id=dup_id)
    await om.submit(intent1, decision=_approved_decision())

    intent2 = PlaceOrder(spec=_order_spec(qty=20), intent_id=dup_id)
    with pytest.raises(SafetyViolation, match=f"Duplicate or empty intent_id rejected: '{dup_id}'"):
        await om.submit(intent2, decision=_approved_decision())


@pytest.mark.parametrize(
    "reconciled_state", [OrderState.FILLED, OrderState.CANCELLED, OrderState.ACKNOWLEDGED]
)
@pytest.mark.anyio
async def test_order_manager_submit_timeout_to_reconciling(reconciled_state: OrderState) -> None:
    journal = MockJournal()
    bus = MockEventBus()
    venue = MockVenue(no_immediate_ack=True)
    clock = StubClock(datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC))
    fee_engine = MockFeeEngine()
    params = {"oms.submit_timeout_ms": 50}

    om = OrderManager(
        bus=bus,
        journal=journal,
        clock=clock,
        venue=venue,
        fee_engine=fee_engine,
        schedule=None,
        params=params,
        run_id="run_timeout",
    )

    intent = PlaceOrder(spec=_order_spec(qty=10), intent_id="intent-to-1")
    order = await om.submit(intent, decision=_approved_decision())
    assert order.state == OrderState.SUBMITTED

    # Advance time beyond submit_timeout_ms (50ms)
    clock.set_time(clock.now() + timedelta(milliseconds=100))

    # Set up venue order status to return reconciled_state
    venue.order_statuses["run_timeout-1"] = OrderStatus(
        client_ref="run_timeout-1",
        state=reconciled_state,
        venue_ref="V-1",
        filled_qty=10 if reconciled_state == OrderState.FILLED else 0,
        avg_price=Decimal("100.00"),
    )

    timed_out = await om.check_timeouts()
    assert timed_out == ["run_timeout-1"]

    # Order should have transitioned to reconciled state
    assert order.state == reconciled_state


@pytest.mark.anyio
async def test_order_manager_cancel_race_with_fill() -> None:
    from algodesk.api import SafetyViolation

    journal = MockJournal()
    bus = MockEventBus()
    venue = MockVenue()
    clock = StubClock(datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC))
    fee_engine = MockFeeEngine()
    params = {"oms.submit_timeout_ms": 1000}

    om = OrderManager(
        bus=bus,
        journal=journal,
        clock=clock,
        venue=venue,
        fee_engine=fee_engine,
        schedule=None,
        params=params,
        run_id="run_race",
    )

    intent = PlaceOrder(spec=_order_spec(qty=10), intent_id="intent-race-1")
    order = await om.submit(intent, decision=_approved_decision())
    om.apply_venue_event(
        VenueEvent(kind="ack", client_ref=order.client_ref, venue_ref="V-1", ts=clock.now())
    )
    assert order.state == OrderState.ACKNOWLEDGED

    # Fill arrives BEFORE cancel request is processed by venue
    ts = datetime(2026, 1, 1, 9, 15, 1, tzinfo=UTC)
    gross_fill = GrossFill(
        client_ref=order.client_ref,
        qty=10,
        price=Decimal("100.00"),
        ts=ts,
        venue_fill_id="F-1",
    )
    fill_event = VenueEvent(
        kind="fill",
        client_ref=order.client_ref,
        venue_ref=order.venue_ref,
        ts=ts,
        gross_fill=gross_fill,
        qty=10,
        price=Decimal("100.00"),
        venue_fill_id="F-1",
    )
    om.apply_venue_event(fill_event)
    assert order.state == OrderState.FILLED

    # Now cancel on the filled order raises SafetyViolation (illegal transition)
    with pytest.raises(SafetyViolation):
        await om.cancel(order.client_ref)


@pytest.mark.parametrize(
    "qty,n_fills,prices",
    [
        (10, 1, [Decimal("100.00")]),
        (50, 2, [Decimal("100.00"), Decimal("101.00")]),
        (100, 4, [Decimal("100.00"), Decimal("100.50"), Decimal("101.00"), Decimal("101.50")]),
    ],
)
@pytest.mark.anyio
async def test_order_manager_fee_engine_multi_fill_charges(
    qty: int, n_fills: int, prices: list[Decimal]
) -> None:
    from algodesk.api import FillEvent

    journal = MockJournal()
    bus = MockEventBus()
    venue = MockVenue()
    clock = StubClock(datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC))
    fee_engine = MockFeeEngine()
    params = {"oms.submit_timeout_ms": 1000}

    om = OrderManager(
        bus=bus,
        journal=journal,
        clock=clock,
        venue=venue,
        fee_engine=fee_engine,
        schedule=None,
        params=params,
        run_id="run_fees",
    )

    intent = PlaceOrder(spec=_order_spec(qty=qty), intent_id=f"intent-fees-{qty}")
    order = await om.submit(intent, decision=_approved_decision())
    om.apply_venue_event(
        VenueEvent(kind="ack", client_ref=order.client_ref, venue_ref="V-1", ts=clock.now())
    )
    assert order.state == OrderState.ACKNOWLEDGED

    fill_qty = qty // n_fills
    for i, p in enumerate(prices):
        is_last = i == len(prices) - 1
        ts = datetime(2026, 1, 1, 9, 15, i + 1, tzinfo=UTC)
        gross_fill = GrossFill(
            client_ref=order.client_ref,
            qty=fill_qty,
            price=p,
            ts=ts,
            venue_fill_id=f"F-{i}",
        )
        ev = VenueEvent(
            kind="fill" if is_last else "partial",
            client_ref=order.client_ref,
            venue_ref=order.venue_ref,
            ts=ts,
            gross_fill=gross_fill,
            qty=fill_qty,
            price=p,
            venue_fill_id=f"F-{i}",
        )
        om.apply_venue_event(ev)

    assert order.state == OrderState.FILLED
    assert order.filled_qty == qty

    # Check FillEvents published to bus
    fill_events = [m for m in bus.messages if isinstance(m, FillEvent)]
    assert len(fill_events) == n_fills
    total_charges = sum(fe.fill.charges.total for fe in fill_events)
    expected_total = Decimal("20.00") * n_fills
    assert total_charges == expected_total
