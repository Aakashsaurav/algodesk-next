"""End-to-end integration and invariant tests for Risk, OMS, Venue, and Ledger.

Verifies:
1. Complete pipeline event flow with real Journal and hash chain verification.
2. Seed determinism across simulation runs.
3. Ledger accounting invariant under Hypothesis fuzzing.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st
from plugins.venues.sim.venue import SimVenue

from algodesk.api import (
    Charges,
    Fill,
    Instrument,
    InstrumentId,
    Level,
    OrderSpec,
    OrderState,
    OrderType,
    PlaceOrder,
    Product,
    Quote,
    Side,
    Validity,
)
from algodesk.engine.bus import EventBus
from algodesk.engine.clock import SimClock
from algodesk.journal.store import events, verify_journal
from algodesk.journal.writer import Journal, JournalSettings
from algodesk.oms.manager import OrderManager
from algodesk.portfolio.ledger import PortfolioLedger
from algodesk.risk.engine import RiskEngine
from algodesk.risk.killswitch import KillSwitch
from algodesk.risk.view import RiskViewContext

RELIANCE = InstrumentId("NSE:EQ:RELIANCE")


class MockFeeEngine:
    """Mock fee engine computing deterministic 0.1% charges."""

    def calculate_trade_charges(
        self,
        turnover: Decimal,
        side: Side | str,
        segment: str,
        product: Product | str,
        trade_date: date,
        schedule: Any = None,
        *,
        is_first_scrip_day: bool = True,
    ) -> Charges:
        brokerage = (turnover * Decimal("0.001")).quantize(Decimal("0.01"))
        return Charges(items={"brokerage": brokerage}, total=brokerage)


class SimpleParamReader:
    def __init__(self, overrides: dict[str, Any] | None = None) -> None:
        self._overrides = overrides or {}

    def get(self, name: str, scope: str | None = None) -> Any:
        return self._overrides.get(name)


def _make_inst(inst_id: InstrumentId) -> Instrument:
    return Instrument(
        id=inst_id,
        exchange="NSE",
        segment="EQ",
        symbol="RELIANCE",
        kind="EQ",
        lot_size=1,
        tick_size=Decimal("0.05"),
    )


def _make_quote(recv_ts: datetime, ask_p: float = 100.0, bid_p: float = 99.95) -> Quote:
    return Quote(
        instrument=RELIANCE,
        exch_ts=recv_ts,
        recv_ts=recv_ts,
        bids=(Level(bid_p, 100),),
        asks=(Level(ask_p, 100),),
        ltp=ask_p,
        volume=1000,
    )


def test_pipeline_integration_event_ordering_and_journal_verification(tmp_path: Path) -> None:
    """Assert write-ahead journaling order (intent -> order.created -> order.event -> fill)."""
    db_path = tmp_path / "journal_integration.db"
    settings_obj = JournalSettings(
        flush_interval_ms=10, max_buffer_events=10, backpressure_timeout_ms=1000
    )
    journal = Journal(db_path, settings=settings_obj)

    t0 = datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC)
    clock = SimClock(initial_time=t0)
    inst = _make_inst(RELIANCE)

    # SimVenue
    sim_venue = SimVenue(params={"sim.latency_ms": 100, "sim.seed": 42})
    sim_venue.configure([inst])

    # Initial quote to prime venue and risk
    q0 = _make_quote(t0, ask_p=100.0)
    sim_venue.on_quote(q0)

    # Risk setup
    ks_path = tmp_path / "killswitch.json"
    ks = KillSwitch(state_file=ks_path, clock=clock)
    risk_params = SimpleParamReader(
        {
            "risk.conservative.stale_data_seconds": 60,
        }
    )
    risk_engine = RiskEngine(
        killswitch=ks,
        rules=(),
        journal=journal,
    )

    # OMS and Ledger setup
    bus = EventBus()
    fee_engine = MockFeeEngine()
    ledger = PortfolioLedger(clock=clock, starting_cash=Decimal("1000000.00"))
    oms = OrderManager(
        bus=bus,
        journal=journal,
        clock=clock,
        venue=sim_venue,
        fee_engine=fee_engine,
        schedule=None,
        params=SimpleParamReader({"oms.submit_timeout_ms": 5000}),
    )

    async def _run() -> None:
        with journal:
            journal.start_run("paper", "v1.0", "integration_run", "r1")

            # 1. Create and evaluate intent
            spec = OrderSpec(
                instrument=RELIANCE,
                side=Side.BUY,
                qty=10,
                type=OrderType.MARKET,
                product=Product.CNC,
                validity=Validity.DAY,
            )
            intent = PlaceOrder(spec=spec, intent_id="intent-test-001")

            risk_view = RiskViewContext(
                portfolio=ledger.snapshot(),
                latest_quotes={RELIANCE: q0},
                clock=clock,
                params=risk_params,
            )
            decision = risk_engine.evaluate(intent, risk_view)
            assert decision.allow

            # 2. OMS submit: write-ahead journaling happens here
            order = await oms.submit(intent, decision)
            assert order.state == OrderState.SUBMITTED

            # 3. Advance time and send second quote to trigger matching
            t1 = t0 + timedelta(milliseconds=150)
            clock.set_time(t1)
            q1 = _make_quote(t1, ask_p=100.0)
            sim_venue.on_quote(q1)

            # 4. Process venue events in OMS
            venue_events = sim_venue.drain_events()
            for ve in venue_events:
                oms.apply_venue_event(ve)
                if ve.kind == "fill" and ve.gross_fill is not None:
                    charges = fee_engine.calculate_trade_charges(
                        turnover=Decimal(ve.gross_fill.qty) * ve.gross_fill.price,
                        side=spec.side,
                        segment="eq",
                        product=spec.product,
                        trade_date=ve.gross_fill.ts.date(),
                    )
                    fill = Fill(
                        fill_id=ve.gross_fill.venue_fill_id,
                        client_ref=order.client_ref,
                        qty=ve.gross_fill.qty,
                        price=ve.gross_fill.price,
                        ts=ve.gross_fill.ts,
                        charges=charges,
                        instrument=spec.instrument,
                        side=spec.side,
                        product=spec.product,
                    )
                    ledger.apply_fill(fill)

            ledger.mark(q1)
            assert order.state == OrderState.FILLED

            journal.end_run("ok")

    asyncio.run(_run())

    # Verify event order in journal: intent -> order.created -> order.event ... -> fill
    recorded_events = list(events(db_path))
    event_kinds = [e["kind"] for e in recorded_events]

    assert "intent" in event_kinds
    assert "order.created" in event_kinds
    assert "order.event" in event_kinds
    assert "fill" in event_kinds

    idx_intent = event_kinds.index("intent")
    idx_created = event_kinds.index("order.created")
    idx_event = event_kinds.index("order.event")
    idx_fill = event_kinds.index("fill")

    assert idx_intent < idx_created < idx_event < idx_fill

    # Verify journal hash chain integrity
    res = verify_journal(db_path)
    assert res.ok, f"Journal verification failed: {res.reason}"


def test_replay_determinism_with_same_seed(tmp_path: Path) -> None:
    """Assert replay with identical seed produces identical decisions, orders, and fills."""

    def _execute_run(db_name: str, seed: int) -> tuple[bool, str, int, Decimal]:
        db_path = tmp_path / db_name
        journal = Journal(
            db_path,
            settings=JournalSettings(
                flush_interval_ms=10, max_buffer_events=10, backpressure_timeout_ms=1000
            ),
        )
        t0 = datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC)
        clock = SimClock(initial_time=t0)
        inst = _make_inst(RELIANCE)

        sim_venue = SimVenue(
            params={"sim.latency_ms": 100, "sim.seed": seed, "sim.partial_fill_rate": 0.0}
        )
        sim_venue.configure([inst])
        q0 = _make_quote(t0, ask_p=100.0)
        sim_venue.on_quote(q0)

        ks = KillSwitch(state_file=tmp_path / f"ks_{db_name}.json", clock=clock)
        risk_params = SimpleParamReader({"risk.conservative.stale_data_seconds": 60})
        risk_engine = RiskEngine(
            killswitch=ks,
            rules=(),
            journal=journal,
        )

        oms = OrderManager(
            bus=EventBus(),
            journal=journal,
            clock=clock,
            venue=sim_venue,
            fee_engine=MockFeeEngine(),
            schedule=None,
            params=SimpleParamReader({"oms.submit_timeout_ms": 5000}),
            run_id="det_run",
        )

        async def _sub() -> tuple[bool, str, int, Decimal]:
            with journal:
                journal.start_run("paper", "v1.0", "det", "r1")
                spec = OrderSpec(
                    instrument=RELIANCE,
                    side=Side.BUY,
                    qty=10,
                    type=OrderType.MARKET,
                    product=Product.CNC,
                    validity=Validity.DAY,
                )
                intent = PlaceOrder(spec=spec, intent_id=f"intent-{db_name}")
                risk_view = RiskViewContext(
                    portfolio=None,
                    latest_quotes={RELIANCE: q0},
                    clock=clock,
                    params=risk_params,
                )
                decision = risk_engine.evaluate(intent, risk_view)
                order = await oms.submit(intent, decision)

                t1 = t0 + timedelta(milliseconds=150)
                clock.set_time(t1)
                q1 = _make_quote(t1, ask_p=100.0)
                sim_venue.on_quote(q1)

                for ve in sim_venue.drain_events():
                    oms.apply_venue_event(ve)

                journal.end_run("ok")
                return (decision.allow, order.client_ref, order.filled_qty, order.avg_price)

        return asyncio.run(_sub())

    run1 = _execute_run("run1.db", seed=999)
    run2 = _execute_run("run2.db", seed=999)

    assert run1 == run2


@given(
    initial_cash=st.decimals(min_value=Decimal("100000"), max_value=Decimal("1000000"), places=2),
    trades=st.lists(
        st.tuples(
            st.sampled_from([Side.BUY, Side.SELL]),
            st.integers(min_value=1, max_value=50),
            st.decimals(min_value=Decimal("10.00"), max_value=Decimal("500.00"), places=2),
            st.decimals(min_value=Decimal("0.00"), max_value=Decimal("20.00"), places=2),
        ),
        min_size=1,
        max_size=20,
    ),
    mark_price=st.decimals(min_value=Decimal("10.00"), max_value=Decimal("500.00"), places=2),
)
@settings(max_examples=50)
def test_hypothesis_ledger_invariant(
    initial_cash: Decimal,
    trades: list[tuple[Side, int, Decimal, Decimal]],
    mark_price: Decimal,
) -> None:
    """Hypothesis invariant: equity change == realized + unrealized - charges."""
    t0 = datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC)
    clock = SimClock(initial_time=t0)
    ledger = PortfolioLedger(clock=clock, starting_cash=initial_cash)

    current_pos_qty = 0
    trade_idx = 0

    for side, qty, price, fee in trades:
        # Prevent shorting beyond cash in this test to ensure valid states
        if side == Side.SELL and current_pos_qty < qty:
            side = Side.BUY

        current_pos_qty = (current_pos_qty + qty) if side == Side.BUY else (current_pos_qty - qty)
        trade_idx += 1
        charges = Charges(items={"fee": fee}, total=fee)
        fill = Fill(
            fill_id=f"f-{trade_idx}",
            client_ref=f"c-{trade_idx}",
            qty=qty,
            price=price,
            ts=t0,
            charges=charges,
            instrument=RELIANCE,
            side=side,
            product=Product.CNC,
        )
        ledger.apply_fill(fill)

    # Mark with final quote
    quote = _make_quote(t0, ask_p=float(mark_price), bid_p=float(mark_price))
    ledger.mark(quote)

    equity_change = ledger.equity - initial_cash
    pnl_sum = ledger.realized + ledger.unrealized - ledger.total_charges
    assert abs(equity_change - pnl_sum) < Decimal("0.0001")
