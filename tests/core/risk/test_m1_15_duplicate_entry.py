"""Summary: Unit, integration, and property tests for M1-15 duplicate-entry guard risk rule.

Covers:
1. Missing registry parameter fails closed.
2. Blocking new entry when position is already open in the same direction.
3. Allowing reducing/exit intents when position is open.
4. Blocking new entry when an entry order is pending in active states.
5. Allowing entry when previous order was CANCELLED or REJECTED.
6. Multi-instrument and multi-strategy independence.
7. Property test: for any interleaving of intents, at most one open entry/position exists.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from plugins.risk_rules.builtin.rules import DuplicateEntryGuardRule

from algodesk.api import (
    InstrumentId,
    Level,
    Order,
    OrderSpec,
    OrderState,
    OrderType,
    PlaceOrder,
    Position,
    Product,
    Quote,
    Side,
    Validity,
)
from algodesk.risk.view import RiskViewContext


class FakeParams:
    def __init__(self, d: dict[str, Any]) -> None:
        self._d = d

    def get(self, key: str) -> Any:
        return self._d.get(key)


def _make_quote(inst: InstrumentId, ltp: float = 100.0) -> Quote:
    now = datetime(2026, 11, 11, 9, 30, tzinfo=UTC)
    return Quote(
        instrument=inst,
        exch_ts=now,
        recv_ts=now,
        bids=(Level(price=ltp - 0.05, qty=100, orders=1),),
        asks=(Level(price=ltp + 0.05, qty=100, orders=1),),
        ltp=ltp,
        volume=1000,
    )


def _make_intent(
    instrument: InstrumentId,
    side: Side = Side.BUY,
    qty: int = 1,
    tag: str = "sma_cross_entry",
) -> PlaceOrder:
    return PlaceOrder(
        spec=OrderSpec(
            instrument=instrument,
            side=side,
            qty=qty,
            type=OrderType.MARKET,
            product=Product.MIS,
            validity=Validity.DAY,
            tag=tag,
        ),
        intent_id=f"intent_{instrument}_{datetime.now(UTC).timestamp()}",
    )


def test_duplicate_entry_missing_param_fails_closed() -> None:
    """When risk.<profile>.prevent_duplicate_entry is missing, rule fails closed."""
    rule = DuplicateEntryGuardRule()
    inst = InstrumentId("NSE:EQ:RELIANCE")
    intent = _make_intent(inst, Side.BUY)
    view = RiskViewContext(params=FakeParams({}), latest_quotes={inst: _make_quote(inst)})

    dec = rule.check(intent, view)
    assert dec.allow is False
    assert "Missing prevent_duplicate_entry parameter" in dec.reason


def test_duplicate_entry_guard_disabled_param_allows() -> None:
    """When prevent_duplicate_entry is False, guard allows duplicate entries."""
    rule = DuplicateEntryGuardRule()
    inst = InstrumentId("NSE:EQ:RELIANCE")
    intent = _make_intent(inst, Side.BUY)
    pos = Position(instrument=inst, product=Product.MIS, qty=1, avg_price=Decimal("100.0"))
    view = RiskViewContext(
        params=FakeParams({"risk.conservative.prevent_duplicate_entry": False}),
        positions={"NSE:EQ:RELIANCE:MIS": pos},
        latest_quotes={inst: _make_quote(inst)},
    )

    dec = rule.check(intent, view)
    assert dec.allow is True


def test_duplicate_entry_blocks_when_position_open_same_side() -> None:
    """When long position is open, another BUY entry is blocked."""
    rule = DuplicateEntryGuardRule()
    inst = InstrumentId("NSE:EQ:RELIANCE")
    pos = Position(instrument=inst, product=Product.MIS, qty=1, avg_price=Decimal("100.0"))
    view = RiskViewContext(
        params=FakeParams({"risk.conservative.prevent_duplicate_entry": True}),
        positions={"NSE:EQ:RELIANCE:MIS": pos},
        latest_quotes={inst: _make_quote(inst)},
    )

    intent_buy = _make_intent(inst, Side.BUY)
    dec_buy = rule.check(intent_buy, view)
    assert dec_buy.allow is False
    assert "Position already open" in dec_buy.reason


def test_duplicate_entry_allows_reducing_intent_when_position_open() -> None:
    """When long position is open, a SELL (exit) intent is allowed."""
    rule = DuplicateEntryGuardRule()
    inst = InstrumentId("NSE:EQ:RELIANCE")
    pos = Position(instrument=inst, product=Product.MIS, qty=1, avg_price=Decimal("100.0"))
    view = RiskViewContext(
        params=FakeParams({"risk.conservative.prevent_duplicate_entry": True}),
        positions={"NSE:EQ:RELIANCE:MIS": pos},
        latest_quotes={inst: _make_quote(inst)},
    )

    intent_sell = _make_intent(inst, Side.SELL, tag="sma_cross_exit")
    dec_sell = rule.check(intent_sell, view)
    assert dec_sell.allow is True


@pytest.mark.parametrize(
    "pending_state",
    [
        OrderState.CREATED,
        OrderState.SUBMITTED,
        OrderState.ACKNOWLEDGED,
        OrderState.PARTIALLY_FILLED,
    ],
)
def test_duplicate_entry_blocks_when_order_pending_in_active_states(
    pending_state: OrderState,
) -> None:
    """When an entry order is pending in any active state, duplicate entry is blocked."""
    rule = DuplicateEntryGuardRule()
    inst = InstrumentId("NSE:EQ:RELIANCE")
    pending_ord = Order(
        client_ref="client-ref-1",
        spec=OrderSpec(
            instrument=inst,
            side=Side.BUY,
            qty=1,
            type=OrderType.MARKET,
            product=Product.MIS,
            validity=Validity.DAY,
            tag="sma_cross_entry",
        ),
        state=pending_state,
        created_ts=datetime(2026, 11, 11, 9, 30, tzinfo=UTC),
    )
    view = RiskViewContext(
        params=FakeParams({"risk.conservative.prevent_duplicate_entry": True}),
        open_orders=(pending_ord,),
        latest_quotes={inst: _make_quote(inst)},
        strategy="sma_cross",
    )

    intent = _make_intent(inst, Side.BUY, tag="sma_cross_entry")
    dec = rule.check(intent, view)
    assert dec.allow is False
    assert "Entry order already pending" in dec.reason


@pytest.mark.parametrize(
    "terminal_state",
    [OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED],
)
def test_duplicate_entry_allows_when_previous_order_terminal_and_no_position(
    terminal_state: OrderState,
) -> None:
    """When previous order reached terminal state and no position remains, entry is allowed."""
    rule = DuplicateEntryGuardRule()
    inst = InstrumentId("NSE:EQ:RELIANCE")
    term_ord = Order(
        client_ref="client-ref-0",
        spec=OrderSpec(
            instrument=inst,
            side=Side.BUY,
            qty=1,
            type=OrderType.MARKET,
            product=Product.MIS,
            validity=Validity.DAY,
            tag="sma_cross_entry",
        ),
        state=terminal_state,
        created_ts=datetime(2026, 11, 11, 9, 20, tzinfo=UTC),
    )
    view = RiskViewContext(
        params=FakeParams({"risk.conservative.prevent_duplicate_entry": True}),
        open_orders=(term_ord,),
        latest_quotes={inst: _make_quote(inst)},
        strategy="sma_cross",
    )

    intent = _make_intent(inst, Side.BUY, tag="sma_cross_entry")
    dec = rule.check(intent, view)
    assert dec.allow is True


def test_duplicate_entry_different_instruments_are_independent() -> None:
    """An open order on RELIANCE does not block an entry on TCS."""
    rule = DuplicateEntryGuardRule()
    rel = InstrumentId("NSE:EQ:RELIANCE")
    tcs = InstrumentId("NSE:EQ:TCS")

    rel_ord = Order(
        client_ref="rel-ref-1",
        spec=OrderSpec(
            instrument=rel,
            side=Side.BUY,
            qty=1,
            type=OrderType.MARKET,
            product=Product.MIS,
            validity=Validity.DAY,
            tag="sma_cross_entry",
        ),
        state=OrderState.ACKNOWLEDGED,
        created_ts=datetime(2026, 11, 11, 9, 30, tzinfo=UTC),
    )
    view = RiskViewContext(
        params=FakeParams({"risk.conservative.prevent_duplicate_entry": True}),
        open_orders=(rel_ord,),
        latest_quotes={rel: _make_quote(rel), tcs: _make_quote(tcs)},
        strategy="sma_cross",
    )

    tcs_intent = _make_intent(tcs, Side.BUY, tag="sma_cross_entry")
    dec = rule.check(tcs_intent, view)
    assert dec.allow is True


def test_duplicate_entry_different_strategies_independent_if_tagged() -> None:
    """If another strategy has an order, sma_cross is evaluated by strategy tag."""
    rule = DuplicateEntryGuardRule()
    inst = InstrumentId("NSE:EQ:RELIANCE")
    strat_a_ord = Order(
        client_ref="a-ref-1",
        spec=OrderSpec(
            instrument=inst,
            side=Side.BUY,
            qty=1,
            type=OrderType.MARKET,
            product=Product.MIS,
            validity=Validity.DAY,
            tag="strat_a_entry",
        ),
        state=OrderState.ACKNOWLEDGED,
        created_ts=datetime(2026, 11, 11, 9, 30, tzinfo=UTC),
    )
    view = RiskViewContext(
        params=FakeParams({"risk.conservative.prevent_duplicate_entry": True}),
        open_orders=(strat_a_ord,),
        latest_quotes={inst: _make_quote(inst)},
        strategy="sma_cross",
    )

    sma_intent = _make_intent(inst, Side.BUY, tag="sma_cross_entry")
    dec = rule.check(sma_intent, view)
    # Different strategy -> allowed
    assert dec.allow is True


# Property-based testing
@settings(max_examples=50)
@given(
    actions=st.lists(
        st.sampled_from(["entry", "fill_entry", "exit", "fill_exit", "cancel"]),
        min_size=1,
        max_size=30,
    )
)
def test_property_duplicate_entry_guard_any_interleaving_gives_at_most_one_open_entry(
    actions: list[str],
) -> None:
    """For any random interleaving of actions, at most one entry order or position is open."""
    rule = DuplicateEntryGuardRule()
    inst = InstrumentId("NSE:EQ:RELIANCE")
    quotes = {inst: _make_quote(inst)}
    params = FakeParams({"risk.conservative.prevent_duplicate_entry": True})

    open_pos_qty = 0
    pending_order: Order | None = None
    ref_counter = 0

    for action in actions:
        # Build current view
        positions = {}
        if open_pos_qty != 0:
            positions["NSE:EQ:RELIANCE:MIS"] = Position(
                instrument=inst, product=Product.MIS, qty=open_pos_qty, avg_price=Decimal("100.0")
            )
        open_orders = (pending_order,) if pending_order is not None else ()
        view = RiskViewContext(
            params=params,
            positions=positions,
            open_orders=open_orders,
            latest_quotes=quotes,
            strategy="sma_cross",
        )

        if action == "entry":
            intent = _make_intent(inst, Side.BUY, qty=1, tag="sma_cross_entry")
            dec = rule.check(intent, view)
            if dec.allow:
                # Place order -> becomes ACKNOWLEDGED pending order
                ref_counter += 1
                pending_order = Order(
                    client_ref=f"ref-{ref_counter}",
                    spec=intent.spec,
                    state=OrderState.ACKNOWLEDGED,
                    created_ts=datetime(2026, 11, 11, 9, 30, tzinfo=UTC),
                )
        elif action == "fill_entry":
            if pending_order is not None and pending_order.spec.side == Side.BUY:
                open_pos_qty += pending_order.spec.qty
                pending_order = None
        elif action == "exit":
            intent = _make_intent(inst, Side.SELL, qty=max(1, open_pos_qty), tag="sma_cross_exit")
            dec = rule.check(intent, view)
            if dec.allow and open_pos_qty > 0 and pending_order is None:
                ref_counter += 1
                pending_order = Order(
                    client_ref=f"ref-{ref_counter}",
                    spec=intent.spec,
                    state=OrderState.ACKNOWLEDGED,
                    created_ts=datetime(2026, 11, 11, 9, 30, tzinfo=UTC),
                )
        elif action == "fill_exit":
            if pending_order is not None and pending_order.spec.side == Side.SELL:
                open_pos_qty = max(0, open_pos_qty - pending_order.spec.qty)
                pending_order = None
        elif action == "cancel":
            if pending_order is not None:
                pending_order = None

        # INVARIANT: At all times, (pending_entry + open_position_entries) <= 1
        is_pending_buy = pending_order is not None and pending_order.spec.side == Side.BUY
        has_pending_entry = 1 if is_pending_buy else 0
        has_open_position = 1 if open_pos_qty > 0 else 0
        err_msg = f"Breach after '{action}': pending={has_pending_entry}, open={has_open_position}"
        assert (has_pending_entry + has_open_position) <= 1, err_msg
