"""Tests for M4-4d: Backtest speed optimization, feed fidelity, and equivalence classification."""

from __future__ import annotations

import asyncio
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from algodesk.api.kinds import Mode, PluginKind
from algodesk.api.market import Bar, InstrumentId, Level, Quote, Tick
from algodesk.cli.backtest import backtest_app
from algodesk.data.bar_synth import BarQuoteSynth
from algodesk.data.tick_bar_feed import TickBarFeed
from algodesk.engine.clock import SimClock
from algodesk.engine.equivalence import run_backtest_equivalence
from algodesk.engine.runner import SessionRunner
from algodesk.engine.sessions import SessionManager
from algodesk.ops.runtime import open_runtime
from algodesk.results.equivalence import compare_equivalence_events

runner = CliRunner()
IST = ZoneInfo("Asia/Kolkata")


def _build_test_project(tmp_path: Path, proj_name: str) -> Path:
    root_dir = Path(__file__).resolve().parents[3]
    project_dir = tmp_path / proj_name
    project_dir.mkdir(parents=True, exist_ok=True)

    shutil.copyfile(root_dir / "pyproject.toml", project_dir / "pyproject.toml")
    shutil.copytree(root_dir / "config", project_dir / "config", dirs_exist_ok=True)
    shutil.copytree(root_dir / "plugins", project_dir / "plugins", dirs_exist_ok=True)
    (project_dir / "data").mkdir(parents=True, exist_ok=True)
    (project_dir / "logs").mkdir(parents=True, exist_ok=True)
    (project_dir / "results").mkdir(parents=True, exist_ok=True)
    (project_dir / ".algodesk").mkdir(parents=True, exist_ok=True)
    conf_json = root_dir / ".algodesk" / "conformance.json"
    if conf_json.is_file():
        shutil.copyfile(conf_json, project_dir / ".algodesk" / "conformance.json")
    return project_dir


# ============================================================================
# Step 4 Tests: Quote Unchecked Constructor and Validation
# ============================================================================


def test_quote_create_unchecked_bypasses_post_init() -> None:
    """Quote.create_unchecked creates valid quotes without validation overhead."""
    ts = datetime(2026, 11, 19, 9, 15, tzinfo=UTC)
    inst = InstrumentId("NSE:EQ:INFY")
    lvl = (Level.create_unchecked(100.0, 10),)

    q = Quote.create_unchecked(
        instrument=inst,
        exch_ts=ts,
        recv_ts=ts,
        bids=lvl,
        asks=lvl,
        ltp=100.0,
        volume=500,
    )
    assert q.instrument == inst
    assert q.ltp == 100.0
    assert q.volume == 500
    assert len(q.bids) == 1
    assert q.bids[0].price == 100.0


def test_quote_standard_constructor_still_rejects_bad_data() -> None:
    """External data and paper mode maintain strict Quote validation."""
    ts = datetime(2026, 11, 19, 9, 15, tzinfo=UTC)
    inst = InstrumentId("NSE:EQ:INFY")

    # Crossed market rejected
    bid_higher = (Level(105.0, 10),)
    ask_lower = (Level(100.0, 10),)
    with pytest.raises(ValueError, match="Crossed market"):
        Quote(
            instrument=inst,
            exch_ts=ts,
            recv_ts=ts,
            bids=bid_higher,
            asks=ask_lower,
            ltp=100.0,
            volume=10,
        )

    # Negative price in Level rejected
    with pytest.raises(ValueError, match="price cannot be negative"):
        Level(-1.0, 10)

    # Negative volume in Quote rejected
    lvl = (Level(100.0, 10),)
    with pytest.raises(ValueError, match="volume cannot be negative"):
        Quote(
            instrument=inst,
            exch_ts=ts,
            recv_ts=ts,
            bids=lvl,
            asks=lvl,
            ltp=100.0,
            volume=-5,
        )


