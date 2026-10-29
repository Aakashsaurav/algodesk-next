"""Safety property tests: Order Manager invariants.

Invariants:
1. No order reaches venue without approved RiskDecision
2. Journal write-ahead: intent and order.created durable BEFORE venue.place
3. No illegal state transition survives
4. Duplicate intent_id rejected
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from algodesk.api import (
    ORDER_TRANSITIONS,
    InstrumentId,
    OrderSpec,
    OrderState,
    OrderType,
    PlaceOrder,
    Product,
    RiskDecision,
    SafetyViolation,
    Side,
    Validity,
    VenueAck,
    check_transition,
)
from algodesk.engine.bus import EventBus
from algodesk.engine.clock import SimClock
from algodesk.fees.engine import FeeEngine
from algodesk.journal.store import JournalReader
from algodesk.journal.writer import Journal, JournalSettings
from algodesk.oms.manager import OrderManager


class MockVenue:
    def __init__(self) -> None:
        self.placed_orders: list[tuple[OrderSpec, str]] = []
        self.call_log: list[str] = []

    async def place(self, spec: OrderSpec, client_ref: str) -> VenueAck:
        self.call_log.append(f"place:{client_ref}")
        self.placed_orders.append((spec, client_ref))
        return VenueAck(client_ref=client_ref, venue_ref=f"vref-{client_ref}", ts=datetime.now(UTC))

    async def cancel(self, client_ref: str) -> VenueAck:
        self.call_log.append(f"cancel:{client_ref}")
        return VenueAck(client_ref=client_ref, venue_ref=f"vref-{client_ref}", ts=datetime.now(UTC))


def _make_spec() -> OrderSpec:
    return OrderSpec(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        side=Side.BUY,
        qty=10,
        type=OrderType.LIMIT,
        price=Decimal("2500.00"),
        product=Product.CNC,
        validity=Validity.DAY,
    )


@pytest.mark.anyio
async def test_no_order_reaches_venue_without_approved_risk_decision(tmp_path: Path) -> None:
    """Invariant 1: Unapproved risk decision prevents order placement and raises SafetyViolation."""
    j_db = tmp_path / "journal.db"
    journal = Journal(
        j_db,
        settings=JournalSettings(
            flush_interval_ms=50, max_buffer_events=1000, backpressure_timeout_ms=5000
        ),
    )
    journal.start_run(mode="paper", code_version="1.0", config_hash="h1", registry_hash="h2")
    venue = MockVenue()
    oms = OrderManager(
        bus=EventBus(),
        journal=journal,
        clock=SimClock(datetime.now(UTC)),
        venue=venue,
        fee_engine=FeeEngine(),
        schedule=None,
        params={},
    )
    spec = _make_spec()
    intent = PlaceOrder(intent_id="intent_rejected", spec=spec)

    # 1. Rejected decision
    rejected_decision = RiskDecision(
        allow=False, rule="limits.max_order_value", reason="Exceeded limit"
    )
    with pytest.raises(SafetyViolation, match="Cannot submit order without approved RiskDecision"):
        await oms.submit(intent, rejected_decision)
    assert len(venue.placed_orders) == 0
    assert len(venue.call_log) == 0

    # 2. None decision
    with pytest.raises(SafetyViolation, match="Cannot submit order without approved RiskDecision"):
        await oms.submit(intent, None)  # type: ignore[arg-type]
    assert len(venue.placed_orders) == 0


@pytest.mark.anyio
async def test_duplicate_intent_id_rejected(tmp_path: Path) -> None:
    """Invariant 4: Duplicate intent_id is immediately rejected with SafetyViolation."""
    j_db = tmp_path / "journal.db"
    journal = Journal(
        j_db,
        settings=JournalSettings(
            flush_interval_ms=50, max_buffer_events=1000, backpressure_timeout_ms=5000
        ),
    )
    journal.start_run(mode="paper", code_version="1.0", config_hash="h1", registry_hash="h2")
    venue = MockVenue()
    oms = OrderManager(
        bus=EventBus(),
        journal=journal,
        clock=SimClock(datetime.now(UTC)),
        venue=venue,
        fee_engine=FeeEngine(),
        schedule=None,
        params={},
    )
    spec = _make_spec()
    intent = PlaceOrder(intent_id="unique_intent_123", spec=spec)
    approved = RiskDecision(allow=True, rule="approved")

    # First submit succeeds
    ord1 = await oms.submit(intent, approved)
    assert ord1.state == OrderState.SUBMITTED
    assert len(venue.placed_orders) == 1

    # Second submit with same intent_id fails
    with pytest.raises(SafetyViolation, match="Duplicate or empty intent_id rejected"):
        await oms.submit(intent, approved)
    # Venue place_order was not called again
    assert len(venue.placed_orders) == 1


@pytest.mark.anyio
async def test_journal_write_ahead_durability(tmp_path: Path) -> None:
    """Invariant 2: Journal records intent and order durable BEFORE venue.place is called."""
    j_db = tmp_path / "journal.db"
    journal = Journal(
        j_db,
        settings=JournalSettings(
            flush_interval_ms=50, max_buffer_events=1000, backpressure_timeout_ms=5000
        ),
    )
    journal.start_run(mode="paper", code_version="1.0", config_hash="h1", registry_hash="h2")

    order_of_ops: list[str] = []

    class LoggingVenue(MockVenue):
        async def place(self, spec: OrderSpec, client_ref: str) -> VenueAck:
            # Check journal state at the moment venue.place is called
            reader = JournalReader(j_db)
            events = reader.events()
            kinds = [e["kind"] for e in events]
            order_of_ops.append(f"venue.place with journal kinds: {kinds}")
            return await super().place(spec, client_ref)

    venue = LoggingVenue()
    oms = OrderManager(
        bus=EventBus(),
        journal=journal,
        clock=SimClock(datetime.now(UTC)),
        venue=venue,
        fee_engine=FeeEngine(),
        schedule=None,
        params={},
    )
    intent = PlaceOrder(intent_id="intent_durable_check", spec=_make_spec())
    approved = RiskDecision(allow=True, rule="approved")

    await oms.submit(intent, approved)

    # Verify journal events were persisted BEFORE venue.place
    assert len(order_of_ops) == 1
    call_desc = order_of_ops[0]
    assert "intent" in call_desc
    assert "order.created" in call_desc


ALL_ORDER_STATES = list(OrderState)
ILLEGAL_TRANSITIONS = [
    (s1, s2)
    for s1 in ALL_ORDER_STATES
    for s2 in ALL_ORDER_STATES
    if s2 not in ORDER_TRANSITIONS.get(s1, set())
]


@pytest.mark.parametrize(("from_state", "to_state"), ILLEGAL_TRANSITIONS)
def test_illegal_order_state_transitions_raise(
    from_state: OrderState, to_state: OrderState
) -> None:
    """Invariant 3: Illegal state transitions raise SafetyViolation and cannot survive."""
    with pytest.raises(SafetyViolation, match="Illegal order transition"):
        check_transition(from_state, to_state)
