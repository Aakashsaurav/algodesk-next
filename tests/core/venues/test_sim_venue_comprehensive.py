"""Comprehensive tests for SimVenue (paper simulated venue).

Tests depth matching, multi-level fills, limit order resting, trade-through tick rules,
DAY vs IOC validity, rejects (tick grid, lot size, unknown instrument, short sell),
quote latency and arrival timing, determinism, and failure injection.
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
    Validity,
    venue_problems,
)

RELIANCE = InstrumentId("NSE:EQ:RELIANCE")


def _make_inst(
    inst_id: InstrumentId, lot_size: int = 1, tick_size: Decimal = Decimal("0.05")
) -> Instrument:
    return Instrument(
        id=inst_id,
        exchange="NSE",
        segment="EQ",
        symbol=inst_id.split(":")[-1],
        kind="EQ",
        lot_size=lot_size,
        tick_size=tick_size,
    )


def _make_quote(
    inst: InstrumentId,
    ts: datetime,
    bids: list[tuple[float, int]] | None = None,
    asks: list[tuple[float, int]] | None = None,
) -> Quote:
    bid_levels = tuple(Level(price=p, qty=q, orders=1) for p, q in (bids or []))
    ask_levels = tuple(Level(price=p, qty=q, orders=1) for p, q in (asks or []))
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


def test_sim_venue_conformance() -> None:
    venue = SimVenue()
    problems = venue_problems(venue)
    assert problems == []


@pytest.mark.anyio
async def test_sim_venue_unknown_instrument_rejected() -> None:
    venue = SimVenue()
    venue.configure([_make_inst(RELIANCE)])
    spec = OrderSpec(
        instrument=InstrumentId("NSE:EQ:UNKNOWN"),
        side=Side.BUY,
        qty=10,
        type=OrderType.MARKET,
        product=Product.CNC,
    )
    ack = await venue.place(spec, "ref-unknown")
    assert not ack.accepted
    assert ack.reason == "unknown instrument"


@pytest.mark.parametrize(
    "qty,lot_size,accepted",
    [(10, 1, True), (50, 25, True), (35, 25, False), (100, 50, True)],
)
@pytest.mark.anyio
async def test_sim_venue_lot_size_validation(qty: int, lot_size: int, accepted: bool) -> None:
    venue = SimVenue()
    venue.configure([_make_inst(RELIANCE, lot_size=lot_size)])
    spec = OrderSpec(
        instrument=RELIANCE,
        side=Side.BUY,
        qty=qty,
        type=OrderType.MARKET,
        product=Product.CNC,
    )
    ack = await venue.place(spec, f"ref-lot-{qty}")
    assert ack.accepted == accepted
    if not accepted:
        assert ack.reason == "quantity not a multiple of lot size"


@pytest.mark.parametrize(
    "price,tick_size,accepted",
    [
        (Decimal("100.05"), Decimal("0.05"), True),
        (Decimal("100.03"), Decimal("0.05"), False),
        (Decimal("2500.50"), Decimal("0.25"), True),
        (Decimal("2500.60"), Decimal("0.25"), False),
    ],
)
@pytest.mark.anyio
async def test_sim_venue_tick_grid_validation(
    price: Decimal, tick_size: Decimal, accepted: bool
) -> None:
    venue = SimVenue()
    venue.configure([_make_inst(RELIANCE, tick_size=tick_size)])
    spec = OrderSpec(
        instrument=RELIANCE,
        side=Side.BUY,
        qty=10,
        type=OrderType.LIMIT,
        price=price,
        product=Product.CNC,
    )
    ack = await venue.place(spec, f"ref-tick-{price}")
    assert ack.accepted == accepted
    if not accepted:
        assert ack.reason == "price off the tick grid"


@pytest.mark.parametrize("allow_short,accepted", [(False, False), (True, True)])
@pytest.mark.anyio
async def test_sim_venue_short_selling_validation(allow_short: bool, accepted: bool) -> None:
    venue = SimVenue(params={"sim.allow_short": allow_short})
    venue.configure([_make_inst(RELIANCE)])
    spec = OrderSpec(
        instrument=RELIANCE,
        side=Side.SELL,
        qty=10,
        type=OrderType.MARKET,
        product=Product.CNC,
    )
    ack = await venue.place(spec, "ref-short")
    assert ack.accepted == accepted
    if not accepted:
        assert ack.reason == "sell beyond position"


@pytest.mark.anyio
async def test_sim_venue_market_order_single_level_fill() -> None:
    venue = SimVenue()
    venue.configure([_make_inst(RELIANCE)])
    t0 = datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC)

    # Triggering quote
    q0 = _make_quote(RELIANCE, t0, bids=[(99.0, 100)], asks=[(100.0, 100)])
    venue.on_quote(q0)

    spec = OrderSpec(
        instrument=RELIANCE,
        side=Side.BUY,
        qty=50,
        type=OrderType.MARKET,
        product=Product.CNC,
    )
    ack = await venue.place(spec, "ref-mkt-1")
    assert ack.accepted

    # Next quote executes order
    t1 = t0 + timedelta(milliseconds=10)
    q1 = _make_quote(RELIANCE, t1, bids=[(99.5, 100)], asks=[(100.5, 100)])
    venue.on_quote(q1)

    status = venue.order_status("ref-mkt-1")
    assert status is not None
    assert status.state == OrderState.FILLED
    assert status.filled_qty == 50
    assert status.avg_price == Decimal("100.50")


@pytest.mark.parametrize(
    "order_qty,asks,expected_filled_qty,expected_state",
    [
        (50, [(100.0, 100)], 50, OrderState.FILLED),
        (150, [(100.0, 100), (101.0, 100)], 150, OrderState.FILLED),
        (
            250,
            [(100.0, 100), (101.0, 80), (102.0, 100)],
            250,
            OrderState.FILLED,
        ),
        (
            150,
            [(100.0, 50), (101.0, 50)],
            100,
            OrderState.CANCELLED,  # Remainder cancelled due to insufficient depth
        ),
    ],
)
@pytest.mark.anyio
async def test_sim_venue_market_order_depth_walking(
    order_qty: int,
    asks: list[tuple[float, int]],
    expected_filled_qty: int,
    expected_state: OrderState,
) -> None:
    venue = SimVenue()
    venue.configure([_make_inst(RELIANCE)])
    t0 = datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC)
    venue.on_quote(_make_quote(RELIANCE, t0, bids=[(99.0, 100)], asks=[(100.0, 100)]))

    spec = OrderSpec(
        instrument=RELIANCE,
        side=Side.BUY,
        qty=order_qty,
        type=OrderType.MARKET,
        product=Product.CNC,
    )
    ack = await venue.place(spec, f"ref-depth-{order_qty}")
    assert ack.accepted

    t1 = t0 + timedelta(milliseconds=10)
    q1 = _make_quote(RELIANCE, t1, bids=[(99.0, 100)], asks=asks)
    venue.on_quote(q1)

    status = venue.order_status(f"ref-depth-{order_qty}")
    assert status is not None
    assert status.state == expected_state
    assert status.filled_qty == expected_filled_qty


@pytest.mark.anyio
async def test_sim_venue_market_order_empty_book_cancelled() -> None:
    venue = SimVenue()
    venue.configure([_make_inst(RELIANCE)])
    t0 = datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC)
    venue.on_quote(_make_quote(RELIANCE, t0, bids=[(99.0, 100)], asks=[(100.0, 100)]))

    spec = OrderSpec(
        instrument=RELIANCE,
        side=Side.BUY,
        qty=10,
        type=OrderType.MARKET,
        product=Product.CNC,
    )
    ack = await venue.place(spec, "ref-empty-book")
    assert ack.accepted

    t1 = t0 + timedelta(milliseconds=10)
    # Empty asks
    q1 = _make_quote(RELIANCE, t1, bids=[(99.0, 100)], asks=[])
    venue.on_quote(q1)

    status = venue.order_status("ref-empty-book")
    assert status is not None
    assert status.state == OrderState.CANCELLED
    assert status.filled_qty == 0


@pytest.mark.parametrize(
    "limit_price,best_ask,should_fill",
    [
        (Decimal("101.00"), 100.0, True),  # Marketable: limit > best ask
        (Decimal("100.00"), 100.0, True),  # Marketable: limit == best ask
        (Decimal("99.50"), 100.0, False),  # Non-marketable: rests
    ],
)
@pytest.mark.anyio
async def test_sim_venue_limit_buy_marketable(
    limit_price: Decimal, best_ask: float, should_fill: bool
) -> None:
    venue = SimVenue()
    venue.configure([_make_inst(RELIANCE)])
    t0 = datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC)
    venue.on_quote(_make_quote(RELIANCE, t0, bids=[(98.0, 100)], asks=[(best_ask, 100)]))

    spec = OrderSpec(
        instrument=RELIANCE,
        side=Side.BUY,
        qty=10,
        type=OrderType.LIMIT,
        price=limit_price,
        product=Product.CNC,
    )
    ack = await venue.place(spec, f"ref-lim-{limit_price}")
    assert ack.accepted

    t1 = t0 + timedelta(milliseconds=10)
    q1 = _make_quote(RELIANCE, t1, bids=[(98.0, 100)], asks=[(best_ask, 100)])
    venue.on_quote(q1)

    status = venue.order_status(f"ref-lim-{limit_price}")
    assert status is not None
    if should_fill:
        assert status.state == OrderState.FILLED
        assert status.filled_qty == 10
    else:
        assert status.state == OrderState.ACKNOWLEDGED
        assert status.filled_qty == 0


@pytest.mark.parametrize(
    "through_ticks,ask_price,expect_fill",
    [
        (1, 99.95, True),  # Limit 100.00, ask 99.95 is 1 tick through (0.05) -> fills!
        (1, 100.00, False),  # At limit, not through -> rests
        (2, 99.95, False),  # Only 1 tick through, requires 2 -> rests
        (2, 99.90, True),  # 2 ticks through (0.10) -> fills!
        (3, 99.90, False),  # Only 2 ticks through, requires 3 -> rests
        (3, 99.85, True),  # 3 ticks through (0.15) -> fills!
    ],
)
@pytest.mark.anyio
async def test_sim_venue_resting_limit_buy_trade_through(
    through_ticks: int, ask_price: float, expect_fill: bool
) -> None:
    venue = SimVenue(params={"sim.through_ticks": through_ticks})
    venue.configure([_make_inst(RELIANCE, tick_size=Decimal("0.05"))])
    t0 = datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC)
    venue.on_quote(_make_quote(RELIANCE, t0, bids=[(99.0, 100)], asks=[(101.0, 100)]))

    spec = OrderSpec(
        instrument=RELIANCE,
        side=Side.BUY,
        qty=10,
        type=OrderType.LIMIT,
        price=Decimal("100.00"),
        product=Product.CNC,
    )
    ack = await venue.place(spec, f"ref-tt-{through_ticks}-{ask_price}")
    assert ack.accepted

    t1 = t0 + timedelta(milliseconds=10)
    q1 = _make_quote(RELIANCE, t1, bids=[(ask_price - 0.5, 100)], asks=[(ask_price, 100)])
    venue.on_quote(q1)

    status = venue.order_status(f"ref-tt-{through_ticks}-{ask_price}")
    assert status is not None
    if expect_fill:
        assert status.state == OrderState.FILLED
        assert status.filled_qty == 10
    else:
        assert status.state == OrderState.ACKNOWLEDGED
        assert status.filled_qty == 0


@pytest.mark.parametrize(
    "through_ticks,bid_price,expect_fill",
    [
        (1, 100.05, True),  # Limit 100.00, bid 100.05 is 1 tick through (0.05) -> fills!
        (1, 100.00, False),  # At limit, not through -> rests
        (2, 100.05, False),  # 1 tick through, requires 2 -> rests
        (2, 100.10, True),  # 2 ticks through (0.10) -> fills!
    ],
)
@pytest.mark.anyio
async def test_sim_venue_resting_limit_sell_trade_through(
    through_ticks: int, bid_price: float, expect_fill: bool
) -> None:
    venue = SimVenue(params={"sim.through_ticks": through_ticks, "sim.allow_short": True})
    venue.configure([_make_inst(RELIANCE, tick_size=Decimal("0.05"))])
    t0 = datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC)
    venue.on_quote(_make_quote(RELIANCE, t0, bids=[(99.0, 100)], asks=[(101.0, 100)]))

    spec = OrderSpec(
        instrument=RELIANCE,
        side=Side.SELL,
        qty=10,
        type=OrderType.LIMIT,
        price=Decimal("100.00"),
        product=Product.CNC,
    )
    ack = await venue.place(spec, f"ref-tt-sell-{through_ticks}-{bid_price}")
    assert ack.accepted

    t1 = t0 + timedelta(milliseconds=10)
    q1 = _make_quote(RELIANCE, t1, bids=[(bid_price, 100)], asks=[(bid_price + 0.5, 100)])
    venue.on_quote(q1)

    status = venue.order_status(f"ref-tt-sell-{through_ticks}-{bid_price}")
    assert status is not None
    if expect_fill:
        assert status.state == OrderState.FILLED
        assert status.filled_qty == 10
    else:
        assert status.state == OrderState.ACKNOWLEDGED
        assert status.filled_qty == 0


@pytest.mark.anyio
async def test_sim_venue_day_validity_rests_until_end_of_day() -> None:
    venue = SimVenue()
    venue.configure([_make_inst(RELIANCE)])
    t0 = datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC)
    venue.on_quote(_make_quote(RELIANCE, t0, bids=[(98.0, 100)], asks=[(102.0, 100)]))

    spec = OrderSpec(
        instrument=RELIANCE,
        side=Side.BUY,
        qty=10,
        type=OrderType.LIMIT,
        price=Decimal("100.00"),
        validity=Validity.DAY,
        product=Product.CNC,
    )
    ack = await venue.place(spec, "ref-day")
    assert ack.accepted

    # Multiple quotes during day without trade-through
    for i in range(1, 4):
        t = t0 + timedelta(minutes=i)
        venue.on_quote(_make_quote(RELIANCE, t, bids=[(98.0, 100)], asks=[(102.0, 100)]))
        status = venue.order_status("ref-day")
        assert status is not None
        assert status.state == OrderState.ACKNOWLEDGED

    # End of day cancels order
    venue.end_of_day()
    status = venue.order_status("ref-day")
    assert status is not None
    assert status.state == OrderState.CANCELLED


@pytest.mark.anyio
async def test_sim_venue_ioc_cancels_remainder_immediately() -> None:
    venue = SimVenue()
    venue.configure([_make_inst(RELIANCE)])
    t0 = datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC)
    venue.on_quote(_make_quote(RELIANCE, t0, bids=[(98.0, 100)], asks=[(100.0, 50)]))

    spec = OrderSpec(
        instrument=RELIANCE,
        side=Side.BUY,
        qty=100,
        type=OrderType.MARKET,
        validity=Validity.IOC,
        product=Product.CNC,
    )
    ack = await venue.place(spec, "ref-ioc")
    assert ack.accepted

    t1 = t0 + timedelta(milliseconds=10)
    q1 = _make_quote(RELIANCE, t1, bids=[(98.0, 100)], asks=[(100.0, 40)])
    venue.on_quote(q1)

    status = venue.order_status("ref-ioc")
    assert status is not None
    assert status.state == OrderState.CANCELLED
    assert status.filled_qty == 40


@pytest.mark.anyio
async def test_sim_venue_never_matches_against_triggering_quote() -> None:
    venue = SimVenue(params={"sim.latency_ms": 0, "sim.latency_jitter_ms": 0})
    venue.configure([_make_inst(RELIANCE)])
    t0 = datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC)

    # Quote t0 is the triggering quote
    q0 = _make_quote(RELIANCE, t0, bids=[(99.0, 100)], asks=[(100.0, 100)])
    venue.on_quote(q0)

    spec = OrderSpec(
        instrument=RELIANCE,
        side=Side.BUY,
        qty=10,
        type=OrderType.MARKET,
        product=Product.CNC,
    )
    ack = await venue.place(spec, "ref-never-trigger")
    assert ack.accepted

    # Feeding q0 again should NOT match
    venue.on_quote(q0)
    status = venue.order_status("ref-never-trigger")
    assert status is not None
    assert status.state == OrderState.ACKNOWLEDGED

    # Next quote matches
    t1 = t0 + timedelta(milliseconds=1)
    q1 = _make_quote(RELIANCE, t1, bids=[(99.0, 100)], asks=[(100.0, 100)])
    venue.on_quote(q1)
    status = venue.order_status("ref-never-trigger")
    assert status is not None
    assert status.state == OrderState.FILLED


@pytest.mark.anyio
async def test_sim_venue_latency_and_arrival_timing() -> None:
    # 50ms latency
    venue = SimVenue(params={"sim.latency_ms": 50, "sim.latency_jitter_ms": 0})
    venue.configure([_make_inst(RELIANCE)])
    t0 = datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC)
    venue.on_quote(_make_quote(RELIANCE, t0, bids=[(99.0, 100)], asks=[(100.0, 100)]))

    spec = OrderSpec(
        instrument=RELIANCE,
        side=Side.BUY,
        qty=10,
        type=OrderType.MARKET,
        product=Product.CNC,
    )
    ack = await venue.place(spec, "ref-lat")
    assert ack.accepted

    # Quote at t0 + 20ms (< 50ms arrival) -> does NOT match
    t_early = t0 + timedelta(milliseconds=20)
    q_early = _make_quote(RELIANCE, t_early, bids=[(99.0, 100)], asks=[(100.0, 100)])
    venue.on_quote(q_early)
    st_early = venue.order_status("ref-lat")
    assert st_early is not None and st_early.state == OrderState.ACKNOWLEDGED

    # Quote at t0 + 50ms (>= arrival) -> matches!
    t_arrival = t0 + timedelta(milliseconds=50)
    q_arrival = _make_quote(RELIANCE, t_arrival, bids=[(99.0, 100)], asks=[(100.0, 100)])
    venue.on_quote(q_arrival)
    st_arrival = venue.order_status("ref-lat")
    assert st_arrival is not None and st_arrival.state == OrderState.FILLED


@pytest.mark.parametrize("seed", [42, 12345, 99999])
@pytest.mark.anyio
async def test_sim_venue_seed_determinism(seed: int) -> None:
    params = {
        "sim.latency_ms": 20,
        "sim.latency_jitter_ms": 10,
        "sim.reject_rate": 0.2,
        "sim.partial_fill_rate": 0.3,
        "sim.seed": seed,
    }

    async def run_sequence(venue_params: dict[str, object]) -> list[object]:
        v = SimVenue(params=venue_params)
        v.configure([_make_inst(RELIANCE)])
        t = datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC)
        v.on_quote(_make_quote(RELIANCE, t, bids=[(99.0, 100)], asks=[(100.0, 100)]))

        results = []
        for i in range(10):
            spec = OrderSpec(
                instrument=RELIANCE,
                side=Side.BUY,
                qty=10,
                type=OrderType.MARKET,
                product=Product.CNC,
            )
            ack = await v.place(spec, f"ref-det-{i}")
            results.append(ack.accepted)
            t += timedelta(milliseconds=25)
            v.on_quote(_make_quote(RELIANCE, t, bids=[(99.0, 100)], asks=[(100.0, 100)]))
            st = v.order_status(f"ref-det-{i}")
            results.append(st.filled_qty if st else 0)
        return results

    res1 = await run_sequence(params)
    res2 = await run_sequence(params)
    assert res1 == res2


@pytest.mark.anyio
async def test_sim_venue_injected_reject_rate() -> None:
    venue = SimVenue(params={"sim.reject_rate": 1.0})
    venue.configure([_make_inst(RELIANCE)])
    spec = OrderSpec(
        instrument=RELIANCE,
        side=Side.BUY,
        qty=10,
        type=OrderType.MARKET,
        product=Product.CNC,
    )
    ack = await venue.place(spec, "ref-inj-rej")
    assert not ack.accepted
    assert ack.reason == "injected random reject"


@pytest.mark.anyio
async def test_sim_venue_injected_timeout_rate() -> None:
    venue = SimVenue(params={"sim.timeout_rate": 1.0})
    venue.configure([_make_inst(RELIANCE)])
    spec = OrderSpec(
        instrument=RELIANCE,
        side=Side.BUY,
        qty=10,
        type=OrderType.MARKET,
        product=Product.CNC,
    )
    with pytest.raises(TimeoutError, match="Injected simulated venue timeout"):
        await venue.place(spec, "ref-inj-timeout")


@pytest.mark.anyio
async def test_sim_venue_cancel_resting_order() -> None:
    venue = SimVenue()
    venue.configure([_make_inst(RELIANCE)])
    t0 = datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC)
    venue.on_quote(_make_quote(RELIANCE, t0, bids=[(98.0, 100)], asks=[(102.0, 100)]))

    spec = OrderSpec(
        instrument=RELIANCE,
        side=Side.BUY,
        qty=10,
        type=OrderType.LIMIT,
        price=Decimal("100.00"),
        product=Product.CNC,
    )
    ack = await venue.place(spec, "ref-to-cancel")
    assert ack.accepted

    # Cancel while resting
    cancel_ack = await venue.cancel("ref-to-cancel")
    assert cancel_ack.accepted

    status = venue.order_status("ref-to-cancel")
    assert status is not None
    assert status.state == OrderState.CANCELLED

    # Cancelling again returns False accepted
    cancel_again = await venue.cancel("ref-to-cancel")
    assert not cancel_again.accepted
    cancel_unk = await venue.cancel("non-existent-ref")
    assert not cancel_unk.accepted