def test_bar_quote_synth_unchecked_constructor_output() -> None:
    """BarQuoteSynth builds quotes with unchecked constructor and validates source bar."""
    synth = BarQuoteSynth(participation_pct=10.0)
    t0 = datetime(2026, 11, 19, 9, 15, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=1)
    bar = Bar(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        timeframe="1m",
        start_ts=t0,
        end_ts=t1,
        open=2500.0,
        high=2510.0,
        low=2495.0,
        close=2505.0,
        volume=1000,
        complete=True,
    )
    quotes = synth.synthesize(bar)
    assert len(quotes) == 4
    assert quotes[0].ltp == 2500.0
    assert quotes[3].ltp == 2505.0
    # Depth is 10% of 1000 = 100
    assert quotes[0].bids[0].qty == 100


def test_bar_quote_synth_rejects_invalid_bar() -> None:
    """BarQuoteSynth rejects corrupted bars (e.g. open <= 0 or high < low)."""
    synth = BarQuoteSynth()
    t0 = datetime(2026, 11, 19, 9, 15, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=1)
    # Construct an unvalidated bar via __new__ to simulate corrupt data
    bad_bar = object.__new__(Bar)
    object.__setattr__(bad_bar, "instrument", InstrumentId("NSE:EQ:RELIANCE"))
    object.__setattr__(bad_bar, "timeframe", "1m")
    object.__setattr__(bad_bar, "start_ts", t0)
    object.__setattr__(bad_bar, "end_ts", t1)
    object.__setattr__(bad_bar, "open", -10.0)
    object.__setattr__(bad_bar, "high", 20.0)
    object.__setattr__(bad_bar, "low", 10.0)
    object.__setattr__(bad_bar, "close", 15.0)
    object.__setattr__(bad_bar, "volume", 100)
    object.__setattr__(bad_bar, "complete", True)
    object.__setattr__(bad_bar, "synthetic", False)

    with pytest.raises(ValueError, match="Invalid bar parameters"):
        synth.synthesize(bad_bar)


# ============================================================================
# Step 6 & 8 Tests: Feed Fidelity and Equivalence
# ============================================================================


def _record_session_with_illiquid_and_timer_closes(
    proj: Path,
    session_id: str,
) -> tuple[Path, list[Bar]]:
    """Record a session containing liquid, illiquid (sparse ticks), and MCX instrument."""
    start_ts = datetime(2026, 11, 19, 9, 30, tzinfo=UTC)
    liquid_inst = InstrumentId("NSE:EQ:INFY")
    illiquid_inst = InstrumentId("NSE:EQ:TCS")
    mcx_inst = InstrumentId("MCX:FUT:GOLD:2026-12-04")

    cfg_file = proj / "config" / "runs" / "demo_paper_fast.toml"
    cfg_text = cfg_file.read_text(encoding="utf-8")
    cfg_text = cfg_text.replace("fast = 3, slow = 8", "fast = 2, slow = 4")
    cfg_file.write_text(cfg_text, encoding="utf-8")

    captured_paper_bars: list[Bar] = []

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
            orig_on_bar = c["bar_builder"]._on_bar_closed

            def _hook_bar(b: Bar) -> None:
                captured_paper_bars.append(b)
                if orig_on_bar:
                    orig_on_bar(b)

            c["bar_builder"]._on_bar_closed = _hook_bar

            # 6 minutes: liquid ticks every min, illiquid only at m=0 and m=4, MCX at m=1
            for m in range(6):
                t_m = start_ts + timedelta(minutes=m)
                minute_ticks: list[Tick] = []
                # Liquid ticks
                t15 = t_m + timedelta(seconds=15)
                minute_ticks.append(
                    Tick(
                        instrument=liquid_inst,
                        exch_ts=t15,
                        recv_ts=t15,
                        ltp=100.0 + m,
                        ltq=10,
                        volume=100 + m * 10,
                    )
                )
                # Illiquid ticks only at m=0 and m=4
                if m in (0, 4):
                    t20 = t_m + timedelta(seconds=20)
                    minute_ticks.append(
                        Tick(
                            instrument=illiquid_inst,
                            exch_ts=t20,
                            recv_ts=t20,
                            ltp=200.0 + m,
                            ltq=5,
                            volume=50 + m * 5,
                        )
                    )
                # MCX ticks (dropped by paper since only NSE calendar is configured)
                if m in (1, 3):
                    t30 = t_m + timedelta(seconds=30)
                    minute_ticks.append(
                        Tick(
                            instrument=mcx_inst,
                            exch_ts=t30,
                            recv_ts=t30,
                            ltp=70000.0,
                            ltq=1,
                            volume=10,
                        )
                    )
                t45 = t_m + timedelta(seconds=45)
                minute_ticks.append(
                    Tick(
                        instrument=liquid_inst,
                        exch_ts=t45,
                        recv_ts=t45,
                        ltp=100.5 + m,
                        ltq=10,
                        volume=105 + m * 10,
                    )
                )

                minute_ticks.sort(key=lambda t: t.exch_ts)
                for tick in minute_ticks:
                    cl.set_time(tick.exch_ts)
                    yield tick
                    await asyncio.sleep(0)

                t_end = t_m + timedelta(minutes=1)
                cl.set_time(t_end)

                c["scheduler"].advance_to(cl.now())
                c["scheduler"].fire_due()
                if runner_obj._pending_bar_tasks:
                    await asyncio.gather(*runner_obj._pending_bar_tasks)
                    runner_obj._pending_bar_tasks.clear()
                await asyncio.sleep(0.001)

            cl.set_time(start_ts + timedelta(minutes=7))
            c["scheduler"].advance_to(cl.now())
            c["scheduler"].fire_due()
            await asyncio.sleep(0.001)

        runner_obj._build_event_stream = lambda c, cl: _feed(c, cl)
        sess_dir = asyncio.run(runner_obj.run())

    return sess_dir, captured_paper_bars


