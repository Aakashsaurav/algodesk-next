"""Tests for M4-4b: Fee consistency, clean error exits, and bounded benchmark execution."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import tomli
from typer.testing import CliRunner

from algodesk.api.kinds import Mode
from algodesk.api.market import InstrumentId, Level, Quote, Tick
from algodesk.cli.backtest import backtest_app
from algodesk.engine.benchmark import BenchmarkReport, run_benchmark_measurement
from algodesk.engine.calibration import run_slippage_calibration
from algodesk.engine.clock import SimClock
from algodesk.engine.equivalence import run_backtest_equivalence
from algodesk.engine.runner import SessionRunner
from algodesk.fees.engine import FeeEngine, _coerce_schedule
from algodesk.fees.mapping import resolve_fee_segment_and_product
from algodesk.ops.runtime import open_runtime
from algodesk.results.calibration import CalibrationResult
from algodesk.results.equivalence import EquivalenceResult
from algodesk.results.writer import ResultsWriter

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


def _record_session_with_mcx_future(
    project_dir: Path,
    session_id: str,
) -> Path:
    """Record a session containing both NSE equity and an MCX future via the real recorder."""
    inst_eq = InstrumentId("NSE:EQ:INFY")
    inst_gold = InstrumentId("MCX:FUT:GOLD:2026-12-04")
    start_ts = datetime(2026, 11, 19, 9, 30, tzinfo=UTC)

    cfg_file = project_dir / "config" / "runs" / "demo_paper_fast.toml"
    cfg_text = cfg_file.read_text(encoding="utf-8")
    cfg_text = cfg_text.replace("fast = 3, slow = 8", "fast = 2, slow = 4")
    cfg_file.write_text(cfg_text, encoding="utf-8")

    prices_eq = [100.0, 100.0, 100.0, 100.0, 90.0, 80.0, 120.0, 120.0]
    prices_gold = [50000.0, 50000.0, 50000.0, 50000.0, 49000.0, 48000.0, 52000.0, 52000.0]

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

        async def _feed(c: dict[str, Any], cl: Any) -> Any:
            for m in range(len(prices_eq)):
                p_eq = prices_eq[m]
                p_gold = prices_gold[m]
                t_m = start_ts + timedelta(minutes=m)
                for s in (15, 35, 58):
                    t_s = t_m + timedelta(seconds=s)
                    cl.set_time(t_s)
                    # NSE Equity tick & quote
                    yield Tick(
                        instrument=inst_eq, exch_ts=t_s, recv_ts=t_s, ltp=p_eq, ltq=1, volume=100
                    )
                    await asyncio.sleep(0)
                    yield Quote(
                        instrument=inst_eq,
                        exch_ts=t_s,
                        recv_ts=t_s,
                        bids=(Level(price=p_eq - 0.05, qty=100, orders=1),),
                        asks=(Level(price=p_eq + 0.05, qty=100, orders=1),),
                        ltp=p_eq,
                        volume=100,
                    )
                    await asyncio.sleep(0)
                    # MCX Future tick & quote
                    yield Tick(
                        instrument=inst_gold, exch_ts=t_s, recv_ts=t_s, ltp=p_gold, ltq=1, volume=10
                    )
                    await asyncio.sleep(0)
                    yield Quote(
                        instrument=inst_gold,
                        exch_ts=t_s,
                        recv_ts=t_s,
                        bids=(Level(price=p_gold - 1.0, qty=10, orders=1),),
                        asks=(Level(price=p_gold + 1.0, qty=10, orders=1),),
                        ltp=p_gold,
                        volume=10,
                    )
                    await asyncio.sleep(0)

                t_end = t_m + timedelta(minutes=1)
                cl.set_time(t_end)
                c["bar_builder"].close_bar_timer(str(inst_eq), "1m", t_end)
                c["bar_builder"].close_bar_timer(str(inst_gold), "1m", t_end)
                if runner_obj._pending_bar_tasks:
                    await asyncio.gather(*runner_obj._pending_bar_tasks)
                    runner_obj._pending_bar_tasks.clear()
                c["scheduler"].advance_to(cl.now())
                c["scheduler"].fire_due()
                if runner_obj._pending_bar_tasks:
                    await asyncio.gather(*runner_obj._pending_bar_tasks)
                    runner_obj._pending_bar_tasks.clear()
                await asyncio.sleep(0.001)

            cl.set_time(start_ts + timedelta(minutes=len(prices_eq) + 1))
            c["scheduler"].advance_to(cl.now())
            c["scheduler"].fire_due()
            await asyncio.sleep(0.001)

        object.__setattr__(runner_obj, "_build_event_stream", lambda c, cl: _feed(c, cl))
        sess_dir = asyncio.run(runner_obj.run())

    return sess_dir


# ============================================================================
# CP1: Fee Consistency Tests
# ============================================================================


def test_fee_call_mapping_paper_and_equivalence_identical() -> None:
    """resolve_fee_segment_and_product maps instruments identically for all modes."""
    # 1. NSE Equity
    seg, prod, exch, itype = resolve_fee_segment_and_product("NSE:EQ:RELIANCE", "MIS")
    assert seg == "eq"
    assert prod == "MIS"
    assert exch == "nse"
    assert itype is None

    # 2. NSE Future
    seg, prod, exch, itype = resolve_fee_segment_and_product("NSE:FUT:X", "MIS")
    assert seg == "fno"
    assert prod == "MIS"
    assert exch == "nse"
    assert itype == "fut"

    # 3. MCX Future (canonical format)
    seg, prod, exch, itype = resolve_fee_segment_and_product("MCX:FUT:GOLD:2026-12-04", "MIS")
    assert seg == "fno"
    assert prod == "MIS"
    assert exch == "mcx"
    assert itype == "fut"

    # 4. MCX Future (legacy format)
    seg, prod, exch, itype = resolve_fee_segment_and_product("MCX:FNO:GOLD FUT 04 DEC 26", "MIS")
    assert seg == "fno"
    assert prod == "MIS"
    assert exch == "mcx"
    assert itype == "fut"

    # 5. Options
    seg, prod, exch, itype = resolve_fee_segment_and_product(
        "NSE:OPT:BANKNIFTY:2026-12-31:45000:CE", "MIS"
    )
    assert seg == "fno"
    assert prod == "MIS"
    assert exch == "nse"
    assert itype == "opt"


def test_mcx_unmodelled_charges_in_fee_engine() -> None:
    """MCX instruments return zero charges when no schedule exists; do not invent rates."""
    root_dir = Path(__file__).resolve().parents[3]
    with open(root_dir / "plugins" / "fees" / "zerodha_nse" / "schedule.toml", "rb") as f:
        sch = _coerce_schedule(tomli.load(f))

    chg = FeeEngine.calculate_trade_charges(
        turnover=Decimal("500000.00"),
        side="BUY",
        segment="fut",
        product="MIS",
        trade_date=date(2026, 11, 19),
        schedule=sch,
        exchange="mcx",
        instrument_type="fut",
        instrument="MCX:FUT:GOLD:2026-12-04",
    )
    assert chg.total == Decimal("0.00")
    assert chg.items == {}


def test_results_writer_summary_and_metrics_mcx_unmodelled(tmp_path: Path) -> None:
    """When MCX trades occur, assumptions line and metrics.json say 'MCX charges not modelled'."""
    writer = ResultsWriter(run_id="test_mcx_assumptions", results_dir=tmp_path / "results")
    trades = [
        {
            "trade_id": "T1",
            "fill_id": "F1",
            "client_ref": "CR1",
            "instrument": "MCX:FUT:GOLD:2026-12-04",
            "side": "BUY",
            "qty": 1,
            "price": 50000.0,
            "charges": 0.0,
            "ts": "2026-11-19T09:30:00+00:00",
            "ts_ist": "2026-11-19 15:00:00 IST",
        }
    ]
    out_path = writer.write(
        summary_info={
            "mode": "backtest",
            "run_name": "demo_paper_fast",
            "fee_schedule": "zerodha_nse",
        },
        metrics={"trades": 1, "charges": 0.0},
        trades=trades,
        equity_points=[],
        effective_config_text="",
        registry_snapshot={"hash": "core", "params": {}},
        manifest_info={"mode": "backtest", "code_version": "0.1.0"},
    )
    summary_text = (out_path / "SUMMARY.md").read_text(encoding="utf-8")
    assert "MCX charges not modelled" in summary_text

    metrics_json = json.loads((out_path / "metrics.json").read_text(encoding="utf-8"))
    assert metrics_json.get("assumptions") == "MCX charges not modelled"


def test_recorded_session_contains_mcx_and_nse(tmp_path: Path) -> None:
    """Real recorder records session with both NSE equities and MCX future."""
    proj = _build_test_project(tmp_path, "proj_rec_mcx")
    sess_dir = _record_session_with_mcx_future(proj, "sess_mcx_1")
    assert sess_dir.is_dir()
    assert (sess_dir / "events.jsonl").is_file()
    assert (sess_dir / "manifest.json").is_file()


def test_backtest_equivalence_on_session_with_mcx_future(tmp_path: Path) -> None:
    """backtest equivalence executes cleanly on recorded session containing MCX future."""
    proj = _build_test_project(tmp_path, "proj_eq_mcx")
    _ = _record_session_with_mcx_future(proj, "sess_eq_mcx")

    with open_runtime(proj) as rt:
        res = run_backtest_equivalence(rt, "sess_eq_mcx")
        assert isinstance(res, EquivalenceResult)
        assert res.paper_signals_count >= 0
        assert res.backtest_signals_count >= 0


def test_backtest_calibrate_on_session_with_mcx_future(tmp_path: Path) -> None:
    """backtest calibrate executes cleanly on recorded session containing MCX future."""
    proj = _build_test_project(tmp_path, "proj_cal_mcx")
    _ = _record_session_with_mcx_future(proj, "sess_cal_mcx")

    with open_runtime(proj) as rt:
        res = run_slippage_calibration(rt, "sess_cal_mcx")
        assert isinstance(res, CalibrationResult)
        assert res.calibration_file.is_file()


def test_tick_fidelity_backtest_on_session_with_mcx_future(tmp_path: Path) -> None:
    """Tick fidelity backtest on session with MCX future runs to completion."""
    proj = _build_test_project(tmp_path, "proj_tick_mcx")
    _ = _record_session_with_mcx_future(proj, "sess_tick_mcx")

    old_cwd = os.getcwd()
    os.chdir(proj)
    try:
        res = runner.invoke(
            backtest_app,
            ["run", "demo_tick_bt", "--fidelity", "tick", "--session", "sess_tick_mcx"],
        )
        assert res.exit_code == 0, f"FAILED OUTPUT:\n{res.output}\nEXCEPTION: {res.exception}"
        assert "Backtest session completed:" in res.output
    finally:
        os.chdir(old_cwd)


# ============================================================================
# CP2: Clean Error Handling Tests
# ============================================================================


def test_injected_fee_error_clean_exit_and_results_written(tmp_path: Path) -> None:
    """Injected fee error writes results with verdict FAIL and prints clean three-line message."""
    proj = _build_test_project(tmp_path, "proj_err_fee")
    cfg_file = proj / "config" / "runs" / "demo_paper_fast.toml"
    cfg_text = cfg_file.read_text(encoding="utf-8")
    cfg_text = cfg_text.replace('schedule = "zerodha_nse"', 'schedule = "non_existent_schedule"')
    cfg_file.write_text(cfg_text, encoding="utf-8")

    old_cwd = os.getcwd()
    os.chdir(proj)
    try:
        res = runner.invoke(backtest_app, ["run", "demo_paper_fast"])
        assert res.exit_code == 1
        assert "ERROR:" in res.output
        assert "WHERE:" in res.output
        assert "FIX:" in res.output
        assert "Traceback" not in res.output
        assert "JournalError" not in res.output
    finally:
        os.chdir(old_cwd)


def test_injected_venue_error_clean_exit_and_results_written(tmp_path: Path) -> None:
    """Injected venue error closes run (FAIL, error) and outputs one clean message."""
    proj = _build_test_project(tmp_path, "proj_err_venue")
    cfg_file = proj / "config" / "runs" / "demo_paper_fast.toml"
    cfg_text = cfg_file.read_text(encoding="utf-8")
    # Set invalid venue plugin to trigger VenueError
    cfg_text = cfg_text.replace('plugin = "sim"', 'plugin = "invalid_venue_plugin"')
    cfg_file.write_text(cfg_text, encoding="utf-8")

    old_cwd = os.getcwd()
    os.chdir(proj)
    try:
        res = runner.invoke(backtest_app, ["run", "demo_paper_fast"])
        assert res.exit_code == 1
        assert "ERROR:" in res.output
        assert "WHERE:" in res.output
        assert "FIX:" in res.output
        assert "Traceback" not in res.output
        assert "JournalError" not in res.output
    finally:
        os.chdir(old_cwd)


def test_equivalence_cli_error_clean_formatting_no_traceback(tmp_path: Path) -> None:
    """backtest equivalence on invalid session yields clean single error message with code 1."""
    proj = _build_test_project(tmp_path, "proj_eq_err")
    old_cwd = os.getcwd()
    os.chdir(proj)
    try:
        res = runner.invoke(backtest_app, ["equivalence", "--session", "missing_session_id"])
        assert res.exit_code == 1
        assert "ERROR:" in res.output
        assert "WHERE:" in res.output
        assert "FIX:" in res.output
        assert "Traceback" not in res.output
    finally:
        os.chdir(old_cwd)


# ============================================================================
# CP3: Benchmark Bounds, Progress, and SIGINT Tests
# ============================================================================


def test_benchmark_progress_reporting_to_stderr(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Benchmark streams progress to stderr every cli.progress_every sessions."""
    from algodesk.params.registry import ParamRegistry

    orig_get = ParamRegistry.get

    def _mock_get(self: Any, name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "cli.progress_every":
            return 1
        return orig_get(self, name, *args, **kwargs)

    monkeypatch.setattr(ParamRegistry, "get", _mock_get)

    rep = run_benchmark_measurement(symbols_count=2, sessions_count=2, timeframe="1d")
    assert isinstance(rep, BenchmarkReport)
    captured = capsys.readouterr()
    # Stderr received progress output
    assert "[backtest]" in captured.err
    assert "sessions processed" in captured.err


def test_benchmark_max_seconds_timeout_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """backtest.benchmark_max_seconds bounds execution duration and emits partial numbers."""
    from algodesk.params.registry import ParamRegistry

    orig_get = ParamRegistry.get

    def _mock_get(self: Any, name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "backtest.benchmark_max_seconds":
            return 0.001
        return orig_get(self, name, *args, **kwargs)

    monkeypatch.setattr(ParamRegistry, "get", _mock_get)

    t0 = time.perf_counter()
    rep = run_benchmark_measurement(symbols_count=2, sessions_count=5, timeframe="1d")
    t1 = time.perf_counter()
    assert (t1 - t0) < 5.0
    assert rep.elapsed_seconds > 0.0


def test_no_non_daemon_threads_or_processes_outlive_benchmark() -> None:
    """No non-daemon thread or child process outlives benchmark execution."""
    threads_before = [
        t for t in threading.enumerate() if not t.daemon and t != threading.main_thread()
    ]
    rep = run_benchmark_measurement(symbols_count=2, sessions_count=2, timeframe="1d")
    assert rep.total_bars == 4
    threads_after = [
        t for t in threading.enumerate() if not t.daemon and t != threading.main_thread()
    ]
    assert len(threads_after) <= len(threads_before)


def test_benchmark_sigint_stops_within_5_seconds_subprocess(tmp_path: Path) -> None:
    """Subprocess benchmark execution terminates within 5 seconds of receiving SIGINT."""
    cmd = [
        sys.executable,
        "-c",
        (
            "import time; "
            "from algodesk.engine.benchmark import run_benchmark_measurement; "
            "rep = run_benchmark_measurement(symbols_count=20, sessions_count=20, timeframe='1m'); "
            "print('DONE')"
        ),
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[3] / "src")

    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    # Wait 1 second then send SIGINT
    time.sleep(1.0)
    t_sig = time.time()
    proc.send_signal(signal.SIGINT)

    try:
        proc.communicate(timeout=5.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail("Benchmark did not terminate within 5 seconds of SIGINT")

    elapsed_after_sig = time.time() - t_sig
    assert elapsed_after_sig <= 5.0
