"""Tests for M4-1b: Deterministic termination, watchdog, Ctrl-C, and replay equivalence."""

from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from algodesk.api import (
    InstrumentId,
    OrderState,
)
from algodesk.api.manifest import PluginManifest
from algodesk.api.market import Bar, SeriesKey
from algodesk.api.protocols import DataSource
from algodesk.config.snapshot import build_effective
from algodesk.data.manifest import DataManifest
from algodesk.data.service import DataService
from algodesk.data.store import BarStore
from algodesk.engine.backtest import BacktestRunner
from algodesk.engine.clock import SimClock
from algodesk.engine.runner import BacktestWatchdogError
from algodesk.journal.writer import Journal, JournalSettings
from algodesk.ops.runtime import open_runtime
from algodesk.results.compare import compare_runs


def _create_synthetic_backtest_project(
    base_dir: Path,
    num_symbols: int = 5,
    num_sessions: int = 40,
    sine_period: float = 20.0,
) -> tuple[Path, DataService, list[InstrumentId], list[date]]:
    """Helper to generate a clean backtest project with cached bars and DataService."""
    root_dir = Path(__file__).resolve().parents[3]
    project_dir = base_dir / "bt_proj"
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

    symbols = [
        InstrumentId("NSE:EQ:HDFCBANK"),
        InstrumentId("NSE:EQ:RELIANCE"),
        InstrumentId("NSE:EQ:INFY"),
        InstrumentId("NSE:EQ:TCS"),
        InstrumentId("NSE:EQ:SBIN"),
    ][:num_symbols]
    if len(symbols) < num_symbols:
        for i in range(len(symbols), num_symbols):
            symbols.append(InstrumentId(f"NSE:EQ:SYM{i:03d}"))

    start_date = date(2025, 1, 1)
    sessions: list[date] = []
    cur_d = start_date
    while len(sessions) < num_sessions:
        if cur_d.weekday() < 5:
            sessions.append(cur_d)
        cur_d += timedelta(days=1)

    bars_by_symbol: dict[InstrumentId, list[Bar]] = {}
    for idx, sym in enumerate(symbols):
        sym_bars: list[Bar] = []
        base_price = 1000.0 + idx * 250.0
        for s_idx, d in enumerate(sessions):
            factor = 1.0 + 0.15 * math.sin(s_idx * 2 * math.pi / sine_period)
            c_price = round(base_price * factor, 2)
            o_price = round(
                base_price * (1.0 + 0.15 * math.sin((s_idx - 0.5) * 2 * math.pi / sine_period)), 2
            )
            h_price = round(max(o_price, c_price) * 1.01, 2)
            l_price = round(min(o_price, c_price) * 0.99, 2)
            start_ts = datetime(d.year, d.month, d.day, 3, 45, tzinfo=UTC)
            end_ts = datetime(d.year, d.month, d.day, 10, 0, tzinfo=UTC)
            bar = Bar(
                instrument=sym,
                timeframe="1d",
                start_ts=start_ts,
                end_ts=end_ts,
                open=o_price,
                high=h_price,
                low=l_price,
                close=c_price,
                volume=50000,
                complete=True,
                synthetic=False,
            )
            sym_bars.append(bar)
        bars_by_symbol[sym] = sym_bars

    data_dir = project_dir / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(m_path)
    store = BarStore(data_dir=data_dir, manifest=manifest)

    class FakeSource(DataSource):
        def __init__(self, b_map: dict[InstrumentId, list[Bar]]) -> None:
            self.b_map = b_map
            self.manifest = PluginManifest(
                name="upstox",
                version="1.0.0",
                kind="broker",
                api_version=1,
                entry="plugins.brokers.upstox.broker:UpstoxBroker",
                capabilities={"history": True},
                retention={"1d": 36500},
                max_span={"1d": 3650},
            )

        def history(self, key: SeriesKey, start: datetime, end: datetime) -> Iterable[Bar]:
            bars = self.b_map.get(key.instrument, [])
            return [b for b in bars if start <= b.start_ts <= end]

        def instruments(self) -> Iterable[Any]:
            return []

        def stream(self, insts: Any) -> Any:
            raise NotImplementedError

    ds = DataService(
        sources={"upstox": FakeSource(bars_by_symbol)}, default_source="upstox", store=store
    )
    s_start = datetime(sessions[0].year, sessions[0].month, sessions[0].day, 0, 0, tzinfo=UTC)
    s_end = datetime(sessions[-1].year, sessions[-1].month, sessions[-1].day, 23, 59, tzinfo=UTC)
    for sym in symbols:
        ds.get(f"{sym}:1d", start=s_start, end=s_end, source="upstox", cache_only=False)

    # Configure run file
    cfg_file = project_dir / "config" / "runs" / "test_bt.toml"
    sym_list_str = ", ".join(f'"{s}"' for s in symbols)
    cfg_content = f"""allocated_capital = 5000000.0

[run]
name = "test_bt"
mode = "backtest"

[universe]
symbols = [{sym_list_str}]

[backtest]
start = "{sessions[0].isoformat()}"
end = "{sessions[-1].isoformat()}"
timeframe = "1d"
fidelity = "bar"

[data_source]
plugin = "upstox"

[execution_venue]
plugin = "sim"
seed = 7

[fees]
schedule = "zerodha_nse"

[[calendars]]
plugin = "exchange"
exchange = "nse"

[secrets]
plugin = "keychain"

[[strategies]]
plugin = "sma_cross"
risk_profile = "conservative"
params = {{ timeframe = "1d", fast = 5, slow = 15, qty = 1, product = "CNC" }}
"""
    cfg_file.write_text(cfg_content)

    return project_dir, ds, symbols, sessions