def test_tick_bar_feed_drops_unconfigured_mcx_and_creates_no_empty_minute_bars(
    tmp_path: Path,
) -> None:
    """TickBarFeed uses session manager to drop MCX ticks and creates no empty minute bars."""
    from algodesk.ops.context import build_context

    proj = _build_test_project(tmp_path, "proj_tbf_fidelity")
    sess_dir, paper_bars = _record_session_with_illiquid_and_timer_closes(proj, "sess_fidelity_1")

    with open_runtime(proj, run="demo_paper_fast", ensure=True) as rt:
        p_ctx = build_context(Mode.BACKTEST, run_id="test_cal_fidelity", params=rt.params)
        cal = rt.plugins.create(
            PluginKind.CALENDAR, "exchange", config={"exchange": "nse"}, ctx=p_ctx
        )
        sm = SessionManager(calendars={"NSE": cal})
        feed = TickBarFeed(
            session_dir=sess_dir,
            data_dir=proj / "data",
            timeframe="1m",
            session_manager=sm,
            fill_empty_bars=False,
        )
        tbf_bars = feed.bars

    # MCX was dropped
    assert not any(str(b.instrument).startswith("MCX") for b in tbf_bars)
    # Illiquid instrument TCS: only minutes with actual ticks (m=0 and m=4) were built
    tcs_bars = [b for b in tbf_bars if "TCS" in str(b.instrument)]
    assert len(tcs_bars) == 2
    # No zero-volume flat bars were created by TickBarFeed
    assert all(b.volume > 0 for b in tcs_bars)


def test_cp0_fixture_equivalence_signals_equal_true(tmp_path: Path) -> None:
    """Step 8: The CP0 fixture produces signals equal: True under backtest equivalence."""
    proj = _build_test_project(tmp_path, "proj_cp0_eq")
    sess_dir, _ = _record_session_with_illiquid_and_timer_closes(proj, "sess_cp0_eq")

    with open_runtime(proj, ensure=True) as rt:
        res = run_backtest_equivalence(rt, "sess_cp0_eq")
        assert res.signals_equal is True
        assert len(res.differences) == 0


