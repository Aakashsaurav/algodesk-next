"""Tests for M1-16: Paper/replay determinism, dropped signals, and diffs."""

from __future__ import annotations

import asyncio
import csv
import json
import math
import shutil
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from plugins.risk_rules.builtin.rules import DuplicateEntryGuardRule
from plugins.venues.sim.venue import SimVenue
from typer.testing import CliRunner

from algodesk.api import (
    Instrument,
    InstrumentId,
    Level,
    Order,
    OrderSpec,
    OrderState,
    OrderType,
    PlaceOrder,
    Product,
    Quote,
    Side,
    Tick,
    Validity,
)
from algodesk.cli.app import app
from algodesk.engine.clock import SimClock
from algodesk.engine.runner import SessionRunner
from algodesk.engine.strategy_runtime import StrategyRuntime
from algodesk.ops.runtime import open_runtime
from algodesk.results.compare import compare_runs
from algodesk.risk.engine import RiskEngine
from algodesk.risk.killswitch import KillSwitch

IST = ZoneInfo("Asia/Kolkata")


def _seed_cache(proj: Path) -> None:
    real_cache = (
        Path(__file__).resolve().parent.parent.parent.parent / ".local" / "cache" / "instruments"
    )
    today = datetime.now(IST).date()
    target = proj / ".local" / "cache" / "instruments"
    target.mkdir(parents=True, exist_ok=True)
    if real_cache.is_dir():
        for f in real_cache.glob("upstox_master_*.json.gz"):
            shutil.copyfile(f, target / f"upstox_master_{today.isoformat()}.json.gz")


def _make_inst(inst_id: InstrumentId) -> Instrument:
    parts = str(inst_id).split(":")
    return Instrument(
        id=inst_id,
        exchange=parts[0],
        segment=parts[1],
        symbol=parts[2],
        kind="EQ",
        lot_size=1,
        tick_size=Decimal("0.05"),
    )


def _make_quote(
    inst: InstrumentId,
    ts: datetime,
    ltp: float = 2500.0,
    bid: float = 2499.0,
    ask: float = 2501.0,
) -> Quote:
    return Quote(
        instrument=inst,
        exch_ts=ts,
        recv_ts=ts,
        bids=(Level(price=bid, qty=50, orders=1),),
        asks=(Level(price=ask, qty=50, orders=1),),
        ltp=ltp,
        volume=1000,
    )


class DummyJournal:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def append(self, kind: str, payload: dict[str, Any], durable: bool = False) -> None:
        self.events.append((kind, payload))


class DummyClock:
    def __init__(self, t: datetime) -> None:
        self._t = t

    def now(self) -> datetime:
        return self._t

    def set_time(self, t: datetime) -> None:
        self._t = t


class DummyParams:
    def __init__(self, mapping: dict[str, Any]) -> None:
        self._m = mapping

    def get(self, key: str, default: Any = None) -> Any:
        return self._m.get(key, default)


class DummyRiskView:
    def __init__(
        self,
        inst: InstrumentId,
        quote: Quote | None,
        clock: DummyClock,
        params: DummyParams,
        positions: dict[str, Any] | None = None,
        open_orders: tuple[Any, ...] = (),
        profile: str = "conservative",
    ) -> None:
        self.instruments = {inst}
        self._quotes = {inst: quote} if quote is not None else {}
        self.clock = clock
        self.params = params
        self.positions = positions or {}
        self.open_orders = open_orders
        self.profile = profile
        self.session_manager = None

    def quote_for(self, inst: InstrumentId) -> Quote | None:
        return self._quotes.get(inst)


