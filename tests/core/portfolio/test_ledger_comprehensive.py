"""Comprehensive tests for PortfolioLedger arithmetic, flips, mark-to-market, and fee deductions."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from algodesk.api import (
    Charges,
    Fill,
    InstrumentId,
    Level,
    Portfolio,
    Product,
    Quote,
    Side,
)
from algodesk.engine.clock import SimClock
from algodesk.portfolio.ledger import PortfolioLedger

RELIANCE = InstrumentId("NSE:EQ:RELIANCE")
TCS = InstrumentId("NSE:EQ:TCS")


def _fill(
    fill_id: str,
    inst: InstrumentId,
    side: Side,
    qty: int,
    price: Decimal,
    fee: Decimal = Decimal("0.00"),
    product: Product = Product.CNC,
) -> Fill:
    t0 = datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC)
    charges = Charges(items={"brokerage": fee}, total=fee)
    return Fill(
        fill_id=fill_id,
        client_ref="c1",
        qty=qty,
        price=price,
        ts=t0,
        charges=charges,
        instrument=inst,
        side=side,
        product=product,
    )


def _quote(inst: InstrumentId, bid: float, ask: float) -> Quote:
    t0 = datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC)
    return Quote(
        instrument=inst,
        exch_ts=t0,
        recv_ts=t0,
        bids=(Level(bid, 100),),
        asks=(Level(ask, 100),),
        ltp=bid,
        volume=1000,
    )


def test_long_accumulation_and_average_price() -> None:
    """Buy 10 @ 100, then buy 10 @ 120 -> 20 @ 110 avg price."""
    clock = SimClock(initial_time=datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC))
    ledger = PortfolioLedger(clock=clock, starting_cash=Decimal("100000.00"))

    ledger.apply_fill(_fill("f1", RELIANCE, Side.BUY, 10, Decimal("100.00"), Decimal("10.00")))
    pos = ledger.positions[(RELIANCE, Product.CNC)]
    assert pos.qty == 10
    assert pos.avg_price == Decimal("100.00")
    assert ledger.cash == Decimal("100000.00") - Decimal("1000.00") - Decimal("10.00")

    ledger.apply_fill(_fill("f2", RELIANCE, Side.BUY, 10, Decimal("120.00"), Decimal("10.00")))
    pos2 = ledger.positions[(RELIANCE, Product.CNC)]
    assert pos2.qty == 20
    assert pos2.avg_price == Decimal("110.00")
    assert ledger.cash == Decimal("100000.00") - Decimal("2200.00") - Decimal("20.00")


def test_long_partial_sale_profit_and_loss() -> None:
    """Buy 20 @ 100, sell 10 @ 130 -> realized profit 300, remaining 10 @ 100."""
    clock = SimClock(initial_time=datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC))
    ledger = PortfolioLedger(clock=clock, starting_cash=Decimal("100000.00"))

    ledger.apply_fill(_fill("f1", RELIANCE, Side.BUY, 20, Decimal("100.00"), Decimal("15.00")))
    ledger.apply_fill(_fill("f2", RELIANCE, Side.SELL, 10, Decimal("130.00"), Decimal("15.00")))

    pos = ledger.positions[(RELIANCE, Product.CNC)]
    assert pos.qty == 10
    assert pos.avg_price == Decimal("100.00")
    assert ledger.realized == Decimal("300.00")
    assert ledger.total_charges == Decimal("30.00")
    assert ledger.net_realized_pnl == Decimal("270.00")


def test_long_to_short_position_flip() -> None:
    """Long 10 @ 100, sell 25 @ 120 -> close 10 long (realized +200), open 15 short @ 120."""
    clock = SimClock(initial_time=datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC))
    ledger = PortfolioLedger(clock=clock, starting_cash=Decimal("100000.00"))

    ledger.apply_fill(_fill("f1", RELIANCE, Side.BUY, 10, Decimal("100.00")))
    ledger.apply_fill(_fill("f2", RELIANCE, Side.SELL, 25, Decimal("120.00")))

    pos = ledger.positions[(RELIANCE, Product.CNC)]
    assert pos.qty == -15
    assert pos.avg_price == Decimal("120.00")
    assert ledger.realized == Decimal("200.00")


def test_short_accumulation_and_average_price() -> None:
    """Short sell 10 @ 150, short sell 10 @ 130 -> short 20 @ 140."""
    clock = SimClock(initial_time=datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC))
    ledger = PortfolioLedger(clock=clock, starting_cash=Decimal("100000.00"))

    ledger.apply_fill(_fill("f1", RELIANCE, Side.SELL, 10, Decimal("150.00")))
    ledger.apply_fill(_fill("f2", RELIANCE, Side.SELL, 10, Decimal("130.00")))

    pos = ledger.positions[(RELIANCE, Product.CNC)]
    assert pos.qty == -20
    assert pos.avg_price == Decimal("140.00")


def test_short_cover_profit_and_loss() -> None:
    """Short 20 @ 150, buy cover 10 @ 120 -> realized profit (150 - 120) * 10 = 300."""
    clock = SimClock(initial_time=datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC))
    ledger = PortfolioLedger(clock=clock, starting_cash=Decimal("100000.00"))

    ledger.apply_fill(_fill("f1", RELIANCE, Side.SELL, 20, Decimal("150.00")))
    ledger.apply_fill(_fill("f2", RELIANCE, Side.BUY, 10, Decimal("120.00")))

    pos = ledger.positions[(RELIANCE, Product.CNC)]
    assert pos.qty == -10
    assert pos.avg_price == Decimal("150.00")
    assert ledger.realized == Decimal("300.00")


def test_short_to_long_position_flip() -> None:
    """Short 10 @ 100, buy 30 @ 80 -> close 10 short (realized +200), open 20 long @ 80."""
    clock = SimClock(initial_time=datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC))
    ledger = PortfolioLedger(clock=clock, starting_cash=Decimal("100000.00"))

    ledger.apply_fill(_fill("f1", RELIANCE, Side.SELL, 10, Decimal("100.00")))
    ledger.apply_fill(_fill("f2", RELIANCE, Side.BUY, 30, Decimal("80.00")))

    pos = ledger.positions[(RELIANCE, Product.CNC)]
    assert pos.qty == 20
    assert pos.avg_price == Decimal("80.00")
    assert ledger.realized == Decimal("200.00")


def test_mark_to_market_long_at_bid_short_at_ask() -> None:
    """Longs valued at best bid, shorts valued at best ask."""
    clock = SimClock(initial_time=datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC))
    ledger = PortfolioLedger(clock=clock, starting_cash=Decimal("100000.00"))

    # Long 10 RELIANCE @ 100.00
    ledger.apply_fill(_fill("f1", RELIANCE, Side.BUY, 10, Decimal("100.00")))
    # Short 10 TCS @ 200.00
    ledger.apply_fill(_fill("f2", TCS, Side.SELL, 10, Decimal("200.00")))

    # Quote with spread: RELIANCE bid 105 (ask 106), TCS ask 190 (bid 188)
    q_rel = _quote(RELIANCE, bid=105.0, ask=106.0)
    q_tcs = _quote(TCS, bid=188.0, ask=190.0)

    ledger.mark(q_rel)
    ledger.mark(q_tcs)

    # RELIANCE unrealized: 10 * (105 - 100) = +50
    # TCS unrealized: -10 * (190 - 200) = +100
    pos_rel = ledger.positions[(RELIANCE, Product.CNC)]
    pos_tcs = ledger.positions[(TCS, Product.CNC)]
    assert pos_rel.unrealized == Decimal("50.00")
    assert pos_tcs.unrealized == Decimal("100.00")
    assert ledger.unrealized == Decimal("150.00")


@pytest.mark.parametrize(
    "buy_price,sell_price,qty,expected_realized",
    [
        (Decimal("100.00"), Decimal("110.00"), 10, Decimal("100.00")),
        (Decimal("100.00"), Decimal("90.00"), 10, Decimal("-100.00")),
        (Decimal("250.50"), Decimal("250.50"), 50, Decimal("0.00")),
        (Decimal("50.25"), Decimal("60.75"), 20, Decimal("210.00")),
    ],
)
def test_realized_pnl_various_prices(
    buy_price: Decimal, sell_price: Decimal, qty: int, expected_realized: Decimal
) -> None:
    """Verify exact realized P&L calculations across different price scenarios."""
    clock = SimClock(initial_time=datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC))
    ledger = PortfolioLedger(clock=clock, starting_cash=Decimal("100000.00"))

    ledger.apply_fill(_fill("f1", RELIANCE, Side.BUY, qty, buy_price))
    ledger.apply_fill(_fill("f2", RELIANCE, Side.SELL, qty, sell_price))

    assert ledger.realized == expected_realized


def test_portfolio_snapshot_dataclass() -> None:
    """snapshot() returns API Portfolio dataclass."""
    clock = SimClock(initial_time=datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC))
    ledger = PortfolioLedger(clock=clock, starting_cash=Decimal("100000.00"))
    ledger.apply_fill(_fill("f1", RELIANCE, Side.BUY, 10, Decimal("100.00")))
    ledger.mark(_quote(RELIANCE, bid=110.0, ask=111.0))

    snap = ledger.snapshot()
    assert isinstance(snap, Portfolio)
    assert snap.cash == Decimal("99000.00")
    assert snap.equity == Decimal("100100.00")
    assert (
        str(RELIANCE) in snap.positions
        or (str(RELIANCE), "CNC") in snap.positions
        or len(snap.positions) == 1
    )


@pytest.mark.parametrize(
    "short_price,cover_price,qty,expected_realized",
    [
        (Decimal("100.00"), Decimal("90.00"), 10, Decimal("100.00")),
        (Decimal("100.00"), Decimal("110.00"), 10, Decimal("-100.00")),
        (Decimal("250.50"), Decimal("250.50"), 50, Decimal("0.00")),
        (Decimal("500.00"), Decimal("450.00"), 20, Decimal("1000.00")),
        (Decimal("1000.00"), Decimal("1020.00"), 15, Decimal("-300.00")),
        (Decimal("75.25"), Decimal("70.25"), 100, Decimal("500.00")),
        (Decimal("80.00"), Decimal("85.50"), 40, Decimal("-220.00")),
        (Decimal("123.45"), Decimal("120.00"), 10, Decimal("34.50")),
    ],
)
def test_short_realized_pnl_various_prices(
    short_price: Decimal, cover_price: Decimal, qty: int, expected_realized: Decimal
) -> None:
    """Verify exact short realized P&L calculations across different prices."""
    clock = SimClock(initial_time=datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC))
    ledger = PortfolioLedger(clock=clock, starting_cash=Decimal("100000.00"))

    ledger.apply_fill(_fill("f1", RELIANCE, Side.SELL, qty, short_price))
    ledger.apply_fill(_fill("f2", RELIANCE, Side.BUY, qty, cover_price))

    assert ledger.realized == expected_realized


@pytest.mark.parametrize(
    "side,pos_qty,entry_price,bid,ask,expected_unrealized",
    [
        (Side.BUY, 10, Decimal("100.00"), 105.0, 106.0, Decimal("50.00")),
        (Side.BUY, 10, Decimal("100.00"), 95.0, 96.0, Decimal("-50.00")),
        (Side.BUY, 50, Decimal("200.00"), 200.0, 202.0, Decimal("0.00")),
        (Side.SELL, 10, Decimal("100.00"), 94.0, 95.0, Decimal("50.00")),
        (Side.SELL, 10, Decimal("100.00"), 104.0, 105.0, Decimal("-50.00")),
        (Side.SELL, 50, Decimal("200.00"), 198.0, 200.0, Decimal("0.00")),
        (Side.BUY, 25, Decimal("400.00"), 410.5, 412.0, Decimal("262.50")),
        (Side.SELL, 25, Decimal("400.00"), 385.0, 388.5, Decimal("287.50")),
    ],
)
def test_unrealized_pnl_spread_marking(
    side: Side,
    pos_qty: int,
    entry_price: Decimal,
    bid: float,
    ask: float,
    expected_unrealized: Decimal,
) -> None:
    """Verify mark values longs at bid and shorts at ask across multiple spreads."""
    clock = SimClock(initial_time=datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC))
    ledger = PortfolioLedger(clock=clock, starting_cash=Decimal("100000.00"))

    ledger.apply_fill(_fill("f1", RELIANCE, side, pos_qty, entry_price))
    ledger.mark(_quote(RELIANCE, bid=bid, ask=ask))

    assert ledger.unrealized == expected_unrealized


@pytest.mark.parametrize(
    "fee_amount,qty,price",
    [
        (Decimal("10.00"), 10, Decimal("100.00")),
        (Decimal("25.50"), 50, Decimal("200.00")),
        (Decimal("50.25"), 100, Decimal("50.00")),
        (Decimal("0.00"), 10, Decimal("100.00")),
        (Decimal("100.00"), 20, Decimal("500.00")),
        (Decimal("15.75"), 30, Decimal("75.00")),
    ],
)
def test_fee_deductions_from_cash_and_net_pnl(
    fee_amount: Decimal, qty: int, price: Decimal
) -> None:
    """Verify fee deductions properly reduce cash and affect net realized P&L."""
    clock = SimClock(initial_time=datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC))
    start_cash = Decimal("100000.00")
    ledger = PortfolioLedger(clock=clock, starting_cash=start_cash)

    ledger.apply_fill(_fill("f1", RELIANCE, Side.BUY, qty, price, fee=fee_amount))
    cost = (Decimal(qty) * price) + fee_amount
    assert ledger.cash == start_cash - cost
    assert ledger.total_charges == fee_amount

    # Close trade at same price
    ledger.apply_fill(_fill("f2", RELIANCE, Side.SELL, qty, price, fee=fee_amount))
    revenue = (Decimal(qty) * price) - fee_amount
    assert ledger.cash == (start_cash - cost) + revenue
    assert ledger.total_charges == fee_amount * Decimal("2")
    assert ledger.realized == Decimal("0.00")
    assert ledger.net_realized_pnl == -ledger.total_charges
