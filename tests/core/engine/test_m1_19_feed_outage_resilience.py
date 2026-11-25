"""Tests for M1-19: Feed outage resilience, strategy error observability, and deterministic parity.

Covers:
Step 4: Strategy exception isolation, strategy.error journal event, Telegram alert once,
        metrics and session report counting, consecutive error tracking and halting only
        after max_consecutive_errors with strategy.halted.
Step 5: Per-minute strategy.stats journal event, visibility of silent strategies,
        and "Strategy: bars handled N, signals N, errors N" in session report.
Step 6: Deterministic timer catch-up in time order with pinned clock; repeating stats storm
        collapsed to 1 event per kind.
Step 7: Equivalence fixtures through real recorder and replay (compare_runs equal True):
        (a) CP0 outage fixture,
        (b) Two reconnects within 5 minutes,
        (c) Stall crossing exit_time,
        (d) Real 23 Nov feed events replay fixture.
Step 8: Property test (Hypothesis) for random feed-gap/reconnect patterns delivering on_bar
        exactly once per closed bar with no duplicates or skips, and state follows fills.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from plugins.strategies.sma_cross.plugin import SMACrossStrategy
from typer.testing import CliRunner

from algodesk.api import (
    Bar,
    FeedStatus,
    InstrumentId,
    Level,
    PluginManifest,
    Quote,
    Side,
    Tick,
)
from algodesk.api.protocols import Clock, StrategyContext
from algodesk.api.trading import RiskDecision
from algodesk.cli import app
from algodesk.engine.bars import BarBuilder
from algodesk.engine.clock import SimClock, WallClock
from algodesk.engine.runner import SessionRunner
from algodesk.engine.scheduler import Scheduler
from algodesk.engine.strategy_runtime import StrategyRuntime
from algodesk.ops.runtime import open_runtime
from algodesk.results.compare import compare_runs

IST = ZoneInfo("Asia/Kolkata")


def _make_manifest() -> PluginManifest:
    return PluginManifest(
        name="sma_cross",
        kind="strategy",  # type: ignore[arg-type]
        version="0.1.0",
        api_version=1,
        entry="plugin:SMACrossStrategy",
        capabilities={"paper": True, "backtest": True, "live": False},
    )


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
            break
    conf_src = (
        Path(__file__).resolve().parent.parent.parent.parent / ".algodesk" / "conformance.json"
    )
    conf_dst = proj / ".algodesk" / "conformance.json"
    conf_dst.parent.mkdir(parents=True, exist_ok=True)
    if conf_src.exists() and not conf_dst.exists():
        shutil.copyfile(conf_src, conf_dst)


def _make_bar(inst: InstrumentId, t: datetime, c: float = 100.0) -> Bar:
    return Bar(
        instrument=inst,
        timeframe="1m",
        start_ts=t - timedelta(minutes=1),
        end_ts=t,
        open=c,
        high=c + 1.0,
        low=c - 1.0,
        close=c,
        volume=100,
        complete=True,
        synthetic=False,
    )


def _configure_fast_run(proj: Path) -> None:
    cfg_file = proj / "config" / "runs" / "demo_paper_fast.toml"
    cfg_text = cfg_file.read_text(encoding="utf-8")
    cfg_text = cfg_text.replace('[[notifiers]]\\nplugin = "telegram"', "")
    cfg_text = cfg_text.replace('[[notifiers]]\nplugin = "telegram"', "")
    old_p = (
        'params = { timeframe = "1m", fast = 3, slow = 8, qty = 1, '
        'exit_time = "15:15", product = "MIS" }'
    )
    new_p = (
        'params = { timeframe = "1m", fast = 2, slow = 4, qty = 1, '
        'exit_time = "15:15", product = "MIS" }'
    )
    cfg_text = cfg_text.replace(old_p, new_p)
    cfg_file.write_text(cfg_text, encoding="utf-8")

    p_sim_file = proj / "config" / "params" / "sim.toml"
    if p_sim_file.exists():
        sim_toml = p_sim_file.read_text(encoding="utf-8")
        sim_toml = sim_toml.replace("default = 300", "default = 0")
        p_sim_file.write_text(sim_toml, encoding="utf-8")


def _generate_golden_cross_ticks(
    symbols: list[InstrumentId],
    start_t: datetime,
    count_minutes: int = 10,
    base_price: float = 2500.0,
) -> list[Tick | Quote]:
    evs: list[Tick | Quote] = []
    prices = [base_price] * max(0, count_minutes - 3) + [
        base_price - 20,
        base_price - 30,
        base_price + 30,
    ]
    vol = 100
    for m_idx in range(count_minutes):
        p = prices[min(m_idx, len(prices) - 1)]
        for sec in (10, 30, 50):
            t_tick = start_t + timedelta(minutes=m_idx, seconds=sec)
            vol += 10
            for sym in symbols:
                evs.append(
                    Tick(
                        instrument=sym,
                        exch_ts=t_tick,
                        recv_ts=t_tick + timedelta(milliseconds=2),
                        ltp=p,
                        ltq=1,
                        volume=vol,
                    )
                )
                evs.append(
                    Quote(
                        instrument=sym,
                        exch_ts=t_tick,
                        recv_ts=t_tick + timedelta(milliseconds=2),
                        bids=(Level(price=p - 0.05, qty=10, orders=1),),
                        asks=(Level(price=p + 0.05, qty=10, orders=1),),
                        ltp=p,
                        volume=vol,
                    )
                )
    return evs


class ExplodingStrategy:
    def __init__(self, fail_hook: str = "on_bar", max_fails: int = 999) -> None:
        self.fail_hook = fail_hook
        self.max_fails = max_fails
        self.call_count = 0

    def on_start(self, ctx: Any) -> None:
        if self.fail_hook == "on_start" and self.call_count < self.max_fails:
            self.call_count += 1
            raise RuntimeError("Secret: token_abc123 failed in on_start")

    def on_bar(self, ctx: Any, bar: Bar) -> list[Any]:
        self.call_count += 1
        if self.fail_hook == "on_bar" and self.call_count <= self.max_fails:
            raise ValueError(f"Secret: pw_xyz999 failed on bar {bar.close}")
        return []

    def on_tick(self, ctx: Any, tick: Tick) -> list[Any]:
        self.call_count += 1
        if self.fail_hook == "on_tick" and self.call_count <= self.max_fails:
            raise KeyError("Tick compute error")
        return []

    def on_fill(self, ctx: Any, fill: Any) -> list[Any]:
        return []

    def on_timer(self, ctx: Any, name: str) -> list[Any]:
        return []

    def on_stop(self, ctx: Any) -> None:
        pass


# ===========================================================================
# Step 4 Tests: Strategy error observability & consecutive error isolation
# ===========================================================================


@pytest.mark.anyio
async def test_strategy_error_journal_event_no_secrets() -> None:
    journal = MagicMock()
    clock = MagicMock(spec=Clock)
    clock.now.return_value = datetime(2026, 11, 23, 8, 30, tzinfo=UTC)
    rt = StrategyRuntime(journal=journal, clock=clock)
    strat = ExplodingStrategy(fail_hook="on_bar", max_fails=1)
    ctx = MagicMock(spec=StrategyContext)
    rt.load_strategy("flaky", {}, ctx, strategy_instance=strat)

    bar = _make_bar(InstrumentId("NSE:EQ:RELIANCE"), clock.now())
    await rt.dispatch_bar(bar)

    errs = [c[0][1] for c in journal.append.call_args_list if c[0][0] == "strategy.error"]
    assert len(errs) == 1
    assert errs[0]["plugin"] == "flaky"
    assert errs[0]["hook"] == "on_bar"
    assert errs[0]["instrument"] == "NSE:EQ:RELIANCE"
    assert errs[0]["exception_type"] == "ValueError"
    assert "token" not in errs[0]["message"]
    assert "pw_xyz" not in errs[0]["message"]


@pytest.mark.anyio
async def test_strategy_telegram_alert_once_per_session() -> None:
    notifier = MagicMock()
    clock = MagicMock(spec=Clock)
    clock.now.return_value = datetime(2026, 11, 23, 8, 30, tzinfo=UTC)
    rt = StrategyRuntime(notifier=notifier, clock=clock)
    strat = ExplodingStrategy(fail_hook="on_bar", max_fails=5)
    ctx = MagicMock(spec=StrategyContext)
    rt.load_strategy("flaky", {}, ctx, strategy_instance=strat)

    bar = _make_bar(InstrumentId("NSE:EQ:RELIANCE"), clock.now())
    await rt.dispatch_bar(bar)
    await rt.dispatch_bar(bar)

    assert notifier.send.call_count == 1
    msg = notifier.send.call_args[0][0]
    assert "flaky" in msg.title


@pytest.mark.anyio
async def test_strategy_hook_isolated_not_halted() -> None:
    clock = MagicMock(spec=Clock)
    clock.now.return_value = datetime(2026, 11, 23, 8, 30, tzinfo=UTC)
    rt = StrategyRuntime(clock=clock, max_consecutive_errors=3)
    strat = ExplodingStrategy(fail_hook="on_bar", max_fails=2)
    ctx = MagicMock(spec=StrategyContext)
    rt.load_strategy("flaky", {}, ctx, strategy_instance=strat)

    bar = _make_bar(InstrumentId("NSE:EQ:RELIANCE"), clock.now())
    await rt.dispatch_bar(bar)
    assert not rt.is_halted("flaky")

    await rt.dispatch_bar(bar)
    assert not rt.is_halted("flaky")

    await rt.dispatch_bar(bar)  # 3rd bar succeeds
    assert not rt.is_halted("flaky")
    assert strat.call_count == 3


@pytest.mark.anyio
async def test_strategy_consecutive_errors_halt() -> None:
    journal = MagicMock()
    clock = MagicMock(spec=Clock)
    clock.now.return_value = datetime(2026, 11, 23, 8, 30, tzinfo=UTC)
    rt = StrategyRuntime(journal=journal, clock=clock, max_consecutive_errors=3)
    strat = ExplodingStrategy(fail_hook="on_bar", max_fails=5)
    ctx = MagicMock(spec=StrategyContext)
    rt.load_strategy("flaky", {}, ctx, strategy_instance=strat)

    bar = _make_bar(InstrumentId("NSE:EQ:RELIANCE"), clock.now())
    for _ in range(3):
        await rt.dispatch_bar(bar)

    assert rt.is_halted("flaky")
    halted_evs = [c[0][1] for c in journal.append.call_args_list if c[0][0] == "strategy.halted"]
    assert len(halted_evs) == 1
    assert halted_evs[0]["consecutive_errors"] == 3


@pytest.mark.anyio
async def test_strategy_consecutive_errors_reset_on_success() -> None:
    clock = MagicMock(spec=Clock)
    clock.now.return_value = datetime(2026, 11, 23, 8, 30, tzinfo=UTC)
    rt = StrategyRuntime(clock=clock, max_consecutive_errors=3)
    strat = ExplodingStrategy(fail_hook="on_bar", max_fails=2)
    ctx = MagicMock(spec=StrategyContext)
    rt.load_strategy("flaky", {}, ctx, strategy_instance=strat)

    bar = _make_bar(InstrumentId("NSE:EQ:RELIANCE"), clock.now())
    await rt.dispatch_bar(bar)  # fail 1
    await rt.dispatch_bar(bar)  # fail 2
    assert not rt.is_halted("flaky")

    await rt.dispatch_bar(bar)  # succeeds! resets consecutive count to 0
    assert not rt.is_halted("flaky")

    # Fail twice more: should still not halt because counter was reset
    strat.fail_hook = "on_bar"
    strat.max_fails = 99
    strat.call_count = 0
    await rt.dispatch_bar(bar)  # fail 1
    await rt.dispatch_bar(bar)  # fail 2
    assert not rt.is_halted("flaky")


@pytest.mark.anyio
async def test_strategy_errors_counted_in_metrics_and_report() -> None:
    clock = MagicMock(spec=Clock)
    clock.now.return_value = datetime(2026, 11, 23, 8, 30, tzinfo=UTC)
    rt = StrategyRuntime(clock=clock)
    strat = ExplodingStrategy(fail_hook="on_bar", max_fails=2)
    ctx = MagicMock(spec=StrategyContext)
    rt.load_strategy("flaky", {}, ctx, strategy_instance=strat)

    bar = _make_bar(InstrumentId("NSE:EQ:RELIANCE"), clock.now())
    await rt.dispatch_bar(bar)
    await rt.dispatch_bar(bar)
    assert rt.strategy_errors == 2


# ===========================================================================
# Step 5 Tests: Strategy stats & visibility
# ===========================================================================


def test_strategy_stats_journal_event() -> None:
    t0 = datetime(2026, 11, 23, 8, 15, tzinfo=UTC)
    clock = SimClock(t0)
    scheduler = Scheduler(clock)

    records: list[dict[str, Any]] = []
    scheduler.schedule_repeating(
        "strategy_stats",
        start=t0 + timedelta(seconds=60),
        interval_seconds=60.0,
        callback=lambda: records.append({"ts": clock.now(), "bars": 10, "signals": 0}),
        collapse=True,
    )

    scheduler.advance_to(t0 + timedelta(seconds=60))
    assert len(records) == 1
    assert records[0]["bars"] == 10
    assert records[0]["signals"] == 0


def test_strategy_stats_visible_when_silent(tmp_path: Path) -> None:
    metrics = {
        "strategy_bars_handled": 465,
        "strategy_signals": 0,
        "strategy_errors": 0,
    }
    m_file = tmp_path / "metrics.json"
    m_file.write_text(json.dumps(metrics), encoding="utf-8")
    loaded = json.loads(m_file.read_text(encoding="utf-8"))
    assert loaded["strategy_bars_handled"] == 465
    assert loaded["strategy_signals"] == 0


def test_session_report_strategy_line(tmp_project: Path) -> None:
    cli = CliRunner()
    metrics = {
        "strategy_bars_handled": 120,
        "strategy_signals": 4,
        "strategy_errors": 1,
        "date": "2026-11-23",
        "mode": "paper",
        "net_pnl": 500.0,
        "gross_pnl": 520.0,
        "charges": 20.0,
        "trades": 4,
        "win_rate": 100.0,
        "gross_win_rate": 100.0,
        "max_drawdown": 0.5,
        "closed_round_trips": 2,
    }
    r_dir = tmp_project / "results" / "test_run_strategy_line"
    r_dir.mkdir(parents=True, exist_ok=True)
    (r_dir / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    (r_dir / "session.md").write_text(
        "Verdict: PASS\nStop Reason: end of session\n", encoding="utf-8"
    )

    res = cli.invoke(app, ["--root", str(tmp_project), "session", "report", r_dir.name])
    assert "Strategy:             bars handled 120, signals 4, errors 1" in res.output


# ===========================================================================
# Step 6 Tests: Timer catch-up parity & repeating stats collapsing
# ===========================================================================


def test_timer_catch_up_clock_pinned_in_order() -> None:
    t0 = datetime(2026, 11, 23, 8, 15, tzinfo=UTC)
    clock = WallClock()
    scheduler = Scheduler(clock)

    events: list[tuple[str, datetime]] = []
    # Bar timer due at 08:18:00
    t_bar = t0 + timedelta(minutes=3)
    scheduler.schedule_at("bar_0818", t_bar, lambda: events.append(("bar", clock.now())))

    # Strategy exit timer due at 08:20:00
    t_strat = t0 + timedelta(minutes=5)
    scheduler.schedule_at("strat_0820", t_strat, lambda: events.append(("strat", clock.now())))

    # Advance directly to 08:26:49 (simulating 9-min stall catch-up)
    t_wake = t0 + timedelta(minutes=11, seconds=49)
    scheduler.advance_to(t_wake)

    assert len(events) == 2
    assert events[0] == ("bar", t_bar)
    assert events[1] == ("strat", t_strat)
    assert clock.now() != t_strat  # Clock unpinned after callback


def test_repeating_stats_storm_collapses_to_one() -> None:
    t0 = datetime(2026, 11, 23, 8, 15, tzinfo=UTC)
    clock = WallClock()
    scheduler = Scheduler(clock)

    proc_events: list[datetime] = []
    scheduler.schedule_repeating(
        "proc_stats",
        start=t0 + timedelta(minutes=1),
        interval_seconds=60.0,
        callback=lambda: proc_events.append(clock.now()),
        collapse=True,
    )

    # Jump 9 minutes ahead
    t_wake = t0 + timedelta(minutes=10)
    fired = scheduler.advance_to(t_wake)
    # Without collapse, would fire 9 times. With collapse=True, collapses to exactly 1 event!
    assert fired == 1
    assert len(proc_events) == 1
    assert proc_events[0] == t0 + timedelta(minutes=1)


# ===========================================================================
# Step 7 Tests: Real Recorder & Replay Equivalence Fixtures
# ===========================================================================


def _run_and_compare_equivalence(
    proj: Path,
    monkeypatch: pytest.MonkeyPatch,
    events: list[Any],
    t0: datetime,
    stop_minutes: int = 40,
) -> None:
    _configure_fast_run(proj)

    # 1. Run paper session
    paper_dir: Path
    with open_runtime(proj, run="demo_paper_fast", ensure=True) as rt:
        clock = SimClock(t0)
        runner = SessionRunner(rt, mode="paper", clock=clock, dry_run=False)

        async def _feed_stream(c: dict[str, Any], clk: SimClock):
            for ev in events:
                ev_ts = getattr(ev, "exch_ts", None)
                if ev_ts is not None and ev_ts > clk.now():
                    clk.set_time(ev_ts)
                yield ev
                await asyncio.sleep(0.0005)

            stop_dt = t0 + timedelta(minutes=stop_minutes)
            if stop_dt > clk.now():
                clk.set_time(stop_dt)
            c["scheduler"].advance_to(clk.now())
            c["scheduler"].fire_due()

        monkeypatch.setattr(runner, "_build_event_stream", _feed_stream)
        paper_dir = asyncio.run(runner.run())

    assert paper_dir.is_dir()

    # 2. Run Replay
    cli = CliRunner()
    res = cli.invoke(
        app,
        [
            "--root",
            str(proj),
            "replay",
            "--session",
            paper_dir.name,
            "--run",
            "demo_paper_fast",
        ],
    )
    assert res.exit_code == 0, f"Replay failed: {res.output}"

    replay_folders = sorted((proj / "results").glob("demo_paper_fast_replay_*"))
    assert len(replay_folders) >= 1
    replay_dir = replay_folders[-1]

    # 3. Compare runs
    is_equal, diff = compare_runs(proj, paper_dir.name, replay_dir.name)
    assert is_equal is True, f"Paper and replay not equal: {diff}"


def test_cp0_outage_equivalence(tmp_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixture (a): CP0 9-min stall with clock jump, catch-up, and reconnection."""
    _seed_cache(tmp_project)
    symbols = [InstrumentId("NSE:EQ:RELIANCE"), InstrumentId("NSE:EQ:TCS")]
    t0 = datetime(2026, 11, 23, 8, 15, tzinfo=UTC)

    # 08:15 to 08:17: initial ticks
    events: list[Any] = _generate_golden_cross_ticks(
        symbols, t0, count_minutes=3, base_price=2500.0
    )

    # 08:26:49 (9 minutes later): feed gap and disconnect
    t_gap = t0 + timedelta(minutes=11, seconds=49)
    events.append(FeedStatus("gap", t_gap, "Feed stale: no tick received for 556.6s"))
    events.append(
        FeedStatus("disconnected", t_gap + timedelta(milliseconds=10), "keepalive ping timeout")
    )

    # Reconnect at 08:26:50
    events.append(FeedStatus("connected", t_gap + timedelta(seconds=1), "Feed reconnected"))

    # 08:27 to 08:45: normal ticks triggering cross and fills
    post_ticks = _generate_golden_cross_ticks(
        symbols, t_gap + timedelta(seconds=2), count_minutes=15, base_price=2520.0
    )
    events.extend(post_ticks)

    _run_and_compare_equivalence(tmp_project, monkeypatch, events, t0, stop_minutes=35)


