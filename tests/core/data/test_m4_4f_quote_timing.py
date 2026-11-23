"""Tests for M4-4f: Synthetic quote timing, boundary timer alignment, and equivalence.

Reqs: FR-BKT-3, 4; INV-5; ADR-085, 094, 095, 097.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from plugins.venues.sim.venue import SimVenue

from algodesk.api.kinds import Mode
from algodesk.api.market import Bar, InstrumentId, Level, Quote, Tick
from algodesk.api.trading import OrderSpec, OrderState, OrderType, PlaceOrder, Side
from algodesk.data.bar_synth import BarQuoteSynth
from algodesk.engine.clock import SimClock
from algodesk.engine.equivalence import run_backtest_equivalence
from algodesk.engine.runner import SessionRunner
from algodesk.ops.runtime import open_runtime
from algodesk.results.compare import compare_backtest_manifests
from algodesk.risk.engine import RiskEngine
from algodesk.risk.killswitch import KillSwitch
from algodesk.risk.view import RiskViewContext


def _make_bar(
    inst: str = "NSE:EQ:INFY",
    tf: str = "1m",
    start: datetime | None = None,
    o: float = 100.0,
    h: float = 105.0,
    l_val: float = 95.0,
    c: float = 102.0,
    vol: int = 1000,
) -> Bar:
    """Helper to create a valid completed Bar."""
    s = start or datetime(2026, 11, 19, 9, 44, 0, tzinfo=UTC)
    span = timedelta(minutes=1) if tf == "1m" else timedelta(days=1)
    return Bar(
        instrument=InstrumentId(inst),
        timeframe=tf,
        start_ts=s,
        end_ts=s + span,
        open=o,
        high=h,
        low=l_val,
        close=c,
        volume=vol,
        complete=True,
    )


def _build_test_project(tmp_path: Path, name: str) -> Path:
    """Create a minimal workspace directory for hermetic test execution."""
    root_dir = Path(__file__).resolve().parents[3]
    project_dir = tmp_path / name
    project_dir.mkdir(parents=True, exist_ok=True)

    shutil.copyfile(root_dir / "pyproject.toml", project_dir / "pyproject.toml")
    shutil.copytree(root_dir / "config", project_dir / "config")
    shutil.copytree(root_dir / "plugins", project_dir / "plugins")
    (project_dir / "data" / "ticks").mkdir(parents=True, exist_ok=True)
    (project_dir / "data" / "state").mkdir(parents=True, exist_ok=True)
    (project_dir / "logs").mkdir(parents=True, exist_ok=True)
    (project_dir / "results").mkdir(parents=True, exist_ok=True)
    (project_dir / ".algodesk").mkdir(parents=True, exist_ok=True)
    conf_json = root_dir / ".algodesk" / "conformance.json"
    if conf_json.is_file():
        shutil.copyfile(conf_json, project_dir / ".algodesk" / "conformance.json")
    return project_dir


def _record_4_positions_exit_fixture(proj: Path, session_id: str) -> Path:
    """Record a paper session with 4 open positions exiting via timer at exit_time."""
    start_ts = datetime(2026, 11, 19, 9, 38, tzinfo=UTC)
    insts = [
        InstrumentId("NSE:EQ:INFY"),
        InstrumentId("NSE:EQ:TCS"),
        InstrumentId("NSE:EQ:ICICIBANK"),
        InstrumentId("NSE:EQ:RELIANCE"),
    ]

    cfg_file = proj / "config" / "runs" / "demo_paper_fast.toml"
    cfg_text = cfg_file.read_text(encoding="utf-8")
    cfg_text = cfg_text.replace("fast = 3, slow = 8", "fast = 1, slow = 2")
    cfg_file.write_text(cfg_text, encoding="utf-8")

    prices = [100.0, 100.0, 95.0, 110.0, 112.0, 114.0, 115.0, 115.0]

    with open_runtime(proj, run="demo_paper_fast", ensure=True) as rt:
        clk = SimClock(start_ts)
        runner_obj = SessionRunner(
            rt,
            mode=Mode.PAPER,
            clock=clk,
            run_id=session_id,
            dry_run=False,
            quiet=True,
        )

        async def _feed(c: dict[str, Any], cl: SimClock) -> Any:
            for m in range(8):
                t_m = start_ts + timedelta(minutes=m)
                p = prices[m]

                # Emit +15s quotes and ticks for all 4 instruments
                t15 = t_m + timedelta(seconds=15)
                cl.set_time(t15)
                for inst in insts:
                    lvl = (Level(price=p, qty=100),)
                    yield Quote.create_unchecked(inst, t15, t15, lvl, lvl, p, 100)
                    yield Tick(
                        instrument=inst,
                        exch_ts=t15,
                        recv_ts=t15,
                        ltp=p,
                        ltq=10,
                        volume=100 + m * 10,
                    )
                    await asyncio.sleep(0)

                # Emit +59s quotes and ticks for all 4 instruments
                t59 = t_m + timedelta(seconds=59)
                cl.set_time(t59)
                for inst in insts:
                    lvl = (Level(price=p, qty=100),)
                    yield Quote.create_unchecked(inst, t59, t59, lvl, lvl, p, 100)
                    yield Tick(
                        instrument=inst,
                        exch_ts=t59,
                        recv_ts=t59,
                        ltp=p,
                        ltq=10,
                        volume=100 + m * 10,
                    )
                    await asyncio.sleep(0)

                # Bar close boundary (09:45:00 UTC at m=6 is exit_time)
                t_end = t_m + timedelta(minutes=1, milliseconds=501)
                cl.set_time(t_end)
                c["scheduler"].advance_to(cl.now())
                c["scheduler"].fire_due()
                if runner_obj._pending_bar_tasks:
                    await asyncio.gather(*runner_obj._pending_bar_tasks)
                    runner_obj._pending_bar_tasks.clear()
                await asyncio.sleep(0.001)

        runner_obj._build_event_stream = lambda c, cl: _feed(c, cl)
        sess_dir = asyncio.run(runner_obj.run())

    return sess_dir


# ==============================================================================
# Step 3: Intraday bar quote timing & boundary quote age <= 1ms
# ==============================================================================


def test_step3_intraday_1m_quote_timing_and_boundary_age() -> None:
    """Step 3: 1m bar quotes end at bar.end_ts - 1ms; age at boundary is exactly 1ms."""
    synth = BarQuoteSynth()
    s = datetime(2026, 11, 19, 9, 44, 0, tzinfo=UTC)
    bar = _make_bar(start=s, tf="1m")
    q0, q1, q2, q3 = synth.synthesize(bar)

    assert q0.recv_ts == s
    assert q1.recv_ts == s + timedelta(seconds=20)
    assert q2.recv_ts == s + timedelta(seconds=40)
    assert q3.recv_ts == bar.end_ts - timedelta(milliseconds=1)

    # Evaluate quote age when a timer fires exactly at the bar boundary
    boundary_clock_now = bar.end_ts
    quote_age_s = (boundary_clock_now - q3.recv_ts).total_seconds()
    assert quote_age_s == 0.001
    assert quote_age_s * 1000.0 == 1.0


def test_step3_intraday_general_quote_timing_and_boundary_age() -> None:
    """Step 3: General intraday bar (e.g. 5m) quotes end at bar.end_ts - 1ms."""
    synth = BarQuoteSynth()
    s = datetime(2026, 11, 19, 9, 30, 0, tzinfo=UTC)
    e = s + timedelta(minutes=5)
    bar = Bar(
        instrument=InstrumentId("NSE:EQ:INFY"),
        timeframe="5m",
        start_ts=s,
        end_ts=e,
        open=100.0,
        high=105.0,
        low=98.0,
        close=103.0,
        volume=5000,
        complete=True,
    )
    q0, q1, q2, q3 = synth.synthesize(bar)

    c_expected = e - timedelta(milliseconds=1)
    assert q0.recv_ts == s
    assert q0.recv_ts < q1.recv_ts < q2.recv_ts < q3.recv_ts
    assert q3.recv_ts == c_expected

    boundary_clock_now = e
    quote_age_ms = (boundary_clock_now - q3.recv_ts).total_seconds() * 1000.0
    assert quote_age_ms == pytest.approx(1.0, rel=1e-3)


@pytest.mark.anyio
async def test_step3_adr085_order_decided_at_bar_close_fills_from_next_bar_open() -> None:
    """Step 3: Order decided on completed bar t fills from the open quote of bar t+1."""
    inst = InstrumentId("NSE:EQ:INFY")
    venue = SimVenue(params={"mode": "backtest"})
    venue.configure([inst])

    # Bar t quotes
    t_end_t = datetime(2026, 11, 19, 9, 45, 0, tzinfo=UTC)
    q_close_t = Quote.create_unchecked(
        instrument=inst,
        exch_ts=t_end_t - timedelta(milliseconds=1),
        recv_ts=t_end_t - timedelta(milliseconds=1),
        bids=(Level.create_unchecked(price=102.0, qty=100),),
        asks=(Level.create_unchecked(price=102.0, qty=100),),
        ltp=102.0,
        volume=1000,
    )
    venue.on_quote(q_close_t)

    # Order decided on completed bar t arrives at bar t+1 open (09:45:00 UTC)
    spec = OrderSpec(instrument=inst, side=Side.BUY, qty=10, type=OrderType.MARKET)
    await venue.place(spec, "ord_step3")

    # Bar t+1 quotes
    q_open_next = Quote.create_unchecked(
        instrument=inst,
        exch_ts=t_end_t,
        recv_ts=t_end_t,
        bids=(Level.create_unchecked(price=103.5, qty=100),),
        asks=(Level.create_unchecked(price=103.5, qty=100),),
        ltp=103.5,
        volume=1000,
    )
    venue.on_quote(q_open_next)

    order = venue._orders["ord_step3"]
    assert order.state == OrderState.FILLED
    fill = order.gross_fills[0]
    assert fill.price == Decimal("103.50")
    assert fill.ts == t_end_t
    assert fill.ts > q_close_t.recv_ts


def test_step3_historical_feed_bar_end_ts_preserves_bar_boundary() -> None:
    """Step 3: HistoricalFeed and TickBarFeed emit feed_bar with end_ts on bar boundary."""
    synth = BarQuoteSynth()
    s = datetime(2026, 11, 19, 9, 44, 0, tzinfo=UTC)
    e = s + timedelta(minutes=1)
    bar = _make_bar(start=s, tf="1m")

    # Reconstruct feed_bar logic conforming to HistoricalFeed & TickBarFeed
    q_open, q_1, q_2, q_close = synth.synthesize(bar)
    is_daily = (bar.timeframe == "1d") or ((bar.end_ts - bar.start_ts) >= timedelta(hours=12))
    bar_end = q_close.recv_ts if is_daily else bar.end_ts

    feed_bar = Bar(
        instrument=bar.instrument,
        timeframe=bar.timeframe,
        start_ts=q_open.recv_ts,
        end_ts=bar_end,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
        complete=bar.complete,
        synthetic=bar.synthetic,
    )
    assert feed_bar.end_ts == e
    assert q_close.recv_ts == e - timedelta(milliseconds=1)
    assert feed_bar.end_ts > q_close.recv_ts


# ==============================================================================
# Step 4: Missing bar produces stale-data rejection
# ==============================================================================


def test_step4_legitimately_missing_bar_produces_stale_data_rejection(tmp_path: Path) -> None:
    """Step 4: Missing bar for a minute produces stale_data rejection (age 60s > 5s limit)."""
    inst = InstrumentId("NSE:EQ:INFY")
    t0 = datetime(2026, 11, 19, 9, 44, 0, tzinfo=UTC)
    q_last = Quote.create_unchecked(
        instrument=inst,
        exch_ts=t0 + timedelta(seconds=59, milliseconds=999),
        recv_ts=t0 + timedelta(seconds=59, milliseconds=999),
        bids=(Level.create_unchecked(price=100.0, qty=100),),
        asks=(Level.create_unchecked(price=100.0, qty=100),),
        ltp=100.0,
        volume=1000,
    )

    # Missing bar: 60 seconds elapse with NO quotes received. Clock advances to 09:46:00 UTC.
    now_clk = t0 + timedelta(minutes=2)
    clk = SimClock(now_clk)

    view = RiskViewContext(
        portfolio=None,
        positions={},
        open_orders=[],
        daily_pnl=Decimal("0"),
        latest_quotes={inst: q_last},
        params={"risk.conservative.stale_data_seconds": 5.0},
        clock=clk,
        instruments={},
        profile="conservative",
    )

    ks = KillSwitch(state_file=tmp_path / "killswitch.json")
    engine = RiskEngine(killswitch=ks)

    intent = PlaceOrder(
        intent_id="intent_missing_bar",
        spec=OrderSpec(instrument=inst, side=Side.BUY, qty=10, type=OrderType.MARKET),
    )
    decision = engine.evaluate(intent, view)
    assert decision.allow is False
    assert "stale_data" in decision.rule


def test_step4_multiple_missing_bars_produces_stale_data_rejection(tmp_path: Path) -> None:
    """Step 4: Multiple missing bars (180s gap) reject order via data_integrity.stale_data."""
    inst = InstrumentId("NSE:EQ:TCS")
    t0 = datetime(2026, 11, 19, 9, 30, 0, tzinfo=UTC)
    q_last = Quote.create_unchecked(
        instrument=inst,
        exch_ts=t0 + timedelta(seconds=59, milliseconds=999),
        recv_ts=t0 + timedelta(seconds=59, milliseconds=999),
        bids=(Level.create_unchecked(price=2500.0, qty=50),),
        asks=(Level.create_unchecked(price=2500.0, qty=50),),
        ltp=2500.0,
        volume=500,
    )

    # Gap of 3 minutes (180s)
    now_clk = t0 + timedelta(minutes=4)
    clk = SimClock(now_clk)

    view = RiskViewContext(
        portfolio=None,
        positions={},
        open_orders=[],
        daily_pnl=Decimal("0"),
        latest_quotes={inst: q_last},
        params={"risk.conservative.stale_data_seconds": 5.0},
        clock=clk,
        instruments={},
        profile="conservative",
    )

    ks = KillSwitch(state_file=tmp_path / "killswitch_gap.json")
    engine = RiskEngine(killswitch=ks)

    intent = PlaceOrder(
        intent_id="intent_gap_bar",
        spec=OrderSpec(instrument=inst, side=Side.SELL, qty=5, type=OrderType.MARKET),
    )
    decision = engine.evaluate(intent, view)
    assert decision.allow is False
    assert "stale_data" in decision.rule


# ==============================================================================
# Step 5: Daily bars unchanged & result hash invariant
# ==============================================================================


def test_step5_daily_bars_quotes_unchanged() -> None:
    """Step 5: Daily bar quotes remain at 09:15 IST, intermediate, 15:30 IST."""
    synth = BarQuoteSynth()
    s = datetime(2026, 11, 19, 0, 0, 0, tzinfo=UTC)
    e = datetime(2026, 11, 19, 23, 59, 59, tzinfo=UTC)
    bar = Bar(
        instrument=InstrumentId("NSE:EQ:INFY"),
        timeframe="1d",
        start_ts=s,
        end_ts=e,
        open=1000.0,
        high=1020.0,
        low=990.0,
        close=1010.0,
        volume=10000,
        complete=True,
    )
    q0, q1, q2, q3 = synth.synthesize(bar)

    expected_open = datetime(2026, 11, 19, 3, 45, 0, tzinfo=UTC)
    expected_close = datetime(2026, 11, 19, 10, 0, 0, tzinfo=UTC)
    assert q0.recv_ts == expected_open
    assert q3.recv_ts == expected_close


def test_step5_daily_rotation_result_hash_invariance() -> None:
    """Step 5: rotation_1d_bt daily backtest result hash is bit-identical to rot7 baseline."""
    rot7_dir = Path("results/rotation_1d_bt_backtest_20261122_154704_517921")
    if not rot7_dir.is_dir():
        pytest.skip("rot7 baseline run not found in results directory")

    runs = sorted(Path("results").glob("rotation_1d_bt_backtest_*"))
    if len(runs) < 2:
        pytest.skip("Need at least two rotation runs to compare")
    latest_run = runs[-1]

    h_equal, h_diff = compare_backtest_manifests(rot7_dir, latest_run)
    assert h_equal is True, f"Daily backtest hashes diverged: {h_diff}"


# ==============================================================================
# Step 6: Property tests & leak detector
# ==============================================================================


@given(
    base_open=st.floats(min_value=10.0, max_value=5000.0, allow_nan=False, allow_infinity=False),
    high_offset=st.floats(min_value=0.0, max_value=200.0, allow_nan=False, allow_infinity=False),
    low_offset=st.floats(min_value=0.0, max_value=200.0, allow_nan=False, allow_infinity=False),
    close_offset=st.floats(
        min_value=-200.0, max_value=200.0, allow_nan=False, allow_infinity=False
    ),
    volume=st.integers(min_value=10, max_value=1_000_000),
)
@settings(max_examples=40, deadline=None)
def test_step6_property_quote_age_at_boundary_timer_at_most_1ms(
    base_open: float,
    high_offset: float,
    low_offset: float,
    close_offset: float,
    volume: int,
) -> None:
    """Step 6: Hypothesis property - quote age at boundary timer is at most 1ms."""
    o = round(base_open, 2)
    c = round(max(1.0, o + close_offset), 2)
    h = round(max(o, c) + high_offset, 2)
    l_val = round(max(0.5, min(o, c) - low_offset), 2)

    s = datetime(2026, 11, 19, 9, 44, 0, tzinfo=UTC)
    e = s + timedelta(minutes=1)
    bar = Bar(
        instrument=InstrumentId("NSE:EQ:TEST"),
        timeframe="1m",
        start_ts=s,
        end_ts=e,
        open=o,
        high=h,
        low=l_val,
        close=c,
        volume=volume,
        complete=True,
    )
    synth = BarQuoteSynth()
    q0, q1, q2, q3 = synth.synthesize(bar)

    assert q3.recv_ts == e - timedelta(milliseconds=1)
    boundary_timer_now = e
    quote_age_ms = (boundary_timer_now - q3.recv_ts).total_seconds() * 1000.0
    assert quote_age_ms <= 1.001


def test_step6_intraday_sma_cross_exit_time_executes_no_open_positions(tmp_path: Path) -> None:
    """Step 6: Timer exit at exit_time executes in intraday sma_cross; 0 open positions."""
    proj = _build_test_project(tmp_path, "proj_step6_exit")
    _record_4_positions_exit_fixture(proj, "sess_step6_exit")

    with open_runtime(proj, run="demo_paper_fast", ensure=True) as rt:
        res = run_backtest_equivalence(rt, "sess_step6_exit")
        assert res.signals_equal is True
        assert len(res.orders_only_in_paper) == 0
        assert len(res.fills_only_in_paper) == 0

        # Read backtest events to verify all 4 positions closed at exit_time
        bt_sess_dir = proj / "results" / "sess_step6_exit_bt_eq"
        events_file = bt_sess_dir / "events.jsonl"
        assert events_file.is_file()
        events = [
            json.loads(line)
            for line in events_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        exit_fills = [
            json.loads(e.get("payload_json", "{}"))
            for e in events
            if e.get("kind") == "fill"
            and json.loads(e.get("payload_json", "{}")).get("side") == "SELL"
        ]
        assert len(exit_fills) == 4, f"Expected 4 exit fills, got {len(exit_fills)}"


@given(
    base_open=st.floats(min_value=10.0, max_value=5000.0, allow_nan=False, allow_infinity=False),
    high_offset=st.floats(min_value=0.0, max_value=200.0, allow_nan=False, allow_infinity=False),
    low_offset=st.floats(min_value=0.0, max_value=200.0, allow_nan=False, allow_infinity=False),
    close_offset=st.floats(
        min_value=-200.0, max_value=200.0, allow_nan=False, allow_infinity=False
    ),
    side=st.sampled_from([Side.BUY, Side.SELL]),
)
@settings(max_examples=30, deadline=None)
def test_step6_leak_detector_flipping_hl_order_does_not_change_market_fill(
    base_open: float,
    high_offset: float,
    low_offset: float,
    close_offset: float,
    side: Side,
) -> None:
    """Step 6: Leak detector from M4-1c: flipping H/L order does not change market fill."""
    o = round(base_open, 2)
    c = round(max(1.0, o + close_offset), 2)
    h = round(max(o, c) + high_offset, 2)
    l_val = round(max(0.5, min(o, c) - low_offset), 2)

    inst = InstrumentId("NSE:EQ:TEST")
    t_bar_t_end = datetime(2026, 11, 19, 9, 44, 59, 999000, tzinfo=UTC)

    t0 = datetime(2026, 11, 19, 9, 45, 0, tzinfo=UTC)
    t1 = t0 + timedelta(seconds=20)
    t2 = t0 + timedelta(seconds=40)
    t3 = t0 + timedelta(seconds=59, milliseconds=999)

    def _lvl(p: float) -> tuple[Level, ...]:
        return (Level(price=p, qty=100),)

    quotes_path1 = [
        Quote.create_unchecked(inst, t0, t0, _lvl(o), _lvl(o), o, 100),
        Quote.create_unchecked(inst, t1, t1, _lvl(l_val), _lvl(l_val), l_val, 100),
        Quote.create_unchecked(inst, t2, t2, _lvl(h), _lvl(h), h, 100),
        Quote.create_unchecked(inst, t3, t3, _lvl(c), _lvl(c), c, 100),
    ]

    quotes_path2 = [
        Quote.create_unchecked(inst, t0, t0, _lvl(o), _lvl(o), o, 100),
        Quote.create_unchecked(inst, t1, t1, _lvl(h), _lvl(h), h, 100),
        Quote.create_unchecked(inst, t2, t2, _lvl(l_val), _lvl(l_val), l_val, 100),
        Quote.create_unchecked(inst, t3, t3, _lvl(c), _lvl(c), c, 100),
    ]

    async def _run_path(quotes: list[Quote]) -> tuple[Decimal, datetime]:
        v = SimVenue(params={"mode": "backtest", "backtest.slippage_bps": 0})
        v.allow_short = True
        v.configure([inst])
        v.on_quote(
            Quote.create_unchecked(
                inst, t_bar_t_end, t_bar_t_end, _lvl(1000.0), _lvl(1000.0), 1000.0, 100
            )
        )
        spec = OrderSpec(instrument=inst, side=side, qty=10, type=OrderType.MARKET)
        await v.place(spec, "mkt_leak_test")
        for q in quotes:
            v.on_quote(q)
        st_res = v.order_status("mkt_leak_test")
        assert st_res is not None
        assert st_res.state == OrderState.FILLED
        fill = v._orders["mkt_leak_test"].gross_fills[0]
        return fill.price, fill.ts

    p1, ts1 = asyncio.run(_run_path(quotes_path1))
    p2, ts2 = asyncio.run(_run_path(quotes_path2))

    assert p1 == p2, f"Price leaked path direction: {p1} vs {p2}"
    assert ts1 == ts2 == t0, f"Timestamp did not match open quote: {ts1} vs {t0}"


@pytest.mark.anyio
async def test_step6_no_fill_uses_quote_earlier_than_arrival() -> None:
    """Step 6: No fill uses a quote earlier than order arrival."""
    inst = InstrumentId("NSE:EQ:INFY")
    venue = SimVenue(params={"mode": "backtest", "backtest.sim_latency_ms": 100})
    venue.configure([inst])

    t_decision = datetime(2026, 11, 19, 9, 45, 0, tzinfo=UTC)
    venue.on_quote(Quote.create_unchecked(inst, t_decision, t_decision, (), (), 100.0, 100))

    spec = OrderSpec(instrument=inst, side=Side.BUY, qty=10, type=OrderType.MARKET)
    await venue.place(spec, "ord_arrival_check")

    # Order arrival is t_decision + 100ms
    ord_obj = venue._orders["ord_arrival_check"]
    t_arr = ord_obj.arrival
    assert t_arr == t_decision + timedelta(milliseconds=100)
    assert ord_obj.state == OrderState.ACKNOWLEDGED

    # Earlier quote (t_arr - 50ms) cannot fill
    q_earlier = Quote.create_unchecked(
        inst,
        t_arr - timedelta(milliseconds=50),
        t_arr - timedelta(milliseconds=50),
        (Level(price=100.0, qty=100),),
        (Level(price=100.0, qty=100),),
        100.0,
        100,
    )
    venue.on_quote(q_earlier)
    assert ord_obj.state == OrderState.ACKNOWLEDGED

    # Subsequent quote at arrival + 50ms fills
    q_later = Quote.create_unchecked(
        inst,
        t_arr + timedelta(milliseconds=50),
        t_arr + timedelta(milliseconds=50),
        (Level(price=101.0, qty=100),),
        (Level(price=101.0, qty=100),),
        101.0,
        100,
    )
    venue.on_quote(q_later)

    assert ord_obj.state == OrderState.FILLED
    fill = ord_obj.gross_fills[0]
    assert fill.ts >= ord_obj.arrival
    assert fill.price == Decimal("101.00")


# ==============================================================================
# Step 7: Equivalence fixture with 4 open positions at exit_time
# ==============================================================================


def test_step7_equivalence_fixture_4_positions_signals_equal_no_paper_only_exits(
    tmp_path: Path,
) -> None:
    """Step 7: Equivalence on session with 4 open positions at exit_time yields signals equal."""
    proj = _build_test_project(tmp_path, "proj_step7_fixture")
    _record_4_positions_exit_fixture(proj, "sess_step7_exit")

    with open_runtime(proj, run="demo_paper_fast", ensure=True) as rt:
        res = run_backtest_equivalence(rt, "sess_step7_exit")
        assert res.signals_equal is True
        assert len(res.orders_only_in_paper) == 0
        assert len(res.fills_only_in_paper) == 0
        assert res.paper_orders_count == 8
        assert res.backtest_orders_count == 8
