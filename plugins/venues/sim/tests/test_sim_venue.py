"""Unit and conformance tests for SimVenue plugin."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from algodesk.api import (
    Instrument,
    InstrumentId,
    Level,
    OrderSpec,
    OrderState,
    OrderType,
    PluginManifest,
    Product,
    Quote,
    Side,
    Validity,
    venue_problems,
)

from ..plugin import SimVenuePlugin
from ..venue import SimVenue

UTC = UTC
RELIANCE = InstrumentId("NSE:EQ:RELIANCE")
TCS = InstrumentId("NSE:EQ:TCS")


def _make_inst(inst_id: InstrumentId, lot: int = 1, tick: Decimal = Decimal("0.05")) -> Instrument:
    parts = str(inst_id).split(":")
    return Instrument(
        id=inst_id,
        exchange=parts[0],
        segment=parts[1],
        symbol=parts[2],
        kind="EQ",
        lot_size=lot,
        tick_size=tick,
    )


def _make_quote(
    inst: InstrumentId,
    recv_ts: datetime,
    bids: tuple[Level, ...] = (),
    asks: tuple[Level, ...] = (),
    ltp: float = 100.0,
) -> Quote:
    return Quote(
        instrument=inst,
        exch_ts=recv_ts,
        recv_ts=recv_ts,
        bids=bids,
        asks=asks,
        ltp=ltp,
        volume=1000,
    )


def test_sim_venue_conformance() -> None:
    manifest = PluginManifest(
        name="sim",
        kind="venue",
        version="0.1.0",
        api_version=1,
        entry="plugin:SimVenuePlugin",
        order_capable=False,
        capabilities={"market_orders": True},
    )
    plugin = SimVenuePlugin(manifest=manifest, config={}, ctx=None)  # type: ignore[arg-type]
    problems = venue_problems(plugin)
    assert not problems, f"Conformance problems: {problems}"


def test_rejections() -> None:
    async def _run() -> None:
        venue = SimVenue(params={"sim.allow_short": False})
        inst = _make_inst(RELIANCE, lot=10, tick=Decimal("0.05"))
        venue.configure([inst])
        t0 = datetime(2026, 1, 1, 9, 15, tzinfo=UTC)
        venue.on_quote(_make_quote(RELIANCE, t0))

        # 1. Unknown instrument
        spec_unk = OrderSpec(
            instrument=TCS,
            side=Side.BUY,
            qty=10,
            type=OrderType.MARKET,
            product=Product.CNC,
            validity=Validity.DAY,
        )
        ack1 = await venue.place(spec_unk, "ref-1")
        assert not ack1.accepted
        assert "unknown instrument" in ack1.reason

        # 2. Invalid lot size
        spec_lot = OrderSpec(
            instrument=RELIANCE,
            side=Side.BUY,
            qty=15,
            type=OrderType.MARKET,
            product=Product.CNC,
            validity=Validity.DAY,
        )
        ack2 = await venue.place(spec_lot, "ref-2")
        assert not ack2.accepted
        assert "lot size" in ack2.reason

        # 3. Price off tick grid
        spec_tick = OrderSpec(
            instrument=RELIANCE,
            side=Side.BUY,
            qty=10,
            type=OrderType.LIMIT,
            price=Decimal("100.03"),
            product=Product.CNC,
            validity=Validity.DAY,
        )
        ack3 = await venue.place(spec_tick, "ref-3")
        assert not ack3.accepted
        assert "tick grid" in ack3.reason

        # 4. Sell beyond position without shorting
        spec_sell = OrderSpec(
            instrument=RELIANCE,
            side=Side.SELL,
            qty=10,
            type=OrderType.MARKET,
            product=Product.CNC,
            validity=Validity.DAY,
        )
        ack4 = await venue.place(spec_sell, "ref-4")
        assert not ack4.accepted
        assert "sell beyond position" in ack4.reason

    asyncio.run(_run())


def test_market_depth_walking_and_insufficient_depth() -> None:
    async def _run() -> None:
        venue = SimVenue(params={"sim.latency_ms": 100})
        inst = _make_inst(RELIANCE, lot=1, tick=Decimal("0.05"))
        venue.configure([inst])

        t0 = datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC)
        venue.on_quote(_make_quote(RELIANCE, t0, asks=(Level(100.0, 5), Level(101.0, 10))))

        spec = OrderSpec(
            instrument=RELIANCE,
            side=Side.BUY,
            qty=20,
            type=OrderType.MARKET,
            product=Product.CNC,
            validity=Validity.DAY,
        )
        ack = await venue.place(spec, "mkt-1")
        assert ack.accepted

        # Quote before arrival (50ms) does NOT trigger execution
        t1 = t0 + timedelta(milliseconds=50)
        venue.on_quote(_make_quote(RELIANCE, t1, asks=(Level(100.0, 5), Level(101.0, 10))))
        st1 = venue.order_status("mkt-1")
        assert st1 is not None and st1.filled_qty == 0

        # Quote at arrival (100ms) executes against available depth (5 + 10 = 15 total)
        t2 = t0 + timedelta(milliseconds=100)
        venue.on_quote(_make_quote(RELIANCE, t2, asks=(Level(100.0, 5), Level(101.0, 10))))

        st2 = venue.order_status("mkt-1")
        assert st2 is not None
        assert st2.filled_qty == 15
        assert st2.state == OrderState.CANCELLED  # remainder cancelled due to insufficient depth
        assert st2.reason == "insufficient depth"

        events = venue.drain_events()
        kinds = [e.kind for e in events if e.client_ref == "mkt-1"]
        assert kinds == ["ack", "partial", "partial", "cancel"]

    asyncio.run(_run())


def test_limit_order_marketable_and_trade_through() -> None:
    async def _run() -> None:
        venue = SimVenue(params={"sim.latency_ms": 0, "sim.through_ticks": 2})
        inst = _make_inst(RELIANCE, lot=1, tick=Decimal("0.05"))
        venue.configure([inst])

        t0 = datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC)
        venue.on_quote(_make_quote(RELIANCE, t0, asks=(Level(105.0, 10),)))

        # Limit buy at 100.00 with best ask at 105.00 is non-marketable
        spec = OrderSpec(
            instrument=RELIANCE,
            side=Side.BUY,
            qty=10,
            type=OrderType.LIMIT,
            price=Decimal("100.00"),
            product=Product.CNC,
            validity=Validity.DAY,
        )
        ack = await venue.place(spec, "lim-1")
        assert ack.accepted

        # Market ask moves down to 100.00 (trades AT limit, but not through by 2 ticks)
        t1 = t0 + timedelta(seconds=1)
        venue.on_quote(_make_quote(RELIANCE, t1, asks=(Level(100.00, 10),)))
        st1 = venue.order_status("lim-1")
        assert st1 is not None and st1.filled_qty == 0

        # Market ask trades through by 1 tick (99.95) - still needs 2 ticks
        # (through_ticks=2 -> 0.10)
        t2 = t0 + timedelta(seconds=2)
        venue.on_quote(_make_quote(RELIANCE, t2, asks=(Level(99.95, 10),)))
        st2 = venue.order_status("lim-1")
        assert st2 is not None and st2.filled_qty == 0

        # Market ask trades through by 2 ticks (99.90 <= 100.00 - 0.10) -> FILLS at limit price
        t3 = t0 + timedelta(seconds=3)
        venue.on_quote(_make_quote(RELIANCE, t3, asks=(Level(99.90, 10),)))
        st3 = venue.order_status("lim-1")
        assert st3 is not None and st3.filled_qty == 10
        assert st3.state == OrderState.FILLED
        assert st3.avg_price == Decimal("100.00")

    asyncio.run(_run())


def test_ioc_limit_order_unfilled_cancellation() -> None:
    async def _run() -> None:
        venue = SimVenue(params={"sim.latency_ms": 0})
        inst = _make_inst(RELIANCE, lot=1, tick=Decimal("0.05"))
        venue.configure([inst])

        t0 = datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC)
        venue.on_quote(_make_quote(RELIANCE, t0, asks=(Level(100.0, 4), Level(102.0, 10))))

        # IOC limit order at 100.00 for qty=10: fills 4 at 100.00, cancels remaining 6
        spec = OrderSpec(
            instrument=RELIANCE,
            side=Side.BUY,
            qty=10,
            type=OrderType.LIMIT,
            price=Decimal("100.00"),
            product=Product.CNC,
            validity=Validity.IOC,
        )
        await venue.place(spec, "ioc-1")
        t1 = t0 + timedelta(seconds=1)
        venue.on_quote(_make_quote(RELIANCE, t1, asks=(Level(100.0, 4), Level(102.0, 10))))

        st = venue.order_status("ioc-1")
        assert st is not None
        assert st.filled_qty == 4
        assert st.state == OrderState.CANCELLED
        assert st.reason == "ioc_unfilled"

    asyncio.run(_run())


def test_end_of_day_cancels_resting_orders() -> None:
    async def _run() -> None:
        venue = SimVenue(params={"sim.latency_ms": 0})
        inst = _make_inst(RELIANCE, lot=1, tick=Decimal("0.05"))
        venue.configure([inst])

        t0 = datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC)
        venue.on_quote(_make_quote(RELIANCE, t0, asks=(Level(110.0, 10),)))

        spec = OrderSpec(
            instrument=RELIANCE,
            side=Side.BUY,
            qty=10,
            type=OrderType.LIMIT,
            price=Decimal("100.00"),
            product=Product.CNC,
            validity=Validity.DAY,
        )
        await venue.place(spec, "day-1")
        st_before = venue.order_status("day-1")
        assert st_before is not None and st_before.state == OrderState.ACKNOWLEDGED

        venue.end_of_day()
        st_after = venue.order_status("day-1")
        assert st_after is not None and st_after.state == OrderState.CANCELLED
        assert st_after.reason == "end_of_day"

    asyncio.run(_run())


def test_seed_determinism_and_failure_injection() -> None:
    async def _run() -> None:
        inst = _make_inst(RELIANCE, lot=1, tick=Decimal("0.05"))
        t0 = datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC)

        params = {
            "sim.latency_ms": 50,
            "sim.reject_rate": 0.5,
            "sim.seed": 12345,
        }

        # Run 1
        venue1 = SimVenue(params=params)
        venue1.configure([inst])
        venue1.on_quote(_make_quote(RELIANCE, t0))

        results1 = []
        for i in range(10):
            spec = OrderSpec(
                instrument=RELIANCE,
                side=Side.BUY,
                qty=1,
                type=OrderType.MARKET,
                product=Product.CNC,
                validity=Validity.DAY,
            )
            ack = await venue1.place(spec, f"det-{i}")
            results1.append(ack.accepted)

        # Run 2 with identical seed
        venue2 = SimVenue(params=params)
        venue2.configure([inst])
        venue2.on_quote(_make_quote(RELIANCE, t0))

        results2 = []
        for i in range(10):
            spec = OrderSpec(
                instrument=RELIANCE,
                side=Side.BUY,
                qty=1,
                type=OrderType.MARKET,
                product=Product.CNC,
                validity=Validity.DAY,
            )
            ack = await venue2.place(spec, f"det-{i}")
            results2.append(ack.accepted)

        assert results1 == results2
        assert False in results1  # Some rejects were injected
        assert True in results1  # Some accepted

    asyncio.run(_run())