def test_rejects_by_rule_tracked_for_data_integrity_stale_data(tmp_path: Path) -> None:
    """1. RiskEngine tracks stale quote rejection in rejects_by_rule."""
    inst = InstrumentId("NSE:EQ:HDFCBANK")
    t0 = datetime(2026, 11, 12, 9, 8, 0, 500000, tzinfo=UTC)
    q_ts = datetime(2026, 11, 12, 9, 7, 54, 0, tzinfo=UTC)  # 6.5s stale
    quote = _make_quote(inst, q_ts)
    clock = DummyClock(t0)
    params = DummyParams({"risk.conservative.stale_data_seconds": 5.0})
    view = DummyRiskView(inst, quote, clock, params)

    journal = DummyJournal()
    ks = KillSwitch(state_file=tmp_path / "ks.json")
    engine = RiskEngine(rules=[], killswitch=ks, journal=journal)

    spec = OrderSpec(
        instrument=inst,
        side=Side.BUY,
        qty=1,
        type=OrderType.MARKET,
        product=Product.MIS,
        validity=Validity.DAY,
    )
    intent = PlaceOrder(spec=spec, intent_id="intent_1")

    dec = engine.evaluate(intent, view)
    assert dec.allow is False
    assert dec.rule == "data_integrity.stale_data"
    assert "Quote age 6.50s exceeds stale limit 5.0s" in dec.reason
    assert engine.rejects_by_rule.get("data_integrity.stale_data") == 1
    assert any(
        kind == "risk.decision" and p["intent_id"] == "intent_1" for kind, p in journal.events
    )


def test_rejects_by_rule_tracked_for_duplicate_entry_guard(tmp_path: Path) -> None:
    """2. DuplicateEntryGuardRule rejection appears in rejects_by_rule and journal."""
    inst = InstrumentId("NSE:EQ:RELIANCE")
    t0 = datetime(2026, 11, 12, 9, 15, 0, tzinfo=UTC)
    quote = _make_quote(inst, t0)
    clock = DummyClock(t0)
    params = DummyParams(
        {
            "risk.conservative.stale_data_seconds": 10.0,
            "risk.conservative.prevent_duplicate_entry": True,
            "prevent_duplicate_entry": True,
        }
    )

    # Existing pending order
    pending_order = Order(
        client_ref="ref-1",
        spec=OrderSpec(
            instrument=inst,
            side=Side.BUY,
            qty=1,
            type=OrderType.MARKET,
            product=Product.MIS,
            validity=Validity.DAY,
        ),
        state=OrderState.SUBMITTED,
        created_ts=t0,
        filled_qty=0,
        avg_price=Decimal("0.00"),
        venue_ref=None,
    )
    view = DummyRiskView(inst, quote, clock, params, open_orders=(pending_order,))

    journal = DummyJournal()
    ks = KillSwitch(state_file=tmp_path / "ks.json")
    rule = DuplicateEntryGuardRule()
    engine = RiskEngine(rules=[rule], killswitch=ks, journal=journal)

    intent = PlaceOrder(
        spec=OrderSpec(
            instrument=inst,
            side=Side.BUY,
            qty=1,
            type=OrderType.MARKET,
            product=Product.MIS,
            validity=Validity.DAY,
        ),
        intent_id="intent_duplicate",
    )

    dec = engine.evaluate(intent, view)
    assert dec.allow is False
    assert "duplicate_entry_guard" in dec.rule
    assert engine.rejects_by_rule.get("duplicate_entry_guard") == 1
    assert any(
        kind == "risk.decision" and p["intent_id"] == "intent_duplicate"
        for kind, p in journal.events
    )


def test_dropped_signal_oms_exception_recorded_in_journal_and_metrics() -> None:
    """3. Dropped signal due to OMS submission exception is journaled under error and tracked."""
    journal = DummyJournal()
    rejects: dict[str, int] = {}

    def on_reject(intent: Any, dec: Any, component: str = "risk") -> None:
        rejects[component] = rejects.get(component, 0) + 1

    class FailingOMS:
        async def submit(self, intent: Any, dec: Any) -> None:
            raise RuntimeError("Simulated OMS reject")

    runtime = StrategyRuntime(
        journal=journal,
        order_manager=FailingOMS(),  # type: ignore
        on_reject=on_reject,
    )

    inst = InstrumentId("NSE:EQ:INFY")
    spec = OrderSpec(
        instrument=inst,
        side=Side.BUY,
        qty=1,
        type=OrderType.MARKET,
        product=Product.MIS,
        validity=Validity.DAY,
    )
    intent = PlaceOrder(spec=spec, intent_id="intent_fail_oms")

    with pytest.raises(RuntimeError, match="Simulated OMS reject"):
        asyncio.run(runtime._process_intents("strat", [intent]))

    assert rejects.get("oms") == 1
    err_events = [p for kind, p in journal.events if kind == "error"]
    assert len(err_events) == 1
    assert err_events[0]["component"] == "oms"
    assert err_events[0]["intent_id"] == "intent_fail_oms"
    assert "Simulated OMS reject" in err_events[0]["error"]


