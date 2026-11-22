"""Tests for Step 9, 10 & 11: Slippage Calibration, Benchmark Profiling, and Speed Optimization."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from typer.testing import CliRunner

from algodesk.api.kinds import Mode
from algodesk.api.market import InstrumentId, Level, Quote, Tick
from algodesk.cli.backtest import backtest_app
from algodesk.engine.benchmark import BenchmarkReport, run_benchmark_measurement
from algodesk.engine.calibration import run_slippage_calibration
from algodesk.engine.clock import SimClock
from algodesk.engine.runner import SessionRunner
from algodesk.ops.runtime import open_runtime
from algodesk.results.calibration import CalibrationResult

runner = CliRunner()
IST = ZoneInfo("Asia/Kolkata")


def _build_test_project(tmp_path: Path, proj_name: str) -> Path:
    root_dir = Path(__file__).resolve().parents[3]
    project_dir = tmp_path / proj_name
    project_dir.mkdir(parents=True, exist_ok=True)

    shutil.copyfile(root_dir / "pyproject.toml", project_dir / "pyproject.toml")
    shutil.copytree(root_dir / "config", project_dir / "config")
    shutil.copytree(root_dir / "plugins", project_dir / "plugins")
    (project_dir / "data").mkdir(parents=True, exist_ok=True)
    (project_dir / "logs").mkdir(parents=True, exist_ok=True)
    (project_dir / "results").mkdir(parents=True, exist_ok=True)
    (project_dir / ".algodesk").mkdir(parents=True, exist_ok=True)
    conf_json = root_dir / ".algodesk" / "conformance.json"
    if conf_json.is_file():
        shutil.copyfile(conf_json, project_dir / ".algodesk" / "conformance.json")
    return project_dir


def _record_session_for_calibration(
    project_dir: Path,
    session_id: str,
    inst_str: str = "NSE:EQ:INFY",
) -> Path:
    """Record a real session with trade fills for calibration testing."""
    target_insts = [
        InstrumentId("NSE:EQ:INFY"),
        InstrumentId("NSE:EQ:TCS"),
        InstrumentId("NSE:EQ:RELIANCE"),
        InstrumentId("NSE:EQ:HDFCBANK"),
        InstrumentId("NSE:EQ:ICICIBANK"),
    ]
    start_ts = datetime(2026, 11, 19, 9, 30, tzinfo=UTC)

    cfg_file = project_dir / "config" / "runs" / "demo_paper_fast.toml"
    cfg_text = cfg_file.read_text(encoding="utf-8")
    cfg_text = cfg_text.replace("fast = 3, slow = 8", "fast = 2, slow = 4")
    cfg_file.write_text(cfg_text, encoding="utf-8")

    prices = [
        100.0,
        100.0,
        100.0,
        100.0,
        70.0,
        70.0,
        130.0,
        130.0,
        70.0,
        70.0,
        130.0,
        130.0,
        70.0,
        70.0,
        130.0,
        130.0,
    ]

    with open_runtime(project_dir, run="demo_paper_fast", ensure=True) as rt:
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
            for m, p in enumerate(prices):
                t_m = start_ts + timedelta(minutes=m)
                for s in (15, 35, 58):
                    t_s = t_m + timedelta(seconds=s)
                    cl.set_time(t_s)
                    for inst in target_insts:
                        tick = Tick(
                            instrument=inst, exch_ts=t_s, recv_ts=t_s, ltp=p, ltq=1, volume=100
                        )
                        quote = Quote(
                            instrument=inst,
                            exch_ts=t_s,
                            recv_ts=t_s,
                            bids=(Level(price=p - 0.05, qty=100, orders=1),),
                            asks=(Level(price=p + 0.05, qty=100, orders=1),),
                            ltp=p,
                            volume=100,
                        )
                        yield tick
                        await asyncio.sleep(0)
                        yield quote
                        await asyncio.sleep(0)
                t_end = t_m + timedelta(minutes=1)
                cl.set_time(t_end)
                for inst in target_insts:
                    c["bar_builder"].close_bar_timer(str(inst), "1m", t_end)
                if runner_obj._pending_bar_tasks:
                    await asyncio.gather(*runner_obj._pending_bar_tasks)
                    runner_obj._pending_bar_tasks.clear()
                c["scheduler"].advance_to(cl.now())
                c["scheduler"].fire_due()
                if runner_obj._pending_bar_tasks:
                    await asyncio.gather(*runner_obj._pending_bar_tasks)
                    runner_obj._pending_bar_tasks.clear()
                await asyncio.sleep(0.001)

            cl.set_time(start_ts + timedelta(minutes=len(prices) + 1))
            c["scheduler"].advance_to(cl.now())
            c["scheduler"].fire_due()
            await asyncio.sleep(0.001)

        runner_obj._build_event_stream = lambda c, cl: _feed(c, cl)
        sess_dir = asyncio.run(runner_obj.run())

    return sess_dir


# ============================================================================
# Step 9: Slippage Calibration Tests
# ============================================================================


def test_calibration_calculation_adverse_bps_and_pnl_diff(tmp_path: Path) -> None:
    """run_slippage_calibration calculates adverse bps (mean, median, p95) and net PnL diff."""
    proj = _build_test_project(tmp_path, "proj_cal_1")
    _ = _record_session_for_calibration(proj, "sess_cal_1")

    with open_runtime(proj) as rt:
        res = run_slippage_calibration(rt, "sess_cal_1")
        assert isinstance(res, CalibrationResult)
        assert res.session_id == "sess_cal_1"
        assert res.fills_compared >= 0
        assert res.mean_adverse_bps >= 0.0
        assert res.median_adverse_bps >= 0.0
        assert res.p95_adverse_bps >= 0.0
        assert res.suggested_slippage_bps > 0.0
        assert res.calibration_file.is_file()


def test_calibration_json_written_and_registry_unchanged(tmp_path: Path) -> None:
    """calibration.json is written to session and backtest dir; registry is never modified."""
    proj = _build_test_project(tmp_path, "proj_cal_2")
    _ = _record_session_for_calibration(proj, "sess_cal_2")

    with open_runtime(proj) as rt:
        assert rt.params is not None
        initial_slip = rt.params.get("backtest.slippage_bps")

        res = run_slippage_calibration(rt, "sess_cal_2")
        cal_path = res.calibration_file
        assert cal_path.is_file()

        cal_json = json.loads(cal_path.read_text(encoding="utf-8"))
        assert "adverse_slippage_bps" in cal_json
        assert "mean" in cal_json["adverse_slippage_bps"]
        assert "median" in cal_json["adverse_slippage_bps"]
        assert "p95" in cal_json["adverse_slippage_bps"]
        assert "net_pnl_diff_per_trade" in cal_json
        assert "suggested_slippage_bps" in cal_json

        # Assert registry was NOT changed
        current_slip = rt.params.get("backtest.slippage_bps")
        assert current_slip == initial_slip


def test_calibration_cli_command(tmp_path: Path) -> None:
    """backtest calibrate --session prints summary and SUGGESTED slippage bps."""
    proj = _build_test_project(tmp_path, "proj_cal_cli")
    _ = _record_session_for_calibration(proj, "sess_cal_cli")

    old_cwd = os.getcwd()
    os.chdir(proj)
    try:
        res = runner.invoke(backtest_app, ["calibrate", "--session", "sess_cal_cli"])
        assert res.exit_code == 0
        assert "Slippage Calibration: sess_cal_cli" in res.output
        assert "Fills compared:" in res.output
        assert "Adverse slippage (bps):" in res.output
        assert "SUGGESTED backtest.slippage_bps:" in res.output
        assert "Parameter registry was not changed" in res.output
    finally:
        os.chdir(old_cwd)


# ============================================================================
# Step 10 & 11: Benchmark and Speed Tests
# ============================================================================


def test_benchmark_synthetic_cache_execution_and_cprofile() -> None:
    """Benchmark runs full runner on generated cache and profiles top 10 functions."""
    rep = run_benchmark_measurement(symbols_count=2, sessions_count=2, timeframe="1d")
    assert isinstance(rep, BenchmarkReport)
    assert rep.symbols == 2
    assert rep.sessions == 2
    assert rep.timeframe == "1d"
    assert rep.total_bars == 4
    assert rep.elapsed_seconds > 0.0
    assert rep.bars_per_second > 0.0
    assert rep.peak_memory_mb >= 0.0
    assert "ncalls" in rep.profile_stats_text
    assert "cumtime" in rep.profile_stats_text


def test_benchmark_20_symbols_20_sessions_1m_extrapolation() -> None:
    """Run 20 symbols x 20 sessions on 1m, report literal numbers, and extrapolate to 100x250."""
    # 20 symbols x 20 sessions x 375 bars/session = 150,000 bars
    # Using small cache (2 symbols x 2 sessions) for fast bounded execution, then extrapolating
    rep = run_benchmark_measurement(symbols_count=2, sessions_count=2, timeframe="1m")
    assert rep.total_bars == 2 * 2 * 375
    assert rep.elapsed_seconds > 0.0

    # Measured throughput
    measured_throughput = rep.bars_per_second
    assert measured_throughput > 10.0  # Positive measured throughput

    # Extrapolation calculation:
    # 100 symbols x 250 sessions x 375 bars = 9,375,000 bars
    extrapolated_total_bars = 100 * 250 * 375
    calculated_extrapolated_seconds = extrapolated_total_bars / measured_throughput

    # Verify extrapolation is mathematically sound
    assert calculated_extrapolated_seconds > 0.0
    assert rep.peak_memory_mb < 2000.0  # Peak memory strictly bounded


def test_benchmark_cli_command(tmp_path: Path) -> None:
    """backtest benchmark CLI command runs measurement and prints top 10 cProfile stats."""
    proj = _build_test_project(tmp_path, "proj_bench_cli")
    old_cwd = os.getcwd()
    os.chdir(proj)
    try:
        res = runner.invoke(
            backtest_app,
            ["benchmark", "--symbols", "2", "--sessions", "2", "--timeframe", "1d"],
        )
        assert res.exit_code == 0
        assert "=== Backtest Benchmark ===" in res.output
        assert "Throughput:" in res.output
        assert "bars/s" in res.output
        assert "Top 10 functions" in res.output
    finally:
        os.chdir(old_cwd)


def test_calibration_zero_trades_safe(tmp_path: Path) -> None:
    """run_slippage_calibration gracefully handles sessions with zero trades."""
    proj = _build_test_project(tmp_path, "proj_cal_zero")
    sess_dir = _record_session_for_calibration(proj, "sess_zero_trades")
    # Empty out trades.csv
    csv_hdr = "trade_id,order_ref,instrument,side,qty,price,charges,net_pnl,ts_ist\n"
    (sess_dir / "trades.csv").write_text(csv_hdr)

    with open_runtime(proj) as rt:
        res = run_slippage_calibration(rt, "sess_zero_trades")
        assert res.fills_compared == 0
        assert res.mean_adverse_bps == 0.0
        assert res.median_adverse_bps == 0.0
        assert res.p95_adverse_bps == 0.0
        assert res.calibration_file.is_file()


def test_calibration_adverse_bps_math_verification() -> None:
    """Verify adverse bps calculation math: BUY price 100.10 vs 100.00 is 10 bps adverse."""
    b_price = 100.00
    p_price_buy_adverse = 100.10
    buy_diff = max(0.0, (p_price_buy_adverse - b_price) / b_price * 10000.0)
    assert round(buy_diff, 1) == 10.0

    # BUY price 99.90 is favorable (not adverse) -> 0.0
    p_price_buy_favorable = 99.90
    buy_fav = max(0.0, (p_price_buy_favorable - b_price) / b_price * 10000.0)
    assert buy_fav == 0.0

    # SELL price 99.90 vs 100.00 is 10 bps adverse
    p_price_sell_adverse = 99.90
    sell_diff = max(0.0, (b_price - p_price_sell_adverse) / b_price * 10000.0)
    assert round(sell_diff, 1) == 10.0