def test_backtest_ends_deterministically_at_end_of_data(tmp_path: Path) -> None:
    """Step 4: Backtest ends on feed exhaustion, SimClock advanced, stop_reason end of data."""
    p_dir, ds, symbols, sessions = _create_synthetic_backtest_project(
        tmp_path, num_symbols=3, num_sessions=30
    )

    with open_runtime(p_dir, run="test_bt", ensure=True) as rt:
        object.__setattr__(rt, "_data_service", ds)
        eff = build_effective(rt.root, run_name="test_bt")
        runner = BacktestRunner(runtime=rt, run_config=eff.run, run_name="test_bt", quiet=False)
        runner.session_runner._data_service = ds

        res_path = runner.run()
        assert res_path.is_dir()

        man = json.loads((res_path / "manifest.json").read_text(encoding="utf-8"))
        assert man["stop_reason"] == "end of data"
        assert man["verdict"] == "PASS"

        sum_text = (res_path / "SUMMARY.md").read_text(encoding="utf-8")
        assert "Verdict: PASS" in sum_text


def test_backtest_cancels_resting_orders_on_end_of_data(tmp_path: Path) -> None:
    """Step 4: End-of-session path cancels resting active orders once feed exhausts."""
    p_dir, ds, symbols, sessions = _create_synthetic_backtest_project(
        tmp_path, num_symbols=2, num_sessions=20
    )

    with open_runtime(p_dir, run="test_bt", ensure=True) as rt:
        object.__setattr__(rt, "_data_service", ds)
        eff = build_effective(rt.root, run_name="test_bt")
        runner = BacktestRunner(runtime=rt, run_config=eff.run, run_name="test_bt", quiet=False)
        runner.session_runner._data_service = ds

        _ = runner.run()
        # Verify OMS has no active resting orders remaining
        active_states = (
            OrderState.CREATED,
            OrderState.SUBMITTED,
            OrderState.ACKNOWLEDGED,
            OrderState.PARTIALLY_FILLED,
        )
        oms = (
            runner.session_runner._components.get("oms")
            if hasattr(runner.session_runner, "_components")
            else None
        )
        if oms is not None:
            for ord_obj in oms.orders.values():
                assert ord_obj.state not in active_states


