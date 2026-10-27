"""Tests for builtin risk rules plugin."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from algodesk.api import (
    InstrumentId,
    Level,
    OrderSpec,
    OrderType,
    PlaceOrder,
    Position,
    Product,
    Quote,
    Side,
    Validity,
    parse_manifest,
)

from ..plugin import BuiltinRiskRulesPlugin
from ..rules import (
    CashAvailableRule,
    DailyLossLimitRule,
    MaxDailyTradesRule,
    MaxGrossExposureRule,
    MaxOpenPositionsRule,
    MaxOrdersPerMinuteRule,
    MaxOrderValueRule,
    MaxPositionRule,
    PriceDeviationRule,
)


class FakePortfolio:
    def __init__(self, cash: Decimal = Decimal("100000.00")) -> None:
        self.cash = cash


class DummyView:
    def __init__(
        self,
        params: dict[str, Any] | None = None,
        cash: Decimal = Decimal("100000.00"),
        positions: dict[str, Position] | None = None,
        ltp: float = 100.0,
        daily_pnl: Decimal = Decimal("0.00"),
        orders_last_minute: int = 0,
        daily_trades_count: int = 0,
    ) -> None:
        self.profile = "conservative"
        self.allocated_capital = Decimal("100000.00")
        self.portfolio = FakePortfolio(cash)
        self.positions = positions or {}
        self.daily_pnl = daily_pnl
        self.orders_last_minute = orders_last_minute
        self.daily_trades_count = daily_trades_count
        self.params = params or {
            "risk.conservative.margin_buffer_pct": 25.0,
            "risk.conservative.max_order_value_pct": 25.0,
            "risk.conservative.max_position_pct": 20.0,
            "risk.conservative.max_open_positions": 5,
            "risk.conservative.max_gross_exposure_pct": 100.0,
            "risk.conservative.price_deviation_pct": 2.0,
            "risk.conservative.max_orders_per_minute": 20,
            "risk.conservative.max_daily_trades": 20,
            "risk.conservative.daily_loss_limit_pct": 1.5,
        }
        self._ltp = ltp

    def quote_for(self, instrument: Any) -> Quote:
        now = datetime(2026, 10, 27, 9, 30, tzinfo=UTC)
        return Quote(
            instrument=InstrumentId(str(instrument)),
            exch_ts=now,
            recv_ts=now,
            bids=(Level(price=self._ltp - 0.05, qty=100, orders=1),),
            asks=(Level(price=self._ltp + 0.05, qty=100, orders=1),),
            ltp=self._ltp,
            volume=10000,
        )


def make_intent(
    qty: int = 10,
    side: Side = Side.BUY,
    price: Decimal | None = None,
    order_type: OrderType = OrderType.MARKET,
) -> PlaceOrder:
    return PlaceOrder(
        spec=OrderSpec(
            instrument=InstrumentId("NSE:EQ:INFY"),
            side=side,
            qty=qty,
            type=order_type,
            price=price,
            product=Product.CNC,
            validity=Validity.DAY,
        )
    )


def test_cash_available_rule_pass_and_fail() -> None:
    rule = CashAvailableRule()
    view_ok = DummyView(cash=Decimal("50000.00"))
    # Order cost: 10 * 100 = 1000. Cash after: 49000 >= 25000 (25%) -> Pass
    dec = rule.check(make_intent(qty=10), view_ok)
    assert dec.allow is True

    # Cash tight: 25500. Order cost: 1000. Cash after: 24500 < 25000 -> Fail
    view_tight = DummyView(cash=Decimal("25500.00"))
    dec_fail = rule.check(make_intent(qty=10), view_tight)
    assert dec_fail.allow is False
    assert "below required buffer" in dec_fail.reason


def test_max_order_value_rule_pass_and_fail() -> None:
    rule = MaxOrderValueRule()
    view = DummyView()
    # 25% of 100,000 = 25,000.
    # Order: 200 * 100 = 20,000 <= 25,000 -> Pass
    assert rule.check(make_intent(qty=200), view).allow is True

    # Order: 300 * 100 = 30,000 > 25,000 -> Fail
    dec = rule.check(make_intent(qty=300), view)
    assert dec.allow is False
    assert "exceeds limit" in dec.reason


def test_max_position_rule_pass_and_fail() -> None:
    rule = MaxPositionRule()
    view = DummyView()
    # Max position: 20% of 100,000 = 20,000.
    # Order: 150 * 100 = 15,000 <= 20,000 -> Pass
    assert rule.check(make_intent(qty=150), view).allow is True

    # Order: 250 * 100 = 25,000 > 20,000 -> Fail
    assert rule.check(make_intent(qty=250), view).allow is False


def test_max_open_positions_rule_pass_and_fail() -> None:
    rule = MaxOpenPositionsRule()
    # Max open = 5. Currently 4 open -> adding 5th passes.
    pos_4 = {
        f"NSE:EQ:SYM{i}": Position(
            instrument=InstrumentId(f"NSE:EQ:SYM{i}"),
            product=Product.CNC,
            qty=10,
            avg_price=Decimal("100"),
            realized=Decimal("0"),
            unrealized=Decimal("0"),
        )
        for i in range(4)
    }
    view_4 = DummyView(positions=pos_4)
    assert rule.check(make_intent(), view_4).allow is True

    # Currently 5 open -> adding 6th fails.
    pos_5 = {
        f"NSE:EQ:SYM{i}": Position(
            instrument=InstrumentId(f"NSE:EQ:SYM{i}"),
            product=Product.CNC,
            qty=10,
            avg_price=Decimal("100"),
            realized=Decimal("0"),
            unrealized=Decimal("0"),
        )
        for i in range(5)
    }
    view_5 = DummyView(positions=pos_5)
    assert rule.check(make_intent(), view_5).allow is False


def test_max_gross_exposure_rule_pass_and_fail() -> None:
    rule = MaxGrossExposureRule()
    # Max gross exposure: 100% of 100,000 = 100,000.
    # Currently 80,000 exposure. Order: 150 * 100 = 15,000 -> Total 95,000 <= 100,000 (Pass)
    pos_existing = {
        "NSE:EQ:SYM1": Position(
            instrument=InstrumentId("NSE:EQ:SYM1"),
            product=Product.CNC,
            qty=800,
            avg_price=Decimal("100"),
            realized=Decimal("0"),
            unrealized=Decimal("0"),
        )
    }
    view = DummyView(positions=pos_existing)
    assert rule.check(make_intent(qty=150), view).allow is True

    # Order: 250 * 100 = 25,000 -> Total 105,000 > 100,000 (Fail)
    assert rule.check(make_intent(qty=250), view).allow is False


def test_price_deviation_rule_pass_and_fail() -> None:
    rule = PriceDeviationRule()
    view = DummyView(ltp=100.0)  # max dev = 2.0% (range 98.0 to 102.0)

    # Market order -> exempt
    assert rule.check(make_intent(order_type=OrderType.MARKET), view).allow is True

    # Limit order at 101.5 (1.5% dev <= 2%) -> Pass
    assert (
        rule.check(make_intent(order_type=OrderType.LIMIT, price=Decimal("101.50")), view).allow
        is True
    )

    # Limit order at 103.0 (3.0% dev > 2%) -> Fail
    dec = rule.check(make_intent(order_type=OrderType.LIMIT, price=Decimal("103.00")), view)
    assert dec.allow is False
    assert "Price deviation" in dec.reason or "deviation" in dec.reason.lower()


def test_order_rate_and_daily_trades_pass_and_fail() -> None:
    r_rpm = MaxOrdersPerMinuteRule()
    r_trades = MaxDailyTradesRule()

    # Rate: 19 < 20 (Pass), 20 >= 20 (Fail)
    assert r_rpm.check(make_intent(), DummyView(orders_last_minute=19)).allow is True
    assert r_rpm.check(make_intent(), DummyView(orders_last_minute=20)).allow is False

    # Trades: 19 < 20 (Pass), 20 >= 20 (Fail)
    assert r_trades.check(make_intent(), DummyView(daily_trades_count=19)).allow is True
    assert r_trades.check(make_intent(), DummyView(daily_trades_count=20)).allow is False


def test_daily_loss_limit_pass_and_fail() -> None:
    rule = DailyLossLimitRule()
    # 1.5% of 100,000 = 1,500.
    # Current loss = 1,000 < 1,500 -> Pass
    assert rule.check(make_intent(), DummyView(daily_pnl=Decimal("-1000.00"))).allow is True

    # Current loss = 1,500 >= 1,500 -> Fail
    assert rule.check(make_intent(), DummyView(daily_pnl=Decimal("-1500.00"))).allow is False


def test_builtin_risk_rules_plugin_conformance_and_composite() -> None:
    manifest_data = {
        "name": "builtin",
        "kind": "risk_rule",
        "version": "0.1.0",
        "api_version": 1,
        "entry": "plugin:BuiltinRiskRulesPlugin",
        "modes_allowed": ["research", "backtest", "paper", "live"],
        "order_capable": False,
        "capabilities": {"pre_trade": True},
        "verified_on": "2026-10-27",
        "sources": ["spec"],
    }
    manifest = parse_manifest(manifest_data, "test")
    plugin = BuiltinRiskRulesPlugin(manifest=manifest)

    view = DummyView()
    dec = plugin.check(make_intent(qty=5), view)
    assert dec.allow is True
    assert len(dec.values) > 5
