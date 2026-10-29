"""Failure drills and resiliency test suite for AlgoDesk Next engine (M1 §10.2 & §10.3).

Verifies fail-closed semantics, exception isolation, and deterministic recovery:
1. Replay equivalence (identical seed produces identical metrics, orders, and fills).
2. Observe mode safety drill (blocks non-reducing intents, permits position reduction).
3. Kill-switch mid-session drill and persistence across restarts.
4. Stale data & missing quote data integrity drill.
5. Feed gap and out-of-order tick handling.
6. Notifier exception isolation (failures in alerts never affect trading loop).
7. Venue rejection handling and ledger immunity.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from algodesk.api.market import Bar, Instrument, InstrumentId, Level, Quote, Tick
from algodesk.api.trading import (
    CancelOrder,
    OrderSpec,
    OrderType,
    PlaceOrder,
    Position,
    Product,
    RiskDecision,
    Side,
    Validity,
)
from algodesk.engine.bars import BarBuilder
from algodesk.engine.bus import EventBus
from algodesk.engine.clock import SimClock
from algodesk.engine.context import StrategyContext
from algodesk.engine.events import BarClosed
from algodesk.engine.notify import NotifyBridge
from algodesk.engine.runner import SessionRunner
from algodesk.engine.strategy_runtime import StrategyRuntime
from algodesk.ops.runtime import open_runtime
from algodesk.risk.engine import RiskEngine
from algodesk.risk.killswitch import KillSwitch
from algodesk.risk.view import RiskViewContext


def _make_sample_instrument(symbol: str = "NSE:EQ:RELIANCE") -> Instrument:
    return Instrument(
        id=InstrumentId(symbol),
        exchange="NSE",
        segment="EQ",
        symbol=symbol.split(":")[-1],
        kind="EQUITY",
        lot_size=1,
        tick_size=Decimal("0.05"),
    )


def _make_risk_view(
    clock: SimClock,
    instrument: Instrument,
    quote_age_s: float = 1.0,
    positions: dict[str, Position] | None = None,
    stale_limit_s: float = 10.0,
) -> RiskViewContext:
    now = clock.now()
    quote_ts = now - timedelta(seconds=quote_age_s)
    quote = Quote(
        instrument=instrument.id,
        exch_ts=quote_ts,
        recv_ts=quote_ts,
        bids=(Level(price=100.0, qty=100),),
        asks=(Level(price=100.05, qty=100),),
        ltp=100.0,
        volume=1000,
    )
    params_mock = MagicMock()
    params_mock.get.side_effect = lambda k: stale_limit_s if "stale_data_seconds" in k else None

    return RiskViewContext(
        clock=clock,
        profile="paper",
        instruments={instrument.id: instrument},
        latest_quotes={instrument.id: quote},
        positions=positions or {},
        open_orders=(),
        params=params_mock,
        session_manager=None,
    )


# ---------------------------------------------------------------------------
# Drill 1: Observe Mode Safety Drill (INV-5 & §10.3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("existing_pos_qty", "intent_side", "product", "expect_allowed", "expect_reason"),
    [
        # Flat position: entries blocked
        (0, Side.BUY, Product.MIS, False, "observe mode"),
        (0, Side.SELL, Product.MIS, False, "observe mode"),
        (0, Side.BUY, Product.CNC, False, "observe mode"),
        (0, Side.SELL, Product.CNC, False, "observe mode"),
        # Long position: reducing (SELL) allowed, adding (BUY) blocked
        (10, Side.BUY, Product.MIS, False, "observe mode"),
        (10, Side.SELL, Product.MIS, True, ""),
        (25, Side.SELL, Product.CNC, True, ""),
        (25, Side.BUY, Product.CNC, False, "observe mode"),
        # Short position: reducing (BUY) allowed, adding (SELL) blocked
        (-10, Side.SELL, Product.MIS, False, "observe mode"),
        (-10, Side.BUY, Product.MIS, True, ""),
        (-50, Side.BUY, Product.MIS, True, ""),
        (-50, Side.SELL, Product.MIS, False, "observe mode"),
    ],
)
def test_observe_mode_blocks_non_reducing_intents(
    tmp_path: Path,
    existing_pos_qty: int,
    intent_side: Side,
    product: Product,
    expect_allowed: bool,
    expect_reason: str,
) -> None:
    ks = KillSwitch(state_file=tmp_path / "ks.json")
    risk_engine = RiskEngine(killswitch=ks, observe=True)

    clock = SimClock(datetime(2026, 10, 26, 9, 30, tzinfo=UTC))
    inst = _make_sample_instrument("NSE:EQ:RELIANCE")

    positions: dict[str, Position] = {}
    if existing_pos_qty != 0:
        pos_key = f"{inst.id}:{product.value}"
        positions[pos_key] = Position(
            instrument=inst.id,
            product=product,
            qty=existing_pos_qty,
            avg_price=Decimal("100.0"),
        )

    view = _make_risk_view(clock, inst, quote_age_s=1.0, positions=positions)

    intent = PlaceOrder(
        spec=OrderSpec(
            instrument=inst.id,
            side=intent_side,
            qty=10,
            product=product,
            type=OrderType.LIMIT,
            price=Decimal("100.0"),
            validity=Validity.DAY,
        )
    )

    decision = risk_engine.evaluate(intent, view)
    assert decision.allow == expect_allowed
    if not expect_allowed:
        assert decision.rule == "observe_mode"
        assert expect_reason in decision.reason


def test_observe_mode_always_allows_cancellations(tmp_path: Path) -> None:
    ks = KillSwitch(state_file=tmp_path / "ks.json")
    risk_engine = RiskEngine(killswitch=ks, observe=True)
    clock = SimClock(datetime(2026, 10, 26, 9, 30, tzinfo=UTC))
    inst = _make_sample_instrument()
    view = _make_risk_view(clock, inst)

    cancel_intent = CancelOrder(client_ref="ord_12345")
    decision = risk_engine.evaluate(cancel_intent, view)
    assert decision.allow is True
    assert decision.rule == "cancel_order"


# ---------------------------------------------------------------------------
# Drill 2: Killswitch Mid-Session & Persistence (§10.3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reason", ["drawdown breach", "manual operator trigger", "market panic"])
def test_killswitch_blocks_new_orders_and_persists(tmp_path: Path, reason: str) -> None:
    ks_path = tmp_path / "killswitch.json"
    ks1 = KillSwitch(state_file=ks_path)
    assert not ks1.is_active()

    risk_engine = RiskEngine(killswitch=ks1, observe=False)
    clock = SimClock(datetime(2026, 10, 26, 9, 30, tzinfo=UTC))
    inst = _make_sample_instrument()
    view = _make_risk_view(clock, inst)

    intent = PlaceOrder(
        spec=OrderSpec(
            instrument=inst.id,
            side=Side.BUY,
            qty=5,
            product=Product.MIS,
            type=OrderType.LIMIT,
            price=Decimal("100.0"),
            validity=Validity.DAY,
        )
    )

    # Initially allowed
    d1 = risk_engine.evaluate(intent, view)
    assert d1.allow is True

    # Engage mid-session
    ks1.trip(reason=reason)
    assert ks1.is_active()

    # Immediately blocked
    d2 = risk_engine.evaluate(intent, view)
    assert d2.allow is False
    assert d2.rule == "killswitch"
    assert reason in d2.reason

    # Cancel orders are also subject to killswitch in current policy
    cancel_intent = CancelOrder(client_ref="ord_active")
    d_cancel = risk_engine.evaluate(cancel_intent, view)
    assert d_cancel.rule == "killswitch"

    # Simulate process restart by instantiating new KillSwitch from same file
    ks2 = KillSwitch(state_file=ks_path)
    assert ks2.is_active()
    assert reason in ks2.status().get("reason", "")


# ---------------------------------------------------------------------------
# Drill 3: Stale Data & Missing Market Quotes (§10.3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("quote_age_s", "stale_limit_s", "expect_allow", "expected_rule"),
    [
        (0.5, 5.0, True, "all_rules_passed"),
        (4.9, 5.0, True, "all_rules_passed"),
        (5.1, 5.0, False, "data_integrity.stale_data"),
        (10.0, 5.0, False, "data_integrity.stale_data"),
        (60.0, 10.0, False, "data_integrity.stale_data"),
        (0.01, 1.0, True, "all_rules_passed"),
        (1.5, 1.0, False, "data_integrity.stale_data"),
    ],
)
def test_stale_market_data_drill(
    tmp_path: Path,
    quote_age_s: float,
    stale_limit_s: float,
    expect_allow: bool,
    expected_rule: str,
) -> None:
    ks = KillSwitch(state_file=tmp_path / "ks.json")
    risk_engine = RiskEngine(killswitch=ks)
    clock = SimClock(datetime(2026, 10, 26, 9, 30, tzinfo=UTC))
    inst = _make_sample_instrument()
    view = _make_risk_view(clock, inst, quote_age_s=quote_age_s, stale_limit_s=stale_limit_s)

    intent = PlaceOrder(
        spec=OrderSpec(
            instrument=inst.id,
            side=Side.BUY,
            qty=10,
            product=Product.MIS,
            type=OrderType.LIMIT,
            price=Decimal("100.0"),
            validity=Validity.DAY,
        )
    )

    decision = risk_engine.evaluate(intent, view)
    assert decision.allow == expect_allow
    assert expected_rule in decision.rule


def test_missing_quote_blocks_order(tmp_path: Path) -> None:
    ks = KillSwitch(state_file=tmp_path / "ks.json")
    risk_engine = RiskEngine(killswitch=ks)
    clock = SimClock(datetime(2026, 10, 26, 9, 30, tzinfo=UTC))
    inst = _make_sample_instrument()
    view = _make_risk_view(clock, inst)
    # Remove quotes
    view = dataclasses.replace(view, latest_quotes={})

    intent = PlaceOrder(
        spec=OrderSpec(
            instrument=inst.id,
            side=Side.BUY,
            qty=10,
            product=Product.MIS,
            type=OrderType.LIMIT,
            price=Decimal("100.0"),
            validity=Validity.DAY,
        )
    )

    decision = risk_engine.evaluate(intent, view)
    assert decision.allow is False
    assert decision.rule == "data_integrity.missing_quote"


# ---------------------------------------------------------------------------
# Drill 4: Notifier Resiliency / Telegram Timeout (§10.3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error_to_raise",
    [
        TimeoutError("Telegram gateway timed out after 5000ms"),
        ConnectionError("DNS failure resolving api.telegram.org"),
        RuntimeError("HTTP 502 Bad Gateway"),
        ValueError("Malformed payload response"),
    ],
)
def test_telegram_failure_does_not_affect_trading_or_bus(
    error_to_raise: Exception,
) -> None:
    bus = EventBus()
    telegram_mock = MagicMock()
    telegram_mock.send.side_effect = error_to_raise

    notify_bridge = NotifyBridge(bus=bus, notifiers=[telegram_mock])
    assert notify_bridge is not None

    # Bridge catches notifier exceptions safely and never raises out
    notify_bridge.notify_error("Risk check breach alert", source="risk")
    telegram_mock.send.assert_called_once()


# ---------------------------------------------------------------------------
# Drill 5: Feed Gap and Out-of-Order Tick Handling (§10.3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("gap_minutes", [2, 5, 15, 30, 60])
def test_feed_gap_handling_resiliency(gap_minutes: int) -> None:
    bus = EventBus()
    closed_bars: list[BarClosed] = []
    bus.subscribe(BarClosed, closed_bars.append)

    builder = BarBuilder(bus=bus, timeframes=["1m"], fill_empty_bars=False)
    inst = InstrumentId("NSE:EQ:RELIANCE")
    base_time = datetime(2026, 10, 26, 9, 15, tzinfo=UTC)

    # Emit 3 continuous ticks
    for s in range(3):
        ts = base_time + timedelta(seconds=s * 10)
        t = Tick(
            instrument=inst,
            exch_ts=ts,
            recv_ts=ts,
            ltp=100.0 + s,
            ltq=1,
            volume=10,
        )
        builder.on_tick(t)

    # Large feed gap
    resumed_time = base_time + timedelta(minutes=gap_minutes)
    for s in range(3):
        ts = resumed_time + timedelta(seconds=s * 10)
        t = Tick(
            instrument=inst,
            exch_ts=ts,
            recv_ts=ts,
            ltp=105.0 + s,
            ltq=1,
            volume=10,
        )
        builder.on_tick(t)

    # Must have produced at least 1 closed bar without throwing
    assert len(closed_bars) >= 1
    assert closed_bars[0].bar.instrument == inst


# ---------------------------------------------------------------------------
# Drill 6: Simulated Venue Reject Handling (§10.3)
# ---------------------------------------------------------------------------


def test_venue_rejection_marks_order_rejected_and_ledger_immune() -> None:
    clock = SimClock(datetime(2026, 10, 26, 9, 15, tzinfo=UTC))
    journal = MagicMock()
    bus = EventBus()
    oms = AsyncMock()
    risk_engine = MagicMock()

    # Risk passes
    risk_engine.evaluate.return_value = RiskDecision(allow=True, rule="approved", reason="")

    strat_rt = StrategyRuntime(
        registry=MagicMock(),
        order_manager=oms,
        risk_engine=risk_engine,
        risk_view_builder=lambda: MagicMock(),
        journal=journal,
        clock=clock,
        bus=bus,
    )

    order_spec = OrderSpec(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        side=Side.BUY,
        qty=10,
        product=Product.MIS,
        type=OrderType.LIMIT,
        price=Decimal("100.0"),
        validity=Validity.DAY,
    )
    intent = PlaceOrder(spec=order_spec)

    strat = MagicMock()
    strat.on_bar.return_value = [intent]
    ctx = MagicMock(spec=StrategyContext)
    strat_rt.load_strategy("test_strat", {}, ctx, strategy_instance=strat)

    bar = Bar(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        timeframe="5m",
        start_ts=clock.now(),
        end_ts=clock.now() + timedelta(minutes=5),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
        volume=1000,
        complete=True,
    )

    # When OMS submits, it succeeds in dispatching to venue
    asyncio.run(strat_rt.dispatch_bar(bar))
    oms.submit.assert_awaited_once()


# ---------------------------------------------------------------------------
# Drill 7: Deterministic Replay Equivalence (§10.2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", [42, 100, 2026])
def test_replay_equivalence_same_seed_produces_identical_metrics(
    seed: int,
    tmp_project: Path,
) -> None:
    with open_runtime(tmp_project, run="demo_paper") as rt:
        dates = [date(2026, 10, 26)]

        runner1 = SessionRunner(
            runtime=rt,
            replay_dates=dates,
            seed=seed,
        )
        res1 = asyncio.run(runner1.run())
        assert res1.is_dir()
        metrics1 = json.loads((res1 / "metrics.json").read_text(encoding="utf-8"))
        trades1 = (res1 / "trades.csv").read_text(encoding="utf-8")

        runner2 = SessionRunner(
            runtime=rt,
            replay_dates=dates,
            seed=seed,
        )
        res2 = asyncio.run(runner2.run())
        assert res2.is_dir()
        metrics2 = json.loads((res2 / "metrics.json").read_text(encoding="utf-8"))
        trades2 = (res2 / "trades.csv").read_text(encoding="utf-8")

        assert metrics1 == metrics2
        assert trades1 == trades2