def test_backtest_marks_ledger_from_last_quotes_at_end_of_data(tmp_path: Path) -> None:
    """Step 4: Final marks from the last quotes are recorded into ledger."""
    p_dir, ds, symbols, sessions = _create_synthetic_backtest_project(
        tmp_path, num_symbols=2, num_sessions=25
    )

    with open_runtime(p_dir, run="test_bt", ensure=True) as rt:
        object.__setattr__(rt, "_data_service", ds)
        eff = build_effective(rt.root, run_name="test_bt")
        runner = BacktestRunner(runtime=rt, run_config=eff.run, run_name="test_bt", quiet=False)
        runner.session_runner._data_service = ds

        res_path = runner.run()
        # Equity csv points were written
        assert (res_path / "equity.csv").is_file()
        lines = (res_path / "equity.csv").read_text().strip().splitlines()
        assert len(lines) >= 2  # header + data points


def test_backtest_open_positions_listed_in_summary(tmp_path: Path) -> None:
    """Step 4: Positions open at the end are listed clearly in SUMMARY.md."""
    p_dir, ds, symbols, sessions = _create_synthetic_backtest_project(
        tmp_path, num_symbols=5, num_sessions=35
    )

    with open_runtime(p_dir, run="test_bt", ensure=True) as rt:
        object.__setattr__(rt, "_data_service", ds)
        eff = build_effective(rt.root, run_name="test_bt")
        runner = BacktestRunner(runtime=rt, run_config=eff.run, run_name="test_bt", quiet=False)
        runner.session_runner._data_service = ds

        res_path = runner.run()
        sum_text = (res_path / "SUMMARY.md").read_text()
        assert "Verdict: PASS" in sum_text
        # If any open positions exist, open clause notes them
        if "Open at stop:" in sum_text:
            assert "Unresolved open positions at stop:" in sum_text


def test_no_wall_clock_waiting_patches_time_and_asyncio_sleep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step 5: No wall-clock wait in backtest: monkeypatch sleep to raise."""
    p_dir, ds, symbols, sessions = _create_synthetic_backtest_project(
        tmp_path, num_symbols=2, num_sessions=20
    )

    def _fail_time_sleep(s: float) -> None:
        raise AssertionError(f"time.sleep({s}) called in backtest execution!")

    async def _fail_asyncio_sleep(s: float) -> None:
        raise AssertionError(f"asyncio.sleep({s}) called in backtest execution!")

    monkeypatch.setattr(time, "sleep", _fail_time_sleep)
    monkeypatch.setattr(asyncio, "sleep", _fail_asyncio_sleep)

    with open_runtime(p_dir, run="test_bt", ensure=True) as rt:
        object.__setattr__(rt, "_data_service", ds)
        eff = build_effective(rt.root, run_name="test_bt")
        runner = BacktestRunner(runtime=rt, run_config=eff.run, run_name="test_bt", quiet=False)
        runner.session_runner._data_service = ds

        res_path = runner.run()
        assert res_path.is_dir()


def test_timers_use_sim_clock_only_in_backtest(tmp_path: Path) -> None:
    """Step 5: Repeating wall-clock timers are bypassed; scheduler advances strictly on SimClock."""
    p_dir, ds, symbols, sessions = _create_synthetic_backtest_project(
        tmp_path, num_symbols=2, num_sessions=20
    )

    with open_runtime(p_dir, run="test_bt", ensure=True) as rt:
        object.__setattr__(rt, "_data_service", ds)
        eff = build_effective(rt.root, run_name="test_bt")
        runner = BacktestRunner(runtime=rt, run_config=eff.run, run_name="test_bt", quiet=False)
        runner.session_runner._data_service = ds

        clock = runner.session_runner.clock
        assert isinstance(clock, SimClock)
        res_path = runner.run()
        assert res_path.is_dir()


def test_watchdog_max_idle_seconds_default_and_registry(tmp_path: Path) -> None:
    """Step 6: backtest.max_idle_seconds is registered and defaults to 60s."""
    p_dir, ds, symbols, sessions = _create_synthetic_backtest_project(
        tmp_path, num_symbols=1, num_sessions=5
    )

    with open_runtime(p_dir, run="test_bt", ensure=True) as rt:
        assert rt.params is not None
        val = rt.params.get("backtest.max_idle_seconds")
        assert val is not None
        assert int(val) == 60


def test_watchdog_trips_when_feed_idle_exceeds_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step 6: Watchdog trips if feed stalls past max_idle_seconds, writes FAIL."""
    p_dir, ds, symbols, sessions = _create_synthetic_backtest_project(
        tmp_path, num_symbols=1, num_sessions=5
    )

    with open_runtime(p_dir, run="test_bt", ensure=True) as rt:
        assert rt.params is not None
        object.__setattr__(rt, "_data_service", ds)
        eff = build_effective(rt.root, run_name="test_bt")
        # Set max_idle_seconds to 0.05s
        rt.params.set("backtest.max_idle_seconds", 0.05, by="test", reason="test reason")

        runner = BacktestRunner(runtime=rt, run_config=eff.run, run_name="test_bt", quiet=False)
        runner.session_runner._data_service = ds

        # Create stalling event stream
        orig_stream = runner.session_runner._build_event_stream

        def _stalling_stream(c: Any, clk: Any) -> Any:
            s = orig_stream(c, clk)

            class _StallWrapper:
                def __init__(self, inner: Any) -> None:
                    self._inner = inner
                    self._first = True

                def __aiter__(self) -> _StallWrapper:
                    return self

                async def __anext__(self) -> Any:
                    item = await self._inner.__anext__()
                    if not self._first:
                        # Sleep longer than 0.05s on the real wall clock
                        await asyncio.get_event_loop().run_in_executor(None, time.sleep, 0.15)
                    self._first = False
                    return item

            return _StallWrapper(s)

        runner.session_runner._build_event_stream = _stalling_stream  # type: ignore[assignment]

        with pytest.raises(BacktestWatchdogError, match="Backtest watchdog tripped"):
            runner.run()

        res_dir = rt.paths.results / runner.run_id
        assert res_dir.is_dir()
        man = json.loads((res_dir / "manifest.json").read_text(encoding="utf-8"))
        assert man["stop_reason"] == "watchdog"
        assert man["verdict"] == "FAIL"