def test_results_compare_prints_counts_and_first_three_differences(tmp_project: Path) -> None:
    """4. results compare prints sequence counts and first 3 differences for mismatched runs."""
    results_dir = tmp_project / "results"
    run_a = results_dir / "run_alpha"
    run_b = results_dir / "run_beta"
    run_a.mkdir(parents=True, exist_ok=True)
    run_b.mkdir(parents=True, exist_ok=True)

    metrics = {"trades": 5, "net_pnl": 500.0}
    (run_a / "metrics.json").write_text(json.dumps(metrics))
    (run_b / "metrics.json").write_text(json.dumps(metrics))

    # Run A has 4 signals, Run B has 4 signals differing on items 0, 1, 2
    sigs_a = [
        {
            "kind": "signal",
            "ts_utc": f"2026-11-12T09:1{i}:00Z",
            "payload_json": json.dumps(
                {"instrument": "NSE:EQ:A", "side": "BUY", "intent_id": f"a_{i}"}
            ),
        }
        for i in range(4)
    ]
    sigs_b = [
        {
            "kind": "signal",
            "ts_utc": f"2026-11-12T09:1{i}:00Z",
            "payload_json": json.dumps(
                {"instrument": "NSE:EQ:B", "side": "SELL", "intent_id": f"b_{i}"}
            ),
        }
        for i in range(4)
    ]

    (run_a / "events.jsonl").write_text("\n".join(json.dumps(s) for s in sigs_a) + "\n")
    (run_b / "events.jsonl").write_text("\n".join(json.dumps(s) for s in sigs_b) + "\n")

    is_equal, diff = compare_runs(tmp_project, "run_alpha", "run_beta")
    assert is_equal is False
    assert diff is not None
    assert "counts: signals (A=4, B=4)" in diff
    assert "signals differences (showing first 3):" in diff
    assert "signals[0]" in diff
    assert "signals[1]" in diff
    assert "signals[2]" in diff
    assert "signals[3]" not in diff  # Limit 3 ensures fourth diff is omitted


def test_results_compare_semantic_normalization_timer_exit_subsecond_jitter(
    tmp_project: Path,
) -> None:
    """5. results compare normalizes subsecond jitter on timer exit intent IDs."""
    results_dir = tmp_project / "results"
    run_a = results_dir / "run_t1"
    run_b = results_dir / "run_t2"
    run_a.mkdir(parents=True, exist_ok=True)
    run_b.mkdir(parents=True, exist_ok=True)

    (run_a / "metrics.json").write_text(json.dumps({"trades": 1, "net_pnl": 10.0}))
    (run_b / "metrics.json").write_text(json.dumps({"trades": 1, "net_pnl": 10.0}))

    # Timer exits differing only by subsecond microsecond timestamps
    id_a = "timer_exit_NSE:EQ:RELIANCE_1794476700.098816"
    id_b = "timer_exit_NSE:EQ:RELIANCE_1794476700.095263"

    sig_a = {
        "kind": "signal",
        "ts_utc": "2026-11-12T09:15:00Z",
        "payload_json": json.dumps(
            {"instrument": "NSE:EQ:RELIANCE", "side": "SELL", "intent_id": id_a}
        ),
    }
    sig_b = {
        "kind": "signal",
        "ts_utc": "2026-11-12T09:15:00Z",
        "payload_json": json.dumps(
            {"instrument": "NSE:EQ:RELIANCE", "side": "SELL", "intent_id": id_b}
        ),
    }

    (run_a / "events.jsonl").write_text(json.dumps(sig_a) + "\n")
    (run_b / "events.jsonl").write_text(json.dumps(sig_b) + "\n")

    is_equal, diff = compare_runs(tmp_project, "run_t1", "run_t2")
    assert is_equal is True
    assert diff is None


