"""Property tests and invariants for M4-1c fill model conforming to ADR-085."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from plugins.venues.sim.venue import SimVenue

from algodesk.api.market import InstrumentId, Level, Quote
from algodesk.api.trading import OrderSpec, OrderState, OrderType, Side


def _make_quote(inst: InstrumentId, ts: datetime, p: float, qty: int = 1000) -> Quote:
    return Quote(
        instrument=inst,
        exch_ts=ts,
        recv_ts=ts,
        bids=(Level(price=p, qty=qty),),
        asks=(Level(price=p, qty=qty),),
        ltp=p,
        volume=qty,
    )


# ==============================================================================
# Step 7: Hypothesis property test - Path flip leak detector
# ==============================================================================


@given(
    base_open=st.floats(min_value=10.0, max_value=5000.0, allow_nan=False, allow_infinity=False),
    high_offset=st.floats(min_value=0.0, max_value=200.0, allow_nan=False, allow_infinity=False),
    low_offset=st.floats(min_value=0.0, max_value=200.0, allow_nan=False, allow_infinity=False),
    close_offset=st.floats(
        min_value=-200.0, max_value=200.0, allow_nan=False, allow_infinity=False
    ),
    side=st.sampled_from([Side.BUY, Side.SELL]),
)
@settings(max_examples=40, deadline=None)
def test_market_fill_leak_detector_path_flip_property(
    base_open: float,
    high_offset: float,
    low_offset: float,
    close_offset: float,
    side: Side,
) -> None:
    """Step 7: Market fill price and time are identical when H/L path order is flipped.

    Tests that orders decided on bar t fill strictly from the open quote of bar t+1,
    with zero leak of whether bar t+1 moved up or down.
    """
    o = round(base_open, 2)
    c = round(max(1.0, o + close_offset), 2)
    h = round(max(o, c) + high_offset, 2)
    l_val = round(max(0.5, min(o, c) - low_offset), 2)

    inst = InstrumentId("NSE:EQ:TEST")
    t_bar_t_end = datetime(2025, 1, 8, 10, 0, tzinfo=UTC)

    # Quote timestamps for bar t+1
    t0 = datetime(2025, 1, 9, 3, 45, tzinfo=UTC)
    t1 = datetime(2025, 1, 9, 5, 50, tzinfo=UTC)
    t2 = datetime(2025, 1, 9, 7, 55, tzinfo=UTC)
    t3 = datetime(2025, 1, 9, 10, 0, tzinfo=UTC)

    # Path 1: Up-bar path [O, L, H, C]
    quotes_path1 = [
        _make_quote(inst, t0, o),
        _make_quote(inst, t1, l_val),
        _make_quote(inst, t2, h),
        _make_quote(inst, t3, c),
    ]

    # Path 2: Down-bar path [O, H, L, C]
    quotes_path2 = [
        _make_quote(inst, t0, o),
        _make_quote(inst, t1, h),
        _make_quote(inst, t2, l_val),
        _make_quote(inst, t3, c),
    ]

    async def _run_path(quotes: list[Quote]) -> tuple[Decimal, datetime]:
        v = SimVenue(params={"mode": "backtest", "backtest.slippage_bps": 5})
        v.allow_short = True
        v.configure([inst])
        # Bar t close quote
        v.on_quote(_make_quote(inst, t_bar_t_end, 1000.0))
        spec = OrderSpec(instrument=inst, side=side, qty=10, type=OrderType.MARKET)
        await v.place(spec, "mkt_test")
        for q in quotes:
            v.on_quote(q)
        st_res = v.order_status("mkt_test")
        assert st_res is not None
        assert st_res.state == OrderState.FILLED
        fill = v._orders["mkt_test"].gross_fills[0]
        return fill.price, fill.ts

    p1, ts1 = asyncio.run(_run_path(quotes_path1))
    p2, ts2 = asyncio.run(_run_path(quotes_path2))

    assert p1 == p2, f"Price leaked path direction: {p1} vs {p2}"
    assert ts1 == ts2 == t0, f"Timestamp did not match open quote: {ts1} vs {t0}"


# ==============================================================================
# Step 5: Limit orders and Stop orders open quote rules
# ==============================================================================


@pytest.mark.anyio
async def test_limit_order_eligible_from_t_plus_1_open() -> None:
    """Step 5: Limit orders become eligible from the open quote of t+1 onward."""
    inst = InstrumentId("NSE:EQ:RELIANCE")
    venue = SimVenue(params={"mode": "backtest"})
    venue.configure([inst])

    t_close_t = datetime(2025, 1, 8, 10, 0, tzinfo=UTC)
    venue.on_quote(_make_quote(inst, t_close_t, 1260.0))

    # Place BUY limit order at 1268.00 at bar t close
    spec = OrderSpec(
        instrument=inst,
        side=Side.BUY,
        qty=10,
        type=OrderType.LIMIT,
        price=Decimal("1268.00"),
    )
    await venue.place(spec, "lim_1")

    # Bar t+1 opens at 1267.00 <= limit 1268.00 -> marketable at open!
    t0_open = datetime(2025, 1, 9, 3, 45, tzinfo=UTC)
    venue.on_quote(_make_quote(inst, t0_open, 1267.0))

    st = venue.order_status("lim_1")
    assert st is not None
    assert st.state == OrderState.FILLED
    fill = venue._orders["lim_1"].gross_fills[0]
    assert fill.price == Decimal("1267.00")
    assert fill.ts == t0_open


@pytest.mark.anyio
async def test_limit_order_unmarketable_at_open_rests_and_trades_through() -> None:
    """Step 5: Limit order not marketable at open rests and fills on trade-through."""
    inst = InstrumentId("NSE:EQ:RELIANCE")
    venue = SimVenue(params={"mode": "backtest", "sim.through_ticks": 1})
    venue.configure([inst])

    t_close_t = datetime(2025, 1, 8, 10, 0, tzinfo=UTC)
    venue.on_quote(_make_quote(inst, t_close_t, 1260.0))

    # BUY limit order at 1260.00
    spec = OrderSpec(
        instrument=inst,
        side=Side.BUY,
        qty=10,
        type=OrderType.LIMIT,
        price=Decimal("1260.00"),
    )
    await venue.place(spec, "lim_rest")

    # Bar t+1 opens at 1267.00 > 1260.00 -> unmarketable, must rest
    t0_open = datetime(2025, 1, 9, 3, 45, tzinfo=UTC)
    venue.on_quote(_make_quote(inst, t0_open, 1267.0))
    st = venue.order_status("lim_rest")
    assert st is not None
    assert st.filled_qty == 0

    # Mid quote trades through at 1259.95 <= limit (1260.00 - 0.05)
    t1_mid = datetime(2025, 1, 9, 5, 50, tzinfo=UTC)
    venue.on_quote(_make_quote(inst, t1_mid, 1259.95))
    st2 = venue.order_status("lim_rest")
    assert st2 is not None
    assert st2.state == OrderState.FILLED
    fill = venue._orders["lim_rest"].gross_fills[0]
    assert fill.price == Decimal("1259.95")
    assert fill.ts == t1_mid


@pytest.mark.anyio
async def test_stop_market_crossed_by_open_fills_at_open_price() -> None:
    """Step 5: STOP_MARKET crossed by the open quote fills at that open price."""
    inst = InstrumentId("NSE:EQ:RELIANCE")
    venue = SimVenue(params={"mode": "backtest", "backtest.slippage_bps": 10})
    venue.allow_short = True
    venue.configure([inst])

    t_close_t = datetime(2025, 1, 8, 10, 0, tzinfo=UTC)
    venue.on_quote(_make_quote(inst, t_close_t, 1275.0))

    # SELL STOP_MARKET with trigger_price 1270.00
    spec = OrderSpec(
        instrument=inst,
        side=Side.SELL,
        qty=10,
        type=OrderType.STOP_MARKET,
        trigger_price=Decimal("1270.00"),
    )
    await venue.place(spec, "stop_mkt_open")

    # Bar t+1 opens with a gap-down at 1265.00 (< trigger 1270.00)
    t0_open = datetime(2025, 1, 9, 3, 45, tzinfo=UTC)
    venue.on_quote(_make_quote(inst, t0_open, 1265.0))

    st = venue.order_status("stop_mkt_open")
    assert st is not None
    assert st.state == OrderState.FILLED
    fill = venue._orders["stop_mkt_open"].gross_fills[0]
    # Fills at the OPEN price (1265.00), not adding slippage beyond open
    assert fill.price == Decimal("1265.00")
    assert fill.ts == t0_open


@pytest.mark.anyio
async def test_stop_limit_crossed_by_open_fills_at_open_price() -> None:
    """Step 5: STOP limit crossed by the open quote fills at that open price."""
    inst = InstrumentId("NSE:EQ:RELIANCE")
    venue = SimVenue(params={"mode": "backtest", "backtest.slippage_bps": 10})
    venue.configure([inst])

    t_close_t = datetime(2025, 1, 8, 10, 0, tzinfo=UTC)
    venue.on_quote(_make_quote(inst, t_close_t, 1250.0))

    # BUY STOP with trigger 1260.00, limit 1270.00
    spec = OrderSpec(
        instrument=inst,
        side=Side.BUY,
        qty=10,
        type=OrderType.STOP,
        trigger_price=Decimal("1260.00"),
        price=Decimal("1270.00"),
    )
    await venue.place(spec, "stop_lim_open")

    # Bar t+1 opens with a gap-up at 1265.00 (>= trigger 1260.00, <= limit 1270.00)
    t0_open = datetime(2025, 1, 9, 3, 45, tzinfo=UTC)
    venue.on_quote(_make_quote(inst, t0_open, 1265.0))

    st = venue.order_status("stop_lim_open")
    assert st is not None
    assert st.state == OrderState.FILLED
    fill = venue._orders["stop_lim_open"].gross_fills[0]
    # Fills at the OPEN price (1265.00)
    assert fill.price == Decimal("1265.00")
    assert fill.ts == t0_open


# ==============================================================================
# Step 8: Invariants - arrival timing, bar t quotes isolation, partial sums, seed
# ==============================================================================


@pytest.mark.anyio
async def test_no_fill_earlier_than_arrival() -> None:
    """Step 8: No fill ever uses a quote with timestamp earlier than order arrival."""
    inst = InstrumentId("NSE:EQ:RELIANCE")
    venue = SimVenue(params={"mode": "backtest"})
    venue.configure([inst])

    t_close_t = datetime(2025, 1, 8, 10, 0, tzinfo=UTC)
    venue.on_quote(_make_quote(inst, t_close_t, 1260.0))

    spec = OrderSpec(instrument=inst, side=Side.BUY, qty=10, type=OrderType.MARKET)
    await venue.place(spec, "order_arr")
    order = venue._orders["order_arr"]

    t0 = datetime(2025, 1, 9, 3, 45, tzinfo=UTC)
    venue.on_quote(_make_quote(inst, t0, 1265.0))

    for fill in order.gross_fills:
        assert fill.ts >= order.arrival


@pytest.mark.anyio
async def test_no_fill_from_bar_t_quote() -> None:
    """Step 8: No fill ever uses any quote of bar t."""
    inst = InstrumentId("NSE:EQ:RELIANCE")
    venue = SimVenue(params={"mode": "backtest"})
    venue.configure([inst])

    # Quotes of Bar t
    quotes_t = [
        _make_quote(inst, datetime(2025, 1, 8, 3, 45, tzinfo=UTC), 1250.0),
        _make_quote(inst, datetime(2025, 1, 8, 5, 50, tzinfo=UTC), 1255.0),
        _make_quote(inst, datetime(2025, 1, 8, 7, 55, tzinfo=UTC), 1252.0),
        _make_quote(inst, datetime(2025, 1, 8, 10, 0, tzinfo=UTC), 1258.0),
    ]
    for q in quotes_t:
        venue.on_quote(q)

    # Order decided at end of bar t
    spec = OrderSpec(instrument=inst, side=Side.BUY, qty=10, type=OrderType.MARKET)
    await venue.place(spec, "order_no_bar_t")

    # Bar t+1 quotes
    quotes_next = [
        _make_quote(inst, datetime(2025, 1, 9, 3, 45, tzinfo=UTC), 1265.0),
        _make_quote(inst, datetime(2025, 1, 9, 10, 0, tzinfo=UTC), 1270.0),
    ]
    for q in quotes_next:
        venue.on_quote(q)

    order = venue._orders["order_no_bar_t"]
    bar_t_timestamps = {q.recv_ts for q in quotes_t}
    for fill in order.gross_fills:
        assert fill.ts not in bar_t_timestamps
        assert fill.ts > datetime(2025, 1, 8, 10, 0, tzinfo=UTC)


@pytest.mark.anyio
async def test_partial_fills_sum_to_order_qty() -> None:
    """Step 8: Partial fills sum exactly to the order quantity filled."""
    inst = InstrumentId("NSE:EQ:RELIANCE")
    venue = SimVenue(params={"mode": "backtest", "sim.partial_fill_rate": 0.8, "sim.seed": 123})
    venue.configure([inst])

    t_close_t = datetime(2025, 1, 8, 10, 0, tzinfo=UTC)
    venue.on_quote(_make_quote(inst, t_close_t, 1260.0))

    spec = OrderSpec(instrument=inst, side=Side.BUY, qty=100, type=OrderType.MARKET)
    await venue.place(spec, "partial_order")

    # Feed multiple quotes with depth
    t0 = datetime(2025, 1, 9, 3, 45, tzinfo=UTC)
    venue.on_quote(_make_quote(inst, t0, 1265.0, qty=50))
    t1 = datetime(2025, 1, 9, 5, 50, tzinfo=UTC)
    venue.on_quote(_make_quote(inst, t1, 1266.0, qty=60))

    order = venue._orders["partial_order"]
    assert len(order.gross_fills) >= 1
    total_qty = sum(f.qty for f in order.gross_fills)
    assert total_qty == order.filled_qty


@pytest.mark.anyio
async def test_identical_seed_identical_fills() -> None:
    """Step 8: Same seed gives identical fills across multiple simulation instances."""
    inst = InstrumentId("NSE:EQ:RELIANCE")

    async def _run(seed: int) -> list[tuple[int, Decimal, datetime]]:
        v = SimVenue(
            params={
                "mode": "backtest",
                "sim.seed": seed,
                "sim.partial_fill_rate": 0.5,
                "backtest.slippage_bps": 5,
            }
        )
        v.configure([inst])
        v.on_quote(_make_quote(inst, datetime(2025, 1, 8, 10, 0, tzinfo=UTC), 1260.0))
        spec = OrderSpec(instrument=inst, side=Side.BUY, qty=100, type=OrderType.MARKET)
        await v.place(spec, "seed_ord")
        v.on_quote(_make_quote(inst, datetime(2025, 1, 9, 3, 45, tzinfo=UTC), 1265.0, qty=60))
        v.on_quote(_make_quote(inst, datetime(2025, 1, 9, 5, 50, tzinfo=UTC), 1266.0, qty=60))
        return [(f.qty, f.price, f.ts) for f in v._orders["seed_ord"].gross_fills]

    fills_a = await _run(42)
    fills_b = await _run(42)
    assert fills_a == fills_b