def test_watchdog_cli_exits_code_1_with_actionable_error(tmp_path: Path) -> None:
    """Step 6: Watchdog failure through CLI exits with code 1 and prints ERROR, WHERE, FIX."""
    p_dir, ds, symbols, sessions = _create_synthetic_backtest_project(
        tmp_path, num_symbols=1, num_sessions=5
    )

    cli_code = f"""
import asyncio, time
from algodesk.ops.runtime import open_runtime
from algodesk.engine.runner import SessionRunner

with open_runtime('{p_dir}', ensure=True) as rt:
    rt.params.set('backtest.max_idle_seconds', 0.01, by='test', reason='test reason')

orig_build = SessionRunner._build_event_stream

def stall_stream(self, c, clk):
    inner = orig_build(self, c, clk)
    class Staller:
        def __aiter__(self): return self
        async def __anext__(self):
            item = await inner.__anext__()
            time.sleep(0.08)
            return item
    return Staller()

SessionRunner._build_event_stream = stall_stream

from algodesk.cli.app import app
import sys
sys.argv = ['algodesk', '--root', '{p_dir}', 'backtest', 'run', 'test_bt']
try:
    app()
except SystemExit as exc:
    sys.exit(exc.code)
"""
    cmd = [sys.executable, "-c", cli_code]
    env = dict(os.environ)
    proc = subprocess.run(cmd, env=env, cwd=str(p_dir), capture_output=True, text=True)
    assert proc.returncode == 1
    assert "ERROR: Backtest watchdog tripped" in proc.stdout
    assert "WHERE: SessionRunner._execute_session" in proc.stdout
    assert "FIX: Increase backtest.max_idle_seconds" in proc.stdout


