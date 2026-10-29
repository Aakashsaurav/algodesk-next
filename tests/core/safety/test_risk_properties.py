"""Safety property tests: Risk Engine fail-closed and rule evaluation invariants.

Invariants:
1. Fail closed on missing quote, missing parameter, or closed session
2. Reduce-only exemption never bypasses the kill-switch
3. All rules are evaluated (no short-circuit)
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from algodesk.api import (
    Instrument,
    InstrumentId,
    OrderSpec,
    OrderType,
    PlaceOrder,
    Position,
    Product,
    Quote,
    RiskDecision,
    Side,
)
from algodesk.engine.clock import SimClock
from algodesk.portfolio.ledger import PortfolioLedger
from algodesk.risk.engine import RiskEngine
from algodesk.risk.killswitch import KillSwitch
from algodesk.risk.view import RiskViewContext


def _make_quote(inst: str, ltp: float, ts: datetime) -> Quote:
    return Quote(
        instrument=InstrumentId(inst),
        exch_ts=ts,
        recv_ts=ts,
        bids=(),
        asks=(),
        ltp=ltp,
        volume=1000,
    )


class MockSessionManager:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed

    def trading_allowed(self, instrument: InstrumentId, dt: datetime) -> bool:
        return self.allowed


def _build_context(
    clock: SimClock,
    inst_str: str = "NSE:EQ:RELIANCE",
    quote: Quote | None = None,
    params: dict[str, Any] | None = None,
    session_allowed: bool = True,
    positions: dict[str, Position] | None = None,
) -> RiskViewContext:
    inst_id = InstrumentId(inst_str)
    inst = Instrument(
        id=inst_id,
        symbol="RELIANCE",
        exchange="NSE",
        segment="EQ",
        kind="EQ",
        lot_size=1,
        tick_size=Decimal("0.05"),
    )
    quotes = {inst_id: quote} if quote is not None else {}
    p_map = (
        params
        if params is not None
        else {
            "risk.conservative.stale_data_seconds": 5.0,
            "risk.conservative.margin_buffer_pct": 5.0,
        }
    )
    pos_map = positions or {}
    ledger = PortfolioLedger(clock=clock, starting_cash=Decimal("100000.00"))
    return RiskViewContext(
        portfolio=ledger.snapshot(),
        positions=pos_map,
        open_orders=[],
        daily_pnl=Decimal("0.00"),
        latest_quotes=quotes,
        params=p_map,
        clock=clock,
        instruments={inst_id: inst},
        allocated_capital=Decimal("100000.00"),
        session_manager=MockSessionManager(session_allowed),
    )


def test_risk_engine_fail_closed_missing_quote(tmp_path: Path) -> None:
    """Invariant 1a: Intent for instrument without market quote is rejected."""
    now = datetime(2026, 10, 26, 9, 15, tzinfo=UTC)
    clock = SimClock(now)
    ks = KillSwitch(state_file=tmp_path / "ks.json", clock=clock)
    engine = RiskEngine(killswitch=ks)

    view = _build_context(clock=clock, quote=None)  # No quote
    spec = OrderSpec(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        side=Side.BUY,
        qty=10,
        type=OrderType.LIMIT,
        price=Decimal("2500.00"),
        product=Product.CNC,
    )
    intent = PlaceOrder(intent_id="intent_no_quote", spec=spec)

    decision = engine.evaluate(intent, view)
    assert decision.allow is False
    assert decision.rule == "data_integrity.missing_quote"
    assert "Missing market quote" in decision.reason


def test_risk_engine_fail_closed_missing_parameter(tmp_path: Path) -> None:
    """Invariant 1b: Intent evaluated with missing required parameter is rejected."""
    now = datetime(2026, 10, 26, 9, 15, tzinfo=UTC)
    clock = SimClock(now)
    ks = KillSwitch(state_file=tmp_path / "ks.json", clock=clock)
    engine = RiskEngine(killswitch=ks)

    quote = _make_quote("NSE:EQ:RELIANCE", 2500.00, now)
    view = _build_context(clock=clock, quote=quote, params={})  # Missing stale_data_seconds param
    spec = OrderSpec(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        side=Side.BUY,
        qty=10,
        type=OrderType.LIMIT,
        price=Decimal("2500.00"),
        product=Product.CNC,
    )
    intent = PlaceOrder(intent_id="intent_no_param", spec=spec)

    decision = engine.evaluate(intent, view)
    assert decision.allow is False
    assert decision.rule == "fail_closed.missing_param"
    assert "Missing required parameter" in decision.reason


def test_risk_engine_fail_closed_session_closed(tmp_path: Path) -> None:
    """Invariant 1c: Intent submitted outside trading session is rejected."""
    now = datetime(2026, 10, 26, 9, 15, tzinfo=UTC)
    clock = SimClock(now)
    ks = KillSwitch(state_file=tmp_path / "ks.json", clock=clock)
    engine = RiskEngine(killswitch=ks)

    quote = _make_quote("NSE:EQ:RELIANCE", 2500.00, now)
    view = _build_context(clock=clock, quote=quote, session_allowed=False)
    spec = OrderSpec(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        side=Side.BUY,
        qty=10,
        type=OrderType.LIMIT,
        price=Decimal("2500.00"),
        product=Product.CNC,
    )
    intent = PlaceOrder(intent_id="intent_session_closed", spec=spec)

    decision = engine.evaluate(intent, view)
    assert decision.allow is False
    assert decision.rule == "data_integrity.session_closed"
    assert "Market session closed" in decision.reason


def test_reduce_only_never_bypasses_killswitch(tmp_path: Path) -> None:
    """Invariant 2: Reduce-only intent is rejected when killswitch is active."""
    now = datetime(2026, 10, 26, 9, 15, tzinfo=UTC)
    clock = SimClock(now)
    ks = KillSwitch(state_file=tmp_path / "ks.json", clock=clock)
    engine = RiskEngine(killswitch=ks)

    inst_id = InstrumentId("NSE:EQ:RELIANCE")
    quote = _make_quote(str(inst_id), 2500.00, now)

    # Long position of 10 shares
    pos = Position(
        instrument=inst_id,
        product=Product.CNC,
        qty=10,
        avg_price=Decimal("2400.00"),
        realized=Decimal("0.00"),
        unrealized=Decimal("0.00"),
    )
    positions = {f"{inst_id}:{Product.CNC.value}": pos}
    view = _build_context(clock=clock, quote=quote, positions=positions)

    # Sell 10 shares (strictly reduce-only intent)
    spec = OrderSpec(
        instrument=inst_id,
        side=Side.SELL,
        qty=10,
        type=OrderType.LIMIT,
        price=Decimal("2500.00"),
        product=Product.CNC,
    )
    intent = PlaceOrder(intent_id="intent_reduce_only", spec=spec)

    # 1. Killswitch inactive -> reduce-only is evaluated and approved
    dec_clear = engine.evaluate(intent, view)
    assert dec_clear.allow is True

    # 2. Trip killswitch -> reduce-only MUST BE REJECTED
    ks.trip("Emergency volatility halt")
    dec_tripped = engine.evaluate(intent, view)
    assert dec_tripped.allow is False
    assert dec_tripped.rule == "killswitch"
    assert "Kill-switch active" in dec_tripped.reason


class RuleA:
    name = "rule_a"

    def check(self, intent: Any, view: Any) -> RiskDecision:
        return RiskDecision(allow=False, rule=self.name, reason="Breached Rule A", values={"a": 1})


class RuleB:
    name = "rule_b"

    def check(self, intent: Any, view: Any) -> RiskDecision:
        return RiskDecision(allow=False, rule=self.name, reason="Breached Rule B", values={"b": 2})


def test_all_rules_evaluated_no_short_circuit(tmp_path: Path) -> None:
    """Invariant 3: All registered rules run; decisions are aggregated with no short-circuit."""
    now = datetime(2026, 10, 26, 9, 15, tzinfo=UTC)
    clock = SimClock(now)
    ks = KillSwitch(state_file=tmp_path / "ks.json", clock=clock)
    engine = RiskEngine(killswitch=ks)
    engine.add_rule(RuleA())  # type: ignore[arg-type]
    engine.add_rule(RuleB())  # type: ignore[arg-type]

    quote = _make_quote("NSE:EQ:RELIANCE", 2500.00, now)
    view = _build_context(clock=clock, quote=quote)
    spec = OrderSpec(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        side=Side.BUY,
        qty=10,
        type=OrderType.LIMIT,
        price=Decimal("2500.00"),
        product=Product.CNC,
    )
    intent = PlaceOrder(intent_id="intent_multi_rule", spec=spec)

    decision = engine.evaluate(intent, view)
    assert decision.allow is False
    # Both rule_a and rule_b were evaluated
    assert "rule_a" in decision.rule
    assert "rule_b" in decision.rule
    assert "Breached Rule A" in decision.reason
    assert "Breached Rule B" in decision.reason
    assert decision.values["a"] == 1
    assert decision.values["b"] == 2


@pytest.mark.parametrize(
    ("qty", "price", "limit_pct", "capital", "expected_allow"),
    [
        (10, Decimal("100.00"), 5.0, Decimal("100000.00"), True),
        (50, Decimal("100.00"), 5.0, Decimal("100000.00"), True),
        (51, Decimal("100.00"), 5.0, Decimal("100000.00"), False),
        (1, Decimal("6000.00"), 5.0, Decimal("100000.00"), False),
        (25, Decimal("400.00"), 10.0, Decimal("100000.00"), True),
        (26, Decimal("400.00"), 10.0, Decimal("100000.00"), False),
    ],
)
def test_max_order_value_rule_thresholds(
    qty: int, price: Decimal, limit_pct: float, capital: Decimal, expected_allow: bool
) -> None:
    """Table-driven test: MaxOrderValueRule blocks orders exceeding allowed capital percentage."""
    from plugins.risk_rules.builtin.rules import MaxOrderValueRule

    rule = MaxOrderValueRule()
    inst_id = InstrumentId("NSE:EQ:INFY")
    now = datetime(2026, 10, 26, 9, 15, tzinfo=UTC)
    clock = SimClock(now)
    quote = _make_quote(str(inst_id), float(price), now)
    view = _build_context(
        clock=clock,
        inst_str=str(inst_id),
        quote=quote,
        params={"risk.conservative.max_order_value_pct": limit_pct},
    )
    object.__setattr__(view, "allocated_capital", capital)

    spec = OrderSpec(
        instrument=inst_id,
        side=Side.BUY,
        qty=qty,
        type=OrderType.LIMIT,
        price=price,
        product=Product.CNC,
    )
    intent = PlaceOrder(intent_id="intent_mov", spec=spec)
    decision = rule.check(intent, view)
    assert decision.allow is expected_allow
    if not expected_allow:
        assert "exceeds limit" in decision.reason


@pytest.mark.parametrize(
    ("ltp", "limit_price", "max_dev_pct", "expected_allow"),
    [
        (100.0, Decimal("100.00"), 2.0, True),
        (100.0, Decimal("101.50"), 2.0, True),
        (100.0, Decimal("98.50"), 2.0, True),
        (100.0, Decimal("102.50"), 2.0, False),
        (100.0, Decimal("97.50"), 2.0, False),
        (2500.0, Decimal("2550.00"), 2.0, True),
        (2500.0, Decimal("2600.00"), 2.0, False),
    ],
)
def test_price_deviation_rule_thresholds(
    ltp: float, limit_price: Decimal, max_dev_pct: float, expected_allow: bool
) -> None:
    """Table-driven test: PriceDeviationRule blocks limit orders deviating too far from LTP."""
    from plugins.risk_rules.builtin.rules import PriceDeviationRule

    rule = PriceDeviationRule()
    inst_id = InstrumentId("NSE:EQ:INFY")
    now = datetime(2026, 10, 26, 9, 15, tzinfo=UTC)
    clock = SimClock(now)
    quote = _make_quote(str(inst_id), ltp, now)
    view = _build_context(
        clock=clock,
        inst_str=str(inst_id),
        quote=quote,
        params={"risk.conservative.price_deviation_pct": max_dev_pct},
    )

    spec = OrderSpec(
        instrument=inst_id,
        side=Side.BUY,
        qty=10,
        type=OrderType.LIMIT,
        price=limit_price,
        product=Product.CNC,
    )
    intent = PlaceOrder(intent_id="intent_pdev", spec=spec)
    decision = rule.check(intent, view)
    assert decision.allow is expected_allow
    if not expected_allow:
        assert "exceeds limit" in decision.reason