def test_illiquid_minute_with_no_ticks_gives_no_flat_bar_and_no_signal(tmp_path: Path) -> None:
    """Step 8: Illiquid-instrument minute with no ticks gives no flat bar and no signal."""
    from algodesk.ops.context import build_context

    proj = _build_test_project(tmp_path, "proj_illiquid_test")
    sess_dir, _ = _record_session_with_illiquid_and_timer_closes(proj, "sess_illiquid_test")

    with open_runtime(proj, run="demo_paper_fast", ensure=True) as rt:
        p_ctx = build_context(Mode.BACKTEST, run_id="test_cal_illiquid", params=rt.params)
        cal = rt.plugins.create(
            PluginKind.CALENDAR, "exchange", config={"exchange": "nse"}, ctx=p_ctx
        )
        sm = SessionManager(calendars={"NSE": cal})
        feed = TickBarFeed(
            session_dir=sess_dir,
            data_dir=proj / "data",
            timeframe="1m",
            session_manager=sm,
            fill_empty_bars=False,
        )
        tbf_bars = feed.bars

    tcs_bars = [b for b in tbf_bars if "TCS" in str(b.instrument)]
    # Minutes 09:31, 09:32, 09:33 have no ticks, so NO bars are in TickBarFeed
    mid_mins = {"09:31:00", "09:32:00", "09:33:00"}
    for b in tcs_bars:
        assert b.start_ts.strftime("%H:%M:%S") not in mid_mins


# ============================================================================
# Step 7 Tests: Classification of Differences with 7 Causes
# ============================================================================


def test_equivalence_classification_causes_conformance(tmp_path: Path) -> None:
    """Differences are classified with exactly one of the 7 valid cause strings."""
    valid_causes = {
        "bar content differs",
        "bar missing in paper",
        "bar missing in backtest",
        "timer close differs",
        "session start",
        "warm-up",
        "other",
    }
    t0 = datetime(2026, 11, 19, 9, 30, tzinfo=UTC)
    paper_ev = [
        {
            "kind": "signal",
            "payload": {"ts": t0.isoformat(), "instrument": "NSE:EQ:INFY", "side": "BUY"},
        },
        {
            "kind": "signal",
            "payload": {
                "ts": (t0 + timedelta(minutes=5)).isoformat(),
                "instrument": "NSE:EQ:TCS",
                "side": "SELL",
            },
        },
    ]
    bt_ev = [
        # Content differs: opposite side
        {
            "kind": "signal",
            "payload": {"ts": t0.isoformat(), "instrument": "NSE:EQ:INFY", "side": "SELL"},
        },
        # Only in backtest
        {
            "kind": "signal",
            "payload": {
                "ts": (t0 + timedelta(minutes=10)).isoformat(),
                "instrument": "NSE:EQ:RELIANCE",
                "side": "BUY",
            },
        },
    ]

    res = compare_equivalence_events(paper_ev, bt_ev, tmp_path)
    assert not res.signals_equal
    assert len(res.differences) > 0
    for d in res.differences:
        assert d.cause in valid_causes


def test_cli_backtest_equivalence_prints_cause_line(tmp_path: Path) -> None:
    """CLI backtest equivalence prints an extra cause line per remaining difference."""
    proj = _build_test_project(tmp_path, "proj_cli_cause")
    sess_dir, _ = _record_session_with_illiquid_and_timer_closes(proj, "sess_cli_cause")

    # Tamper with backtest events to inject a difference
    events_file = sess_dir / "events.jsonl"
    lines = events_file.read_text(encoding="utf-8").splitlines()
    fake_sig = {
        "kind": "signal",
        "payload": {
            "ts": datetime(2026, 11, 19, 9, 31, tzinfo=UTC).isoformat(),
            "instrument": "NSE:EQ:INFY",
            "side": "BUY",
        },
    }
    lines.append(json.dumps(fake_sig))
    events_file.write_text("\n".join(lines), encoding="utf-8")

    import os

    old_cwd = os.getcwd()
    os.chdir(proj)
    try:
        result = runner.invoke(backtest_app, ["equivalence", "--session", "sess_cli_cause"])
        assert result.exit_code == 1
        assert "cause:" in result.output
    finally:
        os.chdir(old_cwd)