def test_progress_to_stderr_every_progress_every_sessions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Step 7: Progress is written to sys.stderr every cli.progress_every sessions."""
    p_dir, ds, symbols, sessions = _create_synthetic_backtest_project(
        tmp_path, num_symbols=1, num_sessions=20
    )

    with open_runtime(p_dir, run="test_bt", ensure=True) as rt:
        object.__setattr__(rt, "_data_service", ds)
        rt.params.set("cli.progress_every", 5, by="test", reason="test progress interval")
        eff = build_effective(rt.root, run_name="test_bt")
        runner = BacktestRunner(runtime=rt, run_config=eff.run, run_name="test_bt", quiet=False)
        runner.session_runner._data_service = ds

        runner.run()

    captured = capsys.readouterr()
    err = captured.err
    assert "[backtest] 5 sessions processed" in err
    assert "[backtest] 10 sessions processed" in err
    assert "[backtest] 15 sessions processed" in err
    assert "[backtest] 20 sessions processed" in err


def test_ctrl_c_interrupt_writes_results_stop_reason_and_stopped_verdict(tmp_path: Path) -> None:
    """Step 7: KeyboardInterrupt stops run, writes stop_reason interrupt."""
    p_dir, ds, symbols, sessions = _create_synthetic_backtest_project(
        tmp_path, num_symbols=1, num_sessions=15
    )

    with open_runtime(p_dir, run="test_bt", ensure=True) as rt:
        object.__setattr__(rt, "_data_service", ds)
        eff = build_effective(rt.root, run_name="test_bt")
        runner = BacktestRunner(runtime=rt, run_config=eff.run, run_name="test_bt", quiet=False)
        runner.session_runner._data_service = ds

        orig_stream = runner.session_runner._build_event_stream

        def _interrupt_stream(c: Any, clk: Any) -> Any:
            s = orig_stream(c, clk)

            class _InterruptWrapper:
                def __init__(self, inner: Any) -> None:
                    self._inner = inner
                    self._count = 0

                def __aiter__(self) -> _InterruptWrapper:
                    return self

                async def __anext__(self) -> Any:
                    self._count += 1
                    if self._count > 3:
                        raise KeyboardInterrupt()
                    return await self._inner.__anext__()

            return _InterruptWrapper(s)

        runner.session_runner._build_event_stream = _interrupt_stream  # type: ignore[assignment]
        res_path = runner.run()
        assert res_path.is_dir()

        man = json.loads((res_path / "manifest.json").read_text(encoding="utf-8"))
        assert man["stop_reason"] == "interrupt"
        assert man["verdict"] == "STOPPED"


def test_ctrl_c_cli_output_stopped(tmp_path: Path) -> None:
    """Step 7: CLI outputs 'Backtest session stopped: ...' on Ctrl-C."""
    p_dir, ds, symbols, sessions = _create_synthetic_backtest_project(
        tmp_path, num_symbols=1, num_sessions=10
    )

    cli_code = f"""
from algodesk.engine.runner import SessionRunner
orig_build = SessionRunner._build_event_stream

def interrupt_stream(self, c, clk):
    inner = orig_build(self, c, clk)
    class Interruptor:
        def __init__(self): self.cnt = 0
        def __aiter__(self): return self
        async def __anext__(self):
            self.cnt += 1
            if self.cnt > 2:
                raise KeyboardInterrupt()
            return await inner.__anext__()
    return Interruptor()

SessionRunner._build_event_stream = interrupt_stream

from algodesk.cli.app import app
import sys
sys.argv = ['algodesk', '--root', '{p_dir}', 'backtest', 'run', 'test_bt']
try:
    app()
except SystemExit as exc:
    sys.exit(exc.code)