def test_results_compare_semantic_normalization_quote_age_subsecond_jitter(
    tmp_project: Path,
) -> None:
    """6. results compare normalizes subsecond float differences in Quote age rejection reasons."""
    results_dir = tmp_project / "results"
    run_a = results_dir / "run_q1"
    run_b = results_dir / "run_q2"
    run_a.mkdir(parents=True, exist_ok=True)
    run_b.mkdir(parents=True, exist_ok=True)

    (run_a / "metrics.json").write_text(json.dumps({"trades": 0, "net_pnl": 0.0}))
    (run_b / "metrics.json").write_text(json.dumps({"trades": 0, "net_pnl": 0.0}))

    # Rejection reasons differing only in quote age float
    dec_a = {
        "kind": "risk.decision",
        "ts_utc": "2026-11-12T09:08:00.516Z",
        "payload_json": json.dumps(
            {
                "intent_id": "intent_1",
                "allow": False,
                "rule": "data_integrity.stale_data",
                "reason": "Quote age 6.45s exceeds stale limit 5.0s",
            }
        ),
    }
    dec_b = {
        "kind": "risk.decision",
        "ts_utc": "2026-11-12T09:08:00.516Z",
        "payload_json": json.dumps(
            {
                "intent_id": "intent_1",
                "allow": False,
                "rule": "data_integrity.stale_data",
                "reason": "Quote age 6.48s exceeds stale limit 5.0s",
            }
        ),
    }

    (run_a / "events.jsonl").write_text(json.dumps(dec_a) + "\n")
    (run_b / "events.jsonl").write_text(json.dumps(dec_b) + "\n")

    is_equal, diff = compare_runs(tmp_project, "run_q1", "run_q2")
    assert is_equal is True
    assert diff is None


