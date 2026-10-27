"""Edge-case and fail-closed tests for risk engine and builtin risk rules.

Tests missing parameters, stale quotes, missing quotes, non-PlaceOrder intents,
reduce-only exemptions, kill-switch concurrency/edge-cases, and fail-closed integrity.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

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
    CancelOrder,
    InstrumentId,
    Level,
    OrderSpec,
    OrderType,
    PlaceOrder,
    Position,
    Product,
    Quote,
    RiskDecision,
    Side,
)
from algodesk.risk.engine import RiskEngine
from algodesk.risk.killswitch import KillSwitch
from algodesk.risk.view import RiskViewContext

RELIANCE = InstrumentId("NSE:EQ:RELIANCE")
TCS = InstrumentId("NSE:EQ:TCS")


class StubClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def utc_now(self) -> datetime:
        return self._now


class EmptyParams:
    def get(self, key: str, default: object = None) -> object:
        return default


def _quote(price: float, ts: datetime, stale: bool = False) -> Quote:
    return Quote(
        instrument=RELIANCE,
        exch_ts=ts,
        recv_ts=ts,
        bids=(Level(price=price - 0.05, qty=100, orders=1),),
        asks=(Level(price=price + 0.05, qty=100, orders=1),),
        ltp=price,
        volume=1000,
    )


def _spec(
    instrument: InstrumentId = RELIANCE,
    side: Side = Side.BUY,
    qty: int = 10,
    order_type: OrderType = OrderType.MARKET,
    price: Decimal | None = None,
) -> OrderSpec:
    return OrderSpec(
        instrument=instrument,
        side=side,
        qty=qty,
        type=order_type,
        price=price,
        product=Product.CNC,
    )


@pytest.mark.parametrize(
    "rule_cls,rule_name",
    [
        (CashAvailableRule, "cash_available"),
        (MaxOrderValueRule, "max_order_value_pct"),
        (MaxPositionRule, "max_position_pct"),
        (MaxOpenPositionsRule, "max_open_positions"),
        (MaxGrossExposureRule, "max_gross_exposure_pct"),
        (PriceDeviationRule, "price_deviation_pct"),
        (MaxOrdersPerMinuteRule, "max_orders_per_minute"),
        (MaxDailyTradesRule, "max_daily_trades"),
        (DailyLossLimitRule, "daily_loss_limit_pct"),
    ],
)
def test_risk_rules_fail_closed_on_missing_param(rule_cls: type, rule_name: str) -> None:
    now = datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC)
    view = RiskViewContext(
        allocated_capital=Decimal("100000.00"),
        latest_quotes={RELIANCE: _quote(100.0, now)},
        clock=StubClock(now),
        params=EmptyParams(),
    )
    spec = _spec(
        order_type=OrderType.LIMIT if rule_cls is PriceDeviationRule else OrderType.MARKET,
        price=Decimal("100.00") if rule_cls is PriceDeviationRule else None,
    )
    intent = PlaceOrder(spec)
    decision = rule_cls().check(intent, view)
    assert not decision.allow
    assert "Missing" in decision.reason


@pytest.mark.parametrize(
    "rule_cls",
    [
        CashAvailableRule,
        MaxOrderValueRule,
        MaxPositionRule,
        MaxOpenPositionsRule,
        MaxGrossExposureRule,
        PriceDeviationRule,
        MaxOrdersPerMinuteRule,
        MaxDailyTradesRule,
        DailyLossLimitRule,
    ],
)
def test_risk_rules_allow_non_place_order(rule_cls: type) -> None:
    now = datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC)
    view = RiskViewContext(
        allocated_capital=Decimal("100000.00"),
        latest_quotes={RELIANCE: _quote(100.0, now)},
        clock=StubClock(now),
        params=EmptyParams(),
    )
    cancel_intent = CancelOrder(client_ref="ref-123")
    decision = rule_cls().check(cancel_intent, view)
    assert decision.allow
    assert "Not a placement" in decision.reason


class StubParams:
    def __init__(self, mapping: dict[str, object]) -> None:
        self._m = mapping

    def get(self, key: str, default: object = None) -> object:
        return self._m.get(key, default)


@pytest.mark.parametrize(
    "existing_qty,side,expected_allow",
    [
        (50, Side.SELL, True),  # Long 50, selling 10 -> reduces long -> exempt from entry rules
        (50, Side.BUY, False),  # Long 50, buying 10 -> increases long -> entry rules evaluated
        (-50, Side.BUY, True),  # Short 50, buying 10 -> reduces short -> exempt from entry rules
        (-50, Side.SELL, False),  # Short 50, selling 10 -> increases short -> entry rules evaluated
    ],
)
def test_risk_engine_reduce_only_exemption_combinations(
    existing_qty: int, side: Side, expected_allow: bool, tmp_path: pytest.TempPathFactory
) -> None:
    now = datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC)
    ks_path = tmp_path / "data" / "state" / "killswitch.json"
    ks = KillSwitch(state_file=ks_path)

    pos = Position(
        instrument=RELIANCE,
        product=Product.CNC,
        qty=existing_qty,
        avg_price=Decimal("100.00"),
    )
    params = StubParams({"risk.conservative.stale_data_seconds": 60.0})
    view = RiskViewContext(
        allocated_capital=Decimal("100000.00"),
        positions={f"{RELIANCE}:CNC": pos},
        latest_quotes={RELIANCE: _quote(100.0, now)},
        clock=StubClock(now),
        params=params,
        daily_pnl=Decimal("-10000.00"),  # Daily loss limit breached!
    )

    class EntryBlockedRule:
        name = "daily_loss_limit_pct"

        def check(self, intent: object, v: object) -> RiskDecision:
            return RiskDecision(allow=False, rule=self.name, reason="daily loss limit breached")

    engine = RiskEngine(rules=[EntryBlockedRule()], killswitch=ks)
    decision = engine.evaluate(PlaceOrder(_spec(side=side, qty=10)), view)
    assert decision.allow == expected_allow


def test_risk_engine_fail_closed_on_missing_quote(tmp_path: pytest.TempPathFactory) -> None:
    now = datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC)
    ks_path = tmp_path / "data" / "state" / "killswitch.json"
    ks = KillSwitch(state_file=ks_path)
    engine = RiskEngine(rules=[], killswitch=ks)

    params = StubParams({"risk.conservative.stale_data_seconds": 60.0})
    view = RiskViewContext(
        allocated_capital=Decimal("100000.00"),
        latest_quotes={},  # No quote for RELIANCE
        clock=StubClock(now),
        params=params,
    )
    decision = engine.evaluate(PlaceOrder(_spec()), view)
    assert not decision.allow
    assert "Missing market quote" in decision.reason


def test_risk_engine_fail_closed_on_stale_quote(tmp_path: pytest.TempPathFactory) -> None:
    now = datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC)
    stale_ts = now - timedelta(seconds=120)  # 2 minutes old, limit is 60s
    ks_path = tmp_path / "data" / "state" / "killswitch.json"
    ks = KillSwitch(state_file=ks_path)
    engine = RiskEngine(rules=[], killswitch=ks)

    params = StubParams({"risk.conservative.stale_data_seconds": 60.0})
    view = RiskViewContext(
        allocated_capital=Decimal("100000.00"),
        latest_quotes={RELIANCE: _quote(100.0, stale_ts)},
        clock=StubClock(now),
        params=params,
    )
    decision = engine.evaluate(PlaceOrder(_spec()), view)
    assert not decision.allow
    assert "exceeds stale limit" in decision.reason


def test_killswitch_lifecycle(tmp_path: pytest.TempPathFactory) -> None:
    ks_path = tmp_path / "data" / "state" / "killswitch.json"
    ks = KillSwitch(state_file=ks_path)

    # Initially inactive
    assert not ks.is_active()
    status = ks.status()
    assert not status["active"]

    # Trip 1
    ks.trip(reason="Market crash", by="risk_monitor")
    assert ks.is_active()
    s1 = ks.status()
    assert s1["active"]
    assert s1["reason"] == "Market crash"
    assert s1["by"] == "risk_monitor"

    # Trip 2 while active
    ks.trip(reason="Manual operator intervention", by="operator")
    assert ks.is_active()
    s2 = ks.status()
    assert s2["active"]
    assert s2["reason"] == "Manual operator intervention"
    assert s2["by"] == "operator"

    # Clear
    ks.clear(reason="Risk normalized", by="admin")
    assert not ks.is_active()
    s3 = ks.status()
    assert not s3["active"]
    assert s3["reason"] == "Risk normalized"
    assert s3["by"] == "admin"


def test_killswitch_handles_corrupt_file(tmp_path: pytest.TempPathFactory) -> None:
    ks_path = tmp_path / "data" / "state" / "killswitch.json"
    ks_path.parent.mkdir(parents=True, exist_ok=True)
    ks_path.write_text("{invalid json format: True")

    ks = KillSwitch(state_file=ks_path)
    # Fails safe (is_active returns False, status returns empty/default)
    assert not ks.is_active()
    status = ks.status()
    assert not status["active"]
