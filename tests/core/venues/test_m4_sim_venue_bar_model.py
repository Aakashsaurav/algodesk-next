"""Tests for Step 10: SimVenue bar model, STOP orders, slippage, and ambiguous bars."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from plugins.venues.sim.venue import SimVenue

from algodesk.api.market import Bar, InstrumentId, Level, Quote
from algodesk.api.trading import OrderSpec, OrderState, OrderType, Side


def _make_quote(
    instrument: InstrumentId,
    ts: datetime,
    bid_p: float,
    ask_p: float,
    qty: int = 100,
    ltp: float | None = None,
) -> Quote:
    p_ltp = ltp if ltp is not None else ask_p
    return Quote(
        instrument=instrument,
        exch_ts=ts,
        recv_ts=ts,
        bids=(Level(price=bid_p, qty=qty),),
        asks=(Level(price=ask_p, qty=qty),),
        ltp=p_ltp,
        volume=qty,
    )


def test_market_orders_add_slippage_bps() -> None:
    async def _run() -> None:
        inst = InstrumentId("NSE:EQ:RELIANCE")
        # slippage_bps = 10 (0.10%)
        venue = SimVenue(params={"backtest.slippage_bps": 10})
        venue.configure([inst])

        t0 = datetime(2026, 11, 10, 9, 15, tzinfo=UTC)
        q0 = _make_quote(inst, t0, 100.0, 100.0, qty=100)
        venue.on_quote(q0)

        # Place BUY market order
        spec_buy = OrderSpec(
            instrument=inst,
            side=Side.BUY,
            qty=10,
            type=OrderType.MARKET,
        )
        await venue.place(spec_buy, client_ref="buy_ref")

        t1 = t0 + timedelta(minutes=1)
        q1 = _make_quote(inst, t1, 100.0, 100.0, qty=100)
        venue.on_quote(q1)

        st = venue.order_status("buy_ref")
        assert st is not None
        assert st.state == OrderState.FILLED
        # 100.00 * (1 + 10/10000) = 100.10
        assert st.avg_price == Decimal("100.10")

        # Place SELL market order
        venue.allow_short = True
        spec_sell = OrderSpec(
            instrument=inst,
            side=Side.SELL,
            qty=10,
            type=OrderType.MARKET,
        )
        await venue.place(spec_sell, client_ref="sell_ref")

        t2 = t1 + timedelta(minutes=1)
        q2 = _make_quote(inst, t2, 100.0, 100.0, qty=100)
        venue.on_quote(q2)

        st_sell = venue.order_status("sell_ref")
        assert st_sell is not None
        assert st_sell.state == OrderState.FILLED
        # 100.00 * (1 - 10/10000) = 99.90
        assert st_sell.avg_price == Decimal("99.90")

    asyncio.run(_run())


def test_stop_market_triggers_on_crossing_quote_and_gap_through() -> None:
    async def _run() -> None:
        inst = InstrumentId("NSE:EQ:RELIANCE")
        venue = SimVenue(params={"backtest.slippage_bps": 0})
        venue.configure([inst])
        venue.allow_short = True

        t0 = datetime(2026, 11, 10, 9, 15, tzinfo=UTC)
        q0 = _make_quote(inst, t0, 100.0, 100.0, qty=100)
        venue.on_quote(q0)

        # Place SELL STOP_MARKET with trigger_price 95.00
        spec = OrderSpec(
            instrument=inst,
            side=Side.SELL,
            qty=10,
            type=OrderType.STOP_MARKET,
            trigger_price=Decimal("95.00"),
        )
        await venue.place(spec, client_ref="stop_mkt_1")

        # Quote 1: price 98.00 (not crossed)
        t1 = t0 + timedelta(minutes=1)
        venue.on_quote(_make_quote(inst, t1, 98.0, 98.0, qty=100, ltp=98.0))
        st1 = venue.order_status("stop_mkt_1")
        assert st1 is not None
        assert st1.state == OrderState.ACKNOWLEDGED
        assert st1.filled_qty == 0

        # Quote 2 (Gap-through): opens at 90.00 (< trigger 95.00)
        t2 = t1 + timedelta(minutes=1)
        venue.on_quote(_make_quote(inst, t2, 90.0, 90.0, qty=100, ltp=90.0))
        st2 = venue.order_status("stop_mkt_1")
        assert st2 is not None
        assert st2.state == OrderState.FILLED
        # A stop crossed by the opening quote fills at that open
        assert st2.avg_price == Decimal("90.00")

    asyncio.run(_run())


def test_stop_order_triggers_and_becomes_limit_order() -> None:
    async def _run() -> None:
        inst = InstrumentId("NSE:EQ:RELIANCE")
        venue = SimVenue(params={"backtest.slippage_bps": 0})
        venue.configure([inst])

        t0 = datetime(2026, 11, 10, 9, 15, tzinfo=UTC)
        venue.on_quote(_make_quote(inst, t0, 100.0, 100.0))

        # BUY STOP order: trigger 105.00, limit price 106.00
        spec = OrderSpec(
            instrument=inst,
            side=Side.BUY,
            qty=5,
            type=OrderType.STOP,
            price=Decimal("106.00"),
            trigger_price=Decimal("105.00"),
        )
        await venue.place(spec, client_ref="stop_limit_1")

        # Quote at 104.00 (not triggered)
        t1 = t0 + timedelta(minutes=1)
        venue.on_quote(_make_quote(inst, t1, 104.0, 104.0, ltp=104.0))
        assert venue.order_status("stop_limit_1").filled_qty == 0

        # Quote at 105.00 (triggers! marketable within limit 106.00)
        t2 = t1 + timedelta(minutes=1)
        venue.on_quote(_make_quote(inst, t2, 105.0, 105.0, ltp=105.0))
        st = venue.order_status("stop_limit_1")
        assert st is not None
        assert st.state == OrderState.FILLED
        assert st.avg_price == Decimal("105.00")

    asyncio.run(_run())


def test_ambiguous_bars_counting() -> None:
    async def _run() -> None:
        inst = InstrumentId("NSE:EQ:RELIANCE")
        venue = SimVenue()
        venue.configure([inst])
        venue.allow_short = True

        t0 = datetime(2026, 11, 10, 9, 15, tzinfo=UTC)
        venue.on_quote(_make_quote(inst, t0, 100.0, 100.0))

        # Resting limit order at 110.00
        spec_lim = OrderSpec(
            instrument=inst,
            side=Side.SELL,
            qty=1,
            type=OrderType.LIMIT,
            price=Decimal("110.00"),
        )
        await venue.place(spec_lim, client_ref="lim_1")

        # Resting stop order at 95.00
        spec_stop = OrderSpec(
            instrument=inst,
            side=Side.SELL,
            qty=1,
            type=OrderType.STOP_MARKET,
            trigger_price=Decimal("95.00"),
        )
        await venue.place(spec_stop, client_ref="stop_1")

        # Bar spanning low 90.00 to high 115.00: BOTH reachable in this bar!
        b = Bar(
            instrument=inst,
            timeframe="1d",
            start_ts=t0,
            end_ts=t0 + timedelta(hours=6),
            open=100.0,
            high=115.0,
            low=90.0,
            close=102.0,
            volume=1000,
        )
        venue.record_bar(b)
        assert venue.ambiguous_bars_count == 1

    asyncio.run(_run())