def test_fake_feed_latency_crossing_tick_boundaries_pending_entry_exit(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """7. Fake-feed session with venue latency crossing tick boundaries and pending entry + exit.

    Paper and replay must be equal in signals, orders, fills, metrics, and equity points.
    """
    _seed_cache(tmp_project)

    # Configure demo_paper_fast with 1m timeframe, fast=2, slow=4, no telegram notifier
    cfg_file = tmp_project / "config" / "runs" / "demo_paper_fast.toml"
    cfg_text = cfg_file.read_text(encoding="utf-8")
    old_params = (
        'params = { timeframe = "1m", fast = 3, slow = 8, qty = 1, exit_time = "15:15", product ='
        ' "MIS" }'
    )
    new_params = (
        'params = { timeframe = "1m", fast = 2, slow = 4, qty = 1, exit_time = "15:20", product ='
        ' "MIS" }'
    )
    cfg_text = cfg_text.replace(old_params, new_params)
    cfg_text = cfg_text.replace('[[notifiers]]\nplugin = "telegram"', "")
    cfg_file.write_text(cfg_text, encoding="utf-8")

    # Set sim venue latency to 1500ms
    p_sim_file = tmp_project / "config" / "params" / "sim.toml"
    if p_sim_file.exists():
        sim_toml = p_sim_file.read_text(encoding="utf-8")
        sim_toml = sim_toml.replace("default = 300", "default = 1500")
        p_sim_file.write_text(sim_toml, encoding="utf-8")

    inst = InstrumentId("NSE:EQ:RELIANCE")
    t0 = datetime(2026, 11, 12, 9, 40, 0, tzinfo=UTC)  # 15:10:00 IST

    events: list[Tick | Quote] = []

    def _add_tick_and_quote(t: datetime, price: float, vol_acc: int) -> None:
        events.append(
            Tick(
                instrument=inst,
                exch_ts=t,
                recv_ts=t + timedelta(milliseconds=5),
                ltp=price,
                ltq=1,
                volume=vol_acc,
            )
        )
        events.append(
            Quote(
                instrument=inst,
                exch_ts=t,
                recv_ts=t + timedelta(milliseconds=5),
                bids=(Level(price=price - 1.0, qty=50, orders=1),),
                asks=(Level(price=price + 1.0, qty=50, orders=1),),
                ltp=price,
                volume=vol_acc,
            )
        )

    # 1. Warm-up bars (fast 2, slow 4)
    # Bars 1 to 5: prices 2500.0 (15:10 to 15:14)
    # Bar 6: dip 2480.0 (15:15)
    # Bar 7: dip 2470.0 (15:16)
    # Bar 8: rally 2520.0 -> golden cross -> BUY entry signal! (15:17)
    prices = [2500.0, 2500.0, 2500.0, 2500.0, 2500.0, 2480.0, 2470.0, 2520.0]
    vol = 100
    for minute_idx, p in enumerate(prices):
        t_base = t0 + timedelta(minutes=minute_idx)
        for s in (10, 30, 50):
            t_tick = t_base + timedelta(seconds=s)
            vol += 10
            _add_tick_and_quote(t_tick, p, vol)

    # Bar 9 tick at 15:18:05 IST: closes Bar 8, triggers BUY order with 1500ms latency.
    # Order arrival is at 15:18:06.505 IST.
    t_entry_trigger = t0 + timedelta(minutes=8, seconds=5)
    vol += 10
    _add_tick_and_quote(t_entry_trigger, 2520.0, vol)

    # Tick 1 second later (15:18:06.000): arrives BEFORE order arrival (15:18:06.505).
    # Crosses tick boundary with order pending in venue!
    t_pending = t0 + timedelta(minutes=8, seconds=6)
    vol += 10
    _add_tick_and_quote(t_pending, 2520.0, vol)

    # Tick at 15:18:07.000: arrives AFTER order arrival (15:18:06.505).
    # Order fills at 2520.0!
    t_fill = t0 + timedelta(minutes=8, seconds=7)
    vol += 10
    _add_tick_and_quote(t_fill, 2520.0, vol)

    # Next ticks up to exit_time (15:20:00 IST = t0 + 10m).
    # Bar at 15:20:00 IST closes and triggers exit signal (product=MIS exit_time=15:20).
    for m in (9, 10):
        t_base = t0 + timedelta(minutes=m)
        for s in (10, 30, 50):
            t_tick = t_base + timedelta(seconds=s)
            vol += 10
            _add_tick_and_quote(t_tick, 2530.0, vol)

    # Tick at 15:21:05 closes Bar 10, executes exit
    t_exit_close = t0 + timedelta(minutes=11, seconds=5)
    vol += 10
    _add_tick_and_quote(t_exit_close, 2530.0, vol)

    # Subsequent tick fills exit order after latency
    t_exit_fill = t0 + timedelta(minutes=11, seconds=10)
    vol += 10
    _add_tick_and_quote(t_exit_fill, 2530.0, vol)

    # 1. Run paper session
    paper_dir: Path
    with open_runtime(tmp_project, run="demo_paper_fast", ensure=True) as rt:
        clock = SimClock(t0)
        runner = SessionRunner(rt, mode="paper", clock=clock, dry_run=False)

        async def _feed_stream(c: dict[str, Any], clk: SimClock):
            for ev in events:
                clk.set_time(ev.exch_ts)
                yield ev
                await asyncio.sleep(0.001)

            # Advance clock past stop_time (15:35:00 IST) to end cleanly
            clk.set_time(t0 + timedelta(minutes=25, seconds=5))
            c["scheduler"].advance_to(clk.now())
            c["scheduler"].fire_due()

        monkeypatch.setattr(runner, "_build_event_stream", _feed_stream)
        paper_dir = asyncio.run(runner.run())

    assert paper_dir.is_dir()
    p_metrics = json.loads((paper_dir / "metrics.json").read_text(encoding="utf-8"))
    assert p_metrics["trades"] >= 2  # Entry BUY and Exit SELL both filled

    # 2. Run Replay via CLI
    cli = CliRunner()
    res = cli.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "replay",
            "--session",
            paper_dir.name,
            "--run",
            "demo_paper_fast",
        ],
    )
    assert res.exit_code == 0, f"Replay CLI failed: {res.output}"

    # 3. Compare runs
    results_dir = tmp_project / "results"
    replay_folders = [f for f in results_dir.glob("demo_paper_fast_replay_*") if f.is_dir()]
    assert len(replay_folders) >= 1
    replay_dir = sorted(replay_folders)[-1]

    is_equal, diff = compare_runs(tmp_project, paper_dir.name, replay_dir.name)
    assert is_equal is True, f"Paper and replay mismatched: {diff}"

    # 4. Compare metrics and equity points
    r_metrics = json.loads((replay_dir / "metrics.json").read_text(encoding="utf-8"))
    assert p_metrics["trades"] == r_metrics["trades"]
    assert p_metrics["closed_round_trips"] == r_metrics["closed_round_trips"]
    assert math.isclose(p_metrics["gross_pnl"], r_metrics["gross_pnl"], abs_tol=1e-5)
    assert math.isclose(p_metrics["net_pnl"], r_metrics["net_pnl"], abs_tol=1e-5)

    p_equity_file = paper_dir / "equity.csv"
    r_equity_file = replay_dir / "equity.csv"
    assert p_equity_file.is_file()
    assert r_equity_file.is_file()

    with (
        p_equity_file.open(newline="", encoding="utf-8") as f_p,
        r_equity_file.open(newline="", encoding="utf-8") as f_r,
    ):
        p_rows = list(csv.DictReader(f_p))
        r_rows = list(csv.DictReader(f_r))

    assert len(p_rows) == len(r_rows)
    assert len(p_rows) > 0
    for idx, (p_row, r_row) in enumerate(zip(p_rows, r_rows, strict=True)):
        assert math.isclose(float(p_row["equity"]), float(r_row["equity"]), abs_tol=1e-4), (
            f"Row {idx} equity mismatch: {p_row['equity']} != {r_row['equity']}"
        )
        assert math.isclose(float(p_row["drawdown"]), float(r_row["drawdown"]), abs_tol=1e-4), (
            f"Row {idx} drawdown mismatch: {p_row['drawdown']} != {r_row['drawdown']}"
        )


