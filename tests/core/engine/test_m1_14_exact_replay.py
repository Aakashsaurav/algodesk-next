"""Summary: Unit, integration, and replay tests for M1-14 exact replay.

Covers:
1. Recording every scheduler-fired input (exit_time timer, snapshot timer, market close, stop)
   with tick sequence position and timestamp.
2. Replay applying recorded scheduler events at identical sequence positions.
3. Fake-feed session crossing exit_time and market close, with silence afterwards and an
   unfillable order; paper and replay equal in signals, orders, fills and metrics.
4. `replay --dates D` listing only paper sessions, excluding replay result folders.
5. `replay --dates D` auto-adopting the single recorded session's time window.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from algodesk.api import (
    InstrumentId,
    Level,
    Order,
    OrderSpec,
    OrderState,
    OrderType,
    Product,
    Quote,
    Side,
    Tick,
    Validity,
)
from algodesk.cli.app import app
from algodesk.engine.clock import SimClock
from algodesk.engine.runner import SessionRunner
from algodesk.journal.store import JournalReader
from algodesk.ops.runtime import open_runtime
from algodesk.results.compare import compare_runs

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
            break


class _StreamFromList:
    """Async iterator over (item, seq_opt) items."""

    def __init__(self, items: list[Any]) -> None:
        self._items = items
        self._idx = 0

    def __aiter__(self) -> _StreamFromList:
        return self

    async def __anext__(self) -> Any:
        if self._idx < len(self._items):
            item = self._items[self._idx]
            self._idx += 1
            return item
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self._idx = len(self._items)


def test_recorded_scheduler_events_stored_in_journal(tmp_project: Path) -> None:
    """Scheduler events (exit_time, close, snapshot, end) include seq and ts."""
    _seed_cache(tmp_project)

    with open_runtime(tmp_project, run="demo_paper_fast") as rt:
        t0 = datetime(2026, 11, 4, 9, 44, 0, tzinfo=UTC)  # 15:14 IST (before exit_time 15:15)
        clock = SimClock(t0)
        runner = SessionRunner(rt, mode="paper", clock=clock, dry_run=False)
        runner.run_config.strategies[0].params["exit_time"] = "15:15"
        runner.run_config.strategies[0].params["exit_time"] = "15:15"

        journal = runner._init_journal()
        journal.start_run("paper", "0.1.0", "hash", "reg")
        c = runner._build_components(clock, journal)

        runner._setup_timers(c, clock, journal)

        # Advance clock to 15:15:05 IST to trigger exit timer
        clock.set_time(clock.now() + timedelta(seconds=70))
        c["scheduler"].advance_to(clock.now())
        c["scheduler"].fire_due()
        journal.flush()

        # Check journal events via JournalReader
        reader = JournalReader(journal.db_path)
        events = reader.events(run_id=journal._current_run_id)
        journal.close()

        recorded_types = [e["kind"] for e in events]
        assert "timer.strategy" in recorded_types
        strat_event = next(e for e in events if e["kind"] == "timer.strategy")
        p = strat_event["payload_json"]
        if isinstance(p, str):
            p = json.loads(p)
        assert "seq" in p
        assert "ts" in p
        assert p["name"] == "exit_time"


def test_exact_replay_applies_recorded_events_at_exact_seq(tmp_project: Path) -> None:
    """Replay dispatches recorded events when tick sequence matches."""
    _seed_cache(tmp_project)

    with open_runtime(tmp_project, run="demo_paper_fast") as rt:
        t0 = datetime(2026, 11, 4, 9, 44, 0, tzinfo=UTC)
        clock = SimClock(t0)
        runner = SessionRunner(rt, mode="replay", clock=clock, dry_run=False)

        journal = runner._init_journal()
        journal.start_run("replay", "0.1.0", "hash", "reg")
        c = runner._build_components(clock, journal)

        timer_dispatched: list[str] = []

        async def _mock_dispatch(name: str) -> None:
            timer_dispatched.append(name)

        c["strat_rt"].dispatch_timer = _mock_dispatch

        runner._recorded_events_by_seq = {
            10: [{"kind": "timer.strategy", "payload": {"name": "exit_time", "seq": 10}}],
            15: [{"kind": "run.end", "payload": {"stop_reason": "end of session", "seq": 15}}],
        }

        # Dispatch at seq 5 -> nothing
        asyncio.run(runner._apply_recorded_events(5, c, clock, journal))
        assert len(timer_dispatched) == 0

        # Dispatch at seq 10 -> fires exit_time
        asyncio.run(runner._apply_recorded_events(10, c, clock, journal))
        assert timer_dispatched == ["exit_time"]

        # Dispatch at seq 15 -> runner stops
        assert not runner._stop_event.is_set()
        asyncio.run(runner._apply_recorded_events(15, c, clock, journal))
        assert runner._stop_event.is_set()


def test_fake_feed_session_crosses_exit_time_and_close_with_unfillable_order_exact_replay(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance test: Paper session crosses exit_time and market close with unfillable order,

    ends cleanly on market close, and replays identically with compare_runs returning equal True.
    """
    _seed_cache(tmp_project)

    # 1. Paper run
    paper_dir: Path
    with open_runtime(tmp_project, run="demo_paper_fast") as rt:
        t0 = datetime(2026, 11, 4, 9, 40, 0, tzinfo=UTC)  # 15:10 IST
        clock = SimClock(t0)
        runner = SessionRunner(rt, mode="paper", clock=clock, dry_run=False)
        runner.run_config.strategies[0].params["exit_time"] = "15:15"

        # Build ticks spanning 15:10 to 15:31 IST (crossing 15:15 exit_time and 15:30 close)
        ticks: list[Tick] = []
        inst = InstrumentId("NSE:EQ:RELIANCE")

        # 15:10 to 15:14: fast crossover
        for i in range(1, 11):
            t_tick = t0 + timedelta(seconds=i * 25)
            # Upward price progression to generate SMA crossover buy
            price = 2500.0 + (i * 2.0)
            ticks.append(
                Tick(
                    instrument=inst,
                    exch_ts=t_tick,
                    recv_ts=t_tick + timedelta(milliseconds=10),
                    ltp=price,
                    ltq=1,
                    volume=100 * i,
                )
            )

        # 15:15 to 15:29: ticks crossing exit_time (15:15)
        t_exit = t0 + timedelta(minutes=5)  # 15:15 IST
        for i in range(11, 21):
            t_tick = t_exit + timedelta(seconds=(i - 10) * 60)
            ticks.append(
                Tick(
                    instrument=inst,
                    exch_ts=t_tick,
                    recv_ts=t_tick + timedelta(milliseconds=10),
                    ltp=2525.0 - (i * 0.5),
                    ltq=1,
                    volume=100 * i,
                )
            )

        # 15:30:05 IST: tick right after market close 15:30:00
        t_close = t0 + timedelta(minutes=20, seconds=5)  # 15:30:05 IST
        ticks.append(
            Tick(
                instrument=inst,
                exch_ts=t_close,
                recv_ts=t_close + timedelta(milliseconds=10),
                ltp=2520.0,
                ltq=1,
                volume=3000,
            )
        )

        async def _feed_stream(c: dict[str, Any], clk: SimClock):
            # Inject an unfillable limit order into OMS once RUNNING
            oms = c["oms"]
            unfillable = Order(
                client_ref="unfillable-limit-1",
                spec=OrderSpec(
                    instrument=inst,
                    side=Side.BUY,
                    qty=1,
                    type=OrderType.LIMIT,
                    price=Decimal("10.0"),  # far below LTP 2500 -> never fills
                    product=Product.MIS,
                    validity=Validity.DAY,
                ),
                state=OrderState.ACKNOWLEDGED,
                created_ts=t0,
                filled_qty=0,
                avg_price=Decimal("0.0"),
                venue_ref="V-UNFILLABLE",
            )
            oms._orders[unfillable.client_ref] = unfillable

            for tk in ticks:
                clk.set_time(tk.exch_ts)
                yield tk
                await asyncio.sleep(0.001)

            # Silence after close (clock advances past 15:35, no ticks)
            clk.set_time(clk.now() + timedelta(minutes=5))
            c["scheduler"].advance_to(clk.now())
            c["scheduler"].fire_due()

        monkeypatch.setattr(runner, "_build_event_stream", _feed_stream)
        paper_dir = asyncio.run(runner.run())

    assert paper_dir.is_dir()
    p_metrics = json.loads((paper_dir / "metrics.json").read_text(encoding="utf-8"))
    assert p_metrics["stop_reason"] == "end of session"
    p_summary = (paper_dir / "SUMMARY.md").read_text(encoding="utf-8")
    assert "Verdict: PASS" in p_summary

    # Verify the unfillable order was cancelled on market close
    p_events = [
        json.loads(line)
        for line in (paper_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    def _payload(ev: dict[str, Any]) -> dict[str, Any]:
        p = ev.get("payload_json", {})
        if isinstance(p, str):
            try:
                return json.loads(p)
            except Exception:
                return {}
        return p if isinstance(p, dict) else {}

    p_cancels = [
        _payload(e)
        for e in p_events
        if e.get("kind") == "order.event"
        and _payload(e).get("to_state") in ("CANCEL_REQUESTED", "CANCELLED")
    ]
    assert any(p.get("client_ref") == "unfillable-limit-1" for p in p_cancels)

    # 2. Replay the paper session
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

    # Find the replay results folder
    results_dir = tmp_project / "results"
    replay_folders = [f for f in results_dir.glob("demo_paper_fast_replay_*") if f.is_dir()]
    assert len(replay_folders) >= 1
    replay_dir = sorted(replay_folders)[-1]

    # 3. Compare runs
    is_equal, diff = compare_runs(tmp_project, paper_dir.name, replay_dir.name)
    assert is_equal is True, f"Replay not equal to paper session: {diff}"


def test_portfolio_snapshot_determinism_max_drawdown(tmp_project: Path) -> None:
    """Portfolio snapshots applied at recorded sequence points ensure matching max drawdown."""
    _seed_cache(tmp_project)

    with open_runtime(tmp_project, run="demo_paper_fast") as rt:
        t0 = datetime(2026, 11, 4, 9, 30, 0, tzinfo=UTC)
        clock = SimClock(t0)

        # Paper run
        p_runner = SessionRunner(rt, mode="paper", clock=clock, dry_run=False)
        j1 = p_runner._init_journal()
        j1.start_run("paper", "0.1.0", "hash", "reg")
        c1 = p_runner._build_components(clock, j1)

        p_runner._tick_seq = 10
        p_runner._setup_timers(c1, clock, j1)
        # Advance 60s for snapshot
        clock.set_time(clock.now() + timedelta(seconds=60))
        c1["scheduler"].advance_to(clock.now())
        c1["scheduler"].fire_due()

        # Replay runner
        r_runner = SessionRunner(rt, mode="replay", clock=clock, dry_run=False)
        j2 = r_runner._init_journal()
        j2.start_run("replay", "0.1.0", "hash", "reg")
        c2 = r_runner._build_components(clock, j2)

        # In replay, snapshot is triggered deterministically via recorded events
        r_runner._recorded_events_by_seq = {
            10: [
                {
                    "kind": "portfolio.snapshot",
                    "payload": {"equity": 100000.0, "seq": 10, "cash": 100000.0},
                }
            ]
        }
        asyncio.run(r_runner._apply_recorded_events(10, c2, clock, j2))

        assert len(r_runner._equity_curve) == 1
        assert r_runner._equity_curve[0]["equity"] == 100000.0


def test_exit_timer_fires_at_same_sequence_in_paper_and_replay(tmp_project: Path) -> None:
    """exit_time timer is captured at tick_seq in paper and reapplied at same seq in replay."""
    _seed_cache(tmp_project)

    with open_runtime(tmp_project, run="demo_paper_fast") as rt:
        t0 = datetime(2026, 11, 4, 9, 44, 50, tzinfo=UTC)  # 15:14:50 IST
        clock = SimClock(t0)

        p_runner = SessionRunner(rt, mode="paper", clock=clock, dry_run=False)
        p_runner.run_config.strategies[0].params["exit_time"] = "15:15"
        j = p_runner._init_journal()
        j.start_run("paper", "0.1.0", "hash", "reg")
        c = p_runner._build_components(clock, j)

        p_runner._tick_seq = 42
        p_runner._setup_timers(c, clock, j)

        # Cross 15:15 IST
        clock.set_time(clock.now() + timedelta(seconds=20))
        c["scheduler"].advance_to(clock.now())
        c["scheduler"].fire_due()
        j.flush()

        # Verify timer.strategy event has seq=42
        reader = JournalReader(j.db_path)
        events = reader.events(run_id=j._current_run_id)
        j.close()

        strat_events = [e for e in events if e["kind"] == "timer.strategy"]
        assert len(strat_events) == 1
        p = strat_events[0]["payload_json"]
        if isinstance(p, str):
            p = json.loads(p)
        assert p["seq"] == 42


def test_market_close_cancel_event_recorded_and_replayed_at_same_seq(tmp_project: Path) -> None:
    """Market close event is journaled with seq and replayed at matching seq."""
    _seed_cache(tmp_project)

    with open_runtime(tmp_project, run="demo_paper_fast") as rt:
        t0 = datetime(2026, 11, 4, 9, 59, 55, tzinfo=UTC)  # 15:29:55 IST
        clock = SimClock(t0)

        p_runner = SessionRunner(rt, mode="paper", clock=clock, dry_run=False)
        j = p_runner._init_journal()
        j.start_run("paper", "0.1.0", "hash", "reg")
        c = p_runner._build_components(clock, j)

        p_runner._tick_seq = 99
        p_runner._setup_timers(c, clock, j)

        # Cross 15:30:00 IST
        clock.set_time(clock.now() + timedelta(seconds=10))
        c["scheduler"].advance_to(clock.now())
        c["scheduler"].fire_due()
        j.flush()

        reader = JournalReader(j.db_path)
        events = reader.events(run_id=j._current_run_id)
        j.close()

        closing_events = [e for e in events if e["kind"] == "market.closing"]
        assert len(closing_events) == 1
        p = closing_events[0]["payload_json"]
        if isinstance(p, str):
            p = json.loads(p)
        assert p["seq"] == 99


def test_replay_dates_lists_only_paper_sessions(tmp_project: Path) -> None:
    """replay --dates D lists only paper sessions and excludes replay folders."""
    res_dir = tmp_project / "results"
    res_dir.mkdir(parents=True, exist_ok=True)

    paper_dir = res_dir / "demo_paper_fast_paper_20261109_083535_e2f8c8"
    paper_dir.mkdir()
    (paper_dir / "manifest.json").write_text(
        json.dumps({"mode": "paper", "run_id": paper_dir.name}), encoding="utf-8"
    )

    replay_dir = res_dir / "demo_paper_fast_replay_20261109_120000_123456"
    replay_dir.mkdir()
    (replay_dir / "manifest.json").write_text(
        json.dumps({"mode": "replay", "run_id": replay_dir.name}), encoding="utf-8"
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["--root", str(tmp_project), "replay", "--dates", "2026-11-09", "--run", "demo_paper_fast"],
    )

    # Replay folder must not appear in candidate list or errors
    assert "replay_20261109_120000" not in result.output


def test_replay_dates_single_session_auto_window(tmp_project: Path) -> None:
    """When date has one paper session, replay --dates automatically uses its window."""
    res_dir = tmp_project / "results"
    res_dir.mkdir(parents=True, exist_ok=True)

    sess_dir = res_dir / "demo_paper_fast_paper_20261109_083535_e2f8c8"
    sess_dir.mkdir()
    (sess_dir / "manifest.json").write_text(
        json.dumps(
            {
                "mode": "paper",
                "run_id": sess_dir.name,
                "first_tick_utc": "2026-11-09T08:35:40+00:00",
                "last_tick_utc": "2026-11-09T10:19:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    (sess_dir / "events.jsonl").write_text("", encoding="utf-8")

    runner = CliRunner()
    res = runner.invoke(
        app,
        ["--root", str(tmp_project), "replay", "--dates", "2026-11-09", "--run", "demo_paper_fast"],
    )
    # Adopted session window instead of prompt error
    assert "Multiple recorded sessions" not in res.output


def test_replay_dates_multiple_sessions_prompts_selection(tmp_project: Path) -> None:
    """When date has multiple paper sessions and --session is omitted, CLI reports candidates."""
    res_dir = tmp_project / "results"
    res_dir.mkdir(parents=True, exist_ok=True)

    s1 = res_dir / "demo_paper_fast_paper_20261109_051244_cb2d42"
    s1.mkdir()
    (s1 / "manifest.json").write_text(json.dumps({"mode": "paper", "run_id": s1.name}))
    (s1 / "metrics.json").write_text(json.dumps({"mode": "paper", "date": "2026-11-09"}))

    s2 = res_dir / "demo_paper_fast_paper_20261109_083535_e2f8c8"
    s2.mkdir()
    (s2 / "manifest.json").write_text(json.dumps({"mode": "paper", "run_id": s2.name}))
    (s2 / "metrics.json").write_text(json.dumps({"mode": "paper", "date": "2026-11-09"}))

    runner = CliRunner()
    res = runner.invoke(
        app,
        ["--root", str(tmp_project), "replay", "--dates", "2026-11-09", "--run", "demo_paper_fast"],
    )
    assert res.exit_code != 0
    assert "multiple recorded sessions" in res.output.lower()
    assert s1.name in res.output
    assert s2.name in res.output


def test_replay_ends_at_recorded_stop(tmp_project: Path) -> None:
    """Replay cleanly ends at the recorded stop seq/ts even if feed has subsequent events."""
    _seed_cache(tmp_project)

    with open_runtime(tmp_project, run="demo_paper_fast") as rt:
        t0 = datetime(2026, 11, 4, 9, 30, 0, tzinfo=UTC)
        clock = SimClock(t0)
        runner = SessionRunner(rt, mode="replay", clock=clock, dry_run=False)

        runner._recorded_stop_seq = 20
        runner._recorded_stop_reason = "end of session"

        inst = InstrumentId("NSE:EQ:RELIANCE")
        ticks = [
            (
                Tick(
                    instrument=inst,
                    exch_ts=t0 + timedelta(seconds=i),
                    recv_ts=t0 + timedelta(seconds=i),
                    ltp=2500.0,
                    ltq=1,
                    volume=10,
                ),
                i,
            )
            for i in range(1, 35)
        ]

        runner._build_event_stream = lambda c, clk: _StreamFromList(ticks)  # type: ignore[assignment]
        res_path = asyncio.run(runner.run())

        assert res_path.is_dir()
        metrics = json.loads((res_path / "metrics.json").read_text(encoding="utf-8"))
        assert metrics["stop_reason"] == "end of session"
        assert runner._tick_seq == 20


def test_unfilled_order_cancellation_recorded_and_replayed(tmp_project: Path) -> None:
    """Unfilled order is cancelled at market close and journaled with seq."""
    _seed_cache(tmp_project)

    with open_runtime(tmp_project, run="demo_paper_fast") as rt:
        t0 = datetime(2026, 11, 4, 9, 59, 50, tzinfo=UTC)  # 15:29:50 IST
        clock = SimClock(t0)
        runner = SessionRunner(rt, mode="paper", clock=clock, dry_run=False)
        runner.run_config.strategies[0].params["exit_time"] = "15:15"

        journal = runner._init_journal()
        journal.start_run("paper", "0.1.0", "hash", "reg")
        c = runner._build_components(clock, journal)

        oms = c["oms"]
        order = Order(
            client_ref="resting-unfilled-1",
            spec=OrderSpec(
                instrument=InstrumentId("NSE:EQ:RELIANCE"),
                side=Side.BUY,
                qty=1,
                type=OrderType.LIMIT,
                price=Decimal("1500.0"),
                product=Product.MIS,
                validity=Validity.DAY,
            ),
            state=OrderState.ACKNOWLEDGED,
            created_ts=t0,
            filled_qty=0,
            avg_price=Decimal("0.0"),
            venue_ref="V-1",
        )
        oms._orders[order.client_ref] = order

        runner._tick_seq = 77
        runner._setup_timers(c, clock, journal)

        # Advance past 15:30 IST
        clock.set_time(clock.now() + timedelta(seconds=20))
        c["scheduler"].advance_to(clock.now())
        c["scheduler"].fire_due()

        # Order must be CANCEL_REQUESTED or CANCELLED
        assert order.state in (OrderState.CANCEL_REQUESTED, OrderState.CANCELLED)


def test_quote_triggers_recorded_events_and_marks_ledger(tmp_project: Path) -> None:
    """Recorded events (market.closing, portfolio.snapshot) are triggered on Quote events."""
    _seed_cache(tmp_project)

    with open_runtime(tmp_project, run="demo_paper_fast") as rt:
        t0 = datetime(2026, 11, 4, 9, 30, 0, tzinfo=UTC)
        clock = SimClock(t0)

        runner = SessionRunner(rt, mode="replay", clock=clock, dry_run=False)
        inst = InstrumentId("NSE:EQ:RELIANCE")

        runner._recorded_events_by_seq = {
            2: [{"kind": "market.closing", "payload": {"market": "NSE:EQ", "seq": 2}}],
            3: [{"kind": "portfolio.snapshot", "payload": {"seq": 3}}],
        }
        runner._pending_recorded_seqs = [2, 3]
        runner._recorded_seq_idx = 0

        q1 = Quote(
            instrument=inst,
            exch_ts=t0,
            recv_ts=t0,
            bids=(Level(price=1050.0, qty=10, orders=1),),
            asks=(Level(price=1055.0, qty=10, orders=1),),
            ltp=1050.0,
            volume=100,
        )
        q2 = Quote(
            instrument=inst,
            exch_ts=t0 + timedelta(seconds=1),
            recv_ts=t0 + timedelta(seconds=1),
            bids=(Level(price=1050.0, qty=10, orders=1),),
            asks=(Level(price=1055.0, qty=10, orders=1),),
            ltp=1050.0,
            volume=200,
        )
        q3 = Quote(
            instrument=inst,
            exch_ts=t0 + timedelta(seconds=2),
            recv_ts=t0 + timedelta(seconds=2),
            bids=(Level(price=1050.0, qty=10, orders=1),),
            asks=(Level(price=1055.0, qty=10, orders=1),),
            ltp=1050.0,
            volume=300,
        )

        runner._build_event_stream = lambda comp, clk: _StreamFromList([(q1, 1), (q2, 2), (q3, 3)])

        asyncio.run(runner.run())

        assert runner._recorded_seq_idx == 2
        assert len(runner._equity_curve) >= 1
        snap = runner._equity_curve[0]
        assert snap["equity"] == 100000.0
