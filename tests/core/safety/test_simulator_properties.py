"""Safety property tests: SimVenue (simulator) invariants.

Invariants:
1. No fill outside the visible book
2. No fill before arrival time (latency model)
3. Deterministic execution per seed
4. Partial fills sum exactly to order quantity
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from plugins.venues.sim.venue import SimVenue

from algodesk.api import (
    Instrument,
    InstrumentId,
    Level,
    OrderSpec,
    OrderState,
    OrderType,
    Product,
    Quote,
    Side,
)

INFY = InstrumentId("NSE:EQ:INFY")


def _make_inst(inst_id: InstrumentId) -> Instrument:
    return Instrument(
        id=inst_id,
        exchange="NSE",
        segment="EQ",
        symbol=inst_id.split(":")[-1],
        kind="EQ",
        lot_size=1,
        tick_size=Decimal("0.05"),
    )


def _make_book_quote(
    inst: InstrumentId,
    ts: datetime,
    bids: list[tuple[float, int]],
    asks: list[tuple[float, int]],
) -> Quote:
    bid_levels = tuple(Level(price=p, qty=q, orders=1) for p, q in bids)
    ask_levels = tuple(Level(price=p, qty=q, orders=1) for p, q in asks)
    ltp = ask_levels[0].price if ask_levels else (bid_levels[0].price if bid_levels else 100.0)
    return Quote(
        instrument=inst,
        exch_ts=ts,
        recv_ts=ts,
        bids=bid_levels,
        asks=ask_levels,
        ltp=ltp,
        volume=1000,
    )


@pytest.mark.anyio
async def test_no_fill_outside_visible_book() -> None:
    """Invariant 1: Fill prices must strictly match visible depth levels."""
    venue = SimVenue(params={"sim.seed": 42})
    venue.configure([_make_inst(INFY)])

    t0 = datetime(2026, 10, 26, 9, 15, 0, tzinfo=UTC)
    bids = [(1499.0, 100), (1498.0, 100), (1497.0, 100)]
    asks = [(1500.0, 20), (1501.0, 30), (1502.0, 50)]
    venue.on_quote(_make_book_quote(INFY, t0, bids=bids, asks=asks))

    # Buy 40 shares: should consume 20 at 1500.0 and 20 at 1501.0
    spec = OrderSpec(
        instrument=INFY,
        side=Side.BUY,
        qty=40,
        type=OrderType.MARKET,
        product=Product.CNC,
    )
    ack = await venue.place(spec, "order_book_walk")
    assert ack.accepted

    # Trigger matching at t1 (past arrival time)
    t1 = t0 + timedelta(milliseconds=10)
    venue.on_quote(_make_book_quote(INFY, t1, bids=bids, asks=asks))

    events = [e for e in venue.drain_events() if e.kind in ("fill", "partial")]
    assert len(events) == 2

    # Check that fill prices exist in the visible book asks
    ask_prices = {Decimal(str(p)) for p, _ in asks}
    for ev in events:
        fill_price = ev.gross_fill.price if ev.gross_fill else ev.price
        assert fill_price in ask_prices, (
            f"Fill price {fill_price} was outside visible asks: {ask_prices}"
        )

    # Fills: 20 @ 1500.0, 20 @ 1501.0
    assert events[0].qty == 20
    assert events[0].price == Decimal("1500.00")
    assert events[1].qty == 20
    assert events[1].price == Decimal("1501.00")


@pytest.mark.anyio
async def test_no_fill_before_arrival_time() -> None:
    """Invariant 2: Order cannot fill before its arrival time (order_time + latency)."""
    # Configure 5ms latency
    venue = SimVenue(params={"sim.seed": 42, "sim.latency_ms": 5})
    venue.configure([_make_inst(INFY)])

    t0 = datetime(2026, 10, 26, 9, 15, 0, 0, tzinfo=UTC)
    venue.on_quote(_make_book_quote(INFY, t0, bids=[(1490.0, 50)], asks=[(1500.0, 50)]))

    spec = OrderSpec(
        instrument=INFY,
        side=Side.BUY,
        qty=10,
        type=OrderType.MARKET,
        product=Product.CNC,
    )
    ack = await venue.place(spec, "order_latency")
    assert ack.accepted

    # Arrival time is t0 + 5ms.
    # Quote arriving at t0 + 2ms (before arrival) must NOT fill the order
    t_early = t0 + timedelta(milliseconds=2)
    venue.on_quote(_make_book_quote(INFY, t_early, bids=[(1490.0, 50)], asks=[(1500.0, 50)]))
    early_events = [e for e in venue.drain_events() if e.kind in ("fill", "partial")]
    assert len(early_events) == 0, "Order filled before arrival time!"

    # Quote arriving at t0 + 6ms (after arrival) triggers fill
    t_after = t0 + timedelta(milliseconds=6)
    venue.on_quote(_make_book_quote(INFY, t_after, bids=[(1490.0, 50)], asks=[(1500.0, 50)]))
    after_events = [e for e in venue.drain_events() if e.kind in ("fill", "partial")]
    assert len(after_events) == 1
    assert after_events[0].ts >= t0 + timedelta(milliseconds=5)


@pytest.mark.anyio
async def test_deterministic_per_seed() -> None:
    """Invariant 3: Two SimVenue runs with same seed produce identical event sequences."""
    seeds = [7, 42, 999]
    for s in seeds:
        v1 = SimVenue(params={"sim.seed": s})
        v2 = SimVenue(params={"sim.seed": s})
        v1.configure([_make_inst(INFY)])
        v2.configure([_make_inst(INFY)])

        t0 = datetime(2026, 10, 26, 9, 15, 0, tzinfo=UTC)
        q0 = _make_book_quote(INFY, t0, bids=[(1499.0, 100)], asks=[(1500.0, 100)])
        v1.on_quote(q0)
        v2.on_quote(q0)

        spec = OrderSpec(
            instrument=INFY,
            side=Side.BUY,
            qty=25,
            type=OrderType.LIMIT,
            price=Decimal("1500.00"),
            product=Product.CNC,
        )
        await v1.place(spec, f"ref-seed-{s}")
        await v2.place(spec, f"ref-seed-{s}")

        t1 = t0 + timedelta(milliseconds=10)
        q1 = _make_book_quote(INFY, t1, bids=[(1499.0, 100)], asks=[(1500.0, 100)])
        v1.on_quote(q1)
        v2.on_quote(q1)

        evs1 = v1.drain_events()
        evs2 = v2.drain_events()

        assert len(evs1) == len(evs2)
        for e1, e2 in zip(evs1, evs2, strict=True):
            assert e1.kind == e2.kind
            assert e1.qty == e2.qty
            assert e1.price == e2.price
            assert e1.client_ref == e2.client_ref


@pytest.mark.parametrize("order_qty", [10, 25, 60, 100])
@pytest.mark.anyio
async def test_partial_fills_sum_to_order_quantity(order_qty: int) -> None:
    """Invariant 4: Partial and complete fills sum up exactly to the total filled quantity."""
    venue = SimVenue(params={"sim.seed": 42})
    venue.configure([_make_inst(INFY)])

    t0 = datetime(2026, 10, 26, 9, 15, 0, tzinfo=UTC)
    # Depth: 15 shares, 25 shares, 50 shares, 50 shares
    asks = [(1500.0, 15), (1501.0, 25), (1502.0, 50), (1503.0, 50)]
    venue.on_quote(_make_book_quote(INFY, t0, bids=[(1499.0, 100)], asks=asks))

    spec = OrderSpec(
        instrument=INFY,
        side=Side.BUY,
        qty=order_qty,
        type=OrderType.MARKET,
        product=Product.CNC,
    )
    ack = await venue.place(spec, f"ref-sum-{order_qty}")
    assert ack.accepted

    t1 = t0 + timedelta(milliseconds=10)
    venue.on_quote(_make_book_quote(INFY, t1, bids=[(1499.0, 100)], asks=asks))

    events = [e for e in venue.drain_events() if e.kind in ("fill", "partial")]
    total_filled_from_events = sum(e.qty for e in events)

    status = venue.order_status(f"ref-sum-{order_qty}")
    assert status is not None
    assert total_filled_from_events == status.filled_qty
    assert total_filled_from_events == order_qty
    assert status.state == OrderState.FILLED