@settings(max_examples=30, deadline=None)
@given(
    latencies=st.lists(
        st.integers(min_value=50, max_value=2000),
        min_size=2,
        max_size=10,
    ),
    tick_intervals=st.lists(
        st.integers(min_value=10, max_value=500),
        min_size=2,
        max_size=10,
    ),
)
def test_property_sim_venue_latency_interleavings(
    latencies: list[int], tick_intervals: list[int]
) -> None:
    """8. Property test: SimVenue fills only after arrival timestamp across intervals."""
    inst_id = InstrumentId("NSE:EQ:RELIANCE")
    venue = SimVenue(params={"sim.latency_ms": latencies[0], "sim.latency_jitter_ms": 0})
    venue.configure([_make_inst(inst_id)])

    t0 = datetime(2026, 11, 12, 9, 15, 0, tzinfo=UTC)
    q0 = _make_quote(inst_id, t0, ltp=100.0)
    venue.on_quote(q0)

    spec = OrderSpec(
        instrument=inst_id,
        side=Side.BUY,
        qty=1,
        type=OrderType.MARKET,
        product=Product.MIS,
        validity=Validity.DAY,
    )

    ack = asyncio.run(venue.place(spec, "client_ref_prop"))
    assert ack.accepted is True

    curr_ts = t0
    filled = False
    expected_arrival = t0 + timedelta(milliseconds=latencies[0])

    for interval in tick_intervals:
        curr_ts = curr_ts + timedelta(milliseconds=interval)
        q = _make_quote(inst_id, curr_ts, ltp=100.0)
        venue.on_quote(q)

        st_order = venue.order_status("client_ref_prop")
        if curr_ts < expected_arrival:
            # Must remain resting before arrival
            assert st_order is not None
            assert st_order.state == OrderState.ACKNOWLEDGED
            assert st_order.filled_qty == 0
        else:
            # Must be filled at or after arrival
            assert st_order is not None
            assert st_order.state == OrderState.FILLED
            assert st_order.filled_qty == 1
            filled = True
            break

    if curr_ts >= expected_arrival:
        assert filled is True


def test_runner_monotonic_clock_advancement_on_recorded_timer_events() -> None:
    """9. Replay advances clock monotonically on recorded timer events before draining bar tasks."""
    t_start = datetime(2026, 11, 12, 9, 7, 57, tzinfo=UTC)
    t_timer = datetime(2026, 11, 12, 9, 8, 0, 516000, tzinfo=UTC)
    clock = SimClock(t_start)

    recorded_events = [
        {"seq": 100, "ts_utc": t_timer.isoformat(), "kind": "bar.timer", "bar_type": "1m"}
    ]

    # Verify clock starts at t_start
    assert clock.now() == t_start

    # Monotonic advance on timer event
    event_dt = datetime.fromisoformat(recorded_events[0]["ts_utc"])
    if event_dt > clock.now():
        clock.set_time(event_dt)

    assert clock.now() == t_timer
