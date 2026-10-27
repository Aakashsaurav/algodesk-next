"""Tests for Portfolio ledger — position accounting, P&L, marks, and snapshots."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from algodesk.api.market import InstrumentId, Level, Quote
from algodesk.api.trading import Charges, Fill, Product, Side
from algodesk.portfolio.ledger import Portfolio


class FakeJournal:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def append(self, kind: str, payload: dict[str, Any], *, durable: bool = False) -> int:
        self.events.append({"kind": kind, "payload": payload, "durable": durable})
        return len(self.events)


def make_fill(
    client_ref: str,
    qty: int,
    price: str,
    side: Side,
    product: Product = Product.CNC,
    fee: str = "20.00",
    instrument: str = "NSE:EQ:RELIANCE",
    fill_id: str = "f1",
) -> Fill:
    p_dec = Decimal(price)
    f_dec = Decimal(fee)
    return Fill(
        fill_id=fill_id,
        client_ref=client_ref,
        qty=qty,
        price=p_dec,
        ts=datetime(2026, 10, 27, 10, 0, tzinfo=UTC),
        charges=Charges(items={"brokerage": f_dec}, total=f_dec),
        instrument=InstrumentId(instrument),
        side=side,
        product=product,
    )


def make_quote(
    instrument: str = "NSE:EQ:RELIANCE",
    bid: float = 100.0,
    ask: float = 101.0,
    ltp: float = 100.5,
) -> Quote:
    now = datetime(2026, 10, 27, 10, 5, tzinfo=UTC)
    return Quote(
        instrument=InstrumentId(instrument),
        exch_ts=now,
        recv_ts=now,
        bids=(Level(price=bid, qty=100, orders=5),),
        asks=(Level(price=ask, qty=100, orders=5),),
        ltp=ltp,
        volume=1000,
    )


def test_portfolio_initial_state() -> None:
    port = Portfolio(starting_cash=Decimal("100000.00"))
    assert port.cash == Decimal("100000.00")
    assert port.equity == Decimal("100000.00")
    assert port.realized_pnl == Decimal("0.00")
    assert port.unrealized_pnl == Decimal("0.00")
    assert port.day_pnl == Decimal("0.00")
    assert port.margin_used == Decimal("0.00")
    assert len(port.positions) == 0


def test_portfolio_long_open_add_reduce_close() -> None:
    port = Portfolio(starting_cash="50000.00")

    # 1. Buy 10 @ 100, fee 10
    fill1 = make_fill("ref1", 10, "100.00", Side.BUY, fee="10.00", fill_id="f1")
    pos1 = port.apply_fill(fill1)
    assert pos1.qty == 10
    assert pos1.avg_price == Decimal("100.00")
    assert pos1.realized == Decimal("0.00")
    # Cash: 50000 - 1000 - 10 = 48990
    assert port.cash == Decimal("48990.00")
    assert port.total_charges == Decimal("10.00")

    # Invariant: equity == starting_cash + realized + unrealized - charges
    assert port.equity == Decimal("50000.00") + Decimal("0.00") + Decimal("0.00") - Decimal("10.00")

    # 2. Add: Buy 10 @ 110, fee 10
    fill2 = make_fill("ref2", 10, "110.00", Side.BUY, fee="10.00", fill_id="f2")
    pos2 = port.apply_fill(fill2)
    assert pos2.qty == 20
    # Weighted avg price: (10*100 + 10*110)/20 = 105.00
    assert pos2.avg_price == Decimal("105.00")
    # Cash: 48990 - 1100 - 10 = 47880
    assert port.cash == Decimal("47880.00")
    assert port.total_charges == Decimal("20.00")

    # 3. Partial reduce: Sell 5 @ 120, fee 10
    # Profit on 5 shares: 5 * (120 - 105) = +75
    fill3 = make_fill("ref3", 5, "120.00", Side.SELL, fee="10.00", fill_id="f3")
    pos3 = port.apply_fill(fill3)
    assert pos3.qty == 15
    assert pos3.avg_price == Decimal("105.00")  # avg price unchanged on reduction
    assert pos3.realized == Decimal("75.00")
    assert port.realized_pnl == Decimal("75.00")
    # Cash: 47880 + (5 * 120) - 10 = 47880 + 600 - 10 = 48470
    assert port.cash == Decimal("48470.00")
    assert port.total_charges == Decimal("30.00")
    assert port.net_realized_pnl == Decimal("45.00")  # 75 - 30

    # 4. Close remainder: Sell 15 @ 100, fee 10
    # P&L on 15 shares: 15 * (100 - 105) = -75
    fill4 = make_fill("ref4", 15, "100.00", Side.SELL, fee="10.00", fill_id="f4")
    pos4 = port.apply_fill(fill4)
    assert pos4.qty == 0
    assert pos4.avg_price == Decimal("0.00")
    assert pos4.realized == Decimal("0.00")  # 75 + (-75) = 0
    assert port.realized_pnl == Decimal("0.00")
    # Cash: 48470 + 1500 - 10 = 49960
    assert port.cash == Decimal("49960.00")
    assert port.total_charges == Decimal("40.00")
    # Invariant: equity = start(50000) + realized(0) + unrealized(0) - charges(40) = 49960
    assert port.equity == Decimal("49960.00")


def test_portfolio_short_open_add_reduce_close() -> None:
    port = Portfolio(starting_cash="50000.00")

    # 1. Sell Short 10 @ 200, fee 15
    fill1 = make_fill(
        "ref1", 10, "200.00", Side.SELL, product=Product.MIS, fee="15.00", fill_id="f1"
    )
    pos1 = port.apply_fill(fill1)
    assert pos1.qty == -10
    assert pos1.avg_price == Decimal("200.00")
    # Cash: 50000 + 2000 - 15 = 51985
    assert port.cash == Decimal("51985.00")
    assert port.total_charges == Decimal("15.00")

    # 2. Add short: Sell 10 @ 220, fee 15
    fill2 = make_fill(
        "ref2", 10, "220.00", Side.SELL, product=Product.MIS, fee="15.00", fill_id="f2"
    )
    pos2 = port.apply_fill(fill2)
    assert pos2.qty == -20
    assert pos2.avg_price == Decimal("210.00")

    # 3. Reduce short: Buy 5 @ 180, fee 15
    # Short profit on 5 shares: 5 * (210 - 180) = +150
    fill3 = make_fill("ref3", 5, "180.00", Side.BUY, product=Product.MIS, fee="15.00", fill_id="f3")
    pos3 = port.apply_fill(fill3)
    assert pos3.qty == -15
    assert pos3.avg_price == Decimal("210.00")
    assert pos3.realized == Decimal("150.00")

    # 4. Close short: Buy 15 @ 230, fee 15
    # Short loss on 15 shares: 15 * (210 - 230) = -300
    fill4 = make_fill(
        "ref4", 15, "230.00", Side.BUY, product=Product.MIS, fee="15.00", fill_id="f4"
    )
    pos4 = port.apply_fill(fill4)
    assert pos4.qty == 0
    # Net realized: 150 - 300 = -150
    assert pos4.realized == Decimal("-150.00")
    assert port.realized_pnl == Decimal("-150.00")
    assert port.total_charges == Decimal("60.00")
    assert port.net_realized_pnl == Decimal("-210.00")


def test_portfolio_flips_long_to_short_and_short_to_long() -> None:
    port = Portfolio(starting_cash="50000.00")

    # Long 10 @ 100
    port.apply_fill(make_fill("r1", 10, "100.00", Side.BUY, fee="0.00", fill_id="f1"))

    # Flip Long to Short: Sell 15 @ 120
    # Closes 10 long @ 120 -> profit = 10 * (120 - 100) = +200
    # Opens 5 short @ 120
    pos_flip1 = port.apply_fill(make_fill("r2", 15, "120.00", Side.SELL, fee="0.00", fill_id="f2"))
    assert pos_flip1.qty == -5
    assert pos_flip1.avg_price == Decimal("120.00")
    assert pos_flip1.realized == Decimal("200.00")

    # Flip Short to Long: Buy 15 @ 110
    # Closes 5 short @ 110 -> short profit = 5 * (120 - 110) = +50
    # Opens 10 long @ 110
    pos_flip2 = port.apply_fill(make_fill("r3", 15, "110.00", Side.BUY, fee="0.00", fill_id="f3"))
    assert pos_flip2.qty == 10
    assert pos_flip2.avg_price == Decimal("110.00")
    assert pos_flip2.realized == Decimal("250.00")


def test_portfolio_mark_to_market() -> None:
    port = Portfolio(starting_cash="50000.00")

    # Long 10 @ 100
    port.apply_fill(make_fill("r1", 10, "100.00", Side.BUY, fee="0.00", fill_id="f1"))

    # Mark: Long marks at bid (bid=105, ask=106, ltp=105.5)
    q1 = make_quote(bid=105.0, ask=106.0, ltp=105.5)
    port.mark(q1)

    key = (InstrumentId("NSE:EQ:RELIANCE"), Product.CNC)
    pos = port.positions[key]
    assert pos.unrealized == Decimal("50.00")  # 10 * (105 - 100)
    assert port.unrealized_pnl == Decimal("50.00")
    assert port.equity == Decimal("50050.00")
    assert port.day_pnl == Decimal("50.00")

    # Flip to Short 10 @ 110
    port.apply_fill(make_fill("r2", 20, "110.00", Side.SELL, fee="0.00", fill_id="f2"))
    # Realized on long: 10 * (110 - 100) = 100
    # Short pos: -10 @ 110

    # Mark: Short marks at ask (bid=103, ask=104)
    q2 = make_quote(bid=103.0, ask=104.0, ltp=103.5)
    port.mark(q2)

    pos_short = port.positions[key]
    # Short unrealized: 10 * (110 - 104) = +60
    assert pos_short.unrealized == Decimal("60.00")
    assert port.unrealized_pnl == Decimal("60.00")
    # Equity: 50000 (start) + 100 (realized) + 60 (unrealized) - 0 (charges) = 50160
    assert port.equity == Decimal("50160.00")


def test_portfolio_snapshot_and_timer() -> None:
    journal = FakeJournal()
    port = Portfolio(starting_cash="25000.00", journal=journal, snapshot_seconds=60)

    port.apply_fill(make_fill("r1", 5, "500.00", Side.BUY, fee="20.00", fill_id="f1"))
    snap = port.snapshot()

    assert snap.cash == port.cash
    assert snap.equity == port.equity
    assert len(snap.positions) == 1

    # Trigger snapshot timer directly
    port._on_snapshot_timer()
    assert len(journal.events) == 1
    assert journal.events[0]["kind"] == "portfolio.snapshot"
    payload = journal.events[0]["payload"]
    assert "cash" in payload
    assert "equity" in payload
    assert "positions" in payload
