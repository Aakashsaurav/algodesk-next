"""Tests for CP2: Stop reasons, platform latency, signal age, and liquid feed delay reporting."""

from __future__ import annotations

import asyncio
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from algodesk.api.market import Bar, InstrumentId, Quote
from algodesk.engine.runner import SessionRunner
from algodesk.ops.runtime import open_runtime
from algodesk.risk.killswitch import KillSwitch

IST = ZoneInfo("Asia/Kolkata")


def _seed_instrument_cache(proj: Path) -> None:
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


def _read_algodesk_log(rt) -> str:
    log_file = rt.paths.logs / "algodesk.log"
    if log_file.is_file():
        return log_file.read_text(encoding="utf-8")
    return ""


def test_stop_reason_end_of_session(tmp_project: Path) -> None:
    """When stream terminates naturally, stop reason is 'end of session'."""
    _seed_instrument_cache(tmp_project)
    with open_runtime(tmp_project, run="demo_paper") as rt:
        runner = SessionRunner(rt, mode="paper", dry_run=False)

        # Mock stream that yields nothing and terminates
        async def _empty_stream(c, clk):
            if False:
                yield None

        runner._build_event_stream = _empty_stream  # type: ignore[assignment]
        res_path = asyncio.run(runner.run())

        assert res_path.is_dir()
        metrics = json.loads((res_path / "metrics.json").read_text(encoding="utf-8"))
        assert metrics["stop_reason"] == "end of session"

        log_content = _read_algodesk_log(rt)
        assert "Session stopped: reason=end of session" in log_content

        # Check exported events.jsonl run.end
        evs = [
            json.loads(line)
            for line in (res_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        ends = [ev for ev in evs if ev.get("kind") == "run.end"]
        assert len(ends) == 1
        p_data = json.loads(ends[0]["payload_json"])
        assert p_data["stop_reason"] == "end of session"


def test_stop_reason_interrupt(tmp_project: Path) -> None:
    """When session is interrupted, stop reason is 'interrupt'."""
    _seed_instrument_cache(tmp_project)
    with open_runtime(tmp_project, run="demo_paper") as rt:
        runner = SessionRunner(rt, mode="paper", dry_run=False)

        async def _interrupted_stream(c, clk):
            runner.stop(reason="interrupt")
            if False:
                yield None

        runner._build_event_stream = _interrupted_stream  # type: ignore[assignment]
        res_path = asyncio.run(runner.run())

        assert res_path.is_dir()
        metrics = json.loads((res_path / "metrics.json").read_text(encoding="utf-8"))
        assert metrics["stop_reason"] == "interrupt"

        log_content = _read_algodesk_log(rt)
        assert "Session stopped: reason=interrupt" in log_content

        evs = [
            json.loads(line)
            for line in (res_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        ends = [ev for ev in evs if ev.get("kind") == "run.end"]
        assert len(ends) == 1
        p_data = json.loads(ends[0]["payload_json"])
        assert p_data["stop_reason"] == "interrupt"


def test_stop_reason_killswitch(tmp_project: Path) -> None:
    """When kill switch is tripped during run, stop reason is 'kill switch'."""
    _seed_instrument_cache(tmp_project)
    with open_runtime(tmp_project, run="demo_paper") as rt:
        runner = SessionRunner(rt, mode="paper", dry_run=False)

        ks_file = rt.state.params_db.parent / "killswitch.json"
        ks = KillSwitch(state_file=ks_file)

        async def _killswitch_stream(c, clk):
            # Trip kill switch while stream is yielding
            ks.trip(reason="Daily loss exceeded", by="test")
            # Yield a quote so the runner checks kill switch
            yield Quote(
                instrument=InstrumentId("NSE:EQ:RELIANCE"),
                exch_ts=datetime.now(UTC),
                recv_ts=datetime.now(UTC),
                ltp=1200.0,
                volume=100,
                bids=(),
                asks=(),
            )

        runner._build_event_stream = _killswitch_stream  # type: ignore[assignment]
        res_path = asyncio.run(runner.run())

        assert res_path.is_dir()
        metrics = json.loads((res_path / "metrics.json").read_text(encoding="utf-8"))
        assert metrics["stop_reason"] == "kill switch"

        log_content = _read_algodesk_log(rt)
        assert "Session stopped: reason=kill switch" in log_content


def test_stop_reason_error(tmp_project: Path) -> None:
    """When session encounters an unhandled error, stop reason is 'error'."""
    _seed_instrument_cache(tmp_project)
    with open_runtime(tmp_project, run="demo_paper") as rt:
        runner = SessionRunner(rt, mode="paper", dry_run=False)

        async def _error_stream(c, clk):
            raise RuntimeError("Simulated feed explosion")
            if False:
                yield None

        runner._build_event_stream = _error_stream  # type: ignore[assignment]
        with pytest.raises(RuntimeError, match="Simulated feed explosion"):
            asyncio.run(runner.run())

        res_path = rt.paths.results / runner.run_id
        assert res_path.is_dir()
        metrics = json.loads((res_path / "metrics.json").read_text(encoding="utf-8"))
        assert metrics["stop_reason"] == "error"

        log_content = _read_algodesk_log(rt)
        assert "Session stopped: reason=error" in log_content


def test_platform_latency_and_signal_age_reporting(tmp_project: Path) -> None:
    """Platform latency measures bar close to intent; signal_age reported separately."""
    _seed_instrument_cache(tmp_project)
    with open_runtime(tmp_project, run="demo_paper") as rt:
        runner = SessionRunner(rt, mode="paper", dry_run=False)

        inst = InstrumentId("NSE:EQ:RELIANCE")
        now_dt = datetime(2026, 11, 4, 9, 30, 0, tzinfo=UTC)

        bar = Bar(
            instrument=inst,
            timeframe="1m",
            start_ts=now_dt - timedelta(minutes=1),
            end_ts=now_dt,
            open=1200.0,
            high=1205.0,
            low=1198.0,
            close=1202.0,
            volume=500,
            complete=True,
            synthetic=False,
        )

        _ = runner._build_components(runner.clock, runner._init_journal())
        runner._last_bar_close_mono[inst] = runner._latest_tick_mono.get(inst, 100.0)
        runner._last_bar_end_ts[inst] = bar.end_ts

        runner._on_strategy_intent(inst, now_dt)

        assert len(runner._platform_latencies) >= 1
        assert len(runner._signal_ages) >= 1
        assert runner._platform_latencies[0] >= 0.0
        assert runner._signal_ages[0] >= 0.0


def test_liquid_instruments_feed_delay_filter(tmp_project: Path) -> None:
    """Aggregate feed delay p95 includes liquid instruments only, while per-instrument keeps all."""
    _seed_instrument_cache(tmp_project)
    with open_runtime(tmp_project, run="demo_paper") as rt:
        runner = SessionRunner(rt, mode="paper", dry_run=False)

        t0 = datetime(2026, 11, 4, 9, 15, 0, tzinfo=UTC)
        t1 = datetime(2026, 11, 4, 9, 17, 0, tzinfo=UTC)
        runner._first_tick_ts = t0
        runner._last_tick_ts = t1

        # Liquid instrument (RELIANCE): 100 ticks in 2 min = 50 ticks/min (> 30)
        runner._tick_stats["NSE:EQ:RELIANCE"] = 100
        for _ in range(100):
            runner._feed_delays_by_inst.setdefault("NSE:EQ:RELIANCE", []).append(10.0)
            runner._feed_delays.append(10.0)

        # Illiquid instrument (MCX:GOLD): 2 ticks in 2 min = 1 tick/min (< 30)
        runner._tick_stats["MCX:FNO:GOLD"] = 2
        for _ in range(2):
            runner._feed_delays_by_inst.setdefault("MCX:FNO:GOLD", []).append(25000.0)
            runner._feed_delays.append(25000.0)

        mock_journal = runner._init_journal()
        components = runner._build_components(runner.clock, mock_journal)

        res_path = runner._finalize_session(components, runner.clock, mock_journal)
        metrics = json.loads((res_path / "metrics.json").read_text(encoding="utf-8"))

        assert "NSE:EQ:RELIANCE" in metrics["liquid_instruments"]
        assert "MCX:FNO:GOLD" not in metrics["liquid_instruments"]

        assert metrics["feed_delay_p95_by_instrument"]["NSE:EQ:RELIANCE"] == 10.0
        assert metrics["feed_delay_p95_by_instrument"]["MCX:FNO:GOLD"] == 25000.0

        assert metrics["feed_delay_p95_ms"] == 10.0
        assert metrics["feed_delay_p95"] == 10.0
