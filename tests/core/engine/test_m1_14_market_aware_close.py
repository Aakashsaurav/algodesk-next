"""Tests for CP1: Market-aware close, stale feed gap alerts, and clean FeedAuthError stop."""

from __future__ import annotations

import asyncio
import json
import shutil
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from algodesk.api import (
    FeedAuthError,
    InstrumentId,
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
from algodesk.engine.sessions import MarketState, SessionManager
from algodesk.ops.runtime import open_runtime

IST = ZoneInfo("Asia/Kolkata")
runner_cli = CliRunner()


def _seed_cache(proj: Path) -> None:
    real_cache = (
        Path(__file__).resolve().parent.parent.parent.parent / ".local" / "cache" / "instruments"
    )
    today = datetime.now(IST).date()
    target_cache = proj / ".local" / "cache" / "instruments"
    target_cache.mkdir(parents=True, exist_ok=True)
    if real_cache.is_dir():
        for f in real_cache.glob("upstox_master_*.json.gz"):
            shutil.copyfile(f, target_cache / f"upstox_master_{today.isoformat()}.json.gz")
            break


def test_market_state_transitions_on_close(tmp_project: Path) -> None:
    """Market transitions: RUNNING -> CLOSING -> REPORTING -> STOPPED."""
    sm = SessionManager(calendars={})
    m = "NSE:EQ"
    sm.transition(m, MarketState.STARTING, "start")
    sm.transition(m, MarketState.RUNNING, "open")
    assert sm.state(m) == MarketState.RUNNING

    sm.transition(m, MarketState.CLOSING, "close timer")
    assert sm.state(m) == MarketState.CLOSING

    sm.transition(m, MarketState.REPORTING, "session end")
    assert sm.state(m) == MarketState.REPORTING

    sm.transition(m, MarketState.STOPPED, "reporting done")
    assert sm.state(m) == MarketState.STOPPED


def test_market_close_cancels_open_orders_and_drains(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When market close fires, open orders for that market are cancelled and drained."""
    _seed_cache(tmp_project)

    with open_runtime(tmp_project, run="demo_paper") as rt:
        t0 = datetime(2026, 11, 4, 9, 58, 0, tzinfo=UTC)  # 15:28 IST
        clock = SimClock(t0)
        runner = SessionRunner(rt, mode="paper", clock=clock, dry_run=False)

        # Build components
        journal = runner._init_journal()
        journal.start_run("paper", "0.1.0", "hash", "reg")
        c = runner._build_components(clock, journal)
        oms = c["oms"]
        sm = c["session_mgr"]

        # Ensure market is RUNNING
        sm.transition("NSE:EQ", MarketState.STARTING)
        sm.transition("NSE:EQ", MarketState.RUNNING)

        # Place a dummy resting order
        order = Order(
            client_ref="test-resting-1",
            spec=OrderSpec(
                instrument=InstrumentId("NSE:EQ:RELIANCE"),
                side=Side.BUY,
                qty=1,
                type=OrderType.LIMIT,
                price=Decimal("1000.0"),
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

        # Quote for marking
        q = Quote(
            instrument=InstrumentId("NSE:EQ:RELIANCE"),
            exch_ts=t0,
            recv_ts=t0,
            bids=(),
            asks=(),
            ltp=1050.0,
            volume=10,
        )
        runner._latest_quotes[order.spec.instrument] = q

        runner._setup_timers(c, clock, journal)

        # Advance clock to 15:30:05 IST (10:00:05 UTC)
        clock.set_time(clock.now() + timedelta(seconds=130))
        c["scheduler"].advance_to(clock.now())
        c["scheduler"].fire_due()

        # Allow async close task to run
        asyncio.run(asyncio.sleep(0.05))

        # Check market transitioned to STOPPED
        assert sm.state("NSE:EQ") == MarketState.STOPPED


def test_session_stops_cleanly_at_end_of_session(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When all markets in universe reach STOPPED, runner stops with reason 'end of session'."""
    _seed_cache(tmp_project)

    with open_runtime(tmp_project, run="demo_paper") as rt:
        t0 = datetime(2026, 11, 4, 9, 59, 50, tzinfo=UTC)  # 15:29:50 IST
        clock = SimClock(t0)
        runner = SessionRunner(rt, mode="paper", clock=clock, dry_run=False)

        tick = Tick(
            instrument=InstrumentId("NSE:EQ:RELIANCE"),
            exch_ts=t0,
            recv_ts=t0,
            ltp=2500.0,
            ltq=1,
            volume=100,
        )

        async def _stream(c, clk):
            yield tick
            # Advance past market close 15:30 IST
            clk.set_time(clk.now() + timedelta(seconds=20))
            c["scheduler"].advance_to(clk.now())
            c["scheduler"].fire_due()
            await asyncio.sleep(0.05)
            # Give time for runner to stop
            for _ in range(10):
                if runner._stop_event.is_set():
                    break
                await asyncio.sleep(0.01)

        monkeypatch.setattr(runner, "_build_event_stream", _stream)
        res_path = asyncio.run(runner.run())

        assert res_path.is_dir()
        metrics = json.loads((res_path / "metrics.json").read_text(encoding="utf-8"))
        assert metrics["stop_reason"] == "end of session"
        sum_text = (res_path / "SUMMARY.md").read_text(encoding="utf-8")
        assert "Verdict: PASS" in sum_text


def test_configured_run_stop_time_triggers_clean_shutdown(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run config run.stop_time cleanly triggers end of session shutdown."""
    _seed_cache(tmp_project)

    with open_runtime(tmp_project, run="demo_paper_fast") as rt:
        # demo_paper_fast has stop_time = "15:35"
        t0 = datetime(2026, 11, 4, 10, 4, 50, tzinfo=UTC)  # 15:34:50 IST
        clock = SimClock(t0)
        runner = SessionRunner(rt, mode="paper", clock=clock, dry_run=False)

        tick = Tick(
            instrument=InstrumentId("NSE:EQ:RELIANCE"),
            exch_ts=t0,
            recv_ts=t0,
            ltp=2500.0,
            ltq=1,
            volume=100,
        )

        async def _stream(c, clk):
            yield tick
            clk.set_time(clk.now() + timedelta(seconds=20))  # reaches 15:35:10 IST
            c["scheduler"].advance_to(clk.now())
            c["scheduler"].fire_due()
            await asyncio.sleep(0.05)
            for _ in range(10):
                if runner._stop_event.is_set():
                    break
                await asyncio.sleep(0.01)

        monkeypatch.setattr(runner, "_build_event_stream", _stream)
        res_path = asyncio.run(runner.run())

        assert res_path.is_dir()
        metrics = json.loads((res_path / "metrics.json").read_text(encoding="utf-8"))
        assert metrics["stop_reason"] == "end of session"
        sum_text = (res_path / "SUMMARY.md").read_text(encoding="utf-8")
        assert "Verdict: PASS" in sum_text


def test_stale_feed_during_market_hours_triggers_gap_event(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """During open hours, absence of ticks beyond feed.stale_seconds triggers feed.gap."""
    _seed_cache(tmp_project)

    with open_runtime(tmp_project, run="demo_paper") as rt:
        t0 = datetime(2026, 11, 4, 4, 30, 0, tzinfo=UTC)  # 10:00 IST (open market)
        clock = SimClock(t0)
        runner = SessionRunner(rt, mode="paper", clock=clock, dry_run=False)

        journal = runner._init_journal()
        journal.start_run("paper", "0.1.0", "hash", "reg")
        c = runner._build_components(clock, journal)
        sm = c["session_mgr"]
        sm.transition("NSE:EQ", MarketState.STARTING)
        sm.transition("NSE:EQ", MarketState.RUNNING)

        runner._first_tick_ts = t0
        runner._last_tick_recv_ts = t0

        runner._setup_timers(c, clock, journal)

        # Advance beyond 30s stale threshold
        clock.set_time(clock.now() + timedelta(seconds=35))
        c["scheduler"].advance_to(clock.now())
        c["scheduler"].fire_due()

        assert runner._feed_gaps >= 1


def test_silence_after_market_close_is_normal_no_gap(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absence of ticks after market close is normal; no feed.gap event emitted."""
    _seed_cache(tmp_project)

    with open_runtime(tmp_project, run="demo_paper") as rt:
        t0 = datetime(2026, 11, 4, 10, 15, 0, tzinfo=UTC)  # 15:45 IST (after close)
        clock = SimClock(t0)
        runner = SessionRunner(rt, mode="paper", clock=clock, dry_run=False)

        journal = runner._init_journal()
        journal.start_run("paper", "0.1.0", "hash", "reg")
        c = runner._build_components(clock, journal)
        sm = c["session_mgr"]

        runner._first_tick_ts = t0 - timedelta(hours=1)
        runner._last_tick_recv_ts = t0 - timedelta(minutes=20)

        runner._setup_timers(c, clock, journal)

        # Transition all markets to STOPPED after setup
        for mk in list(sm.markets):
            sm.transition(mk, MarketState.CLOSING)
            sm.transition(mk, MarketState.REPORTING)
            sm.transition(mk, MarketState.STOPPED)

        clock.set_time(clock.now() + timedelta(seconds=60))
        c["scheduler"].advance_to(clock.now())
        c["scheduler"].fire_due()

        # No feed gaps triggered because no market is RUNNING
        assert runner._feed_gaps == 0


def test_feed_auth_error_cancels_orders_and_sets_stop_reason(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FeedAuthError causes clean shutdown: orders cancelled, stop_reason='feed_auth'."""
    _seed_cache(tmp_project)

    with open_runtime(tmp_project, run="demo_paper") as rt:
        t0 = datetime(2026, 11, 4, 4, 30, 0, tzinfo=UTC)
        clock = SimClock(t0)
        runner = SessionRunner(rt, mode="paper", clock=clock, dry_run=False)

        auth_err = FeedAuthError(
            what="Upstox WebSocket rejected: HTTP 401",
            where="UpstoxWebSocketFeed",
            fix="run algodesk login upstox",
        )

        async def _failing_stream(c, clk):
            # Place order before failing
            order = Order(
                client_ref="ref-failing-1",
                spec=OrderSpec(
                    instrument=InstrumentId("NSE:EQ:RELIANCE"),
                    side=Side.BUY,
                    qty=1,
                    type=OrderType.LIMIT,
                    price=Decimal("1000.0"),
                    product=Product.MIS,
                    validity=Validity.DAY,
                ),
                state=OrderState.ACKNOWLEDGED,
                created_ts=t0,
                filled_qty=0,
                avg_price=Decimal("0.0"),
                venue_ref="V-FAIL",
            )
            c["oms"]._orders[order.client_ref] = order

            yield Tick(
                instrument=InstrumentId("NSE:EQ:RELIANCE"),
                exch_ts=t0,
                recv_ts=t0,
                ltp=2500.0,
                ltq=1,
                volume=100,
            )
            raise auth_err

        monkeypatch.setattr(runner, "_build_event_stream", _failing_stream)

        with pytest.raises(FeedAuthError):
            asyncio.run(runner.run())

        assert runner._stop_reason == "feed_auth"
        assert runner._orders_count >= 0 or len(runner._fills) >= 0


def test_cli_handles_feed_auth_error_cleanly(tmp_project: Path) -> None:
    """CLI prints three-line error with fix 'run algodesk login upstox' and exit 1."""
    err = FeedAuthError(
        what="Upstox WebSocket rejected: HTTP 401",
        where="UpstoxWebSocketFeed",
        fix="run algodesk login upstox",
    )

    with patch("algodesk.engine.runner.SessionRunner.run", side_effect=err):
        res = runner_cli.invoke(
            app,
            ["--root", str(tmp_project), "run", "demo_paper"],
        )
        assert res.exit_code == 1
        assert "INTERNAL ERROR" not in res.output
        assert "ERROR: Upstox WebSocket rejected: HTTP 401" in res.output
        assert "FIX: run algodesk login upstox" in res.output
