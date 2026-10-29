"""Safety property tests: Ledger conservation and cash protection invariants.

Invariants:
1. Conservation: equity change = realized + unrealized - total_charges
2. Safety: cash never negative when risk cash rule is active
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from plugins.risk_rules.builtin.rules import CashAvailableRule

from algodesk.api import (
    Charges,
    Fill,
    InstrumentId,
    OrderSpec,
    OrderType,
    PlaceOrder,
    Product,
    Quote,
    Side,
)
from algodesk.engine.clock import SimClock
from algodesk.portfolio.ledger import PortfolioLedger


def _make_dummy_quote(instrument: str, ltp: float, ts: datetime) -> Quote:
    return Quote(
        instrument=InstrumentId(instrument),
        exch_ts=ts,
        recv_ts=ts,
        bids=(),
        asks=(),
        ltp=ltp,
        volume=1000,
    )


def test_ledger_conservation_exact() -> None:
    """Verify equity change = realized + unrealized - charges over deterministic steps."""
    now = datetime(2026, 10, 26, 9, 15, tzinfo=UTC)
    clock = SimClock(initial_time=now)
    starting_cash = Decimal("100000.00")
    ledger = PortfolioLedger(clock=clock, starting_cash=starting_cash)

    # Initial conservation check
    snap0 = ledger.snapshot()
    assert snap0.equity == starting_cash
    assert ledger.realized == Decimal("0.00")
    assert ledger.unrealized == Decimal("0.00")
    assert ledger.total_charges == Decimal("0.00")

    # Step 1: Buy 100 RELIANCE at 2500.00 with 25.00 charges
    f1 = Fill(
        fill_id="f1",
        client_ref="o1",
        qty=100,
        price=Decimal("2500.00"),
        ts=now,
        charges=Charges(items={"brokerage": Decimal("25.00")}),
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        side=Side.BUY,
        product=Product.CNC,
    )
    ledger.apply_fill(f1)
    # Mark to market at 2550.00
    ledger.mark(_make_dummy_quote("NSE:EQ:RELIANCE", 2550.0, now))
    snap1 = ledger.snapshot()
    equity_change1 = snap1.equity - starting_cash
    expected1 = ledger.realized + ledger.unrealized - ledger.total_charges
    assert equity_change1 == expected1
    assert ledger.unrealized == Decimal("5000.00")
    assert ledger.total_charges == Decimal("25.00")

    # Step 2: Sell 50 RELIANCE at 2600.00 with 30.00 charges (realizes profit)
    f2 = Fill(
        fill_id="f2",
        client_ref="o2",
        qty=50,
        price=Decimal("2600.00"),
        ts=now,
        charges=Charges(items={"brokerage": Decimal("30.00")}),
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        side=Side.SELL,
        product=Product.CNC,
    )
    ledger.apply_fill(f2)
    # Remaining 50 shares marked at 2600.00
    ledger.mark(_make_dummy_quote("NSE:EQ:RELIANCE", 2600.0, now))
    snap2 = ledger.snapshot()
    equity_change2 = snap2.equity - starting_cash
    expected2 = ledger.realized + ledger.unrealized - ledger.total_charges
    assert equity_change2 == expected2
    assert ledger.realized == Decimal("5000.00")  # 50 * (2600 - 2500)
    assert ledger.unrealized == Decimal("5000.00")  # 50 * (2600 - 2500)
    assert ledger.total_charges == Decimal("55.00")


@given(
    buy_qty=st.integers(min_value=1, max_value=500),
    buy_price=st.decimals(min_value=Decimal("10.00"), max_value=Decimal("5000.00"), places=2),
    buy_fee=st.decimals(min_value=Decimal("0.00"), max_value=Decimal("50.00"), places=2),
    sell_qty=st.integers(min_value=1, max_value=500),
    sell_price=st.decimals(min_value=Decimal("10.00"), max_value=Decimal("5000.00"), places=2),
    sell_fee=st.decimals(min_value=Decimal("0.00"), max_value=Decimal("50.00"), places=2),
    final_mark=st.decimals(min_value=Decimal("10.00"), max_value=Decimal("5000.00"), places=2),
)
@settings(max_examples=40, deadline=None)
def test_hypothesis_ledger_conservation(
    buy_qty: int,
    buy_price: Decimal,
    buy_fee: Decimal,
    sell_qty: int,
    sell_price: Decimal,
    sell_fee: Decimal,
    final_mark: Decimal,
) -> None:
    """Hypothesis property: equity change = realized + unrealized - charges across random trades."""
    actual_sell_qty = min(buy_qty, sell_qty)  # Prevent illegal shorting in CNC
    now = datetime(2026, 10, 26, 9, 15, tzinfo=UTC)
    clock = SimClock(initial_time=now)
    starting_cash = Decimal("10000000.00")
    ledger = PortfolioLedger(clock=clock, starting_cash=starting_cash)

    f_buy = Fill(
        fill_id="f_buy",
        client_ref="o_buy",
        qty=buy_qty,
        price=buy_price,
        ts=now,
        charges=Charges(items={"fee": buy_fee}),
        instrument=InstrumentId("NSE:EQ:TCS"),
        side=Side.BUY,
        product=Product.CNC,
    )
    ledger.apply_fill(f_buy)

    if actual_sell_qty > 0:
        f_sell = Fill(
            fill_id="f_sell",
            client_ref="o_sell",
            qty=actual_sell_qty,
            price=sell_price,
            ts=now,
            charges=Charges(items={"fee": sell_fee}),
            instrument=InstrumentId("NSE:EQ:TCS"),
            side=Side.SELL,
            product=Product.CNC,
        )
        ledger.apply_fill(f_sell)

    ledger.mark(_make_dummy_quote("NSE:EQ:TCS", float(final_mark), now))
    snap = ledger.snapshot()
    equity_change = snap.equity - starting_cash
    net_pnl = ledger.realized + ledger.unrealized - ledger.total_charges
    assert equity_change == net_pnl, (
        f"Conservation failed: equity_change={equity_change}, net_pnl={net_pnl}"
    )


class DummyRiskView:
    def __init__(
        self,
        cash: Decimal,
        capital: Decimal,
        margin_buffer_pct: float,
        ltp: float = 100.0,
    ):
        self.profile = "conservative"
        self.allocated_capital = capital
        self.portfolio = type("P", (), {"cash": cash})()
        self.params = {"risk.conservative.margin_buffer_pct": margin_buffer_pct}
        self.ltp = ltp

    def quote_for(self, inst: InstrumentId) -> Quote:
        return _make_dummy_quote(str(inst), self.ltp, datetime.now(UTC))


@pytest.mark.parametrize(
    ("cash", "capital", "buffer_pct", "qty", "price", "expected_allow"),
    [
        (Decimal("100000.00"), Decimal("100000.00"), 5.0, 10, Decimal("1000.00"), True),
        (Decimal("100000.00"), Decimal("100000.00"), 5.0, 95, Decimal("1000.00"), True),
        (Decimal("100000.00"), Decimal("100000.00"), 5.0, 96, Decimal("1000.00"), False),
        (Decimal("5000.00"), Decimal("100000.00"), 5.0, 1, Decimal("100.00"), False),
        (Decimal("1000.00"), Decimal("100000.00"), 5.0, 1, Decimal("500.00"), False),
    ],
)
def test_cash_rule_prevents_negative_cash(
    cash: Decimal,
    capital: Decimal,
    buffer_pct: float,
    qty: int,
    price: Decimal,
    expected_allow: bool,
) -> None:
    """Table-driven test: CashAvailableRule enforces margin buffer, keeping cash positive."""
    rule = CashAvailableRule()
    view = DummyRiskView(cash=cash, capital=capital, margin_buffer_pct=buffer_pct, ltp=float(price))
    spec = OrderSpec(
        instrument=InstrumentId("NSE:EQ:INFY"),
        side=Side.BUY,
        qty=qty,
        type=OrderType.LIMIT,
        price=price,
        product=Product.CNC,
    )
    intent = PlaceOrder(intent_id="intent_1", spec=spec)
    decision = rule.check(intent, view)
    assert decision.allow is expected_allow
    if not decision.allow:
        assert decision.rule == "cash_available"
        assert "falls below required buffer" in decision.reason


def test_cash_rule_missing_parameter_fails_closed() -> None:
    """Cash rule fails closed if margin_buffer_pct is missing from parameters."""
    rule = CashAvailableRule()
    view = DummyRiskView(
        cash=Decimal("100000.00"), capital=Decimal("100000.00"), margin_buffer_pct=5.0
    )
    view.params = {}  # Strip parameter
    spec = OrderSpec(
        instrument=InstrumentId("NSE:EQ:INFY"),
        side=Side.BUY,
        qty=10,
        type=OrderType.LIMIT,
        price=Decimal("100.00"),
        product=Product.CNC,
    )
    intent = PlaceOrder(intent_id="intent_missing_p", spec=spec)
    decision = rule.check(intent, view)
    assert decision.allow is False
    assert "Missing margin_buffer_pct param" in decision.reason


def test_cash_rule_allows_sell_orders_without_cash_consumption() -> None:
    """Sell orders reduce position exposure and do not consume cash; allowed by cash rule."""
    rule = CashAvailableRule()
    view = DummyRiskView(
        cash=Decimal("10000.00"), capital=Decimal("100000.00"), margin_buffer_pct=5.0
    )
    spec = OrderSpec(
        instrument=InstrumentId("NSE:EQ:INFY"),
        side=Side.SELL,
        qty=10,
        type=OrderType.LIMIT,
        price=Decimal("100.00"),
        product=Product.CNC,
    )
    intent = PlaceOrder(intent_id="intent_sell", spec=spec)
    decision = rule.check(intent, view)
    assert decision.allow is True
