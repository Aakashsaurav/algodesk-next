"""Tests for M4-4e: Feed fidelity, equivalence explain, and order/fill cause classification.

Reqs: FR-BKT-1, 9; G-2; ADR-087, 091, 094, 095.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest
from typer.testing import CliRunner

from algodesk.api.kinds import Mode, PluginKind
from algodesk.api.market import Bar, InstrumentId, Tick
from algodesk.cli.app import app
from algodesk.data.tick_bar_feed import TickBarFeed
from algodesk.engine.clock import SimClock
from algodesk.engine.equivalence import run_backtest_equivalence
from algodesk.engine.runner import SessionRunner
from algodesk.engine.sessions import SessionManager
from algodesk.ops.context import build_context
from algodesk.ops.runtime import open_runtime
from algodesk.results.equivalence import (
    compare_equivalence_events,
    explain_equivalence,
)


def _build_test_project(tmp_path: Path, name: str) -> Path:
    """Create a minimal workspace directory for test execution."""
    root_dir = Path(__file__).resolve().parents[3]
    project_dir = tmp_path / name
    project_dir.mkdir(parents=True, exist_ok=True)

    shutil.copyfile(root_dir / "pyproject.toml", project_dir / "pyproject.toml")
    shutil.copytree(root_dir / "config", project_dir / "config")
    shutil.copytree(root_dir / "plugins", project_dir / "plugins")
    (project_dir / "data" / "ticks").mkdir(parents=True, exist_ok=True)
    (project_dir / "logs").mkdir(parents=True, exist_ok=True)
    (project_dir / "results").mkdir(parents=True, exist_ok=True)
    (project_dir / ".algodesk").mkdir(parents=True, exist_ok=True)
    conf_json = root_dir / ".algodesk" / "conformance.json"
    if conf_json.is_file():
        shutil.copyfile(conf_json, project_dir / ".algodesk" / "conformance.json")
    return project_dir


def _record_fixture_session(
    proj: Path,
    session_id: str,
) -> tuple[Path, list[Bar]]:
    """Record a session with illiquid instrument (3-min gap), timer close, mid start."""
    # Mid-session start at 09:33:00 UTC (15:03 IST)
    start_ts = datetime(2026, 11, 19, 9, 33, tzinfo=UTC)
    liquid_inst = InstrumentId("NSE:EQ:INFY")
    illiquid_inst = InstrumentId("NSE:EQ:TCS")

    cfg_file = proj / "config" / "runs" / "demo_paper_fast.toml"
    cfg_text = cfg_file.read_text(encoding="utf-8")
    cfg_text = cfg_text.replace("fast = 3, slow = 8", "fast = 2, slow = 3")
    cfg_file.write_text(cfg_text, encoding="utf-8")

    captured_paper_bars: list[Bar] = []
    prices = [100.0, 100.0, 95.0, 110.0, 115.0]

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

            # 5 minutes:
            # m=0: liquid + illiquid tick. Illiquid closes at end of m=0 via timer.
            # m=1, m=2, m=3: illiquid has 3-minute gap (NO ticks).
            # m=4: illiquid tick arrives -> triggers flat bar synthesis for m=1, m=2, m=3.
            for m in range(5):
                t_m = start_ts + timedelta(minutes=m)
                minute_ticks: list[Tick] = []
                # Liquid ticks every minute
                t15 = t_m + timedelta(seconds=15)
                minute_ticks.append(
                    Tick(
                        instrument=liquid_inst,
                        exch_ts=t15,
                        recv_ts=t15,
                        ltp=prices[m],
                        ltq=10,
                        volume=100 + m * 10,
                    )
                )
                # Illiquid ticks only at m=0 and m=4 (3-minute gap: m=1, 2, 3)
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

                minute_ticks.sort(key=lambda t: t.exch_ts)
                for tick in minute_ticks:
                    cl.set_time(tick.exch_ts)
                    yield tick
                    await asyncio.sleep(0)

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

    return sess_dir, captured_paper_bars


# ============================================================================
# Step 4 Tests: BarBuilder Settings Fidelity from Recorded Settings
# ============================================================================


def test_m4_4e_step4_tick_bar_feed_reads_snapshot_fill_empty_bars(tmp_path: Path) -> None:
    """Step 4: TickBarFeed reads fill_empty_bars and grace from registry snapshot."""
    sess_dir = tmp_path / "mock_sess"
    sess_dir.mkdir(parents=True)
    snap = {
        "params": {
            "engine.fill_empty_bars": False,
            "engine.bar_close_grace_ms": 750,
        }
    }
    (sess_dir / "registry.snapshot.json").write_text(json.dumps(snap), encoding="utf-8")

    feed = TickBarFeed(sess_dir)
    assert not feed._loaded
    _ = feed.bars
    assert feed._loaded
    assert feed.fill_empty_bars is False
    assert feed.bar_close_grace_ms == 750


def test_m4_4e_step4_tick_bar_feed_falls_back_to_paper_defaults(tmp_path: Path) -> None:
    """Step 4: TickBarFeed falls back to paper defaults (fill_empty_bars=True, grace=500)."""
    sess_dir = tmp_path / "empty_sess"
    sess_dir.mkdir(parents=True)

    feed = TickBarFeed(sess_dir)
    _ = feed.bars
    assert feed.fill_empty_bars is True
    assert feed.bar_close_grace_ms == 500


# ============================================================================
# Step 5 Tests: Fixture Parity with Illiquid Gap, Timer Closes, Mid-Session Start
# ============================================================================


def test_m4_4e_step5_fixture_paper_bars_equal_tbf_bars_including_flat_bars(
    tmp_path: Path,
) -> None:
    """Step 5: Paper bars equal TickBarFeed bars including flat bars; signals equal True."""
    proj = _build_test_project(tmp_path, "proj_step5_fixture")
    sess_dir, paper_bars = _record_fixture_session(proj, "sess_fixture_parity")

    with open_runtime(proj, run="demo_paper_fast", ensure=True) as rt:
        p_ctx = build_context(Mode.BACKTEST, run_id="test_cal_s5", params=rt.params)
        cal = rt.plugins.create(
            PluginKind.CALENDAR, "exchange", config={"exchange": "nse"}, ctx=p_ctx
        )
        sm = SessionManager(calendars={"NSE": cal})
        feed = TickBarFeed(
            session_dir=sess_dir, data_dir=proj / "data", timeframe="1m", session_manager=sm
        )
        tbf_bars = feed.bars

        # Illiquid instrument TCS: m=0, flat m=1, flat m=2, flat m=3, m=4
        tcs_paper = [b for b in paper_bars if "TCS" in str(b.instrument)]
        tcs_feed = [b for b in tbf_bars if "TCS" in str(b.instrument)]

        assert len(tcs_paper) == 5
        assert len(tcs_feed) == 5

        # OHLCV and timestamps match exactly
        for pb, fb in zip(tcs_paper, tcs_feed, strict=True):
            assert pb.start_ts == fb.start_ts
            assert pb.end_ts == fb.end_ts
            assert pb.open == fb.open
            assert pb.high == fb.high
            assert pb.low == fb.low
            assert pb.close == fb.close
            assert pb.volume == fb.volume
            assert pb.synthetic == fb.synthetic

        # Equivalence run passes signals_equal: True
        res = run_backtest_equivalence(rt, "sess_fixture_parity")
        assert res.signals_equal is True
        assert len(res.differences) == 0


def test_m4_4e_step5_fixture_altered_tick_gives_false_with_bar_listed(
    tmp_path: Path,
) -> None:
    """Step 5: Deliberately altered tick gives signals_equal: False with the bar listed."""
    proj = _build_test_project(tmp_path, "proj_step5_altered")
    sess_dir, _ = _record_fixture_session(proj, "sess_fixture_alt")

    # Deliberately corrupt a tick's LTP in the parquet file
    parquet_files = list((proj / "data" / "ticks").glob("**/*.parquet"))
    assert len(parquet_files) > 0
    infy_file = next(f for f in parquet_files if "INFY" in str(f))
    tbl = pq.read_table(infy_file)
    df = tbl.to_pandas()
    # Drastically alter price at m=3 to invert SMA crossover
    df.loc[df.index[3], "ltp"] = 90.0
    import pyarrow as pa

    pq.write_table(pa.Table.from_pandas(df), infy_file)

    with open_runtime(proj, ensure=True) as rt:
        res = run_backtest_equivalence(rt, "sess_fixture_alt")
        assert res.signals_equal is False
        assert len(res.differences) > 0
        assert any("INFY" in d.description for d in res.differences)
        assert any(d.bar_info != "" for d in res.differences)


# ============================================================================
# Step 6 Tests: CLI --explain and Phone-Width Output Formatting
# ============================================================================


def test_m4_4e_step6_explain_cli_output_length_and_format(tmp_path: Path) -> None:
    """Step 6: CLI --explain prints <= 25 lines with ticks, bars, timer-closed, and signals."""
    proj = _build_test_project(tmp_path, "proj_step6_cli")
    sess_dir, _ = _record_fixture_session(proj, "sess_step6_cli")

    runner = CliRunner()
    cmd = [
        "--root",
        str(proj),
        "backtest",
        "equivalence",
        "--session",
        "sess_step6_cli",
        "--explain",
        "NSE:EQ:TCS",
        "09:33",
    ]
    result = runner.invoke(app, cmd)
    assert result.exit_code == 0
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) <= 25
    assert any("Equivalence Explain: NSE:EQ:TCS at 09:33 UTC" in ln for ln in lines)
    assert any("Ticks:" in ln for ln in lines)
    assert any("Paper bar:" in ln for ln in lines)
    assert any("Backtest bar:" in ln for ln in lines)
    assert any("Timer closed:" in ln for ln in lines)
    assert any("Signals:" in ln for ln in lines)
    assert any("Diagnosis:" in ln for ln in lines)


def test_m4_4e_step6_explain_illiquid_minute_zero_ticks(tmp_path: Path) -> None:
    """Step 6: Explain on minute with no ticks cleanly shows 0 ticks / flat minute."""
    proj = _build_test_project(tmp_path, "proj_step6_illiquid")
    sess_dir, _ = _record_fixture_session(proj, "sess_step6_illiquid")

    with open_runtime(proj, ensure=True) as rt:
        output = explain_equivalence(rt, "sess_step6_illiquid", "NSE:EQ:TCS", "09:35")
        lines = output.splitlines()
        assert len(lines) <= 25
        assert any("0 ticks" in ln for ln in lines)
        assert any("Paper bar:" in ln for ln in lines)


def test_m4_4e_step6_explain_invalid_time_and_missing_session(tmp_path: Path) -> None:
    """Step 6: Explain cleanly reports error for invalid time string or missing session."""
    proj = _build_test_project(tmp_path, "proj_step6_err")

    with open_runtime(proj, ensure=True) as rt:
        out1 = explain_equivalence(rt, "nonexistent_session", "NSE:EQ:TCS", "09:33")
        assert "Error: session directory 'nonexistent_session' not found" in out1

        out2 = explain_equivalence(rt, "nonexistent_session", "NSE:EQ:TCS", "invalid_time")
        assert "Error: invalid time format" in out2


# ============================================================================
# Step 7 Tests: Order and Fill Matching and Cause Classification
# ============================================================================


def test_m4_4e_step7_order_matching_by_decision_ts_floored(tmp_path: Path) -> None:
    """Step 7: Orders match strictly by (instrument, side, decision_ts_floored)."""
    t0 = datetime(2026, 11, 19, 9, 30, 0, tzinfo=UTC)

    p_events = [
        {
            "kind": "intent",
            "ts_utc": t0.isoformat(),
            "payload": {
                "client_ref": "ref_p1",
                "instrument": "NSE:EQ:INFY",
                "side": "BUY",
                "qty": 1,
            },
        },
        {
            "kind": "fill",
            "ts_utc": (t0 + timedelta(seconds=15)).isoformat(),
            "payload": {
                "client_ref": "ref_p1",
                "instrument": "NSE:EQ:INFY",
                "side": "BUY",
                "qty": 1,
                "price": "100.0",
            },
        },
    ]

    # Backtest has identical order at same floored second
    bt_events = [
        {
            "kind": "intent",
            "ts_utc": t0.isoformat(),
            "payload": {
                "client_ref": "ref_b1",
                "instrument": "NSE:EQ:INFY",
                "side": "BUY",
                "qty": 1,
            },
        },
        {
            "kind": "fill",
            "ts_utc": (t0 + timedelta(seconds=15)).isoformat(),
            "payload": {
                "client_ref": "ref_b1",
                "instrument": "NSE:EQ:INFY",
                "side": "BUY",
                "qty": 1,
                "price": "100.0",
            },
        },
    ]

    res = compare_equivalence_events(p_events, bt_events, tmp_path)
    assert len(res.orders_only_in_paper) == 0
    assert len(res.orders_only_in_backtest) == 0
    assert len(res.fills_only_in_paper) == 0
    assert len(res.fills_only_in_backtest) == 0


def test_m4_4e_step7_orders_only_in_paper_cause_rejected_in_backtest_rule(
    tmp_path: Path,
) -> None:
    """Step 7: Order only in paper due to backtest risk rejection gets rejected cause."""
    t0 = datetime(2026, 11, 19, 9, 45, 0, tzinfo=UTC)

    p_events = [
        {
            "kind": "signal",
            "payload": {"instrument": "NSE:EQ:RELIANCE", "side": "SELL", "ts": t0.isoformat()},
        },
        {
            "kind": "intent",
            "ts_utc": t0.isoformat(),
            "payload": {
                "client_ref": "ref_p_rel",
                "instrument": "NSE:EQ:RELIANCE",
                "side": "SELL",
                "qty": 1,
            },
        },
        {
            "kind": "fill",
            "ts_utc": (t0 + timedelta(seconds=15)).isoformat(),
            "payload": {
                "client_ref": "ref_p_rel",
                "instrument": "NSE:EQ:RELIANCE",
                "side": "SELL",
                "qty": 1,
                "price": "1340.0",
            },
        },
    ]

    bt_events = [
        {
            "kind": "signal",
            "payload": {"instrument": "NSE:EQ:RELIANCE", "side": "SELL", "ts": t0.isoformat()},
        },
        {
            "kind": "risk.decision",
            "ts_utc": t0.isoformat(),
            "payload": {
                "allow": False,
                "instrument": "NSE:EQ:RELIANCE",
                "side": "SELL",
                "rule": "data_integrity.stale_data",
                "intent_id": "timer_exit_NSE:EQ:RELIANCE_123",
            },
        },
    ]

    res = compare_equivalence_events(p_events, bt_events, tmp_path)
    assert len(res.orders_only_in_paper) == 1
    assert res.orders_only_in_paper[0]["cause"] == "rejected in backtest (rule)"
    assert len(res.fills_only_in_paper) == 1
    assert res.fills_only_in_paper[0]["cause"] == "rejected in backtest (rule)"


def test_m4_4e_step7_orders_only_in_backtest_cause_no_signal(tmp_path: Path) -> None:
    """Step 7: Order only in backtest where paper had no signal gets 'no signal'."""
    t0 = datetime(2026, 11, 19, 9, 35, 0, tzinfo=UTC)

    p_events: list[dict[str, Any]] = []

    bt_events = [
        {
            "kind": "signal",
            "payload": {"instrument": "NSE:EQ:ICICIBANK", "side": "BUY", "ts": t0.isoformat()},
        },
        {
            "kind": "intent",
            "ts_utc": t0.isoformat(),
            "payload": {
                "client_ref": "ref_b_icici",
                "instrument": "NSE:EQ:ICICIBANK",
                "side": "BUY",
                "qty": 1,
            },
        },
        {
            "kind": "fill",
            "ts_utc": (t0 + timedelta(seconds=15)).isoformat(),
            "payload": {
                "client_ref": "ref_b_icici",
                "instrument": "NSE:EQ:ICICIBANK",
                "side": "BUY",
                "qty": 1,
                "price": "1605.0",
            },
        },
    ]

    res = compare_equivalence_events(p_events, bt_events, tmp_path)
    assert len(res.orders_only_in_backtest) == 1
    assert res.orders_only_in_backtest[0]["cause"] == "no signal"
    assert len(res.fills_only_in_backtest) == 1
    assert res.fills_only_in_backtest[0]["cause"] == "no signal"


def test_m4_4e_step7_fill_model_difference_cause(tmp_path: Path) -> None:
    """Step 7: Matched order with missing backtest fill gets fill model cause."""
    t0 = datetime(2026, 11, 19, 9, 30, 0, tzinfo=UTC)

    p_events = [
        {
            "kind": "intent",
            "ts_utc": t0.isoformat(),
            "payload": {
                "client_ref": "ref_p_fill_diff",
                "instrument": "NSE:EQ:TCS",
                "side": "BUY",
                "qty": 1,
            },
        },
        {
            "kind": "fill",
            "ts_utc": (t0 + timedelta(seconds=15)).isoformat(),
            "payload": {
                "client_ref": "ref_p_fill_diff",
                "instrument": "NSE:EQ:TCS",
                "side": "BUY",
                "qty": 1,
                "price": "2340.0",
            },
        },
    ]

    # Backtest placed identical order but received no fill (e.g. illiquid or unfilled)
    bt_events = [
        {
            "kind": "intent",
            "ts_utc": t0.isoformat(),
            "payload": {
                "client_ref": "ref_b_fill_diff",
                "instrument": "NSE:EQ:TCS",
                "side": "BUY",
                "qty": 1,
            },
        },
    ]

    res = compare_equivalence_events(p_events, bt_events, tmp_path)
    assert len(res.orders_only_in_paper) == 0
    assert len(res.orders_only_in_backtest) == 0
    assert len(res.fills_only_in_paper) == 1
    assert (
        res.fills_only_in_paper[0]["cause"]
        == "fill model (backtest unfilled or filled differently)"
    )


def test_m4_4e_step7_orders_only_in_paper_cause_session_end(tmp_path: Path) -> None:
    """Step 7: Order in paper near session end without backtest signal gets session end."""
    t0 = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)
    tend = t0
    p_events = [
        {
            "kind": "intent",
            "ts_utc": t0.isoformat(),
            "payload": {
                "client_ref": "ref_p_end",
                "instrument": "NSE:EQ:RELIANCE",
                "side": "BUY",
                "qty": 1,
            },
        },
        {
            "kind": "run.end",
            "ts_utc": tend.isoformat(),
            "payload": {"reason": "session complete"},
        },
    ]
    bt_events = [
        {
            "kind": "run.end",
            "ts_utc": tend.isoformat(),
            "payload": {"reason": "session complete"},
        },
    ]
    res = compare_equivalence_events(p_events, bt_events, tmp_path)
    assert len(res.orders_only_in_paper) == 1
    assert res.orders_only_in_paper[0]["cause"] == "session end"


def test_m4_4e_step7_historical_16_nov_order_fill_difference_classified(
    tmp_path: Path,
) -> None:
    """Step 7: 16 Nov recorded session classifies 46 vs 45 difference as rejected rule."""
    sess_16 = Path("results/demo_paper_fast_paper_20261116_083920_e829a2")
    sess_16_bt = Path("results/demo_paper_fast_paper_20261116_083920_e829a2_bt_eq")
    if not (sess_16 / "events.jsonl").is_file() or not (sess_16_bt / "events.jsonl").is_file():
        pytest.skip("Recorded 16 Nov paper session or bt_eq not available")

    p_events = [
        json.loads(line)
        for line in (sess_16 / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    bt_events = [
        json.loads(line)
        for line in (sess_16_bt / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    res = compare_equivalence_events(p_events, bt_events, tmp_path)
    assert res.signals_equal is True
    assert res.paper_orders_count == 46
    if res.backtest_orders_count == 45:
        # Pre-M4-4f recorded run: stale data rejection classified
        assert len(res.orders_only_in_paper) == 1
        assert res.orders_only_in_paper[0]["cause"] == "rejected in backtest (rule)"
        assert len(res.fills_only_in_paper) == 1
        assert res.fills_only_in_paper[0]["cause"] == "rejected in backtest (rule)"
    else:
        # Post-M4-4f recorded run: boundary timer exit accepted with zero stale rejections
        assert res.backtest_orders_count == 46
        assert len(res.orders_only_in_paper) == 0
        assert len(res.fills_only_in_paper) == 0