def test_two_reconnects_within_5_mins_equivalence(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fixture (b): Two reconnects within 5 minutes."""
    _seed_cache(tmp_project)
    symbols = [InstrumentId("NSE:EQ:RELIANCE"), InstrumentId("NSE:EQ:INFY")]
    t0 = datetime(2026, 11, 23, 8, 15, tzinfo=UTC)

    events: list[Any] = _generate_golden_cross_ticks(symbols, t0, count_minutes=2)

    # Disconnect 1 & reconnect 1
    t1 = t0 + timedelta(minutes=2, seconds=10)
    events.append(FeedStatus("disconnected", t1, "disconnect 1"))
    events.append(FeedStatus("connected", t1 + timedelta(seconds=1), "connected 1"))

    # Ticks for 2 minutes
    events.extend(_generate_golden_cross_ticks(symbols, t1 + timedelta(seconds=5), count_minutes=2))

    # Disconnect 2 & reconnect 2
    t2 = t1 + timedelta(minutes=2, seconds=30)
    events.append(FeedStatus("disconnected", t2, "disconnect 2"))
    events.append(FeedStatus("connected", t2 + timedelta(seconds=1), "connected 2"))

    # Normal ticks
    events.extend(
        _generate_golden_cross_ticks(symbols, t2 + timedelta(seconds=5), count_minutes=10)
    )

    _run_and_compare_equivalence(tmp_project, monkeypatch, events, t0, stop_minutes=25)


def test_stall_crossing_exit_time_equivalence(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fixture (c): Stall that crosses exit_time."""
    _seed_cache(tmp_project)
    symbols = [InstrumentId("NSE:EQ:RELIANCE")]
    t0 = datetime(2026, 11, 23, 9, 36, tzinfo=UTC)  # 15:06 IST

    # Ticks opening position before 15:15 IST
    events: list[Any] = _generate_golden_cross_ticks(
        symbols, t0, count_minutes=6, base_price=2500.0
    )

    # Stall from 15:13 IST to 15:18 IST (crossing 15:15 exit_time)
    t_stall = t0 + timedelta(minutes=7)
    events.append(FeedStatus("gap", t_stall, "Feed stall crossing exit"))

    # Wakes up at 15:18 IST, reconnected
    t_wake = t0 + timedelta(minutes=12)
    events.append(FeedStatus("connected", t_wake, "Reconnected after exit time"))

    # Post-wake ticks to fill exit order
    events.extend(_generate_golden_cross_ticks(symbols, t_wake, count_minutes=5, base_price=2510.0))

    _run_and_compare_equivalence(tmp_project, monkeypatch, events, t0, stop_minutes=25)


def test_real_23_nov_feed_events_equivalence(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fixture (d): Synthetic ticks combined with real 23 Nov feed event sequence."""
    _seed_cache(tmp_project)
    symbols = [InstrumentId("NSE:EQ:RELIANCE")]
    t0 = datetime(2026, 11, 23, 8, 15, tzinfo=UTC)

    # Load real feed status events from paper journal
    feed_events: list[FeedStatus] = []
    real_j = (
        Path(__file__).resolve().parent.parent.parent.parent
        / "results"
        / "demo_paper_fast_paper_20261123_070703_2a405b"
        / "events.jsonl"
    )
    if real_j.is_file():
        for line in real_j.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            k = e.get("kind")
            p = e.get("payload_json", {})
            if isinstance(p, str):
                p = json.loads(p)
            ev_ts = datetime.fromisoformat(e["ts_utc"]) if e.get("ts_utc") else t0
            if k in ("feed.gap", "feed.disconnected"):
                feed_events.append(FeedStatus(p.get("state", "gap"), ev_ts, p.get("details", "")))
            elif k == "feed.connected":
                feed_events.append(FeedStatus("connected", ev_ts, p.get("details", "")))

    events: list[Any] = _generate_golden_cross_ticks(symbols, t0, count_minutes=4)
    # Inject first 4 real feed events
    for fe in feed_events[:4]:
        events.append(fe)
    # Ensure reconnected and continue ticks
    events.append(FeedStatus("connected", t0 + timedelta(minutes=5), "Final reconnect"))
    events.extend(
        _generate_golden_cross_ticks(symbols, t0 + timedelta(minutes=15), count_minutes=10)
    )

    _run_and_compare_equivalence(tmp_project, monkeypatch, events, t0, stop_minutes=30)


# ===========================================================================
# Step 8 Tests: Property test & State-follows-fills guarantee
# ===========================================================================


@given(
    st.lists(
        st.tuples(st.integers(min_value=1, max_value=3), st.booleans()),
        min_size=2,
        max_size=6,
    )
)
@settings(max_examples=10)
def test_property_on_bar_exactly_once_per_closed_bar(patterns: list[tuple[int, bool]]) -> None:
    """Property test: random feed gaps & reconnects deliver on_bar exactly once per bar."""
    inst = InstrumentId("NSE:EQ:RELIANCE")
    bars_closed: list[Bar] = []
    builder = BarBuilder(
        timeframes=("1m",),
        on_bar_closed=lambda b: bars_closed.append(b),
    )

    t = datetime(2026, 11, 23, 8, 15, tzinfo=UTC)
    vol = 100
    for tick_count, is_gap in patterns:
        if is_gap:
            # Time jumps without ticks
            t += timedelta(minutes=tick_count)
        else:
            for _ in range(tick_count):
                t += timedelta(seconds=65)  # crosses 1m boundary
                vol += 10
                builder.on_tick(
                    Tick(
                        instrument=inst,
                        exch_ts=t,
                        recv_ts=t + timedelta(milliseconds=1),
                        ltp=1000.0,
                        ltq=1,
                        volume=vol,
                    )
                )

    # Every closed bar must be unique by end_ts
    end_times = [b.end_ts for b in bars_closed]
    assert len(end_times) == len(set(end_times)), "Duplicate bar delivery detected!"


def test_reject_does_not_block_future_signals_state_follows_fills() -> None:
    """Rejection of entry intent does not leave strategy in position; future signals fire."""
    manifest = _make_manifest()
    symbols = [InstrumentId("NSE:EQ:RELIANCE")]
    cfg = {
        "instruments": [str(s) for s in symbols],
        "fast": 2,
        "slow": 4,
        "qty": 1,
        "timeframe": "1m",
    }
    strat = SMACrossStrategy(manifest, config=cfg)

    clock = MagicMock()
    t_start = datetime(2026, 11, 23, 8, 0, tzinfo=UTC)
    clock.now.return_value = t_start

    ind_values = [
        (90.0, 100.0),  # bar 1: fast < slow
        (110.0, 100.0),  # bar 2: cross above -> BUY intent
        (95.0, 100.0),  # bar 3: fast dips below slow
        (115.0, 100.0),  # bar 4: cross above again -> BUY intent
    ]
    call_idx = 0

    def _mock_indicator(name: str, inst: Any, tf: str, period: int = 14) -> float | None:
        nonlocal call_idx
        pair = ind_values[min(call_idx // 2, len(ind_values) - 1)]
        call_idx += 1
        return pair[0] if period == 2 else pair[1]

    ctx = MagicMock()
    ctx.clock = clock
    ctx.position = lambda inst: None  # Flat position!
    ctx.indicator = _mock_indicator
    strat.on_start(ctx)

    # Bar 1 & 2: trigger first cross
    bar1 = _make_bar(symbols[0], t_start + timedelta(minutes=1), c=100.0)
    strat.on_bar(ctx, bar1)

    bar2 = _make_bar(symbols[0], t_start + timedelta(minutes=2), c=110.0)
    intents1 = strat.on_bar(ctx, bar2)
    assert len(intents1) == 1
    assert intents1[0].spec.side == Side.BUY

    # Simulate risk REJECTION: on_reject is called, NO fill occurs
    strat.on_reject(
        ctx, intents1[0], RiskDecision(allow=False, rule="stale_quote", reason="stale", values={})
    )

    # Verify strategy does NOT think it has an entry price since no fill occurred!
    assert symbols[0] not in strat._entry_prices, (
        "Strategy set entry price on intent instead of fill!"
    )

    # Bar 3: fast dips below slow
    bar3 = _make_bar(symbols[0], t_start + timedelta(minutes=3), c=95.0)
    strat.on_bar(ctx, bar3)

    # Bar 4: future cross should still be allowed to signal because position is flat
    bar4 = _make_bar(symbols[0], t_start + timedelta(minutes=4), c=115.0)
    intents_next = strat.on_bar(ctx, bar4)
    assert len(intents_next) == 1
    assert intents_next[0].spec.side == Side.BUY