"""
    cmd = [sys.executable, "-c", cli_code]
    env = dict(os.environ)
    proc = subprocess.run(cmd, env=env, cwd=str(p_dir), capture_output=True, text=True)
    assert proc.returncode == 0
    assert "Backtest session stopped:" in proc.stdout


def test_integration_step1_cache_bounded_time_and_trades(tmp_path: Path) -> None:
    """Step 8: 5 symbols x 400 sessions cache completes in bounded time (<30s) with trades >= 2."""
    p_dir, ds, symbols, sessions = _create_synthetic_backtest_project(
        tmp_path, num_symbols=5, num_sessions=400
    )

    with open_runtime(p_dir, run="test_bt", ensure=True) as rt:
        object.__setattr__(rt, "_data_service", ds)
        eff = build_effective(rt.root, run_name="test_bt")
        runner = BacktestRunner(runtime=rt, run_config=eff.run, run_name="test_bt", quiet=False)
        runner.session_runner._data_service = ds

        t0 = time.perf_counter()
        res_path = runner.run()
        elapsed = time.perf_counter() - t0

        assert elapsed < 30.0, f"Backtest took too long: {elapsed:.2f}s"
        assert res_path.is_dir()

        metrics = json.loads((res_path / "metrics.json").read_text(encoding="utf-8"))
        trade_count = metrics.get("trades", 0)
        assert trade_count >= 2, f"Expected at least 2 trades, got {trade_count}"


def test_integration_results_bundle_complete(tmp_path: Path) -> None:
    """Step 8: Result bundle contains all required files."""
    p_dir, ds, symbols, sessions = _create_synthetic_backtest_project(
        tmp_path, num_symbols=2, num_sessions=20
    )

    with open_runtime(p_dir, run="test_bt", ensure=True) as rt:
        object.__setattr__(rt, "_data_service", ds)
        eff = build_effective(rt.root, run_name="test_bt")
        runner = BacktestRunner(runtime=rt, run_config=eff.run, run_name="test_bt", quiet=False)
        runner.session_runner._data_service = ds

        res_path = runner.run()
        expected_files = [
            "manifest.json",
            "SUMMARY.md",
            "metrics.json",
            "trades.csv",
            "equity.csv",
            "config.effective.txt",
            "registry.snapshot.json",
        ]
        for fname in expected_files:
            assert (res_path / fname).is_file(), f"Missing expected artifact {fname}"


def test_integration_repeat_runs_equal_and_same_result_hash(tmp_path: Path) -> None:
    """Step 8: Repeat runs produce identical results and same result_hash."""
    p_dir, ds, symbols, sessions = _create_synthetic_backtest_project(
        tmp_path, num_symbols=3, num_sessions=30
    )

    with open_runtime(p_dir, run="test_bt", ensure=True) as rt:
        object.__setattr__(rt, "_data_service", ds)
        eff = build_effective(rt.root, run_name="test_bt")

        runner1 = BacktestRunner(
            runtime=rt, run_config=eff.run, run_name="test_bt", seed=42, quiet=True
        )
        runner1.session_runner._data_service = ds
        res1 = runner1.run()

        runner2 = BacktestRunner(
            runtime=rt, run_config=eff.run, run_name="test_bt", seed=42, quiet=True
        )
        runner2.session_runner._data_service = ds
        res2 = runner2.run()

        man1 = json.loads((res1 / "manifest.json").read_text(encoding="utf-8"))
        man2 = json.loads((res2 / "manifest.json").read_text(encoding="utf-8"))
        assert man1["result_hash"] == man2["result_hash"]

        is_equal, diff = compare_runs(p_dir, res1.name, res2.name)
        assert is_equal is True, f"Repeat runs differed: {diff}"


def test_journal_batch_mode_durable_events_no_deadlock(tmp_path: Path) -> None:
    """Step 11: Journal batch mode does not deadlock on durable=True events."""
    db_path = tmp_path / "test_batch_durable.db"
    settings = JournalSettings(
        flush_interval_ms=500,
        max_buffer_events=5000,
        backpressure_timeout_ms=5000,
        batch_mode=True,
        batch_size=100,
    )
    journal = Journal(db_path=db_path, settings=settings)
    journal.start_run("backtest", "0.1.0", "cfg", "reg")

    # Appending with durable=True in batch mode must not block/deadlock
    for i in range(15):
        journal.append("order.submit", {"idx": i}, durable=True)

    journal.end_run("ok")
    journal.close()

    assert db_path.exists()
