"""Comprehensive tests for builtin risk rules, boundary conditions, and fail-closed safety."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from plugins.risk_rules.builtin.rules import (
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
)
from algodesk.engine.clock import SimClock
from algodesk.risk.view import RiskViewContext

RELIANCE = InstrumentId("NSE:EQ:RELIANCE")


class StubParams:
    def __init__(self, d: dict[str, Any] | None = None) -> None:
        self._d = d or {}

    def get(self, k: str, default: Any = None) -> Any:
        return self._d.get(k, default)


def _quote(price: float = 100.0, recv_ts: datetime | None = None) -> Quote:
    ts = recv_ts or datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC)
    return Quote(
        instrument=RELIANCE,
        exch_ts=ts,
        recv_ts=ts,
        bids=(Level(price, 100),),
        asks=(Level(price, 100),),
        ltp=price,
        volume=1000,
    )


def _spec(
    qty: int = 10,
    price: Decimal | None = None,
    side: Side = Side.BUY,
    order_type: OrderType = OrderType.MARKET,
) -> OrderSpec:
    return OrderSpec(
        instrument=RELIANCE,
        side=side,
        qty=qty,
        type=order_type,
        price=price,
        product=Product.CNC,
        validity=Validity.DAY,
    )


# 1. CashAvailableRule
@pytest.mark.parametrize(
    "cash,capital,buffer_pct,order_qty,price,expected_allow",
    [
        (Decimal("50000.00"), Decimal("100000.00"), 10.0, 10, 100.0, True),
        (Decimal("10500.00"), Decimal("100000.00"), 10.0, 10, 100.0, False),
        (Decimal("10100.00"), Decimal("100000.00"), 10.0, 1, 100.0, True),
    ],
)
def test_cash_available_rule(
    cash: Decimal,
    capital: Decimal,
    buffer_pct: float,
    order_qty: int,
    price: float,
    expected_allow: bool,
) -> None:
    clock = SimClock(initial_time=datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC))
    params = StubParams({"risk.conservative.margin_buffer_pct": buffer_pct})
    view = RiskViewContext(
        allocated_capital=capital,
        latest_quotes={RELIANCE: _quote(price)},
        clock=clock,
        params=params,
    )

    class DummyPortfolio:
        pass

    p = DummyPortfolio()
    p.cash = cash  # type: ignore[attr-defined]
    object.__setattr__(view, "portfolio", p)

    res = CashAvailableRule().check(PlaceOrder(_spec(qty=order_qty)), view)
    assert res.allow == expected_allow


# 2. MaxOrderValueRule
@pytest.mark.parametrize(
    "capital,max_order_pct,qty,price,expected_allow",
    [
        (Decimal("100000.00"), 20.0, 10, 100.0, True),
        (Decimal("100000.00"), 5.0, 100, 100.0, False),
    ],
)
def test_max_order_value_rule(
    capital: Decimal, max_order_pct: float, qty: int, price: float, expected_allow: bool
) -> None:
    clock = SimClock(initial_time=datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC))
    params = StubParams({"risk.conservative.max_order_value_pct": max_order_pct})
    view = RiskViewContext(
        allocated_capital=capital,
        latest_quotes={RELIANCE: _quote(price)},
        clock=clock,
        params=params,
    )
    res = MaxOrderValueRule().check(PlaceOrder(_spec(qty=qty)), view)
    assert res.allow == expected_allow


# 3. MaxPositionRule
@pytest.mark.parametrize(
    "capital,max_pos_pct,existing_qty,new_qty,price,expected_allow",
    [
        (Decimal("100000.00"), 25.0, 100, 50, 100.0, True),
        (Decimal("100000.00"), 25.0, 200, 100, 100.0, False),
    ],
)
def test_max_position_pct_rule(
    capital: Decimal,
    max_pos_pct: float,
    existing_qty: int,
    new_qty: int,
    price: float,
    expected_allow: bool,
) -> None:
    clock = SimClock(initial_time=datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC))
    params = StubParams({"risk.conservative.max_position_pct": max_pos_pct})
    pos = Position(
        instrument=RELIANCE,
        product=Product.CNC,
        qty=existing_qty,
        avg_price=Decimal(str(price)),
    )
    view = RiskViewContext(
        allocated_capital=capital,
        positions={f"{RELIANCE}:CNC": pos},
        latest_quotes={RELIANCE: _quote(price)},
        clock=clock,
        params=params,
    )
    res = MaxPositionRule().check(PlaceOrder(_spec(qty=new_qty)), view)
    assert res.allow == expected_allow


# 4. MaxOpenPositionsRule
@pytest.mark.parametrize(
    "max_positions,num_open,is_new_sym,expected_allow",
    [
        (5, 3, True, True),
        (5, 5, True, False),
        (5, 5, False, True),
    ],
)
def test_max_open_positions_rule(
    max_positions: int, num_open: int, is_new_sym: bool, expected_allow: bool
) -> None:
    clock = SimClock(initial_time=datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC))
    params = StubParams({"risk.conservative.max_open_positions": max_positions})
    positions = {
        f"NSE:EQ:SYM{i}:CNC": Position(
            instrument=InstrumentId(f"NSE:EQ:SYM{i}"),
            product=Product.CNC,
            qty=10,
            avg_price=Decimal("100.00"),
        )
        for i in range(num_open)
    }
    sym = RELIANCE if is_new_sym else InstrumentId("NSE:EQ:SYM0")
    spec = OrderSpec(instrument=sym, side=Side.BUY, qty=10, type=OrderType.MARKET)
    view = RiskViewContext(positions=positions, clock=clock, params=params)
    res = MaxOpenPositionsRule().check(PlaceOrder(spec), view)
    assert res.allow == expected_allow


# 5. MaxGrossExposureRule
@pytest.mark.parametrize(
    "capital,max_gross_pct,existing_exposure,order_val,expected_allow",
    [
        (Decimal("100000.00"), 100.0, Decimal("80000.00"), Decimal("10000.00"), True),
        (Decimal("100000.00"), 100.0, Decimal("95000.00"), Decimal("10000.00"), False),
    ],
)
def test_max_gross_exposure_rule(
    capital: Decimal,
    max_gross_pct: float,
    existing_exposure: Decimal,
    order_val: Decimal,
    expected_allow: bool,
) -> None:
    clock = SimClock(initial_time=datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC))
    params = StubParams({"risk.conservative.max_gross_exposure_pct": max_gross_pct})
    pos = Position(
        instrument=RELIANCE,
        product=Product.CNC,
        qty=int(existing_exposure / Decimal("100.00")),
        avg_price=Decimal("100.00"),
    )
    qty = int(order_val / Decimal("100.00"))
    view = RiskViewContext(
        allocated_capital=capital,
        positions={f"{RELIANCE}:CNC": pos},
        latest_quotes={RELIANCE: _quote(100.0)},
        clock=clock,
        params=params,
    )
    res = MaxGrossExposureRule().check(PlaceOrder(_spec(qty=qty)), view)
    assert res.allow == expected_allow


# 6. PriceDeviationRule
@pytest.mark.parametrize(
    "limit_price,market_price,dev_pct,expected_allow",
    [
        (Decimal("101.00"), 100.0, 5.0, True),
        (Decimal("110.00"), 100.0, 5.0, False),
        (Decimal("95.00"), 100.0, 5.0, True),
        (Decimal("89.00"), 100.0, 5.0, False),
    ],
)
def test_price_deviation_rule(
    limit_price: Decimal, market_price: float, dev_pct: float, expected_allow: bool
) -> None:
    clock = SimClock(initial_time=datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC))
    params = StubParams({"risk.conservative.price_deviation_pct": dev_pct})
    view = RiskViewContext(
        latest_quotes={RELIANCE: _quote(market_price)},
        clock=clock,
        params=params,
    )
    spec = _spec(price=limit_price, order_type=OrderType.LIMIT)
    res = PriceDeviationRule().check(PlaceOrder(spec), view)
    assert res.allow == expected_allow


# 7. MaxOrdersPerMinuteRule
@pytest.mark.parametrize(
    "max_orders,orders_count,expected_allow",
    [
        (10, 5, True),
        (10, 10, False),
    ],
)
def test_max_orders_per_minute_rule(
    max_orders: int, orders_count: int, expected_allow: bool
) -> None:
    clock = SimClock(initial_time=datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC))
    params = StubParams({"risk.conservative.max_orders_per_minute": max_orders})
    view = RiskViewContext(orders_last_minute=orders_count, clock=clock, params=params)
    res = MaxOrdersPerMinuteRule().check(PlaceOrder(_spec()), view)
    assert res.allow == expected_allow


# 8. MaxDailyTradesRule
@pytest.mark.parametrize(
    "max_trades,trades_count,expected_allow",
    [
        (50, 20, True),
        (50, 50, False),
    ],
)
def test_max_daily_trades_rule(max_trades: int, trades_count: int, expected_allow: bool) -> None:
    clock = SimClock(initial_time=datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC))
    params = StubParams({"risk.conservative.max_daily_trades": max_trades})
    view = RiskViewContext(daily_trades_count=trades_count, clock=clock, params=params)
    res = MaxDailyTradesRule().check(PlaceOrder(_spec()), view)
    assert res.allow == expected_allow


# 9. DailyLossLimitRule
@pytest.mark.parametrize(
    "capital,loss_pct,daily_pnl,expected_allow",
    [
        (Decimal("100000.00"), 5.0, Decimal("-2000.00"), True),
        (Decimal("100000.00"), 5.0, Decimal("-5000.00"), False),
        (Decimal("100000.00"), 5.0, Decimal("-6000.00"), False),
    ],
)
def test_daily_loss_limit_rule(
    capital: Decimal, loss_pct: float, daily_pnl: Decimal, expected_allow: bool
) -> None:
    clock = SimClock(initial_time=datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC))
    params = StubParams({"risk.conservative.daily_loss_limit_pct": loss_pct})
    view = RiskViewContext(
        allocated_capital=capital,
        daily_pnl=daily_pnl,
        clock=clock,
        params=params,
    )
    res = DailyLossLimitRule().check(PlaceOrder(_spec()), view)
    assert res.allow == expected_allow
